"""Azure Container Registry: internal images, and which of their dates is real.

Payloads under fixtures/acr/ are recorded from a live registry
(driftscopetestacr) holding four tags of `internal/webapp`, imported from four
Alpine releases so their build dates span 14 months while their push dates are
all identical. That spread is the whole point of the fixture: a bug that reports
push time as the image's age passes every test built on same-day data.

Nothing here opens a socket.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftscope.http import Cache, Client, GitHub
from driftscope.models import Component
from driftscope.resolvers import Context
from driftscope.resolvers import acr as acr_mod
from driftscope.resolvers.acr import parse_acr_ref, resolve_acr_image

FIXTURES = Path(__file__).parent / "fixtures" / "acr"
HOST = "driftscopetestacr.azurecr.io"
REPO = "internal/webapp"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def ctx(tmp_path):
    return Context(
        client=Client(Cache(root=tmp_path / "c")),
        github=GitHub(Cache(root=tmp_path / "g")),
    )


@pytest.fixture(autouse=True)
def clear_token_cache(monkeypatch):
    monkeypatch.setattr(acr_mod, "_TOKENS", {})


@pytest.fixture
def registry(monkeypatch):
    """Replay the recorded registry. Returns the list of paths requested."""
    seen: list[str] = []
    index = load("manifest-index")
    child = load("manifest-child")
    config = load("config-blob")
    child_digest = next(
        m["digest"] for m in index["manifests"]
        if (m.get("platform") or {}).get("os") not in (None, "unknown")
    )

    def fake_get(host, path, token, accept=None):
        seen.append(path)
        if "/_tags" in path:
            return load("tags")
        if path.endswith(f"/manifests/{child_digest}"):
            return child
        if "/manifests/" in path:
            return index
        if "/blobs/" in path:
            return config
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(acr_mod, "_get", fake_get)
    monkeypatch.setattr(acr_mod, "_registry_token", lambda h, r, c: "TOKEN")
    return seen


def component(current="1.0.0", name=f"{HOST}/{REPO}"):
    return Component(
        ecosystem="docker-image", name=name, current=current,
        source="k8s/deployment.yaml", line=10,
    )


# ------------------------------------------------------------------ ref parsing


@pytest.mark.parametrize("name,expected", [
    (f"{HOST}/{REPO}", (HOST, REPO)),
    ("contoso.azurecr.io/app", ("contoso.azurecr.io", "app")),
    ("CONTOSO.AZURECR.IO/app", ("CONTOSO.AZURECR.IO", "app")),  # host is case-insensitive
    ("contoso.azurecr.io/team/sub/app", ("contoso.azurecr.io", "team/sub/app")),
])
def test_acr_refs_are_recognised(name, expected):
    assert parse_acr_ref(name) == expected


@pytest.mark.parametrize("name", [
    "redis", "library/redis", "ghcr.io/org/app", "mcr.microsoft.com/dotnet/sdk",
    "contoso.azurecr.io",          # host with no repository
    "quay.io/prometheus/node",
])
def test_other_registries_are_not_claimed(name):
    """Returning None is what lets `resolve_image` fall through to Docker Hub."""
    assert parse_acr_ref(name) is None


# ------------------------------------------------------------------- the dates


def test_published_is_the_build_time_not_the_push_time(ctx, registry):
    """The bug this module exists to prevent.

    All four tags were pushed on the same day; the image in 1.0.0 was built 17
    months earlier. Reporting the push time would call it new.
    """
    r = resolve_acr_image(component("1.0.0"), ctx)
    rel = next(x for x in r.releases if x.version == "1.0.0")

    assert rel.published is not None
    assert rel.published.date().isoformat() == "2025-02-14"          # built
    assert r.extra["pushed"]["1.0.0"].startswith("2026-07-28")       # pushed
    assert r.extra["dates_are_build_times"] is True


def test_push_times_are_kept_but_never_stand_in_for_a_build_date(ctx, registry):
    """A tag whose build date was not sampled reports None, not its push time --
    `None` means "not measured" everywhere else in this codebase and must here."""
    r = resolve_acr_image(component("1.0.0"), ctx)
    sampled = set(r.extra["build_dates_sampled"])
    unsampled = [x for x in r.releases if x.version not in sampled]

    assert unsampled, "fixture should have tags outside the sample"
    for rel in unsampled:
        assert rel.published is None
        # ...while its push time is still available, just not as a build date.
        assert r.extra["pushed"][rel.version]


def test_only_the_deployed_and_latest_tags_cost_extra_calls(ctx, registry):
    """Build dates are three round trips each. Sampling the two tags that change
    a decision keeps a 500-tag registry from costing 1500 requests."""
    r = resolve_acr_image(component("1.0.0"), ctx)

    assert r.extra["build_dates_sampled"] == ["1.0.0", "2.1.0"]
    assert len([p for p in registry if "/blobs/" in p]) == 2


def test_a_multi_arch_index_resolves_through_to_a_real_child(ctx, registry):
    """The tag points at an index; the build date lives in a child's config. The
    `unknown` platform entries are attestation blobs and have no config at all."""
    resolve_acr_image(component("1.0.0"), ctx)
    manifest_calls = [p for p in registry if "/manifests/" in p]

    assert any("sha256:" in p for p in manifest_calls), "never followed the index"


# ------------------------------------------------------------------- versioning


def test_latest_is_the_highest_version_not_the_most_recent_push(ctx, registry):
    """The tag listing is ordered `timedesc`, so trusting its order would make
    whatever was pushed last look newest regardless of its version."""
    r = resolve_acr_image(component("1.0.0"), ctx)
    assert r.latest_stable == "2.1.0"


def test_every_tag_becomes_a_release(ctx, registry):
    r = resolve_acr_image(component("1.0.0"), ctx)
    assert {x.version for x in r.releases} == {"1.0.0", "1.1.0", "2.0.0", "2.1.0"}


# --------------------------------------------------------------- failure modes


def test_no_credential_is_reported_not_treated_as_no_tags(ctx, monkeypatch):
    """Without a token there is nothing to say about the registry. Saying
    "up to date" would be the dangerous answer."""
    monkeypatch.setattr(acr_mod, "_registry_token", lambda h, r, c: None)
    r = resolve_acr_image(component(), ctx)

    assert r.latest_stable is None
    assert r.releases == []
    assert "no Azure credential" in r.note


def test_a_403_is_an_error_not_an_empty_registry(ctx, monkeypatch):
    """A permissions failure that read as "no tags" would render as a component
    with nothing newer available -- a clean bill of health from a refusal."""
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(acr_mod, "_registry_token", lambda h, r, c: "TOKEN")
    monkeypatch.setattr(acr_mod, "_get", boom)
    r = resolve_acr_image(component(), ctx)

    assert r.error and "403" in r.error
    assert r.latest_stable is None


def test_az_disabled_yields_no_token(ctx, monkeypatch):
    """`--no-az` must not shell out."""
    def explode(*a, **k):  # pragma: no cover -- reaching this is the failure
        raise AssertionError("shelled out to az under --no-az")

    monkeypatch.setattr(acr_mod.subprocess, "run", explode)
    ctx.allow_az = False
    assert acr_mod._mint_token(HOST, REPO, ctx) is None


def test_a_build_date_that_cannot_be_fetched_is_none(ctx, monkeypatch):
    """Not an exception, and emphatically not the push time."""
    monkeypatch.setattr(acr_mod, "_registry_token", lambda h, r, c: "TOKEN")

    def only_tags(host, path, token, accept=None):
        if "/_tags" in path:
            return load("tags")
        raise ValueError("manifest unavailable")

    monkeypatch.setattr(acr_mod, "_get", only_tags)
    r = resolve_acr_image(component("1.0.0"), ctx)

    assert r.latest_stable == "2.1.0"                    # tags still worked
    assert all(x.published is None for x in r.releases)  # dates did not


# ------------------------------------------------------------------ concurrency


def test_one_token_is_minted_for_many_images_in_one_registry(ctx, monkeypatch):
    """Regression: the "not looked up yet" sentinel was written into the shared
    cache *before* the lookup ran, so with eight resolver workers the first
    thread in published "no token" and every sibling reported the registry as
    unauthenticated while the winner resolved it fine -- two findings for one
    registry disagreeing inside a single report.
    """
    import threading
    import time

    calls: list[int] = []
    start = threading.Event()

    def slow_mint(host, repo, c):
        # Deliberately slow: the old code's window was exactly the duration of
        # an `az acr login` subprocess, so an instant mint would not reproduce
        # it. A barrier cannot be used here -- `_mint_token` runs under the lock,
        # so waiting for a second thread inside it would deadlock, which is
        # itself the property being tested.
        time.sleep(0.05)
        calls.append(1)
        return "TOKEN"

    monkeypatch.setattr(acr_mod, "_mint_token", slow_mint)
    results: list[str | None] = []

    def worker():
        start.wait(timeout=5)     # release all eight at once
        results.append(acr_mod._registry_token(HOST, REPO, ctx))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=10)

    # Every worker gets the same real token, and only one exchange happened.
    assert results == ["TOKEN"] * 8
    assert len(calls) == 1

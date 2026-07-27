"""Scan-target parsing: which forge a URL addresses, and what to call the report.

No network and no cloning -- these are all pure string functions.
"""

from __future__ import annotations

import pytest

from pathlib import Path

from driftscope import source as src_mod
from driftscope.source import (
    AdoRef,
    _auth_env_for,
    looks_remote,
    normalize_remote,
    parse_ado_url,
    slug_for,
    strip_userinfo,
)

# The three shapes that turn up in the wild. The middle one is what the ADO
# "Clone" button copies to the clipboard, so it is the one users actually paste.
ADO_URLS = [
    ("https://dev.azure.com/contoso/Platform/_git/payments",
     AdoRef("contoso", "Platform", "payments")),
    ("https://contoso@dev.azure.com/contoso/Platform/_git/payments",
     AdoRef("contoso", "Platform", "payments")),
    ("https://contoso.visualstudio.com/Platform/_git/payments",
     AdoRef("contoso", "Platform", "payments")),
]


@pytest.mark.parametrize("url,expected", ADO_URLS)
def test_parse_ado_url_shapes(url, expected):
    assert parse_ado_url(url) == expected


def test_ado_url_tolerates_git_suffix_and_trailing_slash():
    ref = AdoRef("contoso", "Platform", "payments")
    assert parse_ado_url("https://dev.azure.com/contoso/Platform/_git/payments.git") == ref
    assert parse_ado_url("https://dev.azure.com/contoso/Platform/_git/payments/") == ref


def test_ado_url_without_project_falls_back_to_repo_name():
    """ADO permits omitting the project when it matches the repo name, and the web
    UI resolves it the same way."""
    assert parse_ado_url("https://dev.azure.com/contoso/_git/payments") == AdoRef(
        "contoso", "payments", "payments"
    )


def test_legacy_host_tolerates_default_collection():
    assert parse_ado_url(
        "https://contoso.visualstudio.com/DefaultCollection/Platform/_git/payments"
    ) == AdoRef("contoso", "Platform", "payments")


@pytest.mark.parametrize("url", [
    "https://github.com/org/repo",
    "https://gitlab.com/org/repo",
    "git@github.com:org/repo.git",
    "org/repo",
    "/local/path",
    "",
    None,
])
def test_non_ado_urls_are_not_claimed(url):
    assert parse_ado_url(url) is None


def test_userinfo_is_stripped():
    """An ADO clone URL embeds `{org}@`, which reads as a credential in a report."""
    assert (strip_userinfo("https://contoso@dev.azure.com/contoso/P/_git/r")
            == "https://dev.azure.com/contoso/P/_git/r")
    # Anything without userinfo is returned untouched.
    assert (strip_userinfo("https://github.com/org/repo")
            == "https://github.com/org/repo")
    assert strip_userinfo(None) is None


# ------------------------------------------------------------------------ slug


def test_ado_slug_is_org_project_repo():
    """Regression: the generic remote regex reads an ADO path as `{project}/_git`,
    so every repo in a project produced the same `drift-Platform-_git` filename."""
    assert slug_for("https://dev.azure.com/contoso/Platform/_git/payments") == (
        "contoso-Platform-payments"
    )


def test_ado_slug_distinguishes_repos_in_one_project():
    a = slug_for("https://dev.azure.com/contoso/Platform/_git/payments")
    b = slug_for("https://dev.azure.com/contoso/Platform/_git/ledger")
    assert a != b


def test_github_slug_unchanged():
    assert slug_for("https://github.com/darrinthomascecil/driftscope-testapp") == (
        "darrinthomascecil-driftscope-testapp"
    )


def test_local_path_slug_unchanged():
    assert slug_for("/tmp/estate/") == "estate"


# ------------------------------------------------------------- normalize_remote


def test_bare_shorthand_still_means_github():
    """Changing this silently would break every existing invocation."""
    assert normalize_remote("org/repo") == "https://github.com/org/repo"


def test_ado_org_opts_shorthand_into_azure_devops():
    assert normalize_remote("Platform/payments", ado_org="contoso") == (
        "https://dev.azure.com/contoso/Platform/_git/payments"
    )


def test_ado_org_with_single_segment_uses_it_as_both():
    assert normalize_remote("payments", ado_org="contoso") == (
        "https://dev.azure.com/contoso/payments/_git/payments"
    )


def test_full_url_ignores_ado_org():
    url = "https://github.com/org/repo"
    assert normalize_remote(url, ado_org="contoso") == url


def test_ado_url_looks_remote():
    assert looks_remote("https://dev.azure.com/contoso/Platform/_git/payments")


# ------------------------------------------------------------------ clone auth


@pytest.fixture
def fake_token(monkeypatch):
    """Stand in for `az account get-access-token` -- tests never shell out."""
    monkeypatch.setattr(src_mod, "ado_token", lambda: "TOKEN123")


def test_ado_clone_gets_a_bearer_header(fake_token):
    env = _auth_env_for("https://dev.azure.com/contoso/Platform/_git/payments")
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer TOKEN123"


def test_token_never_reaches_argv(fake_token):
    """argv is world-readable via `ps`; environment is not. The token must travel
    in GIT_CONFIG_VALUE_0, never as a `-c` argument."""
    env = _auth_env_for("https://dev.azure.com/contoso/Platform/_git/payments")
    assert not any(k.startswith("-c") or "TOKEN123" in k for k in env)
    assert "TOKEN123" in env["GIT_CONFIG_VALUE_0"]


def test_github_clone_gets_no_injected_auth(fake_token):
    """Public GitHub needs nothing, and private GitHub already works through the
    user's credential helper -- injecting an ADO token there would be wrong."""
    assert _auth_env_for("https://github.com/org/repo") == {}


def test_no_token_means_no_auth_env(monkeypatch):
    """`az` missing or logged out falls back to whatever git is configured with,
    rather than producing a header with an empty token."""
    monkeypatch.setattr(src_mod, "ado_token", lambda: None)
    assert _auth_env_for("https://dev.azure.com/contoso/Platform/_git/payments") == {}


# --------------------------------------------------------------- cache writes


def test_concurrent_writers_to_one_url_do_not_collide(tmp_path):
    """Regression: the temp filename was derived from the key alone, so two threads
    writing the same URL raced -- the first replace() consumed the shared temp file
    and the second raised FileNotFoundError.

    Reachable in normal use: resolution runs eight workers, and a real scan
    resolves rails/rails from five components and kubernetes/kubernetes from four,
    all hitting one URL. The exception escaped put(), propagated out of the
    resolver, and the dispatcher turned it into a `unknown` finding -- intermittent
    and with no cause visible in the report.
    """
    import threading

    from driftscope.http import Cache

    cache = Cache(root=tmp_path / "c")
    key = "https://registry.example/shared"
    errors: list[str] = []

    def writer(i: int) -> None:
        try:
            for _ in range(40):
                cache.put(key, {"writer": i, "payload": "x" * 4000})
                got = cache.get(key)
                if got is not None and "writer" not in got:
                    errors.append(f"torn read: {sorted(got)[:3]}")
        except Exception as e:  # noqa: BLE001 -- the point is that none escape
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors[:3]
    # And the entry is intact afterwards, not a half-written file.
    assert "writer" in cache.get(key)


def test_a_failed_cache_write_never_fails_the_caller(tmp_path, monkeypatch):
    """The answer is already in hand by the time we write it; losing the cache
    entry is a slower next run, not a failed scan."""
    from driftscope.http import Cache

    cache = Cache(root=tmp_path / "c")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    cache.put("k", {"a": 1})  # must not raise

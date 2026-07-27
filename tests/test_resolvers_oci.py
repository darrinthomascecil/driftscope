"""Container registry resolution tests.

Two layers:

  * Unit tests with a fake HTTP client and canned Docker Hub payloads. These are
    deterministic and offline, and they cover the logic that is easy to get wrong:
    library/ prefixing, variant isolation, moving-alias exclusion and tag filtering.
  * Integration tests marked `network`, which hit the real public registry to catch
    API shape changes. Deselect with `-m "not network"`.
"""

from __future__ import annotations

import pytest

from driftscope.models import DOCKER, Component
from driftscope.resolvers import Context
from driftscope.resolvers.oci import _hub_repo_path, resolve_image
from driftscope.version import parse

# ------------------------------------------------------------- fake transport


class FakeClient:
    """Stands in for http.Client, serving canned tag pages."""

    def __init__(self, pages: dict[str, dict]):
        self.pages = pages
        self.calls: list[str] = []

    def get_json(self, url, **kw):
        self.calls.append(url)
        for key, body in self.pages.items():
            if key in url:
                return body
        from driftscope.http import HttpError

        raise HttpError(f"unexpected URL {url}", 404)


class FakeGitHub:
    def __init__(self, payloads=None):
        self.payloads = payloads or {}
        self.queries = 0

    def api(self, path, **kw):
        self.queries += 1
        for key, body in self.payloads.items():
            if key in path:
                return body
        return None


def tag_page(names_and_dates, next_page=None):
    return {
        "next": next_page,
        "results": [
            {"name": n, "last_updated": d} for n, d in names_and_dates
        ],
    }


def ctx_with(pages, gh_payloads=None):
    return Context(client=FakeClient(pages), github=FakeGitHub(gh_payloads), allow_az=False)


def comp(name, tag, **meta):
    return Component(ecosystem=DOCKER, name=name, current=tag, source="Dockerfile", meta=meta)


# --------------------------------------------------------- repo path mapping


@pytest.mark.parametrize(
    "image,expected",
    [
        ("python", "library/python"),          # official image
        ("nginx", "library/nginx"),
        ("grafana/grafana", "grafana/grafana"),  # Hub user namespace
        ("ghcr.io/org/app", None),            # host has a dot -> not Hub
        ("mcr.microsoft.com/dotnet/sdk", None),
        ("localhost:5000/app", None),         # host has a port -> not Hub
    ],
)
def test_hub_repo_path(image, expected):
    assert _hub_repo_path(image) == expected


# ------------------------------------------------------------ tag resolution


PYTHON_TAGS = tag_page([
    ("3.14.6-slim", "2026-07-01T00:00:00Z"),
    ("3.14-slim", "2026-07-01T00:00:00Z"),
    ("3.13.2-slim", "2026-02-01T00:00:00Z"),
    ("3.8-slim", "2020-05-01T00:00:00Z"),
    ("3.14.6-alpine", "2026-07-01T00:00:00Z"),
    ("3.9.1-alpine", "2021-01-01T00:00:00Z"),
    ("latest", "2026-07-01T00:00:00Z"),
    ("slim", "2026-07-01T00:00:00Z"),
    ("sha-abc123", "2026-07-01T00:00:00Z"),
    ("3", "2026-07-01T00:00:00Z"),
])


def test_resolves_within_the_same_variant():
    ctx = ctx_with({"repositories/library/python/tags": PYTHON_TAGS})
    r = resolve_image(comp("python", "3.8-slim"), ctx)
    assert r.latest_stable == "3.14.6-slim"
    assert "slim" in (r.note or "")


def test_alpine_pin_resolves_to_alpine_not_slim():
    """A variant must never be compared against a different variant."""
    ctx = ctx_with({"repositories/library/python/tags": PYTHON_TAGS})
    r = resolve_image(comp("python", "3.9.1-alpine"), ctx)
    assert r.latest_stable == "3.14.6-alpine"


def test_default_variant_ignores_suffixed_tags():
    ctx = ctx_with({"repositories/library/redis/tags": tag_page([
        ("8.8.1", "2026-06-01T00:00:00Z"),
        ("8.8.1-alpine", "2026-06-01T00:00:00Z"),
        ("5.0.14", "2020-01-01T00:00:00Z"),
    ])})
    r = resolve_image(comp("redis", "5.0.14"), ctx)
    assert r.latest_stable == "8.8.1"


def test_moving_aliases_and_junk_tags_excluded():
    ctx = ctx_with({"repositories/library/python/tags": PYTHON_TAGS})
    r = resolve_image(comp("python", "3.8-slim"), ctx)
    got = {rel.version for rel in r.releases}
    # "3" is a moving major alias; "latest"/"slim"/"sha-*" are not versions.
    assert "3" not in got
    assert not {"latest", "slim", "sha-abc123"} & got
    # 3.14-slim is a two-segment alias and is allowed as a candidate,
    # but the fully-qualified 3.14.6-slim must win.
    assert r.latest_stable == "3.14.6-slim"


def test_publish_dates_are_captured():
    ctx = ctx_with({"repositories/library/python/tags": PYTHON_TAGS})
    r = resolve_image(comp("python", "3.8-slim"), ctx)
    cur = r.release_for("3.8-slim")
    assert cur is not None and cur.published is not None
    assert cur.published.year == 2020


def test_pagination_is_followed():
    page1 = tag_page([("1.0.0", None)], next_page="https://hub/...page=2")
    page2 = tag_page([("2.0.0", None)])
    client_pages = {"page=1": page1, "page=2": page2}
    ctx = ctx_with(client_pages)
    r = resolve_image(comp("thing/app", "1.0.0"), ctx)
    assert r.latest_stable == "2.0.0"


def test_no_comparable_tags_is_reported_not_guessed():
    ctx = ctx_with({"repositories/library/weird/tags": tag_page([
        ("bananas", None), ("latest", None),
    ])})
    r = resolve_image(comp("weird", "3.1-slim"), ctx)
    assert r.latest_stable is None
    assert "no comparable tags" in (r.note or "")


# ----------------------------------------------------- non-comparable inputs


def test_floating_latest_tag_is_flagged_not_resolved():
    ctx = ctx_with({})
    r = resolve_image(comp("nginx", "latest", floating=True), ctx)
    assert r.latest_stable is None
    assert "floating" in (r.note or "")
    assert ctx.client.calls == [], "must not query the registry for a floating tag"


def test_digest_pin_is_flagged_not_resolved():
    c = comp("app", None, digest="sha256:abc")
    ctx = ctx_with({})
    r = resolve_image(c, ctx)
    assert "digest-pinned" in (r.note or "")


def test_registry_error_becomes_a_note_not_an_exception():
    ctx = ctx_with({})  # every URL raises HttpError
    r = resolve_image(comp("python", "3.8-slim"), ctx)
    # A dead registry must degrade to an error field, never propagate.
    assert r.error and "hub.docker.com" in r.error
    assert r.latest_stable is None
    assert r.releases == []


# ------------------------------------------------- non-Hub registry fallback


def test_ghcr_image_without_known_repo_is_unresolved():
    ctx = ctx_with({})
    r = resolve_image(comp("ghcr.io/unknown/app", "1.2.3"), ctx)
    assert r.latest_stable is None
    assert "not supported for tag enumeration" in (r.note or "")


def test_non_hub_image_falls_back_to_github_releases():
    """mcr/ghcr images resolve via GitHub when the project repo is known."""
    gh = {
        "repos/grafana/grafana/releases": [
            {"tag_name": "v12.1.0", "published_at": "2026-06-01T00:00:00Z", "prerelease": False},
            {"tag_name": "v13.0.0-beta1", "published_at": "2026-07-01T00:00:00Z", "prerelease": True},
            {"tag_name": "v10.0.0", "published_at": "2024-01-01T00:00:00Z", "prerelease": False},
        ]
    }
    ctx = ctx_with({}, gh)
    # Force the non-Hub path with a registry host, but a name that maps to a repo.
    c = comp("grafana/grafana", "10.0.0")
    from driftscope.resolvers import oci

    r = oci._resolve_via_github(c, "grafana/grafana", ctx)
    assert r.latest_stable == "12.1.0", "prereleases must not win"
    assert r.repo == "grafana/grafana"
    assert "GitHub releases" in (r.note or "")


# --------------------------------------------------------------- integration


@pytest.mark.network
def test_real_docker_hub_official_image():
    """Hits the public registry: guards against Docker Hub API shape changes."""
    from driftscope.engine import build_context

    ctx = build_context(no_github=True, no_az=True)
    r = resolve_image(comp("python", "3.8-slim"), ctx)
    assert r.error is None, r.error
    assert r.latest_stable, "expected a resolved tag"
    v = parse(r.latest_stable)
    assert v.valid and v.variant == "slim"
    assert v.major == 3 and v.minor >= 12, f"unexpectedly old: {r.latest_stable}"
    assert any(rel.published for rel in r.releases), "expected publish dates"


@pytest.mark.network
def test_real_docker_hub_namespaced_image():
    from driftscope.engine import build_context

    ctx = build_context(no_github=True, no_az=True)
    r = resolve_image(comp("grafana/grafana", "9.0.0"), ctx)
    assert r.error is None, r.error
    assert r.latest_stable
    assert parse(r.latest_stable).major >= 10


@pytest.mark.network
def test_real_registry_404_is_handled():
    from driftscope.engine import build_context

    ctx = build_context(no_github=True, no_az=True)
    r = resolve_image(comp("driftscope-does-not-exist-xyz", "1.0.0"), ctx)
    assert r.latest_stable is None
    assert r.error, "a missing repository must surface as an error, not a crash"


# ------------------------------------------------------------- Helm charts


def helm_detail(*, versions, links=None, repo_url="https://charts.bitnami.com/bitnami",
                home="https://bitnami.com", deprecated=False, name="redis"):
    return {
        "name": name,
        "version": versions[0]["version"] if versions else None,
        "available_versions": versions,
        "links": links or [],
        "repository": {"name": "bitnami", "url": repo_url},
        "home_url": home,
        "deprecated": deprecated,
    }


def v(ver, ts, prerelease=False):
    return {"version": ver, "ts": ts, "prerelease": prerelease}


SEARCH = {"packages": [{"name": "redis", "repository": {"name": "bitnami"}}]}


def helm_ctx(detail, search=None):
    return ctx_with({
        "packages/search": search or SEARCH,
        "packages/helm/bitnami/redis": detail,
    })


def test_chart_versions_are_dated_from_ts():
    from driftscope.resolvers.oci import resolve_chart
    from driftscope.models import HELM

    detail = helm_detail(versions=[v("27.0.0", 1750000000), v("16.11.2", 1654299654)])
    c = Component(ecosystem=HELM, name="redis", current="16.11.2",
                  source="Chart.yaml", meta={"dependency": True})
    r = resolve_chart(c, helm_ctx(detail))
    assert r.latest_stable == "27.0.0"
    cur = r.release_for("16.11.2")
    assert cur is not None and cur.published is not None
    assert cur.published.year == 2022


def test_chart_github_repo_comes_from_source_link():
    """Regression: repo was read from repository.url (a Helm repo), so it was
    always None for Bitnami charts -- silently disabling the issue delta."""
    from driftscope.resolvers.oci import resolve_chart
    from driftscope.models import HELM

    detail = helm_detail(
        versions=[v("27.0.0", 1750000000)],
        links=[{"name": "source", "url": "https://github.com/bitnami/charts/tree/main/bitnami/redis"}],
    )
    c = Component(ecosystem=HELM, name="redis", current="16.11.2",
                  source="Chart.yaml", meta={"dependency": True})
    r = resolve_chart(c, helm_ctx(detail))
    assert r.repo == "bitnami/charts"


def test_source_link_preferred_over_other_links():
    from driftscope.resolvers.oci import resolve_chart
    from driftscope.models import HELM

    detail = helm_detail(
        versions=[v("27.0.0", 1750000000)],
        links=[
            {"name": "support", "url": "https://github.com/some/helpdesk"},
            {"name": "source", "url": "https://github.com/bitnami/charts"},
        ],
    )
    c = Component(ecosystem=HELM, name="redis", current="1.0.0",
                  source="Chart.yaml", meta={"dependency": True})
    assert resolve_chart(c, helm_ctx(detail)).repo == "bitnami/charts"


def test_local_chart_has_no_upstream():
    from driftscope.resolvers.oci import resolve_chart
    from driftscope.models import HELM

    c = Component(ecosystem=HELM, name="my-platform", current="0.1.0",
                  source="Chart.yaml", meta={"self": True})
    r = resolve_chart(c, ctx_with({}))
    assert r.latest_stable is None
    assert "no upstream" in (r.note or "")


def test_deprecated_chart_is_flagged():
    from driftscope.resolvers.oci import resolve_chart
    from driftscope.models import HELM

    detail = helm_detail(versions=[v("27.0.0", 1750000000)], deprecated=True)
    c = Component(ecosystem=HELM, name="redis", current="16.0.0",
                  source="Chart.yaml", meta={"dependency": True})
    r = resolve_chart(c, helm_ctx(detail))
    assert r.deprecated is True


def test_chart_not_on_artifacthub_is_reported():
    from driftscope.resolvers.oci import resolve_chart
    from driftscope.models import HELM

    c = Component(ecosystem=HELM, name="redis", current="1.0.0",
                  source="Chart.yaml", meta={"dependency": True})
    r = resolve_chart(c, ctx_with({"packages/search": {"packages": []}}))
    assert r.latest_stable is None
    assert "not found on Artifact Hub" in (r.note or "")


@pytest.mark.network
def test_real_artifacthub_chart_is_fully_dated():
    from driftscope.engine import build_context
    from driftscope.resolvers.oci import resolve_chart
    from driftscope.models import HELM

    ctx = build_context(no_github=True, no_az=True)
    c = Component(ecosystem=HELM, name="postgresql", current="11.6.12",
                  source="Chart.yaml", meta={"dependency": True})
    r = resolve_chart(c, ctx)
    assert r.error is None, r.error
    assert r.releases, "expected a version list"
    dated = [x for x in r.releases if x.published]
    assert len(dated) == len(r.releases), "Artifact Hub dates every version"
    assert r.repo == "bitnami/charts"


# ------------------------------------------------- truncated listing semantics


def test_truncated_listing_is_flagged():
    """Docker Hub pages by recency, so a truncated listing must be declared.

    Regression: the age lower-bound heuristic treated "oldest tag in the listing"
    as "an older release", producing "node:14-alpine is 13 days behind 26.5".
    """
    p1 = tag_page([("26.5-alpine", "2026-07-15T00:00:00Z")], next_page="page=2")
    p2 = tag_page([("24.1-alpine", "2026-06-23T00:00:00Z")], next_page="page=3")
    ctx = ctx_with({"page=1": p1, "page=2": p2})
    r = resolve_image(comp("node", "14-alpine"), ctx)
    assert r.extra.get("list_truncated") is True
    assert "truncated" in (r.note or "")
    assert "not in the fetched listing" in (r.note or "")


def test_complete_listing_is_not_flagged_truncated():
    ctx = ctx_with({"repositories/library/redis/tags": tag_page([
        ("8.8.1", "2026-06-01T00:00:00Z"), ("5.0.14", "2020-01-01T00:00:00Z"),
    ])})
    r = resolve_image(comp("redis", "5.0.14"), ctx)
    assert r.extra.get("list_truncated") is False


def test_push_time_caveat_always_present():
    """Base images are rebuilt for OS patches, so tag dates are push times."""
    ctx = ctx_with({"repositories/library/python/tags": PYTHON_TAGS})
    r = resolve_image(comp("python", "3.8-slim"), ctx)
    assert "last-push times" in (r.note or "")
    assert r.extra.get("dates_are_push_times") is True

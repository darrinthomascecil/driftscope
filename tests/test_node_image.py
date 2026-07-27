"""AKS node image age and AgentBaker delta tests.

Node images are the one surface where age is the *whole* metric: version strings
are opaque, AKS may report no upgrade target, and the risk is unpatched OS CVEs
rather than feature drift.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from driftscope import advisor
from driftscope.ghub import agentbaker as ab
from driftscope.models import (
    AZURE_NODE_IMAGE,
    SEV_CRITICAL,
    SEV_CURRENT,
    SEV_HIGH,
    SEV_LOW,
    SEV_MEDIUM,
    SEV_UNKNOWN,
    Component,
    Release,
    Resolution,
)
from driftscope.resolvers.azure import parse_node_image_date
from driftscope.version import lag_between

# --------------------------------------------------------- version date parsing


@pytest.mark.parametrize(
    "version,expected",
    [
        # Current format: YYYYMM.DD.revision
        ("AKSUbuntu-2204gen2containerd-202607.15.0", date(2026, 7, 15)),
        ("AKSUbuntu-2204containerd-202506.10.0", date(2025, 6, 10)),
        ("AKSAzureLinux-V2gen2-202601.20.0", date(2026, 1, 20)),
        ("AKSUbuntu-2204gen2arm64containerd-202503.31.0", date(2025, 3, 31)),
        # Legacy format: YYYY.MM.DD
        ("AKSUbuntu-1804gen2containerd-2022.08.15", date(2022, 8, 15)),
        ("AKSUbuntu-1604-2019.10.24", date(2019, 10, 24)),
        # Windows: trailing YYMMDD
        ("AKSWindows-2022-containerd-20348.2582.240716", date(2024, 7, 16)),
        ("AKSWindows-2019-17763.4131.230507", date(2023, 5, 7)),
        # Unparseable
        ("AKSUbuntu-2204gen2containerd", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_node_image_date(version, expected):
    assert parse_node_image_date(version) == expected


def test_impossible_dates_are_rejected_not_guessed():
    assert parse_node_image_date("AKSUbuntu-2204-202613.45.0") is None
    assert parse_node_image_date("AKSUbuntu-2204-202602.30.0") is None  # Feb 30


# --------------------------------------------------------- AgentBaker mapping


def rel(tag, build, published, body=""):
    # The real releases listing includes the body, which is why the delta no
    # longer fetches each release individually.
    return {"tag_name": tag, "published_at": published, "body": body}


class FakeGH:
    """Serves canned AgentBaker release payloads."""

    def __init__(self, releases, bodies=None, counts=None):
        self.releases = releases
        self.bodies = bodies or {}
        self.counts = counts or {}
        self.queries = 0
        self.enabled = True

    def api(self, path, **kw):
        self.queries += 1
        if "releases?per_page" in path:
            return self.releases if "page=1" in path or "page=" not in path else []
        for tag, body in self.bodies.items():
            if f"releases/tags/{tag}" in path:
                return {"tag_name": tag, "body": body}
        return None

    def search_count(self, q):
        return self.counts.get("open" if "is:open" in q else "total", 0)

    def search_issues(self, q, limit=40):
        return []


RELEASES = [
    rel("v0.20260720.1", date(2026, 7, 20), "2026-07-21T23:49:42Z"),
    rel("v0.20260709.1", date(2026, 7, 9), "2026-07-10T22:48:13Z"),
    rel("v0.20260702.0", date(2026, 7, 2), "2026-07-03T01:18:59Z"),
    rel("v0.20260113.0", date(2026, 1, 13), "2026-01-15T00:00:00Z"),
    # Non-node-image tags that must be filtered out
    rel("anc/v202605.14.1", None, "2026-05-26T22:37:44Z"),
    rel("aks-node-controller/hotfix/v202605.14.1", None, "2026-05-27T22:01:02Z"),
    rel("v20260421.mitchzhu", None, "2026-04-21T00:00:00Z"),
]


def test_release_index_filters_non_node_image_tags():
    idx = ab.release_index(FakeGH(RELEASES))
    tags = [r.tag for r in idx]
    assert tags == ["v0.20260113.0", "v0.20260702.0", "v0.20260709.1", "v0.20260720.1"]
    assert not any("anc/" in t or "hotfix" in t or "mitchzhu" in t for t in tags)


def test_release_date_differs_from_tag_date():
    """The tag encodes the build cut; published_at is when it actually shipped."""
    idx = ab.release_index(FakeGH(RELEASES))
    newest = ab.latest_release(idx)
    assert newest.build_date == date(2026, 7, 20)
    assert newest.published_date == date(2026, 7, 21)
    assert newest.effective_date == date(2026, 7, 21)


def test_image_attributed_to_release_cut_at_or_before_build():
    idx = ab.release_index(FakeGH(RELEASES))
    # An image built 2026-07-15 came from the release cut on 07-09, not 07-20.
    match = ab.release_for_date(idx, date(2026, 7, 15))
    assert match.tag == "v0.20260709.1"


def test_image_older_than_all_known_releases_is_unattributed():
    idx = ab.release_index(FakeGH(RELEASES))
    assert ab.release_for_date(idx, date(2019, 1, 1)) is None


def test_delta_counts_releases_in_window():
    releases = [
        rel("v0.20260720.1", date(2026, 7, 20), "2026-07-21T23:49:42Z",
            "* chore: nothing interesting"),
        rel("v0.20260709.1", date(2026, 7, 9), "2026-07-10T22:48:13Z",
            "* feat: add thing\n* security: patch CVE-2026-1234"),
        rel("v0.20260702.0", date(2026, 7, 2), "2026-07-03T01:18:59Z",
            "* fix: remove deprecated flag\n* chore: bump deps"),
        rel("v0.20260113.0", date(2026, 1, 13), "2026-01-15T00:00:00Z"),
    ]
    gh = FakeGH(releases, counts={"total": 5, "open": 2})
    d = ab.node_image_delta(gh, "AKSUbuntu-2204-202601.20.0", date(2026, 1, 20))
    assert d.repo == "Azure/AgentBaker"
    assert d.from_ref == "v0.20260113.0"
    assert d.to_ref == "v0.20260720.1"
    assert d.window_resolved is True
    assert d.releases_in_window == ["v0.20260702.0", "v0.20260709.1", "v0.20260720.1"]
    assert d.open_bug_count == 2 and d.total_bug_count == 5
    joined = " ".join(d.breaking_notes)
    assert "CVE-2026-1234" in joined
    assert "remove deprecated flag" in joined


def test_current_image_reports_no_gap():
    d = ab.node_image_delta(gh := FakeGH(RELEASES), "AKSUbuntu-2204-202607.20.0",
                            date(2026, 7, 20))
    assert d.window_resolved is False
    assert "current with the newest" in (d.note or "")


def test_delta_handles_unreadable_repo():
    class Dead(FakeGH):
        def api(self, path, **kw):
            return None

    d = ab.node_image_delta(Dead([]), "AKSUbuntu-2204-202601.20.0", date(2026, 1, 20))
    assert d.error and "AgentBaker" in d.error


# ----------------------------------------------------------------- severity


def _score_for_age(age_days):
    c = Component(
        ecosystem=AZURE_NODE_IMAGE, name="aks/c/pool/p/nodeimage",
        current="AKSUbuntu-2204gen2containerd-202601.20.0", source="azure://x",
        meta={"pool": "p", "cluster": "c"},
    )
    now = datetime.now(timezone.utc)
    published = now - timedelta(days=age_days)
    r = Resolution(latest_stable=None, releases=[Release(version=c.current, published=published)])
    lag = lag_between(c.current, None, current_published=published, now=now)
    return c, r, lag, advisor.score(c, r, lag)


@pytest.mark.parametrize(
    "age,expected",
    [
        (5, SEV_CURRENT),
        (29, SEV_CURRENT),
        (31, SEV_LOW),
        (75, SEV_MEDIUM),
        (150, SEV_HIGH),
        (300, SEV_CRITICAL),
        (800, SEV_CRITICAL),
    ],
)
def test_node_image_severity_uses_a_tighter_scale(age, expected):
    """Generic year-scale thresholds would rate a 150-day-old image LOW."""
    _, _, _, sev = _score_for_age(age)
    assert sev == expected


def test_node_image_scored_despite_incomparable_version_strings():
    """Regression: the comparability guard swallowed every node image finding.

    Node image versions are opaque and AKS often reports no upgrade target, so
    scoring must happen on age before those guards run.
    """
    c, r, lag, sev = _score_for_age(200)
    assert lag.comparable is False, "node image strings are not comparable"
    assert r.latest_stable is None, "and there may be no upstream target"
    assert sev == SEV_HIGH, "yet it must still be scored"


def test_unparseable_image_is_unknown_not_zero_age():
    c = Component(
        ecosystem=AZURE_NODE_IMAGE, name="aks/c/pool/p/nodeimage",
        current="MysteryImage-nodate", source="azure://x",
    )
    r = Resolution()
    lag = lag_between("MysteryImage-nodate", None)
    assert advisor.score(c, r, lag) == SEV_UNKNOWN


# ------------------------------------------------------------------- advice


def test_advice_gives_the_node_image_only_upgrade_command():
    c, r, lag, sev = _score_for_age(200)
    a = advisor.advise(c, r, lag, sev)
    joined = " ".join(a.steps)
    assert "--node-image-only" in joined
    assert "--cluster-name c" in joined and "-n p" in joined
    assert "auto-upgrade-channel NodeImage" in joined
    assert any("PDB" in x or "PodDisruptionBudget" in x for x in a.steps + a.cautions)


def test_advice_warns_about_node_replacement():
    c, r, lag, sev = _score_for_age(200)
    a = advisor.advise(c, r, lag, sev)
    assert any("replaces nodes" in x for x in a.cautions)


def test_old_image_gets_runtime_caution():
    c, r, lag, sev = _score_for_age(300)
    a = advisor.advise(c, r, lag, sev)
    assert any("kernel and container runtime" in x for x in a.cautions)


# ------------------------------------------------- dates survive incomparability


def test_dates_computed_even_when_versions_are_incomparable():
    """Regression: lag_between returned early on incomparable versions, throwing
    away perfectly good date arithmetic."""
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    old = datetime(2026, 1, 20, tzinfo=timezone.utc)
    new = datetime(2026, 7, 21, tzinfo=timezone.utc)
    lag = lag_between(
        "AKSUbuntu-2204-202601.20.0", "(AgentBaker v0.20260720.1)",
        current_published=old, latest_published=new, now=now,
    )
    assert lag.comparable is False
    assert lag.current_age_days == 187
    assert lag.days_behind == 182


@pytest.mark.network
def test_real_agentbaker_index():
    """Guards against AgentBaker changing its release tagging scheme."""
    from driftscope.engine import build_context

    ctx = build_context(no_az=True)
    idx = ab.release_index(ctx.github)
    assert len(idx) > 20, "expected a substantial release history"
    newest = ab.latest_release(idx)
    assert newest.build_date.year >= 2026
    assert newest.published is not None
    # Every tag must match the v0.YYYYMMDD.R shape.
    assert all(r.tag.startswith("v0.") for r in idx)
    # Index must be sorted oldest-first.
    assert [r.build_date for r in idx] == sorted(r.build_date for r in idx)


# ------------------------------------- cluster version: platform vs upstream


def test_cluster_target_prefers_platform_offered_versions():
    """Regression, caught on a real AKS cluster.

    Upstream Kubernetes had 1.36.3 while AKS offered at most 1.36.2, and the
    resolver reported 1.36.3 -- recommending a version the customer cannot deploy.
    Live-supplied candidates must win over upstream releases.
    """
    from driftscope.models import K8S_CLUSTER
    from driftscope.resolvers.github_src import resolve_k8s_cluster

    c = Component(
        ecosystem=K8S_CLUSTER, name="aks/prod", current="1.35.6",
        source="azure://x", meta={"available": ["1.36.2", "1.36.1", "1.36.0"]},
    )

    class ShouldNotBeCalled:
        enabled = True
        queries = 0

        def api(self, *a, **k):
            raise AssertionError("must not query upstream when candidates are known")

    ctx_obj = type("C", (), {"github": ShouldNotBeCalled(), "client": None, "allow_az": False})()
    r = resolve_k8s_cluster(c, ctx_obj)
    assert r.latest_stable == "1.36.2"
    assert "offered by the platform" in (r.note or "")


def test_cluster_falls_back_to_upstream_without_candidates():
    from driftscope.models import K8S_CLUSTER
    from driftscope.resolvers.github_src import resolve_k8s_cluster

    class GH:
        enabled = True
        queries = 0

        def api(self, path, **k):
            return [
                {"tag_name": "v1.36.3", "published_at": "2026-07-01T00:00:00Z", "prerelease": False},
                {"tag_name": "v1.37.0-alpha.1", "published_at": "2026-07-20T00:00:00Z", "prerelease": True},
            ]

    c = Component(ecosystem=K8S_CLUSTER, name="k8s", current="1.35.0", source="f")
    ctx_obj = type("C", (), {"github": GH(), "client": None, "allow_az": False})()
    r = resolve_k8s_cluster(c, ctx_obj)
    assert r.latest_stable == "1.36.3", "prereleases must not win"


def test_real_world_node_image_string_from_live_cluster():
    """The exact string a live AKS cluster returned, incl. Ubuntu 24.04 naming."""
    assert parse_node_image_date("AKSUbuntu-2404gen2containerd-202607.09.0") == date(2026, 7, 9)


@pytest.mark.parametrize(
    "version,expected",
    [
        # The distro number must never be mistaken for the build date.
        ("AKSUbuntu-2004gen2containerd-202607.09.0", date(2026, 7, 9)),
        ("AKSUbuntu-2204gen2containerd-202607.09.0", date(2026, 7, 9)),
        ("AKSUbuntu-2404gen2containerd-202607.09.0", date(2026, 7, 9)),
        ("AKSUbuntu-2004gen2containerd-2022.08.15", date(2022, 8, 15)),
    ],
)
def test_distro_number_is_not_confused_with_build_date(version, expected):
    assert parse_node_image_date(version) == expected


def test_release_bodies_come_from_the_listing_not_per_tag():
    """Regression: each release body was re-fetched by tag, costing one extra
    call per release in the window (16 for a six-month gap) when the listing
    already carried them."""
    releases = [
        rel("v0.20260720.1", date(2026, 7, 20), "2026-07-21T00:00:00Z", "* fix: removed a flag"),
        rel("v0.20260113.0", date(2026, 1, 13), "2026-01-15T00:00:00Z"),
    ]
    calls: list[str] = []

    class Recorder(FakeGH):
        def api(self, path, **kw):
            calls.append(path)
            return super().api(path, **kw)

    d = ab.node_image_delta(Recorder(releases), "AKSUbuntu-2204-202601.20.0",
                            date(2026, 1, 20))
    assert d.breaking_notes, "notes must still be extracted"
    assert not any("releases/tags/" in c for c in calls), \
        "must not fetch individual releases"

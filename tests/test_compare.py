"""Fleet comparison aggregation. Pure -- synthetic Reports, no network."""

from __future__ import annotations

from driftscope.compare import compare_reports, lag_label, newest_pin
from driftscope.models import (
    Component,
    Finding,
    Lag,
    Report,
    Resolution,
    ScanStats,
    SEV_CURRENT,
    SEV_HIGH,
    SEV_MEDIUM,
)


def _report(root, comps):
    """comps: (ecosystem, name, current, latest, severity[, lag]) tuples."""
    findings = []
    for row in comps:
        e, n, c, l, s = row[:5]
        lag = row[5] if len(row) > 5 else Lag()
        findings.append(Finding(
            component=Component(ecosystem=e, name=n, current=c, source="f"),
            resolution=Resolution(latest_stable=l),
            lag=lag,
            severity=s,
        ))
    return Report(root=root, findings=findings, stats=ScanStats())


def test_divergence_is_flagged_the_ioredis_case():
    a = _report("repo-a", [("npm", "ioredis", "4.3.0", "5.4.1", SEV_HIGH),
                           ("npm", "lodash", "4.17.21", "4.17.21", SEV_CURRENT)])
    b = _report("repo-b", [("npm", "ioredis", "4.4.0", "5.4.1", SEV_HIGH),
                           ("npm", "lodash", "4.17.21", "4.17.21", SEV_CURRENT)])
    fc = compare_reports([("repo-a", a), ("repo-b", b)])

    ioredis = next(r for r in fc.rows if r.name == "ioredis")
    assert ioredis.diverges is True
    assert ioredis.pins["repo-a"].versions == ["4.3.0"]
    assert ioredis.pins["repo-b"].versions == ["4.4.0"]
    assert ioredis.latest == "5.4.1"

    # align_to = the newest pin already in the fleet (the consensus target)
    assert ioredis.align_to == "4.4.0"

    lodash = next(r for r in fc.rows if r.name == "lodash")
    assert lodash.diverges is False
    assert lodash.align_to is None  # nothing to align when they agree

    # per-repo posture roll-up
    assert fc.posture["repo-a"]["high"] == 1 and fc.posture["repo-a"]["current"] == 1

    # divergent rows sort first
    assert fc.rows[0].name == "ioredis"
    assert [r.name for r in fc.divergent] == ["ioredis"]
    assert [r.name for r in fc.shared] == ["ioredis", "lodash"]  # both used by 2 repos


def test_newest_pin_orders_comparable_only():
    assert newest_pin(["4.3.0", "4.4.0"], "npm") == "4.4.0"
    assert newest_pin(["v1.7.0", "v1.8.1"], "go-module") == "v1.8.1"
    # a floating tag can't be ordered -> None, so the report says "differs"
    assert newest_pin(["latest", "1.0"], "docker-image") is None


def test_a_repo_can_pin_one_component_at_several_versions():
    a = _report("mono", [("npm", "ioredis", "4.3.0", "5.0.0", SEV_HIGH),
                         ("npm", "ioredis", "4.4.0", "5.0.0", SEV_HIGH)])
    fc = compare_reports([("mono", a)])
    ioredis = fc.rows[0]
    assert set(ioredis.pins["mono"].versions) == {"4.3.0", "4.4.0"}
    assert ioredis.diverges is True  # internal skew within one repo still counts


def test_per_repo_errors_are_carried():
    a = _report("repo-a", [("npm", "x", "1.0", "2.0", SEV_HIGH)])
    fc = compare_reports([("repo-a", a)], errors={"repo-b": "clone failed"})
    assert fc.errors["repo-b"] == "clone failed"


# --------------------------------------------------------- lag depth: how far behind

def test_lag_label_leads_with_the_coarsest_step():
    assert lag_label(Lag(behind_major=3, releases_behind=40, comparable=True)) == "3 majors"
    assert lag_label(Lag(behind_minor=1, comparable=True)) == "1 minor"
    assert lag_label(Lag(behind_patch=2, comparable=True)) == "2 patches"
    assert lag_label(Lag(releases_behind=5, comparable=True)) == "5 releases"
    assert lag_label(Lag(comparable=True)) == ""             # current -> no label
    assert lag_label(Lag(behind_major=9, comparable=False)) == ""  # not orderable -> no distance
    assert lag_label(None) == ""


def test_depth_aggregates_blast_radius_times_how_far_behind():
    deep = Lag(behind_major=3, releases_behind=40, stable_releases_behind=40, comparable=True)
    shal = Lag(behind_major=1, releases_behind=8, stable_releases_behind=8, comparable=True)
    a = _report("repo-a", [("npm", "ioredis", "4.3.0", "5.4.1", SEV_HIGH, deep)])
    b = _report("repo-b", [("npm", "ioredis", "4.4.0", "5.4.1", SEV_HIGH, shal)])
    row = compare_reports([("repo-a", a), ("repo-b", b)]).rows[0]
    assert row.depth_comparable is True
    assert row.worst_lag.behind_major == 3   # the deepest single laggard drives "Behind"
    assert row.fleet_debt == 48              # 40 + 8 releases summed across the fleet


def test_wide_but_shallow_ranks_below_narrow_but_deep_within_a_band():
    # Both HIGH. deep-pkg: 2 majors behind in 2 repos. shallow-pkg: 1 patch behind in 3.
    deep = Lag(behind_major=2, stable_releases_behind=30, comparable=True)
    shallow = Lag(behind_patch=1, stable_releases_behind=1, comparable=True)
    reports = [
        _report("r1", [("npm", "deep-pkg", "1.0", "3.0", SEV_HIGH, deep),
                       ("npm", "shallow-pkg", "2.0.0", "2.0.1", SEV_HIGH, shallow)]),
        _report("r2", [("npm", "deep-pkg", "1.0", "3.0", SEV_HIGH, deep),
                       ("npm", "shallow-pkg", "2.0.0", "2.0.1", SEV_HIGH, shallow)]),
        _report("r3", [("npm", "shallow-pkg", "2.0.0", "2.0.1", SEV_HIGH, shallow)]),
    ]
    fc = compare_reports([(r.root, r) for r in reports])
    names = [r.name for r in fc.rows]
    # shallow-pkg has the wider blast radius (3 vs 2 repos) but far less debt (3 vs 60),
    # so deep-pkg still leads: depth beats mere spread.
    assert names.index("deep-pkg") < names.index("shallow-pkg")
    assert next(r for r in fc.rows if r.name == "deep-pkg").fleet_debt == 60
    assert next(r for r in fc.rows if r.name == "shallow-pkg").fleet_debt == 3


def test_non_orderable_divergence_invents_no_depth():
    nc = Lag(comparable=False, reason="calver")
    a = _report("repo-a", [("docker-image", "app", "2024.01", "2025.06", SEV_MEDIUM, nc)])
    b = _report("repo-b", [("docker-image", "app", "2025.06", "2025.06", SEV_CURRENT, nc)])
    row = next(r for r in compare_reports([("repo-a", a), ("repo-b", b)]).rows if r.name == "app")
    assert row.diverges is True
    assert row.depth_comparable is False   # calver can't be ordered -> no debt invented
    assert row.fleet_debt == 0 and row.worst_lag is None

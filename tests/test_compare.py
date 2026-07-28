"""Fleet comparison aggregation. Pure -- synthetic Reports, no network."""

from __future__ import annotations

from driftscope.compare import compare_reports, newest_pin
from driftscope.models import (
    Component,
    Finding,
    Lag,
    Report,
    Resolution,
    ScanStats,
    SEV_CURRENT,
    SEV_HIGH,
)


def _report(root, comps):
    findings = [
        Finding(
            component=Component(ecosystem=e, name=n, current=c, source="f"),
            resolution=Resolution(latest_stable=l),
            lag=Lag(),
            severity=s,
        )
        for (e, n, c, l, s) in comps
    ]
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

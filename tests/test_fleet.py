"""Fleet aggregation: ingest -> evaluate -> verdict. No network (localhost HTTP only)."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from driftscope.fleet import FleetService, Store
from driftscope.fleet.api import make_handler
from driftscope.fleet.models import (
    BEHIND,
    BLOCK,
    DEPRECATED,
    PASS,
    UNSANCTIONED_UPGRADE,
)
from driftscope.fleet.policy import Policy


def report(remote, comps, *, generated_at="2026-08-01T00:00:00Z", release=None):
    """A minimal report.json. comps = [(ecosystem, name, current, severity, deprecated)]."""
    findings = [{
        "component": {"ecosystem": eco, "name": name, "current": cur},
        "resolution": {"deprecated": dep, "latest_stable": None},
        "severity": sev,
    } for (eco, name, cur, sev, dep) in comps]
    src = {"remote": remote, "ref": "main", "commit": "abc123"}
    if release:
        src["release"] = release
    return {"schema": "driftscope/1", "source": src,
            "generated_at": generated_at, "findings": findings}


def svc(tmp_path, policy):
    return FleetService(Store(tmp_path), policy)


# --------------------------------------------------------------- ingest / store

def test_ingest_projects_findings(tmp_path):
    s = Store(tmp_path)
    rec = s.ingest("acme", report("gh/app", [("pypi", "django", "4.2.0", "high", False)]))
    assert rec.repo == "gh/app" and len(rec.components) == 1
    assert rec.components[0].key == "pypi:django" and rec.components[0].version == "4.2.0"


def test_latest_wins_keeps_the_newest_report(tmp_path):
    s = Store(tmp_path)
    s.ingest("acme", report("gh/app", [("pypi", "django", "4.2.0", None, False)],
                             generated_at="2026-01-01T00:00:00Z"))
    s.ingest("acme", report("gh/app", [("pypi", "django", "4.2.11", None, False)],
                             generated_at="2026-06-01T00:00:00Z"))
    # An older report arriving late must NOT overwrite the newer one.
    s.ingest("acme", report("gh/app", [("pypi", "django", "3.0.0", None, False)],
                             generated_at="2025-01-01T00:00:00Z"))
    rec = s.record("acme", "gh/app")
    assert rec.components[0].version == "4.2.11", "latest-wins by generated_at"


def test_ingest_without_repo_identity_fails_closed(tmp_path):
    # A report with no source.remote and no explicit repo must NOT silently key to a shared
    # "unknown" and clobber another repo -- it must fail loud. (Regression: found in testing.)
    s = Store(tmp_path)
    with pytest.raises(ValueError):
        s.ingest("acme", {"source": {}, "findings": []})
    # ...but an explicit repo id is accepted when the report carries no remote.
    rec = s.ingest("acme", {"source": {}, "findings": []}, repo="gh/local-scan")
    assert rec.repo == "gh/local-scan"


def test_two_identity_less_reports_do_not_collide(tmp_path):
    s = Store(tmp_path)
    s.ingest("acme", report("", [("npm", "a", "1.0.0", None, False)]), repo="gh/a")
    s.ingest("acme", report("", [("npm", "b", "2.0.0", None, False)]), repo="gh/b")
    assert sorted(r.repo for r in s.records("acme")) == ["gh/a", "gh/b"]


def test_tenants_are_isolated(tmp_path):
    s = Store(tmp_path)
    s.ingest("acme", report("gh/a", [("pypi", "django", "4.2.0", None, False)]))
    s.ingest("globex", report("gh/b", [("pypi", "flask", "3.0.0", None, False)]))
    assert [r.repo for r in s.records("acme")] == ["gh/a"]
    assert [r.repo for r in s.records("globex")] == ["gh/b"]


# --------------------------------------------------------------- conformance (curated)

CURATED = Policy.from_dict({"curated": {"pypi:django": "4.2.11"},
                            "block_on": [UNSANCTIONED_UPGRADE]})


def test_unsanctioned_upgrade_is_detected_and_blocks(tmp_path):
    v = svc(tmp_path, CURATED).ingest("acme", report("gh/app", [("pypi", "django", "5.0.0", None, False)]))
    kinds = {x.kind for x in v.violations}
    assert UNSANCTIONED_UPGRADE in kinds
    assert v.status == BLOCK, "ahead of a curated pin blocks the promotion"


def test_behind_is_flagged_but_advisory_by_default(tmp_path):
    v = svc(tmp_path, CURATED).ingest("acme", report("gh/app", [("pypi", "django", "4.1.0", None, False)]))
    kinds = {x.kind for x in v.violations}
    assert BEHIND in kinds
    assert v.status == PASS, "behind is not in block_on by default -> advisory"


def test_conforming_version_has_no_conformance_violation(tmp_path):
    v = svc(tmp_path, CURATED).ingest("acme", report("gh/app", [("pypi", "django", "4.2.11", None, False)]))
    assert not any(x.kind in (UNSANCTIONED_UPGRADE, BEHIND) for x in v.violations)
    assert v.status == PASS


def test_ungoverned_component_gets_no_conformance_verdict(tmp_path):
    # react is neither curated nor (baseline off) has a baseline -> no conformance claim.
    v = svc(tmp_path, CURATED).ingest("acme", report("gh/app", [("npm", "react", "19.0.0", None, False)]))
    assert not any(x.kind in (UNSANCTIONED_UPGRADE, BEHIND) for x in v.violations)


def test_unresolved_version_makes_no_claim(tmp_path):
    # `current` can be None when the scan couldn't resolve a pin -> compare must make no
    # claim rather than risk a false positive. (In real reports `current` is the resolved
    # version; ranged pins like "~> 4" are already normalised by the scan.)
    v = svc(tmp_path, CURATED).ingest("acme", report("gh/app", [("pypi", "django", None, None, False)]))
    assert not any(x.kind in (UNSANCTIONED_UPGRADE, BEHIND) for x in v.violations)


# --------------------------------------------------------------- risk (policy-free)

def test_deprecated_version_is_flagged_without_any_policy(tmp_path):
    empty = Policy.from_dict({})
    v = svc(tmp_path, empty).ingest("acme", report("gh/app", [("pypi", "old", "1.0.0", None, True)]))
    assert any(x.kind == DEPRECATED for x in v.violations)


# --------------------------------------------------------------- fleet baseline

BASELINE = Policy.from_dict({
    "baseline": {"enabled": True, "min_repos": 3, "min_share": 0.6},
    "block_on": [UNSANCTIONED_UPGRADE],
})


def test_fleet_baseline_flags_the_outlier(tmp_path):
    s = svc(tmp_path, BASELINE)
    for i in range(3):  # three repos on the de-facto standard
        s.ingest("acme", report(f"gh/std{i}", [("npm", "react", "18.2.0", None, False)]))
    # the fourth jumped ahead -> ingested last, so its verdict sees the full baseline
    v = s.ingest("acme", report("gh/outlier", [("npm", "react", "19.0.0", None, False)]))
    assert any(x.kind == UNSANCTIONED_UPGRADE and x.source == "baseline" for x in v.violations)
    assert v.status == BLOCK


def test_baseline_suppressed_without_support(tmp_path):
    s = svc(tmp_path, BASELINE)
    # a split fleet (2 vs 2) never clears min_repos=3 -> no baseline, no verdict
    s.ingest("acme", report("gh/a", [("npm", "react", "18.2.0", None, False)]))
    s.ingest("acme", report("gh/b", [("npm", "react", "18.2.0", None, False)]))
    s.ingest("acme", report("gh/c", [("npm", "react", "19.0.0", None, False)]))
    v = s.ingest("acme", report("gh/d", [("npm", "react", "19.0.0", None, False)]))
    assert not any(x.kind in (UNSANCTIONED_UPGRADE, BEHIND) for x in v.violations)


def test_baseline_tie_produces_no_standard(tmp_path):
    # A 3-vs-3 split has no majority -> no baseline, so NEITHER half is flagged as an outlier
    # of a coin flip. (Regression: min_share=0.5 used to bless one side by hash order.)
    s = svc(tmp_path, BASELINE)
    for i in range(3):
        s.ingest("acme", report(f"gh/x{i}", [("npm", "react", "18.0.0", None, False)]))
    for i in range(3):
        v = s.ingest("acme", report(f"gh/y{i}", [("npm", "react", "19.0.0", None, False)]))
    assert not any(x.kind in (UNSANCTIONED_UPGRADE, BEHIND) for x in v.violations)


def test_reevaluate_reflects_a_shifted_baseline(tmp_path):
    s = svc(tmp_path, BASELINE)
    for i in range(3):
        s.ingest("acme", report(f"gh/std{i}", [("npm", "react", "18.2.0", None, False)]))
    # std0 was ingested when the baseline hadn't formed; re-eval now that it has
    v = s.reevaluate("acme", "gh/std0")
    assert v.status == PASS and not any(x.kind == UNSANCTIONED_UPGRADE for x in v.violations)


# --------------------------------------------------------------- queries + API

def test_uses_is_the_blast_radius_primitive(tmp_path):
    s = svc(tmp_path, CURATED)
    s.ingest("acme", report("gh/a", [("pypi", "django", "4.2.0", None, False)]))
    s.ingest("acme", report("gh/b", [("pypi", "django", "5.0.0", None, False)]))
    s.ingest("acme", report("gh/c", [("npm", "react", "18.2.0", None, False)]))
    uses = dict(s.uses("acme", "pypi:django"))
    assert uses == {"gh/a": "4.2.0", "gh/b": "5.0.0"}


def test_dashboard_renders_repos_and_verdicts(tmp_path):
    from driftscope.fleet.ui import render_dashboard
    service = svc(tmp_path, CURATED)
    service.ingest("acme", report("gh/app", [("pypi", "django", "5.0.0", None, False)]))
    markup = render_dashboard(service, "acme")
    assert "gh/app" in markup and "BLOCK" in markup and "unsanctioned-upgrade" in markup


def test_dashboard_served_at_fleet_prefix(tmp_path):
    # The gateway routes the /fleet prefix to the pod WITHOUT rewriting, so a browser hitting
    # https://host/fleet/ lands as GET /fleet/ -- which must serve the dashboard, not 404.
    service = svc(tmp_path, CURATED)
    service.ingest("default", report("gh/app", [("pypi", "django", "5.0.0", None, False)]))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))  # default_tenant="default"
    threading.Thread(target=httpd.handle_request).start()
    port = httpd.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/fleet/", timeout=5) as r:
        assert r.status == 200 and "gh/app" in r.read().decode()


def test_healthz_needs_no_tenant(tmp_path):
    service = svc(tmp_path, CURATED)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.handle_request).start()
    port = httpd.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
        assert json.loads(r.read())["status"] == "ok"


def test_http_roundtrip_ingest_then_verdict(tmp_path):
    service = svc(tmp_path, CURATED)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.handle_request).start()  # serve the POST
    port = httpd.server_address[1]
    body = json.dumps(report("gh/app", [("pypi", "django", "5.0.0", None, False)])).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/fleet/reports?tenant=acme",
                                 data=body, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        posted = json.loads(r.read())
    assert posted["status"] == BLOCK

    # a second one-shot server for the GET
    httpd2 = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd2.handle_request).start()
    port2 = httpd2.server_address[1]
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port2}/fleet/verdict?tenant=acme&repo=gh/app", timeout=5
    ) as r:
        got = json.loads(r.read())
    assert got["repo"] == "gh/app" and got["status"] == BLOCK

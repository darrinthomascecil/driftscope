"""Fleet rollup: cross-repo estate summary over ingested inventory. Pure, no network."""

from __future__ import annotations

import json

from driftscope.fleet import rollup
from driftscope.fleet.store import project


def comp(name, current, eco="pypi", deprecated=False):
    return {
        "component": {"ecosystem": eco, "name": name, "current": current, "source": "a"},
        "resolution": {"deprecated": deprecated},
        "severity": "medium",
    }


def report(remote, ref, findings):
    return {"source": {"remote": remote, "ref": ref}, "findings": findings,
            "generated_at": "2026-07-01T00:00:00Z"}


def _rollup(reports):
    return rollup.rollup([project(r, tenant="t") for r in reports])


def _comp(r, name):
    return next(c for c in r.components if c.name.lower() == name.lower())


# ---- reuse: it projects through the fleet store, not a parallel model ----


def test_uses_fleet_store_projection():
    rec = project(report("github.com/a/x", "main", [comp("requests", "2.31.0")]), tenant="t")
    # store.project produced fleet InventoryRecord/ComponentUse -- rollup consumes those.
    assert rec.repo == "github.com/a/x"
    assert rec.components[0].name == "requests" and rec.components[0].version == "2.31.0"


# ---- adoption ----


def test_adoption_counts_distinct_repos():
    r = _rollup([
        report("github.com/a/web", "main", [comp("requests", "2.28.0")]),
        report("github.com/a/api", "main", [comp("requests", "2.31.0")]),
        report("github.com/a/web", "dev", [comp("requests", "2.28.0")]),
    ])
    p = _comp(r, "requests")
    assert p.repo_count == 2  # web + api; two branches of web collapse to one repo
    assert len(p.uses) == 3


# ---- fragmentation ----


def test_fragmentation_groups_repos_by_version():
    r = _rollup([
        report("github.com/a/web", "main", [comp("django", "4.2")]),
        report("github.com/a/api", "develop", [comp("django", "5.0")]),
        report("github.com/a/worker", "main", [comp("django", "4.2")]),
    ])
    p = _comp(r, "django")
    assert p.fragmented
    assert p.unique_versions == ["5.0", "4.2"]  # high-to-low
    by = p.by_version
    assert {u.where for u in by["4.2"]} == {"github.com/a/web@main", "github.com/a/worker@main"}
    assert {u.where for u in by["5.0"]} == {"github.com/a/api@develop"}


def test_single_version_not_fragmented():
    r = _rollup([
        report("github.com/a/x", "main", [comp("pytz", "2024.1")]),
        report("github.com/a/y", "main", [comp("pytz", "2024.1")]),
    ])
    assert not _comp(r, "pytz").fragmented


# ---- api versions + deprecation carried from the scan ----


def test_api_versions_and_deprecation():
    r = _rollup([
        report("github.com/a/x", "main", [comp("net/Ingress", "v1", eco="k8s-api")]),
        report("github.com/a/y", "main", [comp("net/Ingress", "v1beta1", eco="k8s-api", deprecated=True)]),
    ])
    assert len(r.apis) == 1
    api = _comp(r, "net/Ingress")
    assert api.is_api and api.any_deprecated
    assert set(api.unique_versions) == {"v1", "v1beta1"}


# ---- io: skips reports with no repo identity (fail-closed) ----


def test_records_from_dir_skips_no_identity_and_junk(tmp_path):
    (tmp_path / "ok.json").write_text(json.dumps(report("github.com/a/x", "main", [comp("rich", "13.0")])))
    (tmp_path / "noid.json").write_text(json.dumps({"findings": [comp("flask", "3.0")]}))  # no source.remote
    (tmp_path / "bad.json").write_text("{ not json")
    (tmp_path / "notreport.json").write_text(json.dumps({"hello": "world"}))
    records, skipped = rollup.records_from_dir(tmp_path)
    assert len(records) == 1
    assert skipped == 3


def test_json_and_markdown_render():
    r = _rollup([
        report("github.com/a/x", "main", [comp("requests", "2.28.0")]),
        report("github.com/a/y", "main", [comp("requests", "2.31.0")]),
    ])
    doc = json.loads(rollup.to_json(r))
    assert doc["repo_count"] == 2
    c = next(x for x in doc["components"] if x["name"] == "requests")
    assert c["repo_count"] == 2 and c["fragmented"] is True
    md = rollup.to_markdown(r)
    assert "Package adoption" in md and "Version fragmentation" in md and "API versions in use" in md

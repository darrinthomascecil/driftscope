"""Estate history: storage and diffing. Pure, no network."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from driftscope import history


def finding(name, sev, current, latest="9.9.9", eco="nuget"):
    return {
        "component": {"ecosystem": eco, "name": name, "current": current, "source": "a.csproj"},
        "resolution": {"latest_stable": latest},
        "lag": {},
        "severity": sev,
    }


def report(findings, *, root="/repo", generated="2026-07-01T00:00:00Z"):
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {
        "schema": "driftscope/1",
        "root": root,
        "generated_at": generated,
        "summary": counts,
        "findings": findings,
    }


# ------------------------------------------------------------------ storage


def test_save_and_list_runs(tmp_path):
    now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    p1 = history.save(json.dumps(report([])), tmp_path, now=now)
    assert p1.name == "20260701T120000Z.json"
    runs = history.list_runs(tmp_path)
    assert runs == [p1]


def test_same_second_does_not_clobber(tmp_path):
    now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    a = history.save(json.dumps(report([])), tmp_path, now=now)
    b = history.save(json.dumps(report([])), tmp_path, now=now)
    assert a != b
    assert len(history.list_runs(tmp_path)) == 2


def test_list_runs_on_missing_directory(tmp_path):
    assert history.list_runs(tmp_path / "nope") == []


def test_previous_run_excludes_the_current_one(tmp_path):
    t1 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 2, tzinfo=timezone.utc)
    a = history.save(json.dumps(report([])), tmp_path, now=t1)
    b = history.save(json.dumps(report([])), tmp_path, now=t2)
    assert history.previous_run(tmp_path, before=b) == a
    assert history.previous_run(tmp_path) == b


def test_load_bad_json_returns_none(tmp_path):
    p = tmp_path / "20260701T000000Z.json"
    p.write_text("not json")
    assert history.load(p) is None


# --------------------------------------------------------------------- diff


def test_improved_when_severity_drops():
    old = report([finding("Pkg", "critical", "1.0.0")])
    new = report([finding("Pkg", "low", "3.0.0")])
    d = history.diff(old, new)
    assert len(d.improved) == 1
    assert d.improved[0].from_severity == "critical"
    assert d.improved[0].to_severity == "low"
    assert d.improved[0].moved_version is True


def test_worsened_when_severity_rises():
    old = report([finding("Pkg", "low", "1.0.0")])
    new = report([finding("Pkg", "critical", "1.0.0")])
    d = history.diff(old, new)
    assert len(d.worsened) == 1 and not d.improved


def test_version_bump_at_same_severity_counts_as_improved():
    """"We bumped it and it is still behind" differs from "nobody touched it"."""
    old = report([finding("Pkg", "high", "1.0.0")])
    new = report([finding("Pkg", "high", "2.0.0")])
    d = history.diff(old, new)
    assert len(d.improved) == 1
    assert d.unchanged == 0


def test_untouched_component_is_unchanged():
    old = report([finding("Pkg", "high", "1.0.0")])
    new = report([finding("Pkg", "high", "1.0.0")])
    d = history.diff(old, new)
    assert d.unchanged == 1 and d.is_empty


def test_appeared_and_resolved():
    old = report([finding("Gone", "high", "1.0.0")])
    new = report([finding("Fresh", "medium", "2.0.0")])
    d = history.diff(old, new)
    assert [c.name for c in d.resolved] == ["Gone"]
    assert [c.name for c in d.appeared] == ["Fresh"]


def test_matching_is_by_ecosystem_and_name_not_location():
    """Moving a dependency between files is not a change in drift."""
    old = report([finding("Pkg", "high", "1.0.0")])
    new = report([finding("Pkg", "high", "1.0.0")])
    new["findings"][0]["component"]["source"] = "other/Project.csproj"
    d = history.diff(old, new)
    assert d.unchanged == 1 and d.is_empty


def test_same_name_different_ecosystem_is_not_matched():
    old = report([finding("redis", "high", "1.0.0", eco="helm-chart")])
    new = report([finding("redis", "high", "1.0.0", eco="docker-image")])
    d = history.diff(old, new)
    assert len(d.resolved) == 1 and len(d.appeared) == 1


def test_name_matching_is_case_insensitive():
    old = report([finding("Newtonsoft.Json", "high", "12.0.3")])
    new = report([finding("newtonsoft.json", "low", "13.0.4")])
    d = history.diff(old, new)
    assert len(d.improved) == 1


def test_duplicate_names_keep_the_worst():
    old = report([finding("Pkg", "low", "1.0.0"), finding("Pkg", "critical", "1.0.0")])
    new = report([finding("Pkg", "low", "1.0.0")])
    d = history.diff(old, new)
    assert len(d.improved) == 1
    assert d.improved[0].from_severity == "critical"


# ------------------------------------------------------------- net movement


def test_net_counts_only_actionable_severities():
    old = report([finding("A", "critical", "1"), finding("B", "low", "1"),
                  finding("C", "current", "1")])
    new = report([finding("A", "low", "2"), finding("B", "low", "1"),
                  finding("C", "current", "1")])
    d = history.diff(old, new)
    assert d.net == -1  # one critical cleared; low/current never counted
    assert d.direction == "improving"


def test_falling_behind_direction():
    old = report([finding("A", "low", "1")])
    new = report([finding("A", "critical", "1")])
    assert history.diff(old, new).direction == "falling behind"


def test_flat_direction():
    old = report([finding("A", "high", "1")])
    new = report([finding("A", "high", "1")])
    assert history.diff(old, new).direction == "flat"


def test_different_roots_are_flagged():
    old = report([], root="/repo-a")
    new = report([], root="/repo-b")
    d = history.diff(old, new)
    assert d.note and "different scan roots" in d.note


def test_diff_latest_returns_none_without_history(tmp_path):
    cur = report([finding("A", "high", "1")])
    saved = history.save(json.dumps(cur), tmp_path)
    assert history.diff_latest(tmp_path, cur, current_path=saved) is None


def test_diff_latest_compares_against_previous(tmp_path):
    t1 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 2, tzinfo=timezone.utc)
    history.save(json.dumps(report([finding("A", "critical", "1")])), tmp_path, now=t1)
    cur = report([finding("A", "low", "2")])
    saved = history.save(json.dumps(cur), tmp_path, now=t2)
    d = history.diff_latest(tmp_path, cur, current_path=saved)
    assert d is not None and len(d.improved) == 1 and d.net == -1


# ------------------------------------------------------------------ render


def test_text_report_shows_direction_and_changes():
    old = report([finding("A", "critical", "1.0.0")])
    new = report([finding("A", "low", "2.0.0")])
    txt = history.to_text(history.diff(old, new, from_name="a.json", to_name="b.json"))
    assert "a.json -> b.json" in txt
    assert "improving" in txt
    assert "improved (1)" in txt
    assert "1.0.0 -> 2.0.0" in txt


def test_html_trend_block_renders():
    from driftscope.models import Report
    from driftscope.report import html as H

    old = report([finding("A", "critical", "1.0.0"), finding("B", "high", "1.0.0")])
    new = report([finding("A", "low", "2.0.0")])
    r = Report(root="/repo")
    r.trend = history.diff(old, new, from_name="a.json", to_name="b.json")
    out = H.to_html(r)
    assert "Since the last run" in out
    assert "improving" in out
    assert "resolved" in out
    assert "matched by ecosystem and name" in out


def test_html_omits_trend_when_absent():
    from driftscope.models import Report
    from driftscope.report import html as H

    assert "Since the last run" not in H.to_html(Report(root="/repo"))


# ------------------------------------------------- declaration-site links


def _report_from_remote():
    from driftscope.models import Component, Finding, Lag, Report, Resolution

    r = Report(root="https://github.com/o/repo")
    r.source = {"remote": "https://github.com/o/repo", "ref": "main",
                "commit": "abc123def456789"}
    r.findings = [
        Finding(
            component=Component(ecosystem="nuget", name="Pkg", current="1.0.0",
                                source="src/App.csproj", line=12,
                                meta={"also_at": ["other/Two.csproj:7"]}),
            resolution=Resolution(latest_stable="2.0.0"),
            lag=Lag(behind_major=1, comparable=True),
            severity="high",
        )
    ]
    return r


def test_declaration_site_links_to_the_scanned_commit():
    from driftscope.report import html as H

    out = H.to_html(_report_from_remote())
    # Permalinked to the commit, not the branch, so the line stays correct.
    assert "https://github.com/o/repo/blob/abc123def456789/src/App.csproj#L12" in out
    assert "target='_blank'" in out


def test_also_at_sites_are_linked_too():
    from driftscope.report import html as H

    out = H.to_html(_report_from_remote())
    assert "blob/abc123def456789/other/Two.csproj#L7" in out


def test_local_scan_has_plain_text_paths():
    from driftscope.models import Component, Finding, Lag, Report, Resolution
    from driftscope.report import html as H

    r = Report(root="/local/repo")  # no .source -> no remote to link to
    r.findings = [
        Finding(
            component=Component(ecosystem="pypi", name="x", current="1", source="a.txt", line=3),
            resolution=Resolution(latest_stable="2"),
            lag=Lag(comparable=True), severity="high",
        )
    ]
    out = H.to_html(r)
    assert "a.txt:3" in out
    assert "blob/" not in out


def test_azure_live_findings_are_not_linked():
    from driftscope.models import Component, Finding, Lag, Report, Resolution
    from driftscope.report import html as H

    r = _report_from_remote()
    r.findings[0].component.source = "azure:///subscriptions/x/rg/y"
    r.findings[0].component.line = None
    r.findings[0].component.meta = {}
    out = H.to_html(r)
    assert "blob/abc123def456789/azure" not in out


def test_non_github_remote_is_not_linked():
    from driftscope.report import html as H

    r = _report_from_remote()
    r.source["remote"] = "https://gitlab.com/o/repo"
    out = H.to_html(r)
    assert "blob/" not in out

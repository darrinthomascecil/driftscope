"""Engine and reporter tests.

Resolution is stubbed so these are deterministic and offline: the point is to test
orchestration, report shape and exit codes, not the registries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftscope import engine
from driftscope.http import Cache, Client, GitHub
from driftscope.models import (
    PYPI,
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_MEDIUM,
    Release,
    Report,
    Resolution,
    severity_rank,
)
from driftscope.report import render
from driftscope.resolvers import Context

FIXTURE = Path(__file__).parent.parent / "fixtures" / "stale-estate"

# name -> (latest, [all versions])
FAKE = {
    "requests": ("2.32.0", ["2.20.0", "2.25.0", "2.32.0"]),
    "Django": ("5.0.0", ["2.2.28", "3.2.0", "4.2.0", "5.0.0"]),
    "flask": ("3.0.0", ["1.0.2", "2.0.0", "3.0.0"]),
}


@pytest.fixture
def ctx(tmp_path):
    cache = Cache(root=tmp_path / "cache")
    return Context(
        client=Client(cache=cache, offline=True),
        github=GitHub(cache=cache, offline=True, enabled=False),
        allow_az=False,
    )


@pytest.fixture
def stub_resolve(monkeypatch):
    def fake(component, ctx):
        entry = FAKE.get(component.name)
        if entry is None:
            return Resolution(error="not in stub table")
        latest, versions = entry
        return Resolution(
            latest_stable=latest,
            releases=[Release(version=v) for v in versions],
            repo="example/repo",
        )

    monkeypatch.setattr(engine, "resolve", fake)
    return fake


def test_scan_produces_findings(ctx, stub_resolve):
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={PYPI}, workers=2)
    assert report.stats.components_found > 0
    names = {f.component.name for f in report.findings}
    assert {"requests", "Django", "flask"} <= names


def test_severity_ordering_is_worst_first(ctx, stub_resolve):
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={PYPI}, workers=2)
    ranks = [severity_rank(f.severity) for f in report.sorted_findings()]
    assert ranks == sorted(ranks)


def test_django_is_critical_three_majors_behind(ctx, stub_resolve):
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={PYPI}, workers=2)
    dj = next(f for f in report.findings if f.component.name == "Django")
    assert dj.severity == SEV_CRITICAL
    assert dj.lag.behind_major == 3
    assert dj.advice.intermediate_versions == ["3.2.0", "4.2.0"]


def test_github_disabled_means_no_delta(ctx, stub_resolve):
    report = engine.run_scan(
        FIXTURE, ctx=ctx, ecosystems={PYPI}, workers=2, delta_threshold=SEV_CRITICAL
    )
    assert all(f.delta is None for f in report.findings)
    assert report.stats.github_queries == 0


def test_offline_never_makes_requests(ctx, stub_resolve):
    engine.run_scan(FIXTURE, ctx=ctx, ecosystems={PYPI}, workers=2)
    assert ctx.client.requests_made == 0


def test_resolver_exception_does_not_abort_scan(ctx, monkeypatch):
    def explode(component, ctx):
        raise RuntimeError("registry on fire")

    # resolve() catches inside itself; verify via the real wrapper.
    from driftscope import resolvers

    monkeypatch.setattr(resolvers, "resolve", explode)
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={PYPI}, workers=2)
    assert report.stats.components_found > 0  # scan still completed


# --------------------------------------------------------------- exit codes


def _report_with(sev):
    from driftscope.models import Component, Finding, Lag

    r = Report(root=".")
    r.findings = [
        Finding(
            component=Component(ecosystem=PYPI, name="x", current="1", source="f"),
            resolution=Resolution(latest_stable="2"),
            lag=Lag(),
            severity=sev,
        )
    ]
    return r


def test_exit_code_zero_without_fail_on():
    assert engine.exit_code_for(_report_with(SEV_CRITICAL), None) == 0


def test_exit_code_trips_at_threshold():
    assert engine.exit_code_for(_report_with(SEV_CRITICAL), SEV_HIGH) == 1
    assert engine.exit_code_for(_report_with(SEV_MEDIUM), SEV_HIGH) == 0
    assert engine.exit_code_for(_report_with(SEV_HIGH), SEV_HIGH) == 1


# ----------------------------------------------------------------- reports


def test_json_is_valid_and_has_schema(ctx, stub_resolve):
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={PYPI}, workers=2)
    payload = json.loads(render.to_json(report))
    assert payload["schema"] == render.SCHEMA_VERSION
    assert "summary" in payload and "findings" in payload
    first = payload["findings"][0]
    assert {"component", "resolution", "lag", "severity", "advice"} <= set(first)
    # Release lists are dropped by default to keep the payload small.
    assert "releases" not in first["resolution"]
    assert "release_count" in first["resolution"]


def test_json_can_include_releases(ctx, stub_resolve):
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={PYPI}, workers=2)
    payload = json.loads(render.to_json(report, include_releases=True))
    assert "releases" in payload["findings"][0]["resolution"]


def test_markdown_contains_key_sections(ctx, stub_resolve):
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={PYPI}, workers=2)
    md = render.to_markdown(report)
    for heading in ("# Version drift report", "## Summary", "## Drift table", "## Upgrade guidance"):
        assert heading in md
    assert "Django" in md
    # The caveat about issue-window semantics must always ship with the report.
    assert "not proof of a regression" in md


def test_text_report_is_single_line_per_finding(ctx, stub_resolve):
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={PYPI}, workers=2)
    txt = render.to_text(report)
    assert "components in" in txt.splitlines()[0]
    assert any("Django" in line for line in txt.splitlines())


def test_empty_scan_renders_without_error(ctx, tmp_path):
    report = engine.run_scan(tmp_path, ctx=ctx, workers=1)
    assert report.findings == []
    assert render.to_markdown(report)
    assert json.loads(render.to_json(report))["findings"] == []


# ------------------------------------------------------------------- cache


def test_cache_roundtrip_and_ttl(tmp_path):
    c = Cache(root=tmp_path / "c", ttl=1000)
    assert c.get("k") is None
    c.put("k", {"v": 1})
    assert c.get("k") == {"v": 1}
    assert c.hits == 1
    expired = Cache(root=tmp_path / "c", ttl=-1)
    assert expired.get("k") is None            # TTL respected
    assert expired.get("k", ignore_ttl=True) == {"v": 1}  # offline fallback works


def test_cache_disabled_is_noop(tmp_path):
    c = Cache(root=tmp_path / "c", enabled=False)
    c.put("k", 1)
    assert c.get("k") is None


def test_offline_client_raises_when_uncached(tmp_path):
    from driftscope.http import HttpError

    client = Client(cache=Cache(root=tmp_path / "c"), offline=True)
    with pytest.raises(HttpError, match="offline"):
        client.get_json("https://example.invalid/x")


# ------------------------------------------------------- calver presentation


def test_calver_lag_reads_as_years_not_majors():
    """Azure API versions are dates: "3 major" would be nonsense."""
    from driftscope.models import AZURE_API, Component, Finding, Lag

    f = Finding(
        component=Component(
            ecosystem=AZURE_API, name="Microsoft.KeyVault/vaults",
            current="2023-07-01", source="f",
        ),
        resolution=Resolution(latest_stable="2026-02-01"),
        lag=Lag(behind_major=3, stable_releases_behind=1, comparable=True),
        severity=SEV_CRITICAL,
    )
    phrase = render._lag_phrase(f)
    assert "3 years" in phrase
    assert "major" not in phrase
    assert "1 api-version" in phrase and "1 api-versions" not in phrase


def test_semver_lag_still_reads_as_majors():
    from driftscope.models import Component, Finding, Lag

    f = Finding(
        component=Component(ecosystem=PYPI, name="x", current="1.0.0", source="f"),
        resolution=Resolution(latest_stable="3.0.0"),
        lag=Lag(behind_major=2, stable_releases_behind=1, comparable=True),
        severity=SEV_CRITICAL,
    )
    phrase = render._lag_phrase(f)
    assert "2 major" in phrase and "1 release" in phrase
    assert "years" not in phrase


def test_version_age_is_surfaced_not_dropped():
    """Regression: current_age_days was computed by the engine and never rendered."""
    from driftscope.models import Component, Finding, Lag

    f = Finding(
        component=Component(ecosystem=PYPI, name="express", current="4.16.0", source="package.json"),
        resolution=Resolution(latest_stable="5.2.1"),
        lag=Lag(behind_major=1, days_behind=2986, current_age_days=3223, comparable=True),
        severity=SEV_HIGH,
    )
    r = Report(root=".")
    r.findings = [f]
    md = render.to_markdown(r)
    assert "Age of your version" in md
    assert "8.8 years" in md
    assert "3223 days" in md


def test_young_version_age_reads_in_days():
    from driftscope.models import Component, Finding, Lag

    f = Finding(
        component=Component(ecosystem=PYPI, name="x", current="1.0.0", source="f"),
        resolution=Resolution(latest_stable="1.1.0"),
        lag=Lag(behind_minor=1, current_age_days=40, comparable=True),
        severity=SEV_MEDIUM,
    )
    r = Report(root=".")
    r.findings = [f]
    assert "40 days" in render.to_markdown(r)


# ----------------------------------------------- age beyond registry window


def test_age_is_a_lower_bound_when_pin_predates_the_window(ctx, monkeypatch):
    """Artifact Hub truncates old versions, so the stalest pin has no date.

    The oldest listed release bounds it: the pin must be at least that old.
    """
    from datetime import datetime, timezone
    from driftscope.models import HELM

    oldest = datetime(2022, 6, 3, tzinfo=timezone.utc)

    def fake(component, ctx):
        return Resolution(
            latest_stable="27.0.0",
            releases=[
                Release(version="27.0.0", published=datetime(2026, 7, 1, tzinfo=timezone.utc)),
                Release(version="16.11.2", published=oldest),
            ],
        )

    monkeypatch.setattr(engine, "resolve", fake)
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={HELM}, workers=1)
    dep = next(f for f in report.findings if f.component.current == "16.8.0")
    assert dep.lag.age_lower_bound is True
    assert dep.lag.current_age_days and dep.lag.current_age_days > 1400
    md = render.to_markdown(report)
    assert "at least" in md and "predates the registry's version window" in md


def test_no_lower_bound_when_pin_is_newer_than_oldest_listed(ctx, monkeypatch):
    """A pin inside the window must not be mislabelled as a bound."""
    from datetime import datetime, timezone
    from driftscope.models import HELM

    def fake(component, ctx):
        return Resolution(
            latest_stable="27.0.0",
            releases=[
                Release(version="27.0.0", published=datetime(2026, 7, 1, tzinfo=timezone.utc)),
                Release(version="16.8.0", published=datetime(2022, 2, 1, tzinfo=timezone.utc)),
                Release(version="1.0.0", published=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            ],
        )

    monkeypatch.setattr(engine, "resolve", fake)
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={HELM}, workers=1)
    dep = next(f for f in report.findings if f.component.current == "16.8.0")
    assert dep.lag.age_lower_bound is False


def test_lower_bound_suppressed_for_truncated_listings(ctx, monkeypatch):
    """A recency-ordered truncated listing has no meaningful 'oldest' release."""
    from datetime import datetime, timezone
    from driftscope.models import DOCKER

    def fake(component, ctx):
        return Resolution(
            latest_stable="26.5-alpine",
            releases=[
                Release(version="26.5-alpine",
                        published=datetime(2026, 7, 15, tzinfo=timezone.utc)),
                Release(version="24.1-alpine",
                        published=datetime(2026, 6, 23, tzinfo=timezone.utc)),
            ],
            extra={"list_truncated": True},
        )

    monkeypatch.setattr(engine, "resolve", fake)
    report = engine.run_scan(FIXTURE, ctx=ctx, ecosystems={DOCKER}, workers=1)
    node = next(f for f in report.findings if f.component.current == "14-alpine")
    assert node.lag.age_lower_bound is False
    assert node.lag.days_behind is None, "must not invent a 13-day gap"


# ------------------------------------------------------------ scan targets


@pytest.mark.parametrize(
    "target,remote",
    [
        ("https://github.com/o/r", True),
        ("http://gitlab.com/o/r", True),
        ("git@github.com:o/r.git", True),
        ("ssh://git@host/o/r", True),
        ("owner/repo", True),          # shorthand, no such local path
        (".", False),
        ("/tmp", False),
        ("./infra", False),
    ],
)
def test_remote_target_detection(target, remote):
    from driftscope.source import looks_remote

    assert looks_remote(target) is remote


def test_local_path_wins_over_shorthand(tmp_path, monkeypatch):
    """A real directory literally named "foo/bar" must not be treated as a repo."""
    from driftscope.source import looks_remote

    (tmp_path / "foo" / "bar").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert looks_remote("foo/bar") is False


def test_shorthand_normalizes_to_github():
    from driftscope.source import normalize_remote

    assert normalize_remote("owner/repo") == "https://github.com/owner/repo"
    assert normalize_remote("https://gitlab.com/o/r") == "https://gitlab.com/o/r"


def test_missing_local_path_raises_clone_error():
    from driftscope.source import CloneError, open_source

    with pytest.raises(CloneError, match="no such path"):
        with open_source("/nonexistent/path/xyz"):
            pass


def test_local_source_is_not_marked_cloned(tmp_path):
    from driftscope.source import open_source

    with open_source(str(tmp_path)) as src:
        assert src.cloned is False
        assert src.remote is None
        assert src.path == tmp_path.resolve()


def test_report_attributes_remote_source():
    """The report must name the repo, not the throwaway temp directory."""
    r = Report(root="https://github.com/o/r")
    r.source = {"remote": "https://github.com/o/r", "ref": "main", "commit": "abc123def456"}
    md = render.to_markdown(r)
    assert "https://github.com/o/r" in md
    assert "abc123def456" in md
    assert "main" in md
    payload = json.loads(render.to_json(r))
    assert payload["source"]["commit"] == "abc123def456"


# ------------------------------------------------------------------- help text


def test_help_text_mentions_remote_targets():
    """The help must not describe scan as directory-only -- it takes URLs too."""
    from driftscope.cli import build_parser

    p = build_parser()
    top = p.format_help()
    assert "remote git repository" in top
    assert "https://github.com/org/repo" in top

    scan = [a for a in p._subparsers._group_actions[0].choices.items() if a[0] == "scan"][0][1]
    body = scan.format_help()
    assert "git URL" in body
    assert "owner/repo shorthand" in body
    assert "--ref" in body and "--keep-clone" in body
    # Exit-code contract is part of the CI story and must be documented.
    assert "Exit codes" in body


def test_documented_exit_codes_match_behaviour():
    from driftscope.cli import main

    # 2 for a bad target
    assert main(["scan", "/nonexistent/xyz-driftscope", "--quiet", "--offline"]) == 2


# ------------------------------------------- markdown parity with the HTML


def _rich_report():
    """A report carrying trend, vulns and velocity, for reporter parity checks."""
    from datetime import date, datetime, timezone

    from driftscope import history
    from driftscope.models import (
        Component, Finding, Lag, Velocity, Window, Report as R, Resolution,
    )
    from driftscope.vuln import Vuln, VulnReport

    f = Finding(
        component=Component(ecosystem=PYPI, name="django", current="2.2.28",
                            source="requirements.txt", line=3),
        resolution=Resolution(latest_stable="5.0.0", repo="django/django"),
        lag=Lag(behind_major=3, comparable=True, current_age_days=1500),
        severity=SEV_CRITICAL,
    )
    f.advice = __import__("driftscope.advisor", fromlist=["x"]).advise(
        f.component, f.resolution, f.lag, f.severity
    )
    f.vulns = VulnReport(
        ecosystem=PYPI, package="django", current_version="2.2.28",
        target_version="5.0.0", fixed_count=2, remaining_count=1,
        fixed_by_upgrade=[
            Vuln(id="GHSA-a", aliases=["CVE-2024-0001"], summary="sql injection",
                 severity="CRITICAL", url="https://osv.dev/vulnerability/GHSA-a"),
            Vuln(id="GHSA-b", aliases=["CVE-2024-0002"], summary="xss",
                 severity="HIGH", url="https://osv.dev/vulnerability/GHSA-b"),
        ],
        remaining_after_upgrade=[
            Vuln(id="GHSA-c", aliases=["CVE-2024-0003"], summary="dos",
                 severity="MODERATE", url="https://osv.dev/vulnerability/GHSA-c"),
        ],
    )
    v = Velocity(repo="django/django", window_days=90,
                 issue_trend="improving", sentiment_trend="steady",
                 release_trend="deteriorating", verdict="deteriorating",
                 backlog_change_abs=-12.0, frustration_change_pts=1.0,
                 release_change_pct=-100.0)
    for i, (op, cl) in enumerate([(20, 10), (18, 12), (15, 14), (10, 22)]):
        v.windows.append(Window(start=date(2025, 8, 1), end=date(2025, 10, 29),
                                opened=op, closed=cl, sampled_issues=op,
                                positive=10, negative=1, releases=(1 if i < 3 else 0)))
    f.velocity = v

    r = R(root="/repo")
    r.findings = [f]
    old = {"root": "/repo", "summary": {"critical": 2},
           "findings": [
               {"component": {"ecosystem": PYPI, "name": "django", "current": "2.2.0"},
                "resolution": {"latest_stable": "5.0.0"}, "severity": "critical"},
               {"component": {"ecosystem": PYPI, "name": "gone", "current": "1.0"},
                "resolution": {}, "severity": "critical"},
           ]}
    new = {"root": "/repo", "summary": {"critical": 1},
           "findings": [
               {"component": {"ecosystem": PYPI, "name": "django", "current": "2.2.28"},
                "resolution": {"latest_stable": "5.0.0"}, "severity": "critical"},
           ]}
    r.trend = history.diff(old, new, from_name="a.json", to_name="b.json")
    return r


def test_markdown_renders_the_trend_section():
    md = render.to_markdown(_rich_report())
    assert "## Since the last run" in md
    assert "actionable findings" in md
    assert "`a.json` -> `b.json`" in md
    assert "matched by ecosystem and name" in md


def test_markdown_renders_vulnerabilities_and_what_upgrading_fixes():
    md = render.to_markdown(_rich_report())
    assert "**Known vulnerabilities**" in md
    assert "Fixed by upgrading to 5.0.0: 2" in md
    assert "Still present after upgrade: 1" in md
    assert "CVE-2024-0001" in md
    assert "https://osv.dev/vulnerability/GHSA-a" in md


def test_markdown_renders_velocity_with_sparklines():
    md = render.to_markdown(_rich_report())
    assert "**Upstream velocity**" in md
    assert "Issue backlog" in md and "Negative reactions" in md
    assert "Releases shipped" in md
    # Block characters stand in for the HTML sparkline.
    assert any(ch in md for ch in "▁▂▃▄▅▆▇█")
    assert "+1pp" in md, "sentiment must be shown in points, not relative percent"


def test_markdown_carries_the_same_caveats_as_the_html():
    md = render.to_markdown(_rich_report())
    assert "not proof of a regression" in md
    assert "acceleration rather than level" in md
    assert "proxy" in md and "sentiment" in md
    assert "Vulnerability data comes from OSV" in md


def test_markdown_indiscriminate_vulns_do_not_claim_a_split():
    from driftscope.models import Component, Finding, Lag, Report as R, Resolution
    from driftscope.vuln import Vuln, VulnReport

    f = Finding(
        component=Component(ecosystem="docker-image", name="redis", current="5", source="f"),
        resolution=Resolution(latest_stable="8.8.1"),
        lag=Lag(behind_major=3, comparable=True), severity=SEV_CRITICAL,
    )
    f.vulns = VulnReport(
        ecosystem="docker-image", package="redis", current_version="5",
        target_version="8.8.1", approximate=True, indiscriminate=True,
        remaining_count=72,
        remaining_after_upgrade=[Vuln(id="X", aliases=["CVE-2013-0178"], summary="old",
                                      url="u")],
        note="the same set matches both versions; treat as an upper bound",
    )
    r = R(root="/repo")
    r.findings = [f]
    md = render.to_markdown(r)
    assert "72 on record" in md
    assert "Advisories on record: 72" in md
    # No fixed/remaining *listing*; the footer legend may still explain the terms.
    assert "- **Fixed by upgrading" not in md
    assert "- **Still present after upgrade" not in md
    assert "upper bound" in md


def test_markdown_omits_new_sections_when_data_is_absent():
    r = Report(root="/repo")
    md = render.to_markdown(r)
    assert "## Since the last run" not in md
    assert "Known vulnerabilities" not in md
    assert "Upstream velocity" not in md


def test_sparkline_shape():
    assert render._sparkline([]) == ""
    assert len(render._sparkline([1, 2, 3, 4])) == 4
    # A flat series is all the same glyph; a rising one ends higher than it starts.
    flat = render._sparkline([5, 5, 5])
    assert len(set(flat)) == 1
    rising = render._sparkline([1, 10])
    assert rising[0] != rising[-1]

"""A deterministic, deliberately awkward Report for golden-output tests.

Every branch the reporters can take is represented exactly once, so a golden diff
localises to the branch that changed:

  * all six severities, including two `unknown`s with different reasons
  * a normal vuln report (fixed / remaining / introduced, with a truncated detail
    list), an `indiscriminate` one, and an `unmatched` one
  * velocity with a complete window history, an incomplete oldest window, and a
    None frustration in the latest window
  * a delta with breaking notes, bug issues and a resolved window
  * a history diff covering worsened / new / improved / resolved / unchanged, with
    one category over the 12-row display cap
  * a cloned-remote `source`, so GitHub blob permalinks render

Nothing here calls datetime.now(): all dates are fixed literals.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from driftscope.history import Change, Diff
from driftscope.models import (
    AZURE_API,
    DOCKER,
    JVM_TOOLCHAIN,
    K8S_API,
    MAVEN,
    SEV_CRITICAL,
    SEV_CURRENT,
    SEV_HIGH,
    SEV_LOW,
    SEV_MEDIUM,
    SEV_UNKNOWN,
    TERRAFORM_PROVIDER,
    Advice,
    Component,
    Finding,
    GitHubDelta,
    IssueSignal,
    Lag,
    Release,
    Report,
    Resolution,
    ScanStats,
    Velocity,
    Window,
)
from driftscope.vuln import Vuln, VulnReport

UTC = timezone.utc


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


def _vuln(n: int, sev: str, fixed: str = "3.0.0") -> Vuln:
    return Vuln(
        id=f"GHSA-xxxx-{n:04d}",
        aliases=[f"CVE-2024-{1000 + n}"],
        summary=f"Advisory number {n} with a summary long enough to exercise the "
                f"110- and 120-character truncation in both reporters.",
        severity=sev,
        cvss="7.5",
        fixed_in=[fixed],
        url=f"https://osv.dev/vulnerability/GHSA-xxxx-{n:04d}",
    )


def _windows() -> list[Window]:
    return [
        # Oldest window is incomplete: the issue walk did not reach back this far.
        Window(start=date(2025, 1, 1), end=date(2025, 4, 1), opened=40, closed=50,
               sampled_issues=20, positive=30, negative=6, comments=95, releases=6,
               complete=False),
        Window(start=date(2025, 4, 1), end=date(2025, 7, 1), opened=60, closed=45,
               sampled_issues=25, positive=40, negative=15, comments=140, releases=4),
        Window(start=date(2025, 7, 1), end=date(2025, 10, 1), opened=75, closed=40,
               sampled_issues=30, positive=35, negative=28, comments=210, releases=3),
        # Latest window has no reactions at all -> frustration is None, which the
        # two reporters render differently and must keep rendering differently.
        Window(start=date(2025, 10, 1), end=date(2026, 1, 1), opened=90, closed=30,
               sampled_issues=35, positive=0, negative=0, comments=260, releases=1),
    ]


def _releases(major_range, per_major=12) -> list[Release]:
    out = []
    for maj in major_range:
        for mi in range(per_major):
            out.append(Release(version=f"{maj}.{mi}.0",
                               published=_dt(2020 + maj, 1 + (mi % 12), 1)))
    return out


def build_report() -> Report:
    findings: list[Finding] = []

    # ---- CRITICAL: two majors behind, full delta + vulns + velocity
    vulns = VulnReport(
        ecosystem="maven", package="com.fasterxml.jackson.core:jackson-databind",
        current_version="2.15.2", target_version="2.20.0",
        fixed_count=9, remaining_count=2, introduced_count=1,
        fixed_by_upgrade=[_vuln(i, s) for i, s in
                          enumerate(("CRITICAL", "HIGH", "MODERATE", "LOW", "", "HIGH"), 1)],
        remaining_after_upgrade=[_vuln(20, "HIGH"), _vuln(21, "MODERATE")],
        introduced_by_upgrade=[_vuln(30, "LOW")],
    )
    findings.append(Finding(
        component=Component(
            ecosystem=MAVEN, name="com.fasterxml.jackson.core:jackson-databind",
            current="2.15.2", source="pom.xml", line=34, raw="2.15.2",
            meta={"also_at": ["service-a/pom.xml", "service-b/pom.xml"]},
        ),
        resolution=Resolution(
            latest_stable="2.20.0", releases=_releases(range(2, 3), 40),
            repo="FasterXML/jackson-databind",
            homepage="https://github.com/FasterXML/jackson-databind",
        ),
        lag=Lag(behind_major=0, behind_minor=5, releases_behind=40,
                stable_releases_behind=38, days_behind=900, current_age_days=1200),
        severity=SEV_CRITICAL,
        advice=Advice(
            risk=SEV_CRITICAL,
            strategy="Staged upgrade. Step through 2.17.x so each minor's "
                     "serialisation changes are isolated and revertible.",
            steps=["Bump the jackson-bom entry, not the individual artifacts",
                   "Run the contract tests before the unit tests",
                   "Change one service per commit so a regression bisects cleanly"],
            cautions=["jackson ships many artifacts on one version line; a partial "
                      "upgrade silently mixes versions",
                      "3 breaking-change note(s) found in release notes"],
            target_version="2.20.0",
            intermediate_versions=["2.17.2"],
        ),
        delta=GitHubDelta(
            repo="FasterXML/jackson-databind", from_ref="2.15.2", to_ref="2.20.0",
            from_date="2023-07-05T00:00:00Z", to_date="2026-01-10T00:00:00Z",
            window_resolved=True, open_bug_count=14, total_bug_count=61,
            bug_issues=[
                IssueSignal(number=4100 + i, title=f"Deserialisation regression #{i}",
                            url=f"https://github.com/FasterXML/jackson-databind/issues/{4100+i}",
                            state="open", labels=["bug"], created_at="2025-03-01T00:00:00Z")
                for i in range(3)
            ],
            breaking_notes=[
                "2.16.0: `ObjectMapper` - the `USE_BIG_DECIMAL_FOR_FLOATS` default has changed",
                "2.17.0: removed support for the legacy `JsonSerializable` interface",
                "2.18.0: minimum required Java version is now 17",
            ],
            releases_in_window=[f"2.{n}.0" for n in range(16, 21)],
            note="issues are filed-in-window signals, not proven regressions; "
                 "breaking-change notes are quoted from release bodies",
        ),
        velocity=Velocity(
            repo="FasterXML/jackson-databind", windows=_windows(), window_days=90,
            issue_trend="deteriorating", sentiment_trend="deteriorating",
            release_trend="deteriorating", verdict="deteriorating",
            backlog_change_abs=42.0, backlog_change_pct=180.0,
            frustration_change_pts=11.0, frustration_change_pct=64.0,
            release_change_pct=-77.0,
            notes=["oldest window excluded from the baseline: the issue walk stopped "
                   "at page 20 before covering it"],
        ),
        vulns=vulns,
    ))

    # ---- CRITICAL: terraform provider, two majors, no velocity
    findings.append(Finding(
        component=Component(ecosystem=TERRAFORM_PROVIDER, name="hashicorp/azurerm",
                            current="2.99.0", source=".terraform.lock.hcl", line=12,
                            raw="~> 2.99", meta={"locked": True}),
        resolution=Resolution(latest_stable="4.81.0", releases=_releases(range(2, 5), 20),
                              repo="hashicorp/terraform-provider-azurerm"),
        lag=Lag(behind_major=2, releases_behind=220, stable_releases_behind=220,
                days_behind=1586, current_age_days=1598),
        severity=SEV_CRITICAL,
        advice=Advice(risk=SEV_CRITICAL,
                      strategy="Staged upgrade. Do not jump 2.99.0 -> 4.81.0 in one change.",
                      steps=['Set version = "~> 4.81.0" in required_providers'],
                      cautions=["Provider major bumps can force resource replacement"],
                      target_version="4.81.0", intermediate_versions=["3.117.1"]),
    ))

    # ---- HIGH: image with an INDISCRIMINATE vuln report (the redis case)
    findings.append(Finding(
        component=Component(ecosystem=DOCKER, name="redis", current="5",
                            source="docker-compose.yml", line=7,
                            meta={"digest": None, "floating": False}),
        resolution=Resolution(latest_stable="8.8.1", repo="redis/redis"),
        lag=Lag(behind_major=3, releases_behind=0, days_behind=2100,
                current_age_days=2400, age_lower_bound=True),
        severity=SEV_HIGH,
        advice=Advice(risk=SEV_HIGH, strategy="Rebuild against the 8.x tag.",
                      steps=["Pin a full version, not a bare major"],
                      target_version="8.8.1"),
        vulns=VulnReport(
            ecosystem="Debian", package="redis", current_version="5",
            target_version="8.8.1", fixed_count=0, remaining_count=72,
            remaining_after_upgrade=[_vuln(40 + i, "HIGH") for i in range(4)],
            approximate=True, indiscriminate=True,
            note="both versions matched the same 72 advisories, so this query did "
                 "not discriminate between them; distro package versions do not "
                 "follow image tags. The fixed/remaining split is withheld.",
        ),
    ))

    # ---- HIGH: image with an UNMATCHED vuln report (the postgres case)
    findings.append(Finding(
        component=Component(ecosystem=DOCKER, name="postgres", current="15",
                            source="docker-compose.yml", line=15),
        resolution=Resolution(latest_stable="18.2", repo="postgres/postgres"),
        lag=Lag(behind_major=3, days_behind=1100, current_age_days=1150),
        severity=SEV_HIGH,
        advice=Advice(risk=SEV_HIGH, strategy="Plan a major-version data migration.",
                      target_version="18.2"),
        vulns=VulnReport(
            ecosystem="Debian", package="postgresql-15", current_version="15",
            target_version="18.2", unmatched=True, approximate=True,
            note="the guessed distro/package/version triple returned no advisories, "
                 "which means the mapping found nothing -- not that the image is clean",
        ),
    ))

    # ---- MEDIUM: calver Azure API version (exercises the calver lag phrasing)
    findings.append(Finding(
        component=Component(ecosystem=AZURE_API,
                            name="Microsoft.CognitiveServices/accounts",
                            current="2023-05-01", source="main.bicep", line=21,
                            meta={"format": "bicep", "preview": False}),
        resolution=Resolution(latest_stable="2026-05-01"),
        lag=Lag(behind_major=3, stable_releases_behind=4, days_behind=1096),
        severity=SEV_MEDIUM,
        advice=Advice(risk=SEV_MEDIUM, strategy="Bump the apiVersion and re-deploy.",
                      target_version="2026-05-01"),
    ))

    # ---- LOW / CURRENT, to fill the summary table
    findings.append(Finding(
        component=Component(ecosystem=JVM_TOOLCHAIN, name="java", current="21",
                            source="pom.xml", line=8),
        resolution=Resolution(latest_stable="25", extra={"lts": True,
                                                         "support_days_left": 900}),
        lag=Lag(behind_major=4),
        severity=SEV_LOW,
        advice=Advice(risk=SEV_LOW, strategy="21 is a supported LTS; no action needed yet.",
                      target_version="25"),
    ))
    findings.append(Finding(
        component=Component(ecosystem=K8S_API, name="apps/Deployment", current="v1",
                            source="k8s/deploy.yaml", line=2),
        resolution=Resolution(latest_stable="v1", note="stable"),
        lag=Lag(),
        severity=SEV_CURRENT,
        advice=Advice(risk=SEV_CURRENT, strategy="No action needed."),
    ))

    # ---- UNKNOWN x2, with different reasons
    findings.append(Finding(
        component=Component(ecosystem=TERRAFORM_PROVIDER, name="hashicorp/kubernetes",
                            current=None, source="versions.tf", line=16,
                            meta={"unresolved_expr": "var.k8s_provider_version"}),
        resolution=Resolution(latest_stable="3.2.1",
                              repo="hashicorp/terraform-provider-kubernetes"),
        lag=Lag(comparable=False, reason="unparseable version ('' vs '3.2.1')"),
        severity=SEV_UNKNOWN,
        advice=Advice(risk=SEV_UNKNOWN,
                      strategy="Cannot assess: version is the expression "
                               "`var.k8s_provider_version`, not a literal."),
    ))
    findings.append(Finding(
        component=Component(ecosystem=DOCKER, name="internal/tool", current="alpine",
                            source="Dockerfile", line=1),
        resolution=Resolution(error="registry returned HTTP 404"),
        lag=Lag(comparable=False, reason="unparseable version ('alpine' vs '')"),
        severity=SEV_UNKNOWN,
        advice=Advice(risk=SEV_UNKNOWN, strategy="Cannot assess: no upstream feed."),
    ))

    # 13 worsened entries so the 12-row display cap and its overflow row both fire.
    worsened = [
        Change(ecosystem=MAVEN, name=f"org.example:lib-{i}", from_severity=SEV_MEDIUM,
               to_severity=SEV_HIGH, from_version="1.0.0", to_version="1.0.0",
               from_latest="1.5.0", to_latest="2.0.0")
        for i in range(13)
    ]
    trend = Diff(
        from_run="2026-01-02T00-00-00", to_run="2026-07-27T00-00-00",
        from_generated="2026-01-02T00:00:00+00:00",
        to_generated="2026-07-27T00:00:00+00:00",
        appeared=[Change(ecosystem=DOCKER, name="redis", to_severity=SEV_HIGH,
                         to_version="5", to_latest="8.8.1")],
        resolved=[Change(ecosystem=TERRAFORM_PROVIDER, name="hashicorp/random",
                         from_severity=SEV_HIGH, from_version="3.1.0")],
        worsened=worsened,
        improved=[Change(ecosystem=MAVEN, name="org.apache.commons:commons-lang3",
                         from_severity=SEV_HIGH, to_severity=SEV_LOW,
                         from_version="3.9", to_version="3.19.0")],
        unchanged=7,
        from_counts={SEV_CRITICAL: 1, SEV_HIGH: 3, SEV_MEDIUM: 2},
        to_counts={SEV_CRITICAL: 2, SEV_HIGH: 2, SEV_MEDIUM: 1},
        note="matched 24 of 26 components across the two runs",
    )

    return Report(
        root="/tmp/estate",
        findings=findings,
        stats=ScanStats(files_scanned=41, components_found=9, resolved=7,
                        resolve_errors=1, github_queries=18, cache_hits=64,
                        started="2026-07-27T00:00:00+00:00", duration_s=12.34),
        errors=["velocity: measured 6 of 11 eligible repos (--max-velocity); "
                "the rest were skipped"],
        source={"remote": "https://github.com/darrinthomascecil/driftscope-testapp",
                "ref": "main", "commit": "0123456789abcdef0123456789abcdef01234567"},
        trend=trend,
    )

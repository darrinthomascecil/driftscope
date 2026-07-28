"""Severity scoring and advice tests. Pure -- no network, synthetic resolutions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from driftscope.advisor import advise, score
from driftscope.models import (
    AZURE_API,
    DOCKER,
    K8S_API,
    PYPI,
    SEV_CRITICAL,
    SEV_CURRENT,
    SEV_HIGH,
    SEV_LOW,
    SEV_MEDIUM,
    SEV_UNKNOWN,
    TERRAFORM_PROVIDER,
    Component,
    GitHubDelta,
    Release,
    Resolution,
)
from driftscope.version import lag_between


def mk(eco, name, current, latest, *, versions=None, meta=None, **res_kw):
    c = Component(ecosystem=eco, name=name, current=current, source="f", meta=meta or {})
    releases = [Release(version=v) for v in (versions or [current, latest] if current else [latest])]
    r = Resolution(latest_stable=latest, releases=releases, **res_kw)
    lag = lag_between(current, latest, releases=releases)
    return c, r, lag


def sev(eco, current, latest, **kw):
    c, r, lag = mk(eco, "x", current, latest, **kw)
    return score(c, r, lag)


def test_up_to_date_is_current():
    assert sev(PYPI, "2.0.0", "2.0.0") == SEV_CURRENT


def test_patch_is_low():
    assert sev(PYPI, "2.0.0", "2.0.3") == SEV_LOW


def test_one_major_is_high():
    assert sev(PYPI, "1.9.0", "2.0.0") == SEV_HIGH


def test_two_majors_is_critical():
    assert sev(PYPI, "1.0.0", "3.0.0") == SEV_CRITICAL


def test_many_minors_is_medium():
    assert sev(PYPI, "2.1.0", "2.9.0") == SEV_MEDIUM


def test_deprecated_beats_everything():
    assert sev(PYPI, "2.0.0", "2.0.0", deprecated=True) == SEV_CRITICAL


def test_unresolvable_is_unknown():
    c = Component(ecosystem=PYPI, name="x", current="1.0.0", source="f")
    r = Resolution(error="HTTP 404")
    assert score(c, r, lag_between("1.0.0", None)) == SEV_UNKNOWN


def test_floating_tag_is_medium_even_without_a_latest():
    """Regression: floating tags must be scored before the resolvability check."""
    c = Component(
        ecosystem=DOCKER, name="nginx", current="latest", source="f",
        meta={"floating": True},
    )
    r = Resolution(note="floating tag")  # no latest_stable, by design
    assert score(c, r, lag_between("latest", None)) == SEV_MEDIUM


def test_age_escalates_severity():
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    lag = lag_between(
        "2.1.0", "2.2.0",
        current_published=old,
        latest_published=old + timedelta(days=900),
    )
    c = Component(ecosystem=PYPI, name="x", current="2.1.0", source="f")
    r = Resolution(latest_stable="2.2.0")
    # One minor behind would be LOW, but 900 days stale escalates it.
    assert score(c, r, lag) == SEV_HIGH


def test_preview_azure_api_escalates():
    c = Component(
        ecosystem=AZURE_API, name="Microsoft.App/containerApps",
        current="2023-04-01-preview", source="f", meta={"preview": True},
    )
    r = Resolution(latest_stable="2025-01-01", releases=[Release(version="2025-01-01")])
    lag = lag_between("2023-04-01-preview", "2025-01-01")
    # calver vs calver, so comparable; preview forces at least HIGH
    assert score(c, r, lag) in (SEV_HIGH, SEV_CRITICAL)


def test_k8s_removed_api_is_critical():
    c = Component(ecosystem=K8S_API, name="extensions/Ingress", current="v1beta1", source="f")
    r = Resolution(latest_stable="v1", note="extensions/v1beta1 deprecated in k8s 1.14, REMOVED in 1.22; migrate to networking.k8s.io/v1")
    assert score(c, r, lag_between("v1beta1", "v1")) == SEV_CRITICAL


def test_k8s_deprecated_not_removed_is_high():
    c = Component(ecosystem=K8S_API, name="flowcontrol/FlowSchema", current="v1beta3", source="f")
    r = Resolution(latest_stable="v1", note="deprecated in k8s 1.29; migrate to v1")
    assert score(c, r, lag_between("v1beta3", "v1")) == SEV_HIGH


# ------------------------------------------------------------------- advice


def test_staged_path_for_multi_major():
    versions = ["1.9.0", "2.4.0", "3.7.0", "4.1.0"]
    c, r, lag = mk(PYPI, "x", "1.9.0", "4.1.0", versions=versions)
    a = advise(c, r, lag, SEV_CRITICAL)
    assert a.intermediate_versions == ["2.4.0", "3.7.0"]
    assert "Staged" in a.strategy


def test_no_staged_path_for_single_major():
    c, r, lag = mk(PYPI, "x", "1.9.0", "2.0.0", versions=["1.9.0", "2.0.0"])
    a = advise(c, r, lag, SEV_HIGH)
    assert a.intermediate_versions == []
    assert "Single major" in a.strategy


def test_terraform_provider_advice_mentions_lock_and_plan():
    c, r, lag = mk(TERRAFORM_PROVIDER, "hashicorp/azurerm", "3.0.0", "4.1.0")
    a = advise(c, r, lag, SEV_HIGH)
    joined = " ".join(a.steps + a.cautions).lower()
    assert "init -upgrade" in joined
    assert "lock file" in joined
    assert "replace" in joined


def test_azure_api_advice_recommends_what_if():
    c, r, lag = mk(AZURE_API, "Microsoft.Storage/storageAccounts", "2021-04-01", "2026-05-01")
    a = advise(c, r, lag, SEV_HIGH)
    assert any("what-if" in s for s in a.steps)


def test_floating_docker_advice_is_about_pinning_not_upgrading():
    c = Component(
        ecosystem=DOCKER, name="nginx", current="latest", source="f",
        raw="nginx:latest", meta={"floating": True},
    )
    r = Resolution(note="floating tag")
    a = advise(c, r, lag_between("latest", None), SEV_MEDIUM)
    assert "Pin this image" in a.strategy


def test_delta_signals_folded_into_cautions():
    c, r, lag = mk(PYPI, "x", "1.0.0", "2.0.0")
    d = GitHubDelta(
        repo="o/r", breaking_notes=["v2.0.0: BREAKING CHANGE: removed foo"],
        open_bug_count=25,
    )
    a = advise(c, r, lag, SEV_HIGH, d)
    joined = " ".join(a.cautions)
    assert "breaking-change note" in joined
    assert "one release below" in joined


def test_unknown_advice_explains_variable_expression():
    c = Component(
        ecosystem=TERRAFORM_PROVIDER, name="hashicorp/kubernetes", current=None,
        source="f", meta={"unresolved_expr": "var.k8s_provider_version"},
    )
    r = Resolution(latest_stable="3.2.1")
    a = advise(c, r, lag_between(None, "3.2.1"), SEV_UNKNOWN)
    assert "var.k8s_provider_version" in a.strategy


def test_current_needs_no_advice():
    c, r, lag = mk(PYPI, "x", "2.0.0", "2.0.0")
    a = advise(c, r, lag, SEV_CURRENT)
    assert a.steps == [] and "No action" in a.strategy


# --------------------------------------------------------- upgrade verdict

from driftscope.advisor import upgrade_verdict
from driftscope.models import (
    Lag,
    Resolution,
    VERDICT_HOLD,
    VERDICT_LOW,
    VERDICT_PLAN,
    VERDICT_UPGRADE,
)
from driftscope.vuln import Vuln, VulnReport


def _vr(fixed=(), introduced=(), target="2.0.0"):
    return VulnReport(
        package="p", target_version=target,
        fixed_count=len(fixed), introduced_count=len(introduced),
        fixed_by_upgrade=[Vuln(id=f"f{i}", severity=s) for i, s in enumerate(fixed)],
        introduced_by_upgrade=[Vuln(id=f"i{i}", severity=s) for i, s in enumerate(introduced)],
    )


def test_verdict_upgrade_now_when_pressure_and_low_friction():
    v = upgrade_verdict(Resolution(latest_stable="2.0.0"),
                        Lag(behind_major=0, current_age_days=800),
                        None, _vr(fixed=("HIGH",)), SEV_HIGH)
    assert v.verdict == VERDICT_UPGRADE
    assert any("cleared by upgrading" in r for r in v.move_reasons)


def test_verdict_plans_a_staged_upgrade_across_majors():
    v = upgrade_verdict(Resolution(latest_stable="4.0.0"),
                        Lag(behind_major=2), None, None, SEV_HIGH)
    assert v.verdict == VERDICT_PLAN
    assert any("major" in r for r in v.wait_reasons)


def test_verdict_holds_when_target_adds_risk_without_pressure():
    v = upgrade_verdict(Resolution(latest_stable="2.0.0"),
                        Lag(behind_major=1), None, _vr(introduced=("CRITICAL",)), SEV_MEDIUM)
    assert v.verdict == VERDICT_HOLD


def test_verdict_is_low_priority_when_severity_is_low():
    # 4 majors behind, but a low severity (e.g. a supported LTS) must not overstate.
    v = upgrade_verdict(Resolution(latest_stable="25"),
                        Lag(behind_major=4), None, None, SEV_LOW)
    assert v.verdict == VERDICT_LOW


def test_no_verdict_when_there_is_nothing_to_weigh():
    assert upgrade_verdict(Resolution(latest_stable="1.0.0"),
                           Lag(behind_major=0), None, None, SEV_MEDIUM) is None

"""The presentation-neutral layer, tested without going through a renderer.

Before this layer existed these decisions could only be asserted by matching
against rendered HTML or Markdown, which meant a test for "an indiscriminate
report withholds the fixed/remaining split" was really a test of table markup.
"""

from __future__ import annotations

from datetime import date

import pytest

from driftscope.history import Change, Diff
from driftscope.models import (
    DOCKER,
    MAVEN,
    SEV_HIGH,
    SEV_LOW,
    SEV_MEDIUM,
    Velocity,
    Window,
)
from driftscope.report.summarize import (
    TREND_ROW_CAP,
    VULN_DETAIL_CAP,
    spark_ratios,
    summarize_trend,
    summarize_velocity,
    summarize_vulns,
)
from driftscope.vuln import Vuln, VulnReport


def _v(n: int, sev: str = "HIGH") -> Vuln:
    return Vuln(id=f"GHSA-{n:04d}", aliases=[f"CVE-2024-{n}"], severity=sev,
                summary="x", url=f"https://osv.dev/{n}")


# ------------------------------------------------------------------ vulnerabilities


def test_unmatched_short_circuits_to_the_note_only():
    r = VulnReport(package="postgresql-15", current_version="15", unmatched=True,
                   approximate=True, note="the mapping found nothing")
    s = summarize_vulns(r)
    assert s.unmatched is True
    assert s.sections == (), "an unmatched query has no advisory sets to show"
    assert s.note == "the mapping found nothing"


def test_indiscriminate_withholds_the_fixed_remaining_split():
    """The redis case: identical advisory sets at both versions, so a fixed/remaining
    split would be a confident guess."""
    r = VulnReport(package="redis", current_version="5", target_version="8.8.1",
                   remaining_count=72, remaining_after_upgrade=[_v(i) for i in range(4)],
                   approximate=True, indiscriminate=True)
    s = summarize_vulns(r)
    assert [sec.title for sec in s.sections] == ["Advisories on record"]
    assert s.headline_version is None, "no 'affecting <version>' claim is defensible"
    assert s.headline_count == 72
    assert s.upgrade_clears_nothing is False, (
        "that phrasing asserts the upgrade was evaluated, which it was not"
    )


def test_normal_report_splits_three_ways_and_labels_the_target():
    r = VulnReport(package="jackson", current_version="2.15.2", target_version="2.20.0",
                   fixed_count=9, remaining_count=2, introduced_count=1,
                   fixed_by_upgrade=[_v(1)], remaining_after_upgrade=[_v(2)],
                   introduced_by_upgrade=[_v(3)])
    s = summarize_vulns(r)
    assert [sec.title for sec in s.sections] == [
        "Fixed by upgrading to 2.20.0",
        "Still present after upgrade",
        "Introduced by the upgrade",
    ]
    assert [sec.role for sec in s.sections] == ["good", "critical", "critical"]
    assert s.headline_count == 11, "current exposure is fixed + remaining"


def test_missing_target_drops_the_version_from_the_fixed_label():
    r = VulnReport(package="p", fixed_count=1, fixed_by_upgrade=[_v(1)])
    assert summarize_vulns(r).sections[0].title == "Fixed by upgrading"


def test_zero_count_sections_are_omitted_entirely():
    r = VulnReport(package="p", current_version="1", target_version="2",
                   fixed_count=0, remaining_count=3,
                   remaining_after_upgrade=[_v(1)])
    titles = [sec.title for sec in summarize_vulns(r).sections]
    assert titles == ["Still present after upgrade"]


def test_upgrade_clears_nothing_only_when_nothing_is_fixed():
    base = dict(package="p", current_version="1", target_version="2",
                remaining_after_upgrade=[_v(1)])
    assert summarize_vulns(VulnReport(remaining_count=3, **base)).upgrade_clears_nothing
    assert not summarize_vulns(
        VulnReport(remaining_count=3, fixed_count=1, fixed_by_upgrade=[_v(2)], **base)
    ).upgrade_clears_nothing


def test_detail_list_is_capped_and_the_remainder_is_counted():
    r = VulnReport(package="p", fixed_count=50,
                   fixed_by_upgrade=[_v(i) for i in range(20)])
    sec = summarize_vulns(r).sections[0]
    assert len(sec.shown) == VULN_DETAIL_CAP
    assert sec.hidden == 50 - VULN_DETAIL_CAP


def test_counted_but_undescribed_advisories_still_report_a_count():
    """Counts come from advisory ids; details are fetched separately and may be
    absent entirely. The count must survive that."""
    sec = summarize_vulns(VulnReport(package="p", fixed_count=7)).sections[0]
    assert sec.count == 7 and sec.shown == ()


# ----------------------------------------------------------------------- velocity


def _windows(n=3, frustration_last=True):
    out = []
    for i in range(n):
        pos, neg = (10, 5) if (frustration_last or i < n - 1) else (0, 0)
        out.append(Window(start=date(2025, 1 + i, 1), end=date(2025, 2 + i, 1),
                          opened=10 * (i + 1), closed=5, sampled_issues=5,
                          positive=pos, negative=neg, releases=3 - i))
    return out


def test_three_metrics_with_the_units_that_make_them_readable():
    v = Velocity(repo="o/r", windows=_windows(), backlog_change_abs=12.0,
                 frustration_change_pts=4.0, release_change_pct=-50.0,
                 issue_trend="deteriorating", sentiment_trend="steady",
                 release_trend="deteriorating", verdict="deteriorating")
    s = summarize_velocity(v)
    assert [(m.label, m.unit, m.kind) for m in s.metrics] == [
        # Absolute, because a percentage of a signed value straddling zero is noise.
        ("Issue backlog", "", "signed_delta"),
        # Percentage POINTS: +64% relative beside a "steady" verdict read as a lie.
        ("Negative reactions", "pp", "share"),
        ("Releases shipped", "%", "count"),
    ]


def test_unmeasurable_latest_window_is_none_not_zero():
    """No reactions at all is not the same as zero frustration, and a reporter must
    be able to tell the difference to print an em dash instead of 0%."""
    v = Velocity(repo="o/r", windows=_windows(frustration_last=False))
    s = summarize_velocity(v)
    sentiment = s.metrics[1]
    assert sentiment.latest is None
    assert sentiment.series[-1] == 0.0, "the plotted series still needs a number"


def test_error_short_circuits_before_any_metric():
    s = summarize_velocity(Velocity(repo="o/r", windows=_windows(), error="rate limited"))
    assert s.error == "rate limited" and s.metrics == ()


def test_notes_are_capped():
    v = Velocity(repo="o/r", windows=_windows(), notes=[f"n{i}" for i in range(9)])
    assert len(summarize_velocity(v).notes) == 3


# -------------------------------------------------------------------------- trend


def _diff(**kw):
    return Diff(from_counts={SEV_HIGH: 1}, to_counts={SEV_HIGH: 2}, **kw)


def test_categories_are_ordered_worst_news_first():
    d = _diff(
        worsened=[Change(ecosystem=MAVEN, name="a")],
        appeared=[Change(ecosystem=DOCKER, name="b")],
        improved=[Change(ecosystem=MAVEN, name="c")],
        resolved=[Change(ecosystem=MAVEN, name="d")],
        unchanged=4,
    )
    s = summarize_trend(d)
    assert [g.label for g in s.groups] == ["worsened", "new", "improved", "resolved"]
    assert [t.label for t in s.tiles] == [
        "worsened", "new", "improved", "resolved", "unchanged"
    ]
    assert s.tiles[-1].count == 4, "unchanged is a tile but never a row group"


def test_rows_capped_with_an_overflow_count():
    many = [Change(ecosystem=MAVEN, name=f"lib-{i}") for i in range(TREND_ROW_CAP + 5)]
    g = summarize_trend(_diff(worsened=many)).groups[0]
    assert len(g.shown) == TREND_ROW_CAP
    assert g.overflow == 5


def test_missing_run_ids_get_readable_placeholders():
    s = summarize_trend(_diff())
    assert (s.from_run, s.to_run) == ("previous", "current")


def test_empty_diff_is_flagged_so_a_reporter_can_say_so():
    s = summarize_trend(_diff())
    assert s.is_empty is True and s.has_rows is False


# --------------------------------------------------------------------- sparklines


def test_ratios_scale_to_the_absolute_peak():
    assert spark_ratios([0, 5, 10]) == [0.0, 0.5, 1.0]
    # Negative magnitudes still show relative size; sign is the reporter's business.
    assert spark_ratios([-10, 5]) == [1.0, 0.5]


def test_all_zero_series_does_not_divide_by_zero():
    assert spark_ratios([0, 0, 0]) == [0.0, 0.0, 0.0]


def test_empty_series_is_empty():
    assert spark_ratios([]) == []

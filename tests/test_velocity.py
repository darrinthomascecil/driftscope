"""Upstream velocity engine tests. Offline, canned GitHub payloads."""

from __future__ import annotations

from datetime import date

import pytest

from driftscope.forge.github import GitHubForge
from driftscope.ghub import velocity as vel
from driftscope.models import (
    CAT_FALLING,
    CAT_RISING,
    TREND_DETERIORATING,
    TREND_IMPROVING,
    TREND_STEADY,
    TREND_UNKNOWN,
)


def forge_of(gh) -> GitHubForge:
    """Wrap the canned client in the real adapter.

    These tests drove `measure` directly with raw GitHub JSON. Routing them
    through GitHubForge means the adapter's own translation is exercised by every
    velocity case rather than only by its unit tests.
    """
    return GitHubForge(gh)


class GH:
    """Serves a canned `/issues` page plus releases.

    `counts` and `issues` are expressed per window for readability and expanded
    into issue records with the right created_at / closed_at dates.
    """

    enabled = True
    queries = 0

    def __init__(self, counts=None, issues=None, releases=None, pages=None):
        self.counts = counts or {}
        self.issues = issues or {}
        self.releases = releases or []
        self.pages = pages
        self.calls = 0

    @staticmethod
    def _midpoint(window_start: str) -> str:
        """The true midpoint of the window whose start is `window_start`.

        Deriving a date as "the 15th of the start month" put `2025-10-30`'s
        records in the *previous* window, so every count landed one bucket early.
        """
        for s, e in vel._windows(TODAY, len(W), 90):
            if s.isoformat() == window_start:
                return (s + (e - s) / 2).isoformat()
        return window_start

    def _records(self):
        out = []
        for window, (opened, closed) in self.counts.items():
            day = self._midpoint(window)
            samples = self.issues.get(window) or []
            for i in range(opened):
                rec = {"created_at": f"{day}T00:00:00Z", "updated_at": f"{day}T00:00:00Z",
                       "closed_at": None, "comments": 0,
                       "reactions": {k: 0 for k in ("+1", "-1", "heart", "hooray",
                                                    "rocket", "laugh", "confused")}}
                if i < len(samples):
                    rec.update(samples[i])
                    rec["created_at"] = f"{day}T00:00:00Z"
                    rec["updated_at"] = f"{day}T00:00:00Z"
                out.append(rec)
            for _ in range(closed):
                out.append({"created_at": "2000-01-01T00:00:00Z",
                            "updated_at": f"{day}T00:00:00Z",
                            "closed_at": f"{day}T00:00:00Z", "comments": 0,
                            "reactions": {}})
        return out

    def api(self, path, **kw):
        self.calls += 1
        if "releases" in path:
            return [{"published_at": f"{d.isoformat()}T00:00:00Z"} for d in self.releases]
        if "/issues" in path:
            # Honour pagination: returning the same page for every request would
            # multiply every count by the page cap.
            import re as _re

            # Anchored on a parameter boundary: a bare `page=` also matches inside
            # `per_page=100`, which sliced past the end of the data.
            m = _re.search(r"[?&]page=(\d+)", path)
            page = int(m.group(1)) if m else 1
            data = self.pages if self.pages is not None else self._records()
            chunk = data[(page - 1) * 100 : page * 100]
            return chunk
        return None


def rx(pos=0, neg=0, comments=0):
    return {
        "comments": comments,
        "reactions": {"+1": pos, "-1": neg, "heart": 0, "hooray": 0,
                      "rocket": 0, "laugh": 0, "confused": 0, "eyes": 0},
    }


TODAY = date(2026, 7, 26)
# _windows() with 4 x 90 days back from TODAY
W = ["2025-08-01", "2025-10-30", "2026-01-28", "2026-04-28"]


def test_windows_are_contiguous_and_ordered():
    ws = vel._windows(TODAY, 4, 90)
    assert len(ws) == 4
    assert ws[-1][1] == TODAY
    for (s1, e1), (s2, e2) in zip(ws, ws[1:]):
        assert (e1 - s1).days == 89
        assert s2 == e1.replace() + (s2 - e1)  # contiguous
        assert s2 > e1


def test_backlog_shrinking_after_growth_is_improving():
    counts = {w: c for w, c in zip(W, [(20, 5), (20, 5), (20, 5), (10, 30)])}
    v = vel.measure(forge_of(GH(counts=counts)), "o/r", now=TODAY)
    assert v.issue_trend == TREND_IMPROVING
    assert v.windows[-1].backlog_delta == -20
    assert any("shrank" in n for n in v.notes)


def test_backlog_growing_after_shrinking_is_deteriorating():
    counts = {w: c for w, c in zip(W, [(5, 20), (5, 20), (5, 20), (30, 10)])}
    v = vel.measure(forge_of(GH(counts=counts)), "o/r", now=TODAY)
    assert v.issue_trend == TREND_DETERIORATING


def test_flat_backlog_is_steady():
    counts = {w: (10, 10) for w in W}
    v = vel.measure(forge_of(GH(counts=counts)), "o/r", now=TODAY)
    assert v.issue_trend == TREND_STEADY


def test_absolute_backlog_change_is_reported():
    """A percentage of a signed value straddling zero is meaningless."""
    counts = {w: c for w, c in zip(W, [(12, 10), (12, 10), (12, 10), (10, 14)])}
    v = vel.measure(forge_of(GH(counts=counts)), "o/r", now=TODAY)
    assert v.backlog_change_abs == pytest.approx(-6.0)  # -4 vs prior mean +2


def test_sentiment_deteriorates_when_negative_share_rises():
    issues = {W[i]: [rx(pos=10, neg=0)] for i in range(3)}
    issues[W[3]] = [rx(pos=5, neg=5)]  # 50% negative
    counts = {w: (5, 5) for w in W}
    v = vel.measure(forge_of(GH(counts=counts, issues=issues)), "o/r", now=TODAY)
    assert v.sentiment_trend == TREND_DETERIORATING
    assert v.frustration_change_pts == pytest.approx(50.0)
    assert any("negative reactions rose" in n for n in v.notes)


def test_sentiment_change_in_points_matches_the_threshold():
    """Regression: the relative % read as a contradiction beside "steady"."""
    issues = {W[i]: [rx(pos=88, neg=12)] for i in range(3)}  # 12%
    issues[W[3]] = [rx(pos=83, neg=17)]  # 17% -> +5pp, +42% relative
    v = vel.measure(forge_of(GH(counts={w: (5, 5) for w in W}, issues=issues)), "o/r", now=TODAY)
    assert v.frustration_change_pts == pytest.approx(5.0, abs=0.6)
    assert v.frustration_change_pct > 30, "relative change is much larger"
    assert v.sentiment_trend == TREND_STEADY, "5pp is at the steady boundary"


def test_release_drought_is_deteriorating():
    rel = [date(2025, 9, 1), date(2025, 11, 1), date(2026, 2, 1)]
    v = vel.measure(forge_of(
        GH(counts={w: (5, 5) for w in W}, releases=rel)), "o/r", now=TODAY
    )
    assert v.windows[-1].releases == 0
    assert v.release_trend == TREND_DETERIORATING
    assert any("no releases" in n for n in v.notes)


def test_no_activity_is_an_error_not_a_verdict():
    """A wrong repo guess must not produce a confident "healthy" reading."""
    v = vel.measure(forge_of(GH()), "o/does-not-exist", now=TODAY)
    assert v.error and "no issue activity" in v.error
    assert v.verdict == TREND_UNKNOWN


def test_missing_repo_is_rejected():
    v = vel.measure(forge_of(GH()), "", now=TODAY)
    assert v.error and "no repository" in v.error


def test_disagreeing_signals_report_pessimistically():
    # backlog improving, releases stalled
    counts = {w: c for w, c in zip(W, [(20, 5), (20, 5), (20, 5), (5, 30)])}
    rel = [date(2025, 9, 1), date(2025, 11, 1), date(2026, 2, 1)]
    v = vel.measure(forge_of(GH(counts=counts, releases=rel)), "o/r", now=TODAY)
    assert v.issue_trend == TREND_IMPROVING
    assert v.release_trend == TREND_DETERIORATING
    assert v.verdict == TREND_DETERIORATING
    assert any("disagree" in n for n in v.notes)


def test_window_derived_values():
    from driftscope.models import Window

    w = Window(start=date(2026, 1, 1), end=date(2026, 3, 31),
               opened=10, closed=4, sampled_issues=5, positive=15, negative=5,
               comments=20)
    assert w.backlog_delta == 6
    assert w.close_ratio == pytest.approx(0.4)
    assert w.total_reactions == 20
    assert w.frustration == pytest.approx(0.25)
    assert w.comments_per_issue == pytest.approx(4.0)
    assert w.label == "2026-01-01..2026-03-31"


def test_zero_reaction_window_has_no_frustration():
    from driftscope.models import Window

    w = Window(start=date(2026, 1, 1), end=date(2026, 3, 31))
    assert w.frustration is None
    assert w.close_ratio is None


@pytest.mark.network
def test_real_repo_velocity():
    from driftscope.engine import build_context

    ctx = build_context(no_az=True)
    v = vel.measure(forge_of(ctx.github), "prometheus/prometheus", windows=2, window_days=90)
    assert v.error is None, v.error
    assert len(v.windows) == 2
    assert any(w.opened for w in v.windows), "expected issue activity"
    assert v.verdict in (TREND_IMPROVING, TREND_STEADY, TREND_DETERIORATING)


# ------------------------------------------------------------- rate limiting


def test_search_slot_paces_requests(monkeypatch):
    """The search endpoint allows ~30/min; without pacing a multi-repo run comes
    back empty and the velocity reads as "no activity"."""
    from pathlib import Path

    from driftscope import http as H

    slept: list[float] = []
    monkeypatch.setattr(H.time, "sleep", lambda s: slept.append(s))
    gh = H.GitHub(cache=H.Cache(root=Path("/tmp/ds-none"), enabled=False))
    for _ in range(H.SEARCH_PER_MINUTE):
        gh._await_search_slot()
    assert slept == [], "the first batch must not block"
    gh._await_search_slot()
    assert slept and slept[0] > 0, "the next call must wait for a slot"
    assert gh.throttled_waits == 1


# --------------------------------------------- coverage vs the page cap


def _issue(created, updated, closed=None):
    return {"created_at": f"{created}T00:00:00Z", "updated_at": f"{updated}T00:00:00Z",
            "closed_at": (f"{closed}T00:00:00Z" if closed else None),
            "comments": 0, "reactions": {}}


def test_pull_requests_are_excluded():
    """Counting merged PRs as closed issues makes every project look healthy."""
    page = [_issue("2026-05-01", "2026-05-01", "2026-05-02") for _ in range(3)]
    page += [dict(_issue("2026-05-01", "2026-05-01", "2026-05-02"),
                  pull_request={"url": "x"}) for _ in range(5)]
    v = vel.measure(forge_of(GH(pages=page)), "o/r", now=TODAY)
    assert v.windows[-1].closed == 3, "the five PRs must not be counted"


def test_windows_beyond_the_page_cap_are_marked_incomplete():
    """A truncated walk undercounts old windows, which fakes acceleration.

    A window counts as covered only if it lies entirely after the oldest record
    retrieved. Here the walk reaches back to 2026-04-01, which precedes the last
    window's start (2026-04-28) but not the earlier ones.
    """
    page = [_issue("2026-04-01", "2026-04-01") for _ in range(100)]
    v = vel.measure(forge_of(GH(pages=page * 5)), "o/r", now=TODAY, max_pages=2)
    assert [w.complete for w in v.windows] == [False, False, False, True]
    assert any("excluded from the trend" in n for n in v.notes)


def test_a_partly_covered_latest_window_is_also_incomplete():
    """Coverage stopping mid-window must not be reported as covered."""
    page = [_issue("2026-06-01", "2026-06-01") for _ in range(100)]
    v = vel.measure(forge_of(GH(pages=page * 5)), "o/r", now=TODAY, max_pages=2)
    # 2026-06-01 falls inside the last window, so nothing is fully covered.
    assert not any(w.complete for w in v.windows)
    assert v.verdict == TREND_UNKNOWN


def test_no_complete_baseline_refuses_a_verdict():
    page = [_issue("2026-06-01", "2026-06-01") for _ in range(100)]
    v = vel.measure(forge_of(GH(pages=page * 5)), "o/r", now=TODAY, max_pages=1)
    assert v.verdict == TREND_UNKNOWN
    assert any("no complete earlier window" in n for n in v.notes)


def test_short_history_is_fully_complete():
    """A repo whose history fits inside one page needs no caveats."""
    page = [_issue("2026-05-01", "2026-05-01", "2026-05-02")]
    v = vel.measure(forge_of(GH(pages=page)), "o/r", now=TODAY)
    assert all(w.complete for w in v.windows)
    assert not any("excluded" in n for n in v.notes)


def test_issue_walk_uses_the_core_endpoint_not_search():
    """Regression: the search endpoint's ~30/min limit silently emptied results."""
    calls: list[str] = []

    class Recorder(GH):
        def api(self, path, **kw):
            calls.append(path)
            return super().api(path, **kw)

    vel.measure(forge_of(Recorder(counts={w: (2, 1) for w in W})), "o/r", now=TODAY)
    assert calls, "expected API calls"
    assert not any("search/" in c for c in calls), "must not hit the search endpoint"
    assert any("/issues?" in c for c in calls)


# --------------------------------------------------- issues bucketed by release

from datetime import datetime, timezone

from driftscope.forge import IssueRecord, IssueWalk, ReleaseRecord


class _FakeForge:
    kind, enabled, queries = "github", True, 0

    def __init__(self, releases, issues):
        self._rels, self._issues = releases, issues

    def releases(self, repo):
        return self._rels

    def issues_since(self, repo, since, *, max_pages=20):
        return IssueWalk(items=[i for i in self._issues if i.created and i.created >= since],
                         oldest_seen=since)

    def issue_counts(self, *a, **k):
        return (0, 0)

    def web_url(self, repo):
        return "x"


def test_velocity_buckets_open_issues_by_release():
    def dt(s):
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

    rels = [ReleaseRecord(tag="v2.0.0", published=dt("2026-05-01T00:00:00")),
            ReleaseRecord(tag="v2.1.0", published=dt("2026-06-15T00:00:00"))]
    issues = [
        IssueRecord(number=1, title="a", url="", state="open", created=date(2026, 5, 10), closed=None),
        IssueRecord(number=2, title="b", url="", state="closed", created=date(2026, 5, 20), closed=date(2026, 6, 1)),
        IssueRecord(number=3, title="c", url="", state="open", created=date(2026, 6, 20), closed=None),
    ]
    v = vel.measure(_FakeForge(rels, issues), "o/r", now=date(2026, 7, 1))
    got = {s.version: (s.opened, s.still_open) for s in v.by_release}
    assert got["v2.0.0"] == (2, 1)   # #1 (open) + #2 (closed) filed in v2.0.0's window
    assert got["v2.1.0"] == (1, 1)   # #3 filed after v2.1.0
    # newest release first
    assert v.by_release[0].version == "v2.1.0"


# ---- issue category decomposition over time --------------------------------


def _lab(*names):
    return {"labels": [{"name": n} for n in names]}


def test_categorize_labels_then_title():
    assert vel._categorize(("security",), "anything") == "security"
    assert vel._categorize(("enhancement",), "x") == "feature"
    assert vel._categorize((), "app crashes with an exception") == "bug"
    assert vel._categorize((), "slow response, high memory usage") == "performance"
    assert vel._categorize((), "a general question about usage") == "other"
    assert vel._categorize(("bug", "security"), "x") == "security"  # priority: security > bug


def test_category_counts_are_bucketed_per_window():
    counts = {W[3]: (4, 0)}
    issues = {W[3]: [
        _lab("security"),
        _lab("bug"),
        {"title": "please add a new export feature"},  # -> feature (via title)
        {"title": "a general question about usage"},    # -> other
    ]}
    v = vel.measure(forge_of(GH(counts=counts, issues=issues)), "o/r", now=TODAY)
    totals = {c.category: c.total for c in v.by_category}
    assert totals == {"security": 1, "bug": 1, "feature": 1, "other": 1}
    # sorted by total desc then name; all tie at 1 -> alphabetical
    assert [c.category for c in v.by_category] == ["bug", "feature", "other", "security"]


def test_category_rising_is_detected():
    counts = {W[0]: (1, 0), W[1]: (1, 0), W[2]: (2, 0), W[3]: (5, 0)}
    issues = {w: [_lab("security")] * n for w, (n, _) in counts.items()}
    v = vel.measure(forge_of(GH(counts=counts, issues=issues)), "o/r", now=TODAY)
    sec = next(c for c in v.by_category if c.category == "security")
    assert sec.per_window == [1, 1, 2, 5]
    assert sec.direction == CAT_RISING


def test_category_falling_is_detected():
    counts = {W[0]: (6, 0), W[1]: (6, 0), W[2]: (6, 0), W[3]: (1, 0)}
    issues = {w: [_lab("bug")] * n for w, (n, _) in counts.items()}
    v = vel.measure(forge_of(GH(counts=counts, issues=issues)), "o/r", now=TODAY)
    bug = next(c for c in v.by_category if c.category == "bug")
    assert bug.per_window == [6, 6, 6, 1]
    assert bug.direction == CAT_FALLING

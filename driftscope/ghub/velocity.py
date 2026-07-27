"""Upstream project health: the velocity of issues and of sentiment.

"How far behind am I" is a static question. This answers a different one: *which
way is the upstream project moving*, and how fast. A dependency two majors behind
whose maintainers are closing issues faster than they arrive is a very different
risk from one that is one minor behind and drowning.

Two velocities, measured over consecutive time windows:

  issue velocity     opened vs closed per window -> is the backlog growing?
  sentiment velocity reaction mix per window     -> is the mood deteriorating?

Sentiment is derived from GitHub *reactions*, which are structured data rather
than inferred text: 👍 ❤️ 🎉 🚀 😄 read positive, 👎 and 😕 read negative. That is a
proxy for how users feel, not a measurement of it -- 😕 in particular often means
"confused by the docs" rather than "angry" -- so the raw counts travel with the
verdict and nothing is asserted beyond what the numbers show.

"Relative" means each metric is compared against the mean of the preceding
windows, so the output is an acceleration, not a level.

Cost: one paginated walk of `/repos/{o}/{r}/issues` plus one call for releases.
That endpoint is on the *core* rate limit (5000/hour), not the search limit
(~30/minute), and carries created_at, closed_at, reactions and comments on every
item -- so a quiet project costs 2 calls and a very busy one costs ~15, all with
ample headroom. An earlier version issued three *search* calls per window (~13 per
repo) and got throttled, which surfaced as "no activity" -- a confident wrong
answer. Pages are capped; windows the walk could not reach are marked incomplete
and excluded from the trend rather than reported as quiet.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ..http import GitHub
from ..models import (
    NEGATIVE_REACTIONS,
    POSITIVE_REACTIONS,
    TREND_DETERIORATING,
    TREND_IMPROVING,
    TREND_STEADY,
    TREND_UNKNOWN,
    Velocity,
    Window,
)


# A sentiment move counts only past this many percentage points, so ordinary
# sampling noise does not read as a mood swing.
SENTIMENT_PTS = 5.0
_EPS = 1e-6


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct_change(latest: float | None, baseline: float | None) -> float | None:
    if latest is None or baseline is None:
        return None
    if baseline == 0:
        return None if latest == 0 else 100.0
    return (latest - baseline) / abs(baseline) * 100.0


def _windows(now: date, count: int, days: int) -> list[tuple[date, date]]:
    out = []
    end = now
    for _ in range(count):
        start = end - timedelta(days=days - 1)
        out.append((start, end))
        end = start - timedelta(days=1)
    return list(reversed(out))


def measure(
    gh: GitHub,
    repo: str,
    *,
    windows: int = 4,
    window_days: int = 90,
    max_pages: int = 20,
    now: date | None = None,
) -> Velocity:
    v = Velocity(repo=repo, window_days=window_days)
    if not repo:
        v.error = "no repository known for this component"
        return v

    today = now or datetime.now(timezone.utc).date()
    release_dates = _release_dates(gh, repo)

    bounds = _windows(today, windows, window_days)
    v.windows = [Window(start=s, end=e) for s, e in bounds]

    # One paginated walk of the issues list replaces three search calls per window.
    # `/issues` sits on the *core* rate limit (5000/hour) instead of the search
    # limit (~30/minute), and each item already carries created_at, closed_at,
    # reactions and comments -- so this is both far fewer calls and 160x more
    # headroom. Sorting by `updated` with `since` guarantees the walk sees every
    # issue either opened or closed inside the period.
    issues, truncated, oldest_seen = _fetch_issues(
        gh, repo, bounds[0][0], max_pages=max_pages
    )
    if issues is None:
        v.error = (
            f"could not read issues for {repo} (repo missing, issues disabled, or "
            "the GitHub call failed)"
        )
        return v

    for it in issues:
        created = _as_date(it.get("created_at"))
        closed = _as_date(it.get("closed_at"))
        for w in v.windows:
            if created and w.start <= created <= w.end:
                w.opened += 1
                # Sentiment is attributed to the window the issue was raised in,
                # so a mood shift lands where the frustration was expressed.
                w.sampled_issues += 1
                w.comments += int(it.get("comments") or 0)
                rx = it.get("reactions") or {}
                w.positive += sum(int(rx.get(k) or 0) for k in POSITIVE_REACTIONS)
                w.negative += sum(int(rx.get(k) or 0) for k in NEGATIVE_REACTIONS)
            if closed and w.start <= closed <= w.end:
                w.closed += 1

    for w in v.windows:
        w.releases = sum(1 for d in release_dates if w.start <= d <= w.end)

    if truncated and oldest_seen:
        # Only windows entirely newer than the oldest record we retrieved are
        # trustworthy; the rest are undercounts and are excluded from the trend.
        for w in v.windows:
            if w.start < oldest_seen:
                w.complete = False
        dropped = [w for w in v.windows if not w.complete]
        if dropped:
            v.notes.append(
                f"{len(dropped)} of {len(v.windows)} windows fell outside the "
                f"{max_pages}-page issue history (oldest record "
                f"{oldest_seen.isoformat()}) and are excluded from the trend"
            )
    if not any(w.opened or w.closed for w in v.windows):
        v.error = "no issue activity found (issues disabled, or repo not found)"
        return v

    _derive_trends(v)
    return v


def _as_date(s: object) -> date | None:
    if not isinstance(s, str) or len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _fetch_issues(
    gh: GitHub, repo: str, since: date, *, max_pages: int
) -> tuple[list[dict] | None, bool, date | None]:
    """Every issue opened or closed since `since`, in as few calls as possible.

    Returns (issues, truncated). `None` means the repository could not be read at
    all, which is different from a repository with no issues.

    Pull requests are filtered out: `/issues` includes them, and counting merged
    PRs as closed issues would make every active project look like it was clearing
    a backlog.
    """
    out: list[dict] = []
    seen_any = False
    oldest: date | None = None
    for page in range(1, max_pages + 1):
        body = gh.api(
            f"repos/{repo}/issues?state=all&since={since.isoformat()}T00:00:00Z"
            f"&sort=updated&direction=desc&per_page=100&page={page}"
        )
        if body is None or not isinstance(body, list):
            return (out if seen_any else None), False, oldest
        seen_any = True
        for it in body:
            if not isinstance(it, dict):
                continue
            upd = _as_date(it.get("updated_at"))
            if upd and (oldest is None or upd < oldest):
                oldest = upd
            # /issues includes pull requests; counting merged PRs as closed issues
            # would make every active project look like it was clearing a backlog.
            if "pull_request" in it:
                continue
            out.append(it)
        if len(body) < 100:
            # Reached the end of the history: everything since `since` is covered.
            return out, False, since
    return out, True, oldest


def _release_dates(gh: GitHub, repo: str) -> list[date]:
    body = gh.api(f"repos/{repo}/releases?per_page=100")
    if not isinstance(body, list):
        return []
    out = []
    for r in body:
        s = r.get("published_at")
        if isinstance(s, str) and len(s) >= 10:
            try:
                out.append(date.fromisoformat(s[:10]))
            except ValueError:
                continue
    return out


def _derive_trends(v: Velocity) -> None:
    latest = v.windows[-1]
    prior = [w for w in v.windows[:-1] if w.complete]
    if not prior:
        v.verdict = TREND_UNKNOWN
        v.notes.append(
            'no complete earlier window to compare against; trend not computed'
        )
        return

    # --- issue velocity: backlog growth, relative to the prior windows
    prior_backlog = _mean([float(w.backlog_delta) for w in prior])
    v.backlog_change_pct = _pct_change(float(latest.backlog_delta), prior_backlog)
    if prior_backlog is not None:
        v.backlog_change_abs = float(latest.backlog_delta) - prior_backlog
    if latest.opened == 0 and latest.closed == 0:
        v.issue_trend = TREND_UNKNOWN
        v.notes.append("no issue activity in the most recent window")
    elif latest.backlog_delta <= 0 and (prior_backlog or 0) > 0:
        v.issue_trend = TREND_IMPROVING
        v.notes.append(
            f"backlog shrank by {abs(latest.backlog_delta)} in the latest window "
            f"after growing on average {prior_backlog:.0f} per window before it"
        )
    elif latest.backlog_delta > 0 and (prior_backlog or 0) <= 0:
        v.issue_trend = TREND_DETERIORATING
        v.notes.append(
            f"backlog grew by {latest.backlog_delta} after previously shrinking"
        )
    elif prior_backlog is not None and latest.backlog_delta > prior_backlog * 1.5 and latest.backlog_delta > 5:
        v.issue_trend = TREND_DETERIORATING
        v.notes.append("backlog growth is accelerating")
    elif prior_backlog is not None and latest.backlog_delta < prior_backlog * 0.5:
        v.issue_trend = TREND_IMPROVING
    else:
        v.issue_trend = TREND_STEADY

    # --- sentiment velocity: share of negative reactions
    latest_f = latest.frustration
    prior_f = _mean([w.frustration for w in prior if w.frustration is not None])
    v.frustration_change_pct = _pct_change(latest_f, prior_f)
    if latest_f is not None and prior_f is not None:
        v.frustration_change_pts = (latest_f - prior_f) * 100
    if latest_f is None or prior_f is None:
        v.sentiment_trend = TREND_UNKNOWN
        if latest.total_reactions == 0:
            v.notes.append("no reactions in the latest window; sentiment not measurable")
    # Compare in percentage points with a tolerance. Chained float arithmetic on
    # the 0-1 fractions put exact-boundary cases (0.12 -> 0.17) on the wrong side:
    # the subtraction yields 5.000000000000002pp.
    elif (v.frustration_change_pts or 0) > SENTIMENT_PTS + _EPS:
        v.sentiment_trend = TREND_DETERIORATING
        v.notes.append(
            f"negative reactions rose from {prior_f:.0%} to {latest_f:.0%} of all reactions"
        )
    elif (v.frustration_change_pts or 0) < -SENTIMENT_PTS - _EPS:
        v.sentiment_trend = TREND_IMPROVING
        v.notes.append(
            f"negative reactions fell from {prior_f:.0%} to {latest_f:.0%} of all reactions"
        )
    else:
        v.sentiment_trend = TREND_STEADY

    # --- shipping velocity
    prior_rel = _mean([float(w.releases) for w in prior])
    v.release_change_pct = _pct_change(float(latest.releases), prior_rel)
    if prior_rel is None:
        v.release_trend = TREND_UNKNOWN
    elif latest.releases == 0 and prior_rel > 0:
        v.release_trend = TREND_DETERIORATING
        v.notes.append(
            f"no releases in the latest {v.window_days} days, down from "
            f"{prior_rel:.1f} per window"
        )
    elif latest.releases > prior_rel:
        v.release_trend = TREND_IMPROVING
    elif latest.releases < prior_rel * 0.5:
        v.release_trend = TREND_DETERIORATING
        v.notes.append("release cadence slowing")
    else:
        v.release_trend = TREND_STEADY

    # --- overall verdict: majority vote, ties resolved pessimistically
    votes = [v.issue_trend, v.sentiment_trend, v.release_trend]
    bad = votes.count(TREND_DETERIORATING)
    good = votes.count(TREND_IMPROVING)
    if bad == 0 and good == 0:
        v.verdict = TREND_STEADY if TREND_STEADY in votes else TREND_UNKNOWN
    elif bad > good:
        v.verdict = TREND_DETERIORATING
    elif good > bad:
        v.verdict = TREND_IMPROVING
    else:
        # A tie means signals disagree; say so rather than averaging it away.
        v.verdict = TREND_DETERIORATING
        v.notes.append("signals disagree; reported pessimistically")

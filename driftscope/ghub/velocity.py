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

from ..forge import Forge
from ..models import (
    CAT_FALLING,
    CAT_FLAT,
    CAT_RISING,
    CategoryTrend,
    TREND_DETERIORATING,
    TREND_IMPROVING,
    TREND_STEADY,
    TREND_UNKNOWN,
    ReleaseIssueStat,
    Velocity,
    Window,
)


# A sentiment move counts only past this many percentage points, so ordinary
# sampling noise does not read as a mood swing.
SENTIMENT_PTS = 5.0
_EPS = 1e-6

# Issue taxonomy: (category, keywords). Order is PRIORITY -- a "security" bug lands in
# security, not bug. Labels (curated by the project) are matched before the title (free
# text), and anything unmatched is "other". Deliberately coarse: the value is the shape of
# the trajectory over time, not a precise triage.
_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("security", ("security", "vuln", "cve", "xss", "csrf", "injection", "exploit", "rce", "auth")),
    ("regression", ("regression", "regressed", "broke", "broken", "no longer", "used to work")),
    ("performance", ("performance", "perf", "slow", "memory", "leak", "latency", "cpu", "throughput")),
    ("bug", ("bug", "defect", "crash", "error", "exception", "fault", "incorrect", "broken", "fails")),
    ("feature", ("feature", "enhancement", "request", "proposal", "rfc", "wishlist")),
    ("docs", ("docs", "documentation", "typo", "readme", "example")),
)
CATEGORY_OTHER = "other"


def _categorize(labels: tuple[str, ...], title: str) -> str:
    """Bucket one issue by its labels (preferred) then its title, into a coarse category."""
    label_hay = " ".join(labels).lower()
    for cat, kws in _CATEGORY_RULES:  # labels first -- curated, high signal
        for kw in kws:
            if kw in label_hay:
                return cat
    title_hay = (title or "").lower()
    for cat, kws in _CATEGORY_RULES:  # fall back to the title
        for kw in kws:
            if kw in title_hay:
                return cat
    return CATEGORY_OTHER


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
    forge: Forge,
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
    release_records = forge.releases(repo)
    release_dates = [r.published.date() for r in release_records if r.published]

    bounds = _windows(today, windows, window_days)
    v.windows = [Window(start=s, end=e) for s, e in bounds]

    # One walk of the forge's issue feed replaces three search calls per window.
    # On GitHub that is `/issues`, which sits on the core rate limit (5000/hour)
    # rather than the search limit (~30/minute) and carries created_at, closed_at,
    # reactions and comments on every item. On Azure DevOps it is a WIQL query plus
    # a batched field fetch. Either way the engine sees normalised IssueRecords.
    walk = forge.issues_since(repo, bounds[0][0], max_pages=max_pages)
    if not walk.readable:
        v.error = (
            f"could not read issues for {repo} (project missing, issues disabled, "
            "or the call failed)"
        )
        return v

    # Whatever the forge could not measure travels with the result rather than
    # being silently absorbed -- ADO does not sample reactions by default.
    v.notes.extend(walk.notes)

    for it in walk.items:
        for w in v.windows:
            if it.created and w.start <= it.created <= w.end:
                w.opened += 1
                cat = _categorize(it.labels, it.title)
                w.by_category[cat] = w.by_category.get(cat, 0) + 1
                # Sentiment is attributed to the window the issue was raised in,
                # so a mood shift lands where the frustration was expressed.
                w.sampled_issues += 1
                w.comments += it.comments
                # Unsampled reactions contribute nothing rather than zero, so an
                # unmeasured window ends with no reactions at all and reports
                # sentiment as unknown instead of as contentment.
                if it.reactions_sampled:
                    w.positive += it.positive
                    w.negative += it.negative
            if it.closed and w.start <= it.closed <= w.end:
                w.closed += 1

    for w in v.windows:
        w.releases = sum(1 for d in release_dates if w.start <= d <= w.end)

    # Issues bucketed by the release that was current when they were filed -- a
    # per-version activity view. A proxy, not a version tag (see ReleaseIssueStat):
    # only releases inside the issue walk's horizon (bounds[0][0]) can be populated,
    # and an issue "opened while vX was latest" is not necessarily about vX.
    dated_rels = sorted((r for r in release_records if r.published and r.tag),
                        key=lambda r: r.published)
    horizon = bounds[0][0]
    for i, r in enumerate(dated_rels):
        start = r.published.date()
        if start < horizon:
            continue
        end = dated_rels[i + 1].published.date() if i + 1 < len(dated_rels) else today
        stat = ReleaseIssueStat(version=r.tag)
        for it in walk.items:
            if it.created and start <= it.created < end:
                stat.opened += 1
                if it.closed is None:
                    stat.still_open += 1
        if stat.opened:
            v.by_release.append(stat)
    v.by_release.reverse()  # newest release first

    truncated, oldest_seen = walk.truncated, walk.oldest_seen
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
    _derive_category_trends(v)
    return v


def _derive_category_trends(v: Velocity) -> None:
    """Per-category opened-count trajectory across the windows, with a rising/falling/flat
    direction. Same latest-vs-prior-mean method as the backlog trend, restricted to complete
    windows so an undercounted old window can't invent a rise."""
    cats: set[str] = set()
    for w in v.windows:
        cats |= set(w.by_category)
    complete = [w for w in v.windows if w.complete]
    out: list[CategoryTrend] = []
    for cat in cats:
        per_window = [w.by_category.get(cat, 0) for w in v.windows]
        total = sum(per_window)
        if total == 0:
            continue
        ct = CategoryTrend(category=cat, per_window=per_window, total=total)
        comp = [w.by_category.get(cat, 0) for w in complete]
        if len(comp) >= 2:
            latest = float(comp[-1])
            prior = _mean([float(c) for c in comp[:-1]])
            if prior is not None:
                ct.change_abs = latest - prior
                if latest > prior * 1.5 and (latest - prior) >= 2:
                    ct.direction = CAT_RISING
                elif latest < prior * 0.5:
                    ct.direction = CAT_FALLING
                else:
                    ct.direction = CAT_FLAT
        out.append(ct)
    out.sort(key=lambda c: (-c.total, c.category))
    v.by_category = out


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

"""GitHub delta: what landed, and what broke, between two versions.

There is no API for "issues introduced by version X". This builds the closest
defensible approximation and labels it as such:

  1. Resolve both versions to release tags, and both tags to publish dates.
  2. Collect the releases in that window and mine their notes for breaking-change
     markers (this is the highest-signal part -- maintainers announce breakage).
  3. Search bug-labelled issues *created* in the window (open AND closed), analyse
     each one shallowly from the fields the search already returns -- severity,
     whether it was fixed or dismissed, the version it points at, how many people
     reacted -- and rank by an impact score so the report leads with what bit the
     most people. An issue filed after the target release shipped is evidence about
     the target, not proof; issues are reported as ranked signals, never verdicts.

Step 3's weakness is real and stated in the output: a bug filed in the window may
predate the release, and a bug introduced by it may be filed long after. The
release notes in step 2 are the part to trust. The per-issue analysis is a triage
aid, not an affected-version guarantee (see analyze_issue).
"""

from __future__ import annotations

import re
from datetime import timezone

from ..forge import BUG_LABELS, Forge, ReleaseRecord
from ..models import POSITIVE_REACTIONS, GitHubDelta, IssueSignal, Release, Resolution

BREAKING_PATTERNS = (
    r"breaking change",
    r"\bbreaking\b",
    r"backwards[- ]incompatible",
    r"backward[- ]incompatible",
    r"no longer supported",
    r"\bremoved\b",
    r"\brenamed\b",
    r"migration guide",
    r"upgrade guide",
    r"action required",
    r"\bdeprecat",
    r"minimum (?:required )?version",
    r"drops? support",
)
_BREAKING_RE = re.compile("|".join(BREAKING_PATTERNS), re.I)

def _is_section_heading(s: str) -> bool:
    """True for release-note section labels such as `DEPRECATIONS:`.

    Maintainers group notes under short all-caps headings, and those headings match
    the breaking-change patterns while carrying no information. Counting them
    inflated the breaking-note total with content-free entries, so a report could
    claim 25 breaking changes when several were just the word "DEPRECATIONS:".
    """
    if not s.endswith(":"):
        return False
    return len(s[:-1].split()) <= 3


def _match_release(releases: list[ReleaseRecord], version: str):
    """The ReleaseRecord for `version`, trying the tag shapes projects actually use.

    This used to be up to four `GET /releases/tags/{candidate}` calls per version
    -- eight per delta -- probing shapes until one answered. The forge already
    hands back the whole release list for the note mining, so the same answer is a
    lookup rather than a round trip.
    """
    v = (version or "").strip().lstrip("vV")
    if not v:
        return None
    wanted = {v.lower(), f"v{v}".lower(), f"release-{v}".lower()}
    for r in releases:
        tag = (r.tag or "").strip()
        if not tag:
            continue  # an untagged release cannot identify a version
        if tag.lower() in wanted or tag.lower().lstrip("v") == v.lower():
            return r
        # `{project}-1.2.3`, used by monorepos that tag per component.
        if tag.lower().endswith(f"-{v.lower()}"):
            return r
    return None


def _instant(s: object):
    """An ISO-8601 timestamp as a comparable instant, or None.

    The window used to be computed by comparing these strings directly, which is
    only correct when every timestamp shares one format. Two real forms break it:

      `12:00:00.123456Z` vs `12:00:00Z` -- the fractional one is LATER but sorts
      earlier, because '.' precedes 'Z'. GitHub emits whole seconds while a
      datetime round-tripped through parse_iso keeps microseconds, so a window
      bound and a release date routinely arrive in different forms.

      `14:00+02:00` vs `12:00Z` -- the same instant, but the first looks two hours
      later as text, so a release exactly on the boundary lands on the wrong side.
    """
    from ..resolvers import parse_iso

    dt = parse_iso(s) if isinstance(s, str) else None
    if dt is None:
        return None
    # Normalise to UTC so offsets compare as instants rather than as wall clocks.
    return dt.astimezone(timezone.utc)


def _releases_between(releases, from_date, to_date) -> list:
    """Releases published inside the window, oldest first.

    Bounds are half-open: the version you are ON is not part of what changed; the
    version you are moving TO is.
    """
    lo, hi = _instant(from_date), _instant(to_date)
    if lo is None or hi is None:
        return []
    out = []
    for r in releases:
        if r.published is None:
            # A lightweight tag carries no date and cannot be placed in a window.
            continue
        pub = r.published if r.published.tzinfo else r.published.replace(tzinfo=timezone.utc)
        if pub is not None and lo < pub <= hi:
            out.append((pub, r))
    out.sort(key=lambda pair: pair[0])
    return [r for _, r in out]


def _breaking_from_releases(releases) -> list[str]:
    """Quote breaking-change lines from release notes, tagged with their release.

    Accepts anything with `.tag`/`.body`, or the raw `{tag_name, body}` dicts the
    GitHub API returns, so existing callers and tests keep working.
    """
    notes: list[str] = []
    for r in releases:
        if isinstance(r, dict):
            tag, body = r.get("tag_name") or "?", r.get("body") or ""
        else:
            tag, body = getattr(r, "tag", "?") or "?", getattr(r, "body", "") or ""
        if not isinstance(body, str):
            continue
        for line in body.splitlines():
            s = line.strip().lstrip("#*-\u00b7 ").strip()
            if len(s) < 8 or len(s) > 300:
                continue
            if _is_section_heading(s):
                continue
            if _BREAKING_RE.search(s):
                entry = f"{tag}: {s}"
                if entry not in notes:
                    notes.append(entry)
            if len(notes) >= 25:
                return notes
    return notes


def _dates_from_resolution(
    res: Resolution, version: str | None
) -> str | None:
    if not version:
        return None
    r: Release | None = res.release_for(version)
    if r and r.published:
        return r.published.isoformat().replace("+00:00", "Z")
    return None


# --- issue analysis -------------------------------------------------------------
#
# The old listing kept number/title/url and threw away everything else the search
# response carries. But GitHub returns the body, reaction breakdown, comment count,
# milestone and (for closed issues) a state_reason on every item -- enough to make a
# shallow judgement without a single extra call. analyze_issue() turns each raw item
# into an IssueSignal scored on `impact`, which the delta then ranks on so the report
# leads with the issue that bit the most people, not just the newest one.

# Labels that, on their own, mark an issue as high-consequence. Matched as substrings
# so "priority: critical", "kind/regression", "sev1" etc. all land.
_SEVERITY_LABEL_HINTS = (
    "security", "vulnerab", "cve", "critical", "regression", "data-loss",
    "data loss", "dataloss", "blocker", "sev1", "sev-1", "sev:1", "p0",
    "priority:critical", "priority: critical", "severity:critical",
)
# Body/title wording for the same, when maintainers did not label it.
_CRITICAL_RE = re.compile(
    r"\b(crash(?:es|ed|ing)?|panic|segfault|deadlock|data ?loss|corrupt\w*|"
    r"cve-\d|vulnerab\w*|\brce\b|memory leak|\boom\b|hangs?|hung|"
    r"infinite loop|silently (?:fail|drop|lose))\b", re.I)
# Weaker but still bug-shaped wording -> "high".
_HIGH_RE = re.compile(
    r"\b(regression|broke(?:n)?|fail(?:s|ed|ing)?|throws?|exception|traceback|"
    r"stack ?trace|cannot|can't|does ?n[o']?t work|not working|unable to|"
    r"unexpected|incorrect|wrong)\b", re.I)
# "affects 2.3", "since v1.4", "broken in 3.0.1" -> the version an issue points at.
_VERSION_REF_RE = re.compile(
    r"\b(?:affect\w*|since|introduced in|broke(?:n)? in|regress\w* in|"
    r"reproduc\w+ on|started in)\s+v?(\d+\.\d+(?:\.\d+)?)", re.I)
# A milestone/label that looks like a version ("v2.4", "2.4.0", "1.0").
_VERSIONISH_RE = re.compile(r"^v?\d+\.\d+")

_SEVERITY_WEIGHT = {"critical": 100, "high": 30, "normal": 0}


def _clean_excerpt(body: object, limit: int = 180) -> str:
    """The first meaningful line of an issue body, collapsed to one line.

    Skips template scaffolding -- HTML comments, short "Steps to reproduce:" style
    headings, and markdown bullet/quote markers -- so the excerpt is the actual first
    sentence of the report rather than a form label.
    """
    if not isinstance(body, str):
        return ""
    picked = ""
    for line in body.splitlines():
        s = line.strip().lstrip("#>*-• ").strip()
        if not s or s.startswith("<!--"):
            continue
        # a short line ending in ':' is a template section heading, not content
        if s.endswith(":") and len(s.split()) <= 4:
            continue
        picked = s
        break
    picked = re.sub(r"\s+", " ", picked)
    return (picked[: limit - 1] + "…") if len(picked) > limit else picked


def _reaction_score(item: dict) -> int:
    r = item.get("reactions")
    if not isinstance(r, dict):
        return 0
    return sum(int(r.get(k) or 0) for k in POSITIVE_REACTIONS)


def _severity(labels: list[str], title: str, body: str) -> str:
    low = " ".join(labels).lower()
    if any(h in low for h in _SEVERITY_LABEL_HINTS):
        return "critical"
    blob = f"{title}\n{body[:1000]}"
    if _CRITICAL_RE.search(blob):
        return "critical"
    if _HIGH_RE.search(blob):
        return "high"
    return "normal"


def _fix_status(state: str, state_reason: str | None) -> str:
    """open -> still live; fixed/dismissed distinguish a real fix from a won't-fix.

    A closed-completed bug in the window is the strongest signal there is -- the
    maintainers confirmed it was real and shipped a fix -- so it must not be hidden
    the way the old `is:open` filter hid it. dismissed (not_planned) is the opposite:
    triaged away as not-a-bug, so it should sink rather than surface.
    """
    if state == "open":
        return "open"
    if state_reason == "completed":
        return "fixed"
    if state_reason == "not_planned":
        return "dismissed"
    return "closed"


def _affected_version(milestone: str | None, labels: list[str],
                      title: str, body: str) -> str | None:
    if milestone and _VERSIONISH_RE.match(milestone.strip().lstrip("vV")):
        return milestone.strip()
    for lab in labels:
        if _VERSIONISH_RE.match(lab.strip().lstrip("vV")):
            return lab.strip()
    m = _VERSION_REF_RE.search(f"{title}\n{body[:1000]}")
    return m.group(1) if m else None


def analyze_issue(item: dict) -> IssueSignal:
    """Turn a raw GitHub /search/issues item into an analysed IssueSignal.

    Pure and side-effect free -- it reads only fields already on the item, so the
    analysis costs no extra API calls. `impact` combines severity, how many people
    reacted, discussion volume, and whether it was actually fixed; the delta ranks
    the window's issues on it.
    """
    labels = [l.get("name") for l in (item.get("labels") or [])
              if isinstance(l, dict) and l.get("name")]
    title = (item.get("title") or "")[:200]
    body = item.get("body") if isinstance(item.get("body"), str) else ""
    state = item.get("state") or ""
    state_reason = item.get("state_reason")
    ms = item.get("milestone")
    milestone = ms.get("title") if isinstance(ms, dict) else None

    reactions = _reaction_score(item)
    comments = int(item.get("comments") or 0)
    severity = _severity(labels, title, body)
    fix_status = _fix_status(state, state_reason)

    impact = (_SEVERITY_WEIGHT.get(severity, 0)
              + reactions * 3 + comments
              + (10 if fix_status == "fixed" else 0)        # confirmed-real, fix shipped
              - (40 if fix_status == "dismissed" else 0))   # triaged away -> demote
    impact = max(impact, 0)

    return IssueSignal(
        number=int(item.get("number") or 0),
        title=title,
        url=item.get("html_url") or "",
        state=state,
        labels=labels,
        created_at=item.get("created_at"),
        kind="pr" if item.get("pull_request") else "issue",
        reactions=reactions,
        comments=comments,
        milestone=milestone,
        state_reason=state_reason,
        updated_at=item.get("updated_at"),
        body_excerpt=_clean_excerpt(body),
        severity=severity,
        fix_status=fix_status,
        affected_version=_affected_version(milestone, labels, title, body),
        impact=impact,
    )


def compute_delta(
    forge: Forge,
    repo: str | None,
    current: str | None,
    target: str | None,
    resolution: Resolution,
    *,
    max_issues: int = 15,
) -> GitHubDelta:
    d = GitHubDelta(repo=repo, from_ref=current, to_ref=target)
    if not repo:
        d.note = "no upstream project known for this component"
        return d
    if not current or not target or current == target:
        d.note = "nothing to compare"
        return d

    # One call feeds all three steps: dating both versions, bounding the window,
    # and mining the notes. Dating used to probe up to four tag shapes per version
    # -- eight round trips -- before any of the real work began.
    releases = forge.releases(repo)

    # Normally a resolution's own dates ARE the upstream release dates, so they
    # bound the issue window directly. A private image is the exception: its dates
    # are image *build* times (see resolvers/acr.py), which cluster when several
    # versions are rebuilt in one batch -- collapsing the window to a single day
    # and silently dropping every issue in it. When the resolution says its dates
    # are build times, ignore them for the window and take the forge's own release
    # dates, which are the axis an "issues between these versions" question means.
    build_times = bool(resolution.extra.get("dates_are_build_times"))
    from_date = None if build_times else _dates_from_resolution(resolution, current)
    to_date = None if build_times else _dates_from_resolution(resolution, target)
    from_rel = _match_release(releases, current) if not from_date else None
    to_rel = _match_release(releases, target) if not to_date else None
    if from_rel and from_rel.published:
        from_date = from_rel.published.isoformat()
    if to_rel and to_rel.published:
        to_date = to_rel.published.isoformat()
    d.from_ref = (from_rel.tag if from_rel else None) or current
    d.to_ref = (to_rel.tag if to_rel else None) or target
    d.from_date, d.to_date = from_date, to_date

    if not from_date or not to_date:
        d.note = (
            f"could not date both versions on {forge.kind}; issue window not "
            "computed. Release notes comparison unavailable."
        )
        return d
    lo, hi = _instant(from_date), _instant(to_date)
    if lo is None or hi is None or lo >= hi:
        # Compared as instants, not strings: fractional seconds and non-UTC
        # offsets both misjudge a window when compared as text, either abandoning
        # a real window or accepting an inverted one.
        d.note = "current release is not older than target; nothing to analyse"
        return d
    d.window_resolved = True

    rels = _releases_between(releases, from_date, to_date)
    d.releases_in_window = [r.tag for r in rels][:60]
    d.breaking_notes = _breaking_from_releases(rels)
    if not rels and releases:
        # Tags exist but none carry a date -- Azure DevOps lightweight tags. The
        # window is real; it simply cannot be populated, which is worth saying
        # rather than rendering as "nothing changed".
        if all(r.published is None for r in releases):
            d.note = (
                f"{len(releases)} tag(s) found but none are dated (lightweight "
                "git tags carry no tagger date), so no release could be placed "
                "in the window"
            )

    day_from, day_to = from_date[:10], to_date[:10]
    total, opened = forge.issue_counts(repo, day_from, day_to)
    if total is None or opened is None:
        # A failed or throttled count is unknown, never zero: reporting 0 here
        # once read as "this project has no issues".
        d.counts_available = False
    d.total_bug_count = total or 0
    d.open_bug_count = opened or 0

    # The individual issue listing is GitHub-only: it uses the search endpoint,
    # which has no Azure DevOps analogue worth the call budget. The counts above
    # cover both forges, so an ADO delta reports numbers without links.
    lister = getattr(forge, "search_issues", None)
    if lister is not None:
        clause = getattr(forge, "label_clause", lambda: 'label:"bug"')()
        # No `is:open`: a fixed regression is the strongest signal in the window, and
        # dropping it (as the old query did) hid exactly the issues worth leading with.
        # analyze_issue re-weights closed-completed up and dismissed down instead.
        query = f"repo:{repo} is:issue created:{day_from}..{day_to} {clause}"
        # Over-fetch a candidate pool and ask GitHub to pre-sort by interactions
        # (reactions + comments) so high-impact issues are in the page even when old,
        # then analyse, rank on our own impact score, and keep the top max_issues.
        fetch_n = max(40, max_issues * 2)
        try:
            raw = lister(query, limit=fetch_n, sort="interactions")
        except TypeError:
            # A forge/mocked lister without the `sort` kwarg -- fall back to its default.
            raw = lister(query, limit=fetch_n)
        signals = [analyze_issue(item) for item in (raw or [])
                   if isinstance(item, dict)]
        # Rank by impact, then recency as a tiebreak (newest first among equals).
        signals.sort(key=lambda s: (s.impact, s.created_at or ""), reverse=True)
        d.bug_issues = signals[:max_issues]

    if d.total_bug_count == 0 and not d.breaking_notes and not d.note:
        d.note = "no labelled bugs or breaking-change notes found in the window"
    elif not d.note:
        d.note = (
            "issues are filed-in-window signals, not proven regressions; "
            "breaking-change notes are quoted from release bodies"
        )
    return d

"""GitHub delta: what landed, and what broke, between two versions.

There is no API for "issues introduced by version X". This builds the closest
defensible approximation and labels it as such:

  1. Resolve both versions to release tags, and both tags to publish dates.
  2. Collect the releases in that window and mine their notes for breaking-change
     markers (this is the highest-signal part -- maintainers announce breakage).
  3. Search issues *created* in the window and still open, filtered to bug-ish
     labels. An issue filed after the target release shipped is evidence about the
     target, not proof; issues are reported as signals with links, never verdicts.

Step 3's weakness is real and stated in the output: a bug filed in the window may
predate the release, and a bug introduced by it may be filed long after. The
release notes in step 2 are the part to trust.
"""

from __future__ import annotations

import re
from datetime import timezone

from ..forge import Forge, ReleaseRecord
from ..models import GitHubDelta, IssueSignal, Release, Resolution

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

BUG_LABELS = ("bug", "regression", "defect", "type/bug", "kind/bug", "crash", "panic")


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

    from_date = _dates_from_resolution(resolution, current)
    to_date = _dates_from_resolution(resolution, target)
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
        query = (f'repo:{repo} is:issue created:{day_from}..{day_to} '
                 f'label:"bug" is:open')
        for item in lister(query, limit=max_issues):
            d.bug_issues.append(
                IssueSignal(
                    number=int(item.get("number") or 0),
                    title=(item.get("title") or "")[:200],
                    url=item.get("html_url") or "",
                    state=item.get("state") or "",
                    labels=[l.get("name") for l in (item.get("labels") or [])
                            if isinstance(l, dict) and l.get("name")],
                    created_at=item.get("created_at"),
                    kind="pr" if item.get("pull_request") else "issue",
                )
            )

    if d.total_bug_count == 0 and not d.breaking_notes and not d.note:
        d.note = "no labelled bugs or breaking-change notes found in the window"
    elif not d.note:
        d.note = (
            "issues are filed-in-window signals, not proven regressions; "
            "breaking-change notes are quoted from release bodies"
        )
    return d

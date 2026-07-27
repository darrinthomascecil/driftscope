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

from ..http import GitHub
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


def _find_tag(repo: str, version: str, gh: GitHub) -> tuple[str | None, str | None]:
    """Return (tag_name, published_at) for a version, trying common tag shapes."""
    v = (version or "").lstrip("vV")
    if not v:
        return None, None
    for candidate in (f"v{v}", v, f"release-{v}", f"{repo.split('/')[-1]}-{v}"):
        body = gh.api(f"repos/{repo}/releases/tags/{candidate}")
        if isinstance(body, dict) and body.get("tag_name"):
            return body["tag_name"], body.get("published_at")
    return None, None


def _releases_between(
    repo: str, from_date: str, to_date: str, gh: GitHub
) -> list[dict]:
    body = gh.api(f"repos/{repo}/releases?per_page=100")
    if not isinstance(body, list):
        return []
    out = []
    for r in body:
        pub = r.get("published_at")
        if not pub or not isinstance(pub, str):
            continue
        if from_date < pub <= to_date:
            out.append(r)
    out.sort(key=lambda r: r.get("published_at") or "")
    return out


def _breaking_from_releases(releases: list[dict]) -> list[str]:
    notes: list[str] = []
    for r in releases:
        tag = r.get("tag_name") or "?"
        body = r.get("body") or ""
        if not isinstance(body, str):
            continue
        for line in body.splitlines():
            s = line.strip().lstrip("#*-· ").strip()
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
    repo: str | None,
    current: str | None,
    target: str | None,
    resolution: Resolution,
    gh: GitHub,
    *,
    max_issues: int = 15,
) -> GitHubDelta:
    d = GitHubDelta(repo=repo, from_ref=current, to_ref=target)
    if not repo:
        d.note = "no GitHub repository known for this component"
        return d
    if not current or not target or current == target:
        d.note = "nothing to compare"
        return d

    # Prefer dates already known from the registry; fall back to the releases API.
    from_date = _dates_from_resolution(resolution, current)
    to_date = _dates_from_resolution(resolution, target)
    from_tag, to_tag = None, None
    if not from_date:
        from_tag, from_date = _find_tag(repo, current, gh)
    if not to_date:
        to_tag, to_date = _find_tag(repo, target, gh)
    d.from_ref = from_tag or current
    d.to_ref = to_tag or target
    d.from_date, d.to_date = from_date, to_date

    if not from_date or not to_date:
        d.note = (
            "could not date both versions on GitHub; issue window not computed. "
            "Release notes comparison unavailable."
        )
        return d
    if from_date >= to_date:
        d.note = "current release is not older than target; nothing to analyse"
        return d
    d.window_resolved = True

    rels = _releases_between(repo, from_date, to_date, gh)
    d.releases_in_window = [r.get("tag_name") or "?" for r in rels][:60]
    d.breaking_notes = _breaking_from_releases(rels)

    day_from, day_to = from_date[:10], to_date[:10]
    label_clause = " ".join(f'label:"{lbl}"' for lbl in ("bug",))
    base = f"repo:{repo} is:issue created:{day_from}..{day_to}"

    total = gh.search_count(f"{base} {label_clause}")
    opened = gh.search_count(f"{base} {label_clause} is:open")
    if total is None or opened is None:
        d.counts_available = False
    d.total_bug_count = total or 0
    d.open_bug_count = opened or 0
    for item in gh.search_issues(f"{base} {label_clause} is:open", limit=max_issues):
        d.bug_issues.append(
            IssueSignal(
                number=int(item.get("number") or 0),
                title=(item.get("title") or "")[:200],
                url=item.get("html_url") or "",
                state=item.get("state") or "",
                labels=[l.get("name") for l in (item.get("labels") or []) if isinstance(l, dict) and l.get("name")],
                created_at=item.get("created_at"),
                kind="pr" if item.get("pull_request") else "issue",
            )
        )

    if d.total_bug_count == 0 and not d.breaking_notes:
        d.note = "no labelled bugs or breaking-change notes found in the window"
    else:
        d.note = (
            "issues are filed-in-window signals, not proven regressions; "
            "breaking-change notes are quoted from release bodies"
        )
    return d

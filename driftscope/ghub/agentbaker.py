"""Map AKS node images to Azure/AgentBaker releases.

AgentBaker is the repo that builds AKS node image VHDs, so it is the upstream
whose history explains what changed between two node images.

The mapping has to go through dates, not strings: AgentBaker releases are tagged
`v0.YYYYMMDD.R` and node images are versioned `<distro>-YYYYMM.DD.R`, but release
bodies never quote node image version strings, so there is nothing to string-match
on. Both encode a date, so a node image is attributed to the AgentBaker release
cut at-or-before its build date.

Two dates matter and they are not the same:
  * build date   -- embedded in the node image version string
  * release date -- when the AgentBaker release was actually published

v0.20260720.1 was tagged 20260720 but published 2026-07-21. The release date is
the honest answer to "when did this ship", so it is preferred when available and
the build date is the fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..http import GitHub
from ..models import GitHubDelta, IssueSignal

REPO = "Azure/AgentBaker"

# Release tags for node image builds: v0.20260720.1
# Excludes component tags like anc/v202605.14.1 and dev tags like v20260421.author
_RELEASE_TAG = re.compile(r"^v0\.(20\d{2})(0[1-9]|1[0-2])([0-3]\d)\.(\d+)$")


@dataclass(frozen=True)
class ABRelease:
    tag: str
    build_date: date
    published: datetime | None
    revision: int
    # The releases listing already includes the body; re-fetching each one by tag
    # cost an extra call per release in the window (16 for a 6-month gap).
    body: str = ""

    @property
    def published_date(self) -> date | None:
        return self.published.date() if self.published else None

    @property
    def effective_date(self) -> date:
        """When this shipped: the release date, or the tag date as a fallback."""
        return self.published_date or self.build_date


def _parse_iso(s: object) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def release_index(gh: GitHub, *, pages: int = 3) -> list[ABRelease]:
    """All node-image AgentBaker releases, oldest first.

    Paginated because a stale node image can be a year or more behind and the
    default page of 30 would not reach back far enough to attribute it.
    """
    out: list[ABRelease] = []
    for page in range(1, pages + 1):
        body = gh.api(f"repos/{REPO}/releases?per_page=100&page={page}")
        if not isinstance(body, list) or not body:
            break
        for r in body:
            tag = r.get("tag_name")
            if not isinstance(tag, str):
                continue
            m = _RELEASE_TAG.match(tag)
            if not m:
                continue  # component/hotfix/dev tag, not a node image release
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            out.append(
                ABRelease(
                    tag=tag,
                    build_date=d,
                    published=_parse_iso(r.get("published_at")),
                    revision=int(m.group(4)),
                    body=r.get("body") if isinstance(r.get("body"), str) else "",
                )
            )
        if len(body) < 100:
            break
    out.sort(key=lambda r: (r.build_date, r.revision))
    return out


def release_for_date(index: list[ABRelease], when: date | None) -> ABRelease | None:
    """The release cut at-or-before `when` -- i.e. the one that built that image."""
    if not when or not index:
        return None
    candidates = [r for r in index if r.build_date <= when]
    return candidates[-1] if candidates else None


def latest_release(index: list[ABRelease]) -> ABRelease | None:
    return index[-1] if index else None


def releases_between(
    index: list[ABRelease], start: ABRelease, end: ABRelease
) -> list[ABRelease]:
    return [
        r
        for r in index
        if (r.build_date, r.revision) > (start.build_date, start.revision)
        and (r.build_date, r.revision) <= (end.build_date, end.revision)
    ]


BREAKING = re.compile(
    r"breaking|no longer|removed|remove |deprecat|drops? support|migrat|"
    r"security|CVE-\d{4}|action required|must be|renamed",
    re.I,
)


def node_image_delta(
    gh: GitHub,
    current_version: str | None,
    current_build_date: date | None,
    latest_version: str | None = None,
    latest_build_date: date | None = None,
    *,
    max_notes: int = 20,
    max_issues: int = 10,
) -> GitHubDelta:
    """Delta between the AgentBaker releases behind two node images."""
    d = GitHubDelta(repo=REPO, from_ref=current_version, to_ref=latest_version)
    index = release_index(gh)
    if not index:
        d.error = "could not read Azure/AgentBaker releases"
        return d

    start = release_for_date(index, current_build_date)
    end = release_for_date(index, latest_build_date) or latest_release(index)
    if start is None:
        d.note = (
            f"no AgentBaker release found at or before {current_build_date}; the "
            "image predates the release history read (3 pages)"
        )
        return d
    if end is None or (end.build_date, end.revision) <= (start.build_date, start.revision):
        d.from_ref, d.to_ref = start.tag, start.tag
        d.note = "node image is current with the newest AgentBaker release"
        return d

    d.from_ref, d.to_ref = start.tag, end.tag
    d.from_date = start.effective_date.isoformat()
    d.to_date = end.effective_date.isoformat()
    d.window_resolved = True

    between = releases_between(index, start, end)
    d.releases_in_window = [r.tag for r in between]

    # AgentBaker bodies are PR-level changelogs, which is unusually good source
    # material: fix/feat/chore prefixes and CVE references are all in there.
    for r in between:
        for line in r.body.splitlines():
            s = line.strip().lstrip("#*-· ").strip()
            if len(s) < 12 or len(s) > 300:
                continue
            if BREAKING.search(s):
                entry = f"{r.tag}: {s}"
                if entry not in d.breaking_notes:
                    d.breaking_notes.append(entry)
            if len(d.breaking_notes) >= max_notes:
                break
        if len(d.breaking_notes) >= max_notes:
            break

    day_from = start.effective_date.isoformat()
    day_to = end.effective_date.isoformat()
    base = f"repo:{REPO} is:issue created:{day_from}..{day_to}"
    total = gh.search_count(f'{base} label:"bug"')
    opened = gh.search_count(f'{base} label:"bug" is:open')
    if total is None or opened is None:
        d.counts_available = False
    d.total_bug_count = total or 0
    d.open_bug_count = opened or 0
    for item in gh.search_issues(f'{base} label:"bug" is:open', limit=max_issues):
        d.bug_issues.append(
            IssueSignal(
                number=int(item.get("number") or 0),
                title=(item.get("title") or "")[:200],
                url=item.get("html_url") or "",
                state=item.get("state") or "",
                labels=[
                    l.get("name")
                    for l in (item.get("labels") or [])
                    if isinstance(l, dict) and l.get("name")
                ],
                created_at=item.get("created_at"),
                kind="pr" if item.get("pull_request") else "issue",
            )
        )

    d.note = (
        f"{len(between)} AgentBaker release(s) between {start.tag} and {end.tag}. "
        "Node images are attributed to the release cut at or before their build "
        "date, since release notes do not quote image version strings."
    )
    return d

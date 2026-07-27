"""GitHub, behind the Forge protocol.

This is an adapter, not a rewrite: the transport, disk cache, `gh` token handling
and search throttle all stay in `http.GitHub`. What moves here is the translation
from GitHub's JSON into the normalised records, so `velocity.py` and `delta.py`
stop parsing raw API payloads.

The call-count design is unchanged and worth keeping in view: one paginated walk of
`/repos/{o}/{r}/issues` replaces three search calls per window. That endpoint sits
on the core rate limit (5000/hour) rather than the search limit (~30/minute), and
each item already carries created_at, closed_at, reactions and comments -- so this
is both far fewer calls and 160x more headroom.
"""

from __future__ import annotations

from datetime import date, datetime

from ..http import GitHub as _GitHubClient
from ..models import NEGATIVE_REACTIONS, POSITIVE_REACTIONS
from . import GITHUB, Forge, IssueRecord, IssueWalk, ReleaseRecord


def _as_instant(s: object) -> datetime | None:
    """Full timestamp, UTC-normalised. Release windows compare instants."""
    from ..resolvers import parse_iso
    from datetime import timezone

    dt = parse_iso(s) if isinstance(s, str) else None
    return dt.astimezone(timezone.utc) if dt else None


def _as_date(s: object) -> date | None:
    if not isinstance(s, str) or len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


class GitHubForge(Forge):
    kind = GITHUB

    def __init__(self, client: _GitHubClient):
        self._gh = client

    # `enabled` and `queries` proxy the underlying client so the engine's stats and
    # --no-forge switch keep working against one counter.
    @property
    def enabled(self) -> bool:
        return self._gh.enabled

    @property
    def queries(self) -> int:
        return self._gh.queries

    def web_url(self, repo: str) -> str:
        return f"https://github.com/{repo}"

    def releases(self, repo: str) -> list[ReleaseRecord]:
        body = self._gh.api(f"repos/{repo}/releases?per_page=100")
        if not isinstance(body, list):
            return []
        out: list[ReleaseRecord] = []
        for r in body:
            if not isinstance(r, dict):
                continue
            tag = r.get("tag_name")
            tag = tag if isinstance(tag, str) else ""
            published = _as_instant(r.get("published_at"))
            if not tag and published is None:
                continue  # neither identifiable nor placeable in a window
            # A release with a date but no usable tag still counts toward cadence.
            # Dropping it would silently undercount how often the project ships,
            # and an undercount reads as a slowdown that never happened.
            out.append(ReleaseRecord(
                tag=tag,
                published=published,
                body=r.get("body") if isinstance(r.get("body"), str) else "",
                prerelease=bool(r.get("prerelease")),
            ))
        return out

    def issues_since(self, repo: str, since: date, *, max_pages: int = 20) -> IssueWalk:
        walk = IssueWalk(items=[], reactions_sampled=True)
        out: list[IssueRecord] = []
        seen_any = False
        oldest: date | None = None

        for page in range(1, max_pages + 1):
            body = self._gh.api(
                f"repos/{repo}/issues?state=all&since={since.isoformat()}T00:00:00Z"
                f"&sort=updated&direction=desc&per_page=100&page={page}"
            )
            if body is None or not isinstance(body, list):
                # A failure on page 1 means the repo could not be read at all,
                # which is different from a repo with no issues.
                return IssueWalk(
                    items=out if seen_any else None,
                    truncated=False, oldest_seen=oldest, reactions_sampled=True,
                )
            seen_any = True
            for it in body:
                if not isinstance(it, dict):
                    continue
                upd = _as_date(it.get("updated_at"))
                if upd and (oldest is None or upd < oldest):
                    oldest = upd
                # /issues includes pull requests; counting merged PRs as closed
                # issues would make every active project look like it was clearing
                # a backlog.
                if "pull_request" in it:
                    continue
                rx = it.get("reactions") or {}
                out.append(IssueRecord(
                    number=int(it.get("number") or 0),
                    title=(it.get("title") or "")[:200],
                    url=it.get("html_url") or "",
                    state=it.get("state") or "",
                    created=_as_date(it.get("created_at")),
                    closed=_as_date(it.get("closed_at")),
                    comments=int(it.get("comments") or 0),
                    positive=sum(int(rx.get(k) or 0) for k in POSITIVE_REACTIONS),
                    negative=sum(int(rx.get(k) or 0) for k in NEGATIVE_REACTIONS),
                    is_pr=False,
                    labels=tuple(
                        l.get("name") for l in (it.get("labels") or [])
                        if isinstance(l, dict) and l.get("name")
                    ),
                ))
            if len(body) < 100:
                # Reached the end: everything since `since` is covered.
                walk.items = out
                walk.oldest_seen = since
                return walk

        walk.items = out
        walk.truncated = True
        walk.oldest_seen = oldest
        return walk

    def issue_counts(
        self, repo: str, day_from: str, day_to: str, *, label: str = "bug"
    ) -> tuple[int | None, int | None]:
        base = f'repo:{repo} is:issue created:{day_from}..{day_to} label:"{label}"'
        # search_count returns None on a throttled/failed call rather than 0 -- a
        # throttled search once read as "this project has no issues".
        return self._gh.search_count(base), self._gh.search_count(f"{base} is:open")

    def search_issues(self, query: str, *, limit: int = 15) -> list[dict]:
        """Raw passthrough for the delta phase's issue listing (GitHub-only)."""
        return self._gh.search_issues(query, limit=limit)

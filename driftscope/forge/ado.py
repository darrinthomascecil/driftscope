"""Azure DevOps, behind the Forge protocol.

Four things differ from GitHub deeply enough to be worth stating up front, because
each one changes what the report can honestly claim:

**Work items are a two-step query.** There is no paginated `/issues`. WIQL returns
IDs only (capped at 20,000), then a batch endpoint fetches fields 200 IDs at a
time. So a window costs `1 + ceil(n/200)` calls -- cheaper than GitHub's issue walk
-- and the count comes free from step one, which is why `issue_counts` here is
always exact rather than a search estimate.

**"Closed" is process-dependent.** Agile says `Closed`, Scrum says `Done`, CMMI
says `Resolved`. Hardcoding a name list would silently undercount closures on any
project using a custom process, making every such project look like it never
resolves anything. State names are resolved through their *category* instead.

**Releases do not exist.** ADO "Releases" are deployment pipelines, not tagged
artifacts. The closest analogue is an annotated git tag, whose message stands in
for release notes. A lightweight tag has neither date nor message and is reported
as such rather than dated from its commit.

**Reactions live on comments**, one call per work item, so they are not sampled by
default -- see NOT_SAMPLED in the package docstring.

Auth reuses `source.ado_credential()`: a token already on the machine or in the
environment -- an AAD bearer token or a PAT, each sent with its own scheme,
read once per process. No PAT.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Any

from ..http import USER_AGENT, Cache
from ..source import ado_credential, parse_ado_url
from . import AZURE_DEVOPS, Forge, IssueRecord, IssueWalk, ReleaseRecord

API = "7.1"

# State categories that mean the item is off the board. Everything else (Proposed,
# InProgress, Resolved-as-in-awaiting-verification) still counts as open.
CLOSED_CATEGORIES = {"Completed", "Removed"}

WORK_ITEM_FIELDS = [
    "System.Id",
    "System.Title",
    "System.State",
    "System.WorkItemType",
    "System.CreatedDate",
    "System.ChangedDate",
    "System.Tags",
    "Microsoft.VSTS.Common.ClosedDate",
]

BATCH_MAX = 200  # the workitemsbatch endpoint's hard cap

# Work-item types that are process artifacts rather than reported problems. This is
# the ADO analogue of filtering pull requests out of GitHub's /issues feed: counting
# a Test Case as an issue would inflate the backlog with test scaffolding. Measured
# on a real Scrum project, these were 16 of 60 items -- enough to distort a trend.
NON_ISSUE_TYPES = {
    "test case", "test suite", "test plan", "shared steps", "shared parameter",
    "code review request", "code review response",
    "feedback request", "feedback response",
}


def _as_instant(s: object) -> datetime | None:
    from ..resolvers import parse_iso

    dt = parse_iso(s) if isinstance(s, str) else None
    return dt.astimezone(timezone.utc) if dt else None


def _as_date(s: object) -> date | None:
    if not isinstance(s, str) or len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


class AdoForge(Forge):
    """Reads one Azure DevOps organisation.

    `repo` strings are `org/project/repo`, which is what `Resolution.repo` carries
    when `Resolution.forge == "azure-devops"`.
    """

    kind = AZURE_DEVOPS

    def __init__(self, cache: Cache, *, offline: bool = False, enabled: bool = True,
                 timeout: float = 30.0, retries: int = 2):
        self.cache = cache
        self.offline = offline
        self.enabled = enabled
        self.queries = 0
        self.timeout = timeout
        self.retries = retries
        # Work-item type -> {state name: category}. One call per type, per run.
        self._state_cache: dict[str, dict[str, str]] = {}

    # ----------------------------------------------------------------- transport

    def _url(self, org: str, project: str | None, path: str, **params) -> str:
        from urllib.parse import quote, urlencode

        scope = f"{quote(org)}/{quote(project)}" if project else quote(org)
        q = {"api-version": API, **{k: v for k, v in params.items() if v is not None}}
        return f"https://dev.azure.com/{scope}/_apis/{path}?{urlencode(q)}"

    def _call(self, url: str, *, body: dict | None = None) -> Any:
        """GET, or POST when `body` is given. Returns None on any failure.

        None rather than an exception, and never an empty dict: a 403 on a private
        project must reach the caller as "unknown", not as "no results".
        """
        key = f"ado:{url}:{json.dumps(body, sort_keys=True) if body else ''}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if self.offline or not self.enabled:
            return self.cache.get(key, ignore_ttl=True)

        cred = ado_credential()
        if not cred:
            return None

        payload = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=payload,
                                         method="POST" if body is not None else "GET")
            req.add_header("Authorization", cred.header)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", USER_AGENT)
            if payload is not None:
                req.add_header("Content-Type", "application/json")
            try:
                self.queries += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    parsed = json.loads(resp.read().decode("utf-8", "replace"))
                self.cache.put(key, parsed)
                return parsed
            except urllib.error.HTTPError as e:
                # ADO meters in throughput units and answers with Retry-After
                # rather than a fixed per-minute budget, so honour the header
                # instead of pacing on a guess the way the GitHub search path does.
                if e.code in (429, 503) and attempt < self.retries:
                    wait = e.headers.get("Retry-After") if e.headers else None
                    try:
                        delay = float(wait) if wait else 2.0 * (attempt + 1)
                    except (TypeError, ValueError):
                        delay = 2.0 * (attempt + 1)
                    time.sleep(min(delay, 30.0))
                    continue
                return None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return None
        return None

    # ------------------------------------------------------------------- helpers

    @staticmethod
    def _split(repo: str) -> tuple[str, str, str] | None:
        """`org/project/repo`, or an ADO URL, into its three parts."""
        ref = parse_ado_url(repo)
        if ref:
            return ref.org, ref.project, ref.repo
        parts = [p for p in (repo or "").split("/") if p]
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:  # org/project, repo shares the project name
            return parts[0], parts[1], parts[1]
        return None

    def web_url(self, repo: str) -> str:
        parts = self._split(repo)
        if not parts:
            return ""
        org, project, name = parts
        return f"https://dev.azure.com/{org}/{project}/_git/{name}"

    def _state_categories(self, org: str, project: str, wi_type: str) -> dict[str, str]:
        """{state name: category} for one work-item type, cached for the run.

        Necessary because "Closed" is spelled differently per process template, and
        a hardcoded list would undercount closures on custom processes.
        """
        key = f"{org}/{project}/{wi_type}"
        if key in self._state_cache:
            return self._state_cache[key]
        from urllib.parse import quote

        body = self._call(self._url(
            org, project, f"wit/workitemtypes/{quote(wi_type)}/states"))
        mapping: dict[str, str] = {}
        for s in (body or {}).get("value", []) if isinstance(body, dict) else []:
            if isinstance(s, dict) and s.get("name"):
                mapping[s["name"]] = s.get("category") or ""
        self._state_cache[key] = mapping
        return mapping

    def _is_closed(self, org: str, project: str, fields: dict) -> bool:
        if fields.get("Microsoft.VSTS.Common.ClosedDate"):
            return True
        wi_type = fields.get("System.WorkItemType") or ""
        state = fields.get("System.State") or ""
        if not wi_type or not state:
            return False
        return self._state_categories(org, project, wi_type).get(state) in CLOSED_CATEGORIES

    def _query_ids(self, org: str, project: str, wiql: str) -> list[int] | None:
        body = self._call(self._url(org, project, "wit/wiql"), body={"query": wiql})
        if not isinstance(body, dict):
            return None
        items = body.get("workItems")
        if not isinstance(items, list):
            return None
        return [int(w["id"]) for w in items if isinstance(w, dict) and w.get("id")]

    def _fetch_batch(self, org: str, project: str, ids: list[int]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), BATCH_MAX):
            chunk = ids[i:i + BATCH_MAX]
            body = self._call(
                self._url(org, project, "wit/workitemsbatch"),
                body={"ids": chunk, "fields": WORK_ITEM_FIELDS},
            )
            if not isinstance(body, dict):
                continue
            out.extend(w for w in body.get("value", []) if isinstance(w, dict))
        return out

    # -------------------------------------------------------------------- Forge

    def releases(self, repo: str) -> list[ReleaseRecord]:
        """Git tags. Annotated ones carry a date and message; lightweight ones do
        not, and are returned with `published=None` rather than dated from their
        commit."""
        parts = self._split(repo)
        if not parts:
            return []
        org, project, name = parts
        from urllib.parse import quote

        body = self._call(self._url(
            org, project, f"git/repositories/{quote(name)}/refs", filter="tags"))
        if not isinstance(body, dict):
            return []

        out: list[ReleaseRecord] = []
        for ref in body.get("value", []):
            if not isinstance(ref, dict):
                continue
            tag = (ref.get("name") or "").removeprefix("refs/tags/")
            if not tag:
                continue
            oid = ref.get("objectId")
            published, message = None, ""
            if oid:
                ann = self._call(self._url(
                    org, project,
                    f"git/repositories/{quote(name)}/annotatedtags/{oid}"))
                if isinstance(ann, dict):
                    published = _as_instant((ann.get("taggedBy") or {}).get("date"))
                    message = ann.get("message") or ""
            out.append(ReleaseRecord(tag=tag, published=published, body=message))
        return out

    def issues_since(self, repo: str, since: date, *, max_pages: int = 20) -> IssueWalk:
        """Work items created or changed since `since`.

        `max_pages` is accepted for protocol compatibility but not needed: WIQL
        returns the complete ID set in one call, so the result is never truncated
        the way GitHub's paginated walk can be.
        """
        parts = self._split(repo)
        if not parts:
            return IssueWalk(items=None)
        org, project, _ = parts

        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{project}' "
            f"AND [System.ChangedDate] >= '{since.isoformat()}' "
            "ORDER BY [System.ChangedDate] DESC"
        )
        ids = self._query_ids(org, project, wiql)
        if ids is None:
            # Unreadable: missing project, no permission, or the call failed.
            return IssueWalk(items=None)
        if not ids:
            return IssueWalk(items=[], oldest_seen=since)

        org_url = f"https://dev.azure.com/{org}/{project}/_workitems/edit"
        out: list[IssueRecord] = []
        oldest: date | None = None
        skipped = 0
        for w in self._fetch_batch(org, project, ids):
            f = w.get("fields") or {}
            if (f.get("System.WorkItemType") or "").lower() in NON_ISSUE_TYPES:
                skipped += 1
                continue
            changed = _as_date(f.get("System.ChangedDate"))
            if changed and (oldest is None or changed < oldest):
                oldest = changed
            created = _as_date(f.get("System.CreatedDate"))
            closed = _as_date(f.get("Microsoft.VSTS.Common.ClosedDate"))
            if closed is None and self._is_closed(org, project, f):
                # Closed by state category but with no ClosedDate field (Basic
                # process). ChangedDate is the best available proxy, and is only
                # used to place the closure in a window.
                closed = changed
            tags = tuple(
                t.strip() for t in (f.get("System.Tags") or "").split(";") if t.strip()
            )
            wid = int(w.get("id") or 0)
            out.append(IssueRecord(
                number=wid,
                title=(f.get("System.Title") or "")[:200],
                url=f"{org_url}/{wid}",
                state=f.get("System.State") or "",
                created=created,
                closed=closed,
                comments=0,
                # Reactions are per-comment on ADO -- one extra call per work item
                # -- so they are left unsampled and the sentinel says so.
                is_pr=False,
                labels=tags,
            ))

        notes = ["reactions were not sampled for this Azure DevOps project "
                 "(they live on work-item comments, one API call per item); "
                 "sentiment not measured"]
        if skipped:
            notes.append(
                f"{skipped} test/review artifact(s) excluded from the issue counts"
            )
        return IssueWalk(
            items=out,
            truncated=False,
            oldest_seen=oldest or since,
            reactions_sampled=False,
            notes=notes,
        )

    def issue_counts(
        self, repo: str, day_from: str, day_to: str, *, labels=None
    ) -> tuple[int | None, int | None]:
        """Exact counts -- WIQL returns the full ID set, so there is no estimate.

        `labels` is accepted for protocol compatibility and ignored: Azure DevOps
        models a defect as a work-item *type*, not a label, so `Bug` is the direct
        and unambiguous analogue. There is no per-project naming drift to defend
        against the way there is on GitHub.
        """
        parts = self._split(repo)
        if not parts:
            return None, None
        org, project, _ = parts

        base = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{project}' "
            "AND [System.WorkItemType] = 'Bug' "
            f"AND [System.CreatedDate] >= '{day_from}' "
            f"AND [System.CreatedDate] <= '{day_to}'"
        )
        total_ids = self._query_ids(org, project, base)
        if total_ids is None:
            return None, None
        if not total_ids:
            return 0, 0

        open_count = 0
        for w in self._fetch_batch(org, project, total_ids):
            if not self._is_closed(org, project, w.get("fields") or {}):
                open_count += 1
        return len(total_ids), open_count

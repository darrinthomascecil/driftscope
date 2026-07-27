"""The forge seam: where a component's source and its bug history live.

`ghub/delta.py` and `ghub/velocity.py` used to build GitHub REST paths directly, so
"which project is this" and "how do I ask GitHub about it" were the same question.
Azure DevOps answers the same questions with a different shape -- work items instead
of issues, a two-step WIQL query instead of a paginated list, annotated git tags
instead of releases -- so the seam has to sit above the transport.

It deliberately does **not** sit at `api(path)`. Abstracting there would mean ADO
pretending to serve GitHub paths. It sits at the semantic operations, and the
callers stop touching raw JSON.

## The one invariant

Every count here distinguishes *measured zero* from *not measured*. `None` means
the call failed or was not made; `-1` on a reaction count means "not sampled".
Neither is ever silently coerced to `0`.

That is not fastidiousness -- it is this codebase's most-repeated bug. A
rate-limited GitHub search returning `0` read as "this project has no issues"; an
OSV query matching nothing read as "this image is clean"; a truncated Docker Hub
listing read as "node:14 is 13 days behind". Each shipped a confident wrong answer
from data that did not support it. A forge that cannot measure something must say
so, and the type system is where that gets enforced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, runtime_checkable

GITHUB = "github"
AZURE_DEVOPS = "azure-devops"

# Sentinel for a reaction count that was never sampled. Azure DevOps keeps
# reactions on work-item *comments*, one API call per item, so a velocity run does
# not gather them by default -- and "no reactions found" must stay distinguishable
# from "we did not look".
NOT_SAMPLED = -1


@dataclass(frozen=True)
class IssueRecord:
    """A GitHub issue or an Azure DevOps work item, normalised.

    `positive`/`negative` are NOT_SAMPLED when reactions were not gathered. Callers
    must check before summing: treating -1 as a count would make an unsampled
    window look like it had negative sentiment.
    """

    number: int
    title: str
    url: str
    state: str
    created: date | None
    closed: date | None
    comments: int = 0
    positive: int = NOT_SAMPLED
    negative: int = NOT_SAMPLED
    is_pr: bool = False
    labels: tuple[str, ...] = ()

    @property
    def reactions_sampled(self) -> bool:
        return self.positive != NOT_SAMPLED and self.negative != NOT_SAMPLED


@dataclass(frozen=True)
class ReleaseRecord:
    """A GitHub release, or an Azure DevOps git tag.

    `published` is a full instant, not a date: two releases can share a day, and a
    version window whose bound falls at midday must place them correctly. Velocity
    narrows it to a date itself, where 90-day windows make time-of-day irrelevant.

    It is None for a lightweight git tag, which carries neither a date nor
    a message. Such a tag still counts toward release *cadence* but must not be
    dated, or "days behind" would quietly mean something different per tag.

    `body` is "" when the forge has no release-notes concept. An ADO annotated tag
    message is the closest analogue and is usually far thinner than a GitHub release
    body, so a sparse breaking-change list on ADO is not evidence of no breaking
    changes.
    """

    tag: str
    published: datetime | None = None
    body: str = ""
    prerelease: bool = False


@dataclass
class IssueWalk:
    """The result of asking a forge for issues since a date.

    `items is None` means the project could not be read at all -- missing, private,
    or the call failed. That is different from a project with no issues, and the
    velocity engine reports the two differently.
    """

    items: list[IssueRecord] | None = None
    truncated: bool = False
    oldest_seen: date | None = None
    reactions_sampled: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def readable(self) -> bool:
        return self.items is not None


@runtime_checkable
class Forge(Protocol):
    """What driftscope needs from a source-and-issues host."""

    kind: str
    enabled: bool
    queries: int

    def releases(self, repo: str) -> list[ReleaseRecord]:
        """Releases or tags, newest-first order not guaranteed. Empty on failure."""
        ...

    def issues_since(self, repo: str, since: date, *, max_pages: int = 20) -> IssueWalk:
        """Every issue opened or closed since `since`, pull requests excluded."""
        ...

    def issue_counts(
        self, repo: str, day_from: str, day_to: str, *, label: str = "bug"
    ) -> tuple[int | None, int | None]:
        """(total, still_open) bug-labelled issues created in the window.

        `(None, None)` when the query failed -- never `(0, 0)`, which would read as
        a clean project.
        """
        ...

    def web_url(self, repo: str) -> str:
        """Browser URL for the project."""
        ...


def resolve_forge(kind: str | None, forges: dict[str, Forge]) -> Forge | None:
    """Pick the client for a component, defaulting to GitHub.

    Resolutions carry `forge="github"` unless a resolver says otherwise, so the
    ~20 existing `repo=` assignments across `resolvers/` need no edits.
    """
    return forges.get(kind or GITHUB)

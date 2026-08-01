"""Fleet-aggregation data model.

The unit that flows through the system is an *inventory record*: a projection of one
repo's latest driftscope report down to what governance needs -- which components it
uses, at what versions, with the deprecation signals the scan already computed. The
evaluator turns a record + a sanctioned-version source into a Verdict.

See docs/design-org-aggregation.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

# Violation kinds.
UNSANCTIONED_UPGRADE = "unsanctioned-upgrade"  # repo version is AHEAD of what's sanctioned
BEHIND = "behind"                              # repo version is BELOW the sanctioned floor
DEPRECATED = "deprecated"                       # upstream marks the used version deprecated/EOL
RISK = "risk"                                   # high-severity drift (stale, at-risk)

PASS = "pass"
BLOCK = "block"

# Where a sanctioned version came from (for explainability + confidence).
SRC_CURATED = "curated"
SRC_BASELINE = "baseline"


@dataclass
class ComponentUse:
    """One component as a repo actually uses it (projected from a finding)."""

    ecosystem: str
    name: str
    version: str | None            # component.current -- the version in use (resolved pin)
    severity: str | None = None    # the scan's drift severity (critical/high/...)
    deprecated: bool = False        # resolution.deprecated
    latest_stable: str | None = None

    @property
    def key(self) -> str:
        return f"{self.ecosystem}:{self.name}"


@dataclass
class InventoryRecord:
    """One repo's latest shipped state, per tenant. The latest-wins unit of the store."""

    tenant: str
    repo: str                       # source.remote -- the identity we key latest-wins on
    components: list[ComponentUse] = field(default_factory=list)
    ref: str | None = None
    commit: str | None = None
    release: str | None = None      # future artifact field; provenance = "what shipped"
    generated_at: str | None = None  # freshness for latest-wins

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InventoryRecord":
        comps = [ComponentUse(**c) for c in (d.get("components") or [])]
        return cls(
            tenant=d["tenant"], repo=d["repo"], components=comps,
            ref=d.get("ref"), commit=d.get("commit"), release=d.get("release"),
            generated_at=d.get("generated_at"),
        )


@dataclass
class Violation:
    kind: str                       # one of the *_ constants above
    component: str                  # ecosystem:name
    current: str | None             # version in use
    sanctioned: str | None = None   # what policy/baseline expected (None for deprecation-only)
    source: str | None = None       # curated | baseline (for conformance violations)
    detail: str = ""


@dataclass
class Verdict:
    tenant: str
    repo: str
    release: str | None
    status: str                     # pass | block
    violations: list[Violation] = field(default_factory=list)
    evaluated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

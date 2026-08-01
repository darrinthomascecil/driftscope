"""Policy: where the *sanctioned version* comes from.

A pluggable source with two composable providers (docs/design-org-aggregation.md):

  * curated  -- a hand-authored allowlist {ecosystem:name -> version}. Explicit control.
  * baseline -- the de-facto standard learned from THIS tenant's fleet (modal version,
                gated by min-support so a thin fleet doesn't produce noise).

Composite precedence: curated wins where present; baseline fills the rest. A component
neither curated nor with a confident baseline is *ungoverned* -> no conformance verdict.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .models import SRC_BASELINE, SRC_CURATED, InventoryRecord


@dataclass
class BaselineConfig:
    enabled: bool = False
    min_repos: int = 3       # need at least this many repos on a version to trust it
    min_share: float = 0.6   # ...and a real majority (not a 50/50 tie) of users


@dataclass
class Policy:
    curated: dict[str, str] = field(default_factory=dict)      # "eco:name" -> version
    baseline: BaselineConfig = field(default_factory=BaselineConfig)
    block_on: tuple[str, ...] = ("unsanctioned-upgrade",)       # violation kinds that -> block

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        d = json.loads(Path(path).read_text())
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        b = d.get("baseline") or {}
        return cls(
            curated=dict(d.get("curated") or {}),
            baseline=BaselineConfig(
                enabled=bool(b.get("enabled", False)),
                min_repos=int(b.get("min_repos", 3)),
                min_share=float(b.get("min_share", 0.5)),
            ),
            block_on=tuple(d.get("block_on") or ("unsanctioned-upgrade",)),
        )


def fleet_baseline(records: list[InventoryRecord], cfg: BaselineConfig) -> dict[str, str]:
    """The modal version per component across the fleet, kept only when it clears support.

    Support gating is the whole point: with three repos split 1/1/1 there is no baseline,
    so we emit nothing rather than flag two of them as outliers of a coin-flip 'standard'.
    """
    if not cfg.enabled:
        return {}
    versions: dict[str, Counter] = {}
    for rec in records:
        for c in rec.components:
            if c.version:
                versions.setdefault(c.key, Counter())[c.version] += 1
    out: dict[str, str] = {}
    for key, counts in versions.items():
        ranked = counts.most_common()
        version, n = ranked[0]
        total = sum(counts.values())
        # Reject a tie for the top spot: a 3-vs-3 split has no "standard," and picking one
        # (by arbitrary hash order) would flag half the fleet as outliers of a coin flip.
        tied = len(ranked) > 1 and ranked[1][1] == n
        if not tied and n >= cfg.min_repos and (n / total) >= cfg.min_share:
            out[key] = version
    return out


class SanctionedSource:
    """Composite: curated overrides baseline. Returns (version, provenance) or (None, None)."""

    def __init__(self, policy: Policy, baseline_map: dict[str, str] | None = None):
        self.policy = policy
        self.baseline_map = baseline_map or {}

    def sanctioned(self, component_key: str) -> tuple[str | None, str | None]:
        if component_key in self.policy.curated:
            return self.policy.curated[component_key], SRC_CURATED
        if component_key in self.baseline_map:
            return self.baseline_map[component_key], SRC_BASELINE
        return None, None

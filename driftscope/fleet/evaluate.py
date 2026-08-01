"""The evaluation engine + the service that ties store + policy together.

Two independent signals per component (docs/design-org-aggregation.md):

  1. Conformance -- compare the version in use to the sanctioned version. Ahead of it is an
     UNSANCTIONED_UPGRADE ("you moved off the pin"); below it is BEHIND. Needs a policy.
  2. Risk -- from the scan's own output: a deprecated version, or a high-severity drift.
     Needs no policy at all, so it works from day one.

status = block if any violation's kind is in policy.block_on, else pass.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import (
    BEHIND,
    BLOCK,
    DEPRECATED,
    PASS,
    RISK,
    UNSANCTIONED_UPGRADE,
    InventoryRecord,
    Verdict,
    Violation,
)
from .policy import Policy, SanctionedSource, fleet_baseline
from .store import Store


def _cmp(current: str | None, sanctioned: str | None, *, hint: str | None) -> int | None:
    """Sign of (current - sanctioned) via driftscope's own version parser, or None when
    either side can't be parsed (a ranged/opaque pin) -- in which case we make no claim
    rather than risk a false positive."""
    if not current or not sanctioned:
        return None
    try:
        from ..version import compare, parse

        return compare(parse(current, hint=hint), parse(sanctioned, hint=hint))
    except Exception:
        return None


def evaluate_record(
    rec: InventoryRecord | None, source: SanctionedSource, policy: Policy,
    *, now: str | None = None,
) -> Verdict:
    if rec is None:
        return Verdict(tenant="", repo="", release=None, status=PASS)
    violations: list[Violation] = []
    for c in rec.components:
        # ---- signal 2: risk (policy-free) ----
        if c.deprecated:
            violations.append(Violation(
                kind=DEPRECATED, component=c.key, current=c.version,
                detail="upstream marks this version deprecated"))
        elif c.severity in ("critical", "high"):
            violations.append(Violation(
                kind=RISK, component=c.key, current=c.version,
                detail=f"{c.severity}-severity drift (stale/at-risk)"))

        # ---- signal 1: conformance (needs a sanctioned version) ----
        sv, src = source.sanctioned(c.key)
        if sv is None:
            continue  # ungoverned component
        cmp = _cmp(c.version, sv, hint=c.ecosystem)
        if cmp is None or cmp == 0:
            continue  # conform, or unparseable -> no claim
        kind = UNSANCTIONED_UPGRADE if cmp > 0 else BEHIND
        arrow = "ahead of" if cmp > 0 else "below"
        violations.append(Violation(
            kind=kind, component=c.key, current=c.version, sanctioned=sv, source=src,
            detail=f"{c.version} is {arrow} the {src} version {sv}"))

    status = BLOCK if any(v.kind in policy.block_on for v in violations) else PASS
    return Verdict(
        tenant=rec.tenant, repo=rec.repo, release=rec.release, status=status,
        violations=violations,
        evaluated_at=now or datetime.now(timezone.utc).isoformat())


class FleetService:
    """Ingest -> evaluate -> store a verdict; and answer verdict/inventory queries.

    Evaluation is synchronous on ingest here, so a later GET verdict always has an answer.
    At production scale the resolution is heavier and this becomes an async job, but the
    surface is the same.
    """

    def __init__(self, store: Store, policy: Policy):
        self.store = store
        self.policy = policy

    def _source(self, tenant: str) -> SanctionedSource:
        base = fleet_baseline(self.store.records(tenant), self.policy.baseline)
        return SanctionedSource(self.policy, base)

    def ingest(self, tenant: str, report: dict, *, repo: str | None = None) -> Verdict:
        rec = self.store.ingest(tenant, report, repo=repo)
        verdict = evaluate_record(rec, self._source(tenant), self.policy)
        self.store.save_verdict(verdict)
        return verdict

    def reevaluate(self, tenant: str, repo: str) -> Verdict:
        """Re-run a repo's verdict against the CURRENT fleet baseline (which shifts as other
        repos ship). The gate reads the stored verdict; this refreshes it."""
        verdict = evaluate_record(self.store.record(tenant, repo), self._source(tenant), self.policy)
        self.store.save_verdict(verdict)
        return verdict

    def verdict(self, tenant: str, repo: str) -> dict | None:
        return self.store.get_verdict(tenant, repo)

    def uses(self, tenant: str, component_key: str) -> list[tuple[str, str | None]]:
        return self.store.uses(tenant, component_key)

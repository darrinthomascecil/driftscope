"""Fleet aggregation: turn per-repo driftscope reports into org-wide governance.

Each CI release emits a report; this package ingests them per tenant, evaluates each
against a pluggable sanctioned-version source (curated + fleet baseline) plus the scan's
own deprecation signals, and produces a Verdict a CD gate can enforce.

See docs/design-org-aggregation.md.
"""

from __future__ import annotations

from .evaluate import FleetService, evaluate_record
from .models import InventoryRecord, Verdict, Violation
from .policy import Policy, SanctionedSource, fleet_baseline
from .store import Store

__all__ = [
    "FleetService", "evaluate_record", "Store", "Policy", "SanctionedSource",
    "fleet_baseline", "InventoryRecord", "Verdict", "Violation",
]

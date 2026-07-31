"""Structured audit logging for the scan service.

A product that clones and reads customers' PRIVATE repositories must be able to answer *who
scanned which repo, when, under which app, and was it allowed* -- for incident response and for
compliance (SOC 2). This emits one structured JSON event per scan request, covering both the
scans that ran AND the ones that were denied (an audit trail is mostly about the denials).

Where it goes: **stdout, one JSON line per event**, for the platform's log pipeline (Azure
Monitor / Loki) to capture, retain, and make immutable. The audited service deliberately does NOT
own its own tamper-proof store -- separating "who did the thing" from "who keeps the record" is
the whole point of an audit trail. `set_sink` swaps the destination (tests, or a future forwarder).

What it never logs: bearer tokens, secrets, or repository CONTENTS. The caller is identified by an
opaque subject (Entra `oid`), never an email; the repo URL is stripped of any `user@` userinfo.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

_sink = sys.stdout


def set_sink(stream) -> None:
    """Redirect audit output (tests, or a future forwarder). Default is stdout."""
    global _sink
    _sink = stream


def emit(event: str, **fields) -> None:
    """Write one audit event as a JSON line. Never raises into the request path -- a dropped
    audit line is strictly preferable to turning a scan into a 500."""
    record = {"ts": datetime.now(timezone.utc).isoformat(), "audit": True, "event": event}
    record.update({k: v for k, v in fields.items() if v is not None})
    try:
        _sink.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        _sink.flush()
    except Exception:  # noqa: BLE001 -- auditing must never break a scan
        pass


def caller_fields(caller) -> dict:
    """The safe subset of an `authz.Caller` for the record -- opaque subject + roles, never the
    token. `None` means authorization is disabled (single-tenant); an unauthenticated caller is
    recorded as `anonymous` so a denied, tokenless probe is still attributable to 'no identity'."""
    if caller is None:
        return {"caller": "authz-disabled", "authenticated": None}
    return {
        "caller": getattr(caller, "subject", None) or (None if caller.ok else "anonymous"),
        "authenticated": bool(caller.ok),
        "roles": sorted(caller.roles) or None,
        "tenant": getattr(caller, "tenant", None),
    }

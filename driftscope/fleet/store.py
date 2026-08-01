"""Per-tenant inventory store: ingest report.json, keep the latest per repo.

File-backed and stdlib-only -- this proves the *logic*, not the *scale*. The production
store is a per-tenant indexed DB so "who uses log4j < 2.17" answers in seconds over 10k
repos (see docs/design-org-aggregation.md); the interface here is what that would grow into.

Layout:  <root>/<tenant>/inventory/<repo-hash>.json   (latest record per repo)
         <root>/<tenant>/verdicts/<repo-hash>.json     (last verdict per repo)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import ComponentUse, InventoryRecord, Verdict


def _project(report: dict, tenant: str, repo: str | None = None) -> InventoryRecord:
    """Project a full report.json down to the governance inventory record.

    Uses only fields the scan already produced -- no re-resolution. `component.current`
    is the version in use; `resolution.deprecated`/`severity` carry the risk signals.

    A repo identity is REQUIRED (explicit `repo`, else `source.remote`). We fail closed
    rather than fall back to a shared "unknown" key -- two identity-less reports must never
    collapse onto one record and silently overwrite each other.
    """
    src = report.get("source") or {}
    repo = repo or src.get("remote")
    if not repo:
        raise ValueError(
            "report has no repo identity: pass repo=, or set source.remote in the report")
    comps: list[ComponentUse] = []
    for f in report.get("findings") or []:
        c = f.get("component") or {}
        r = f.get("resolution") or {}
        eco, name = c.get("ecosystem"), c.get("name")
        if not eco or not name:
            continue  # not a governable component
        comps.append(ComponentUse(
            ecosystem=eco, name=name, version=c.get("current"),
            severity=f.get("severity"), deprecated=bool(r.get("deprecated")),
            latest_stable=r.get("latest_stable"),
        ))
    return InventoryRecord(
        tenant=tenant,
        repo=repo,
        components=comps,
        ref=src.get("ref"), commit=src.get("commit"),
        release=src.get("release"),  # optional future field
        generated_at=report.get("generated_at"),
    )


def _newer(a: str | None, b: str | None) -> bool:
    """Is `a` at least as recent as `b`? ISO-8601 strings sort chronologically; a missing
    timestamp is treated as oldest so a dated record always supersedes an undated one."""
    return (a or "") >= (b or "")


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _hash(self, repo: str) -> str:
        return hashlib.sha256(repo.encode()).hexdigest()[:16]

    def _inv_dir(self, tenant: str) -> Path:
        return self.root / tenant / "inventory"

    def _ver_dir(self, tenant: str) -> Path:
        return self.root / tenant / "verdicts"

    def ingest(self, tenant: str, report: dict, *, repo: str | None = None) -> InventoryRecord:
        """Store a report's projection as this repo's latest -- unless an equal-or-newer
        record already exists (latest-wins keeps the store to one record per repo)."""
        rec = _project(report, tenant, repo)
        d = self._inv_dir(tenant)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self._hash(rec.repo)}.json"
        if path.exists():
            prev = InventoryRecord.from_dict(json.loads(path.read_text()))
            if _newer(prev.generated_at, rec.generated_at):
                return prev  # incoming is older -- keep the newer one
        path.write_text(json.dumps(rec.to_dict(), indent=2))
        return rec

    def records(self, tenant: str) -> list[InventoryRecord]:
        d = self._inv_dir(tenant)
        if not d.is_dir():
            return []
        return [InventoryRecord.from_dict(json.loads(p.read_text()))
                for p in sorted(d.glob("*.json"))]

    def record(self, tenant: str, repo: str) -> InventoryRecord | None:
        path = self._inv_dir(tenant) / f"{self._hash(repo)}.json"
        if not path.exists():
            return None
        return InventoryRecord.from_dict(json.loads(path.read_text()))

    def save_verdict(self, v: Verdict) -> None:
        d = self._ver_dir(v.tenant)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self._hash(v.repo)}.json").write_text(json.dumps(v.to_dict(), indent=2))

    def get_verdict(self, tenant: str, repo: str) -> dict[str, Any] | None:
        path = self._ver_dir(tenant) / f"{self._hash(repo)}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def uses(self, tenant: str, component_key: str) -> list[tuple[str, str | None]]:
        """Fleet query: (repo, version) for every repo using a component -- the blast-radius
        primitive behind "who's on log4j < 2.17"."""
        out = []
        for rec in self.records(tenant):
            for c in rec.components:
                if c.key == component_key:
                    out.append((rec.repo, c.version))
        return out

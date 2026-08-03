"""Fleet rollup: a read-side cross-repo summary over the ingested inventory.

`store` keeps the governance inventory (latest-per-repo, feeding baseline + verdicts). This is
the reporting projection the CLI prints -- three views over the same records:

  * adoption      -- how many distinct repos use each component
  * fragmentation -- which repos/branches run which version (the upgrade backlog)
  * api-versions  -- the unique Azure/K8s api-versions in use across the fleet

Pure aggregation over `InventoryRecord`s (`fleet/models`); no scanning or network. It reads the
report.json files each CI run emits (one per repo/branch) and projects each with `store.project`,
so it shares the exact ingest the fleet service uses -- no parallel data model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import AZURE_API, K8S_API
from .models import InventoryRecord
from .store import project

# Ecosystems whose "version" is an API version, not a package release -- the api-versions view.
API_ECOSYSTEMS = frozenset({AZURE_API, K8S_API})
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Use:
    """One (repo, branch) running one version of a component."""

    repo: str
    ref: str | None
    version: str | None
    deprecated: bool = False

    @property
    def where(self) -> str:
        return f"{self.repo}@{self.ref}" if self.ref else self.repo


@dataclass
class ComponentRollup:
    ecosystem: str
    name: str
    uses: list[Use] = field(default_factory=list)

    @property
    def is_api(self) -> bool:
        return self.ecosystem in API_ECOSYSTEMS

    @property
    def repos(self) -> set[str]:
        return {u.repo for u in self.uses}

    @property
    def repo_count(self) -> int:
        return len(self.repos)

    @property
    def by_version(self) -> dict[str, list[Use]]:
        """version -> the uses on it (which repos/branches), versions high-to-low."""
        out: dict[str, list[Use]] = {}
        for u in self.uses:
            out.setdefault(u.version or UNKNOWN, []).append(u)
        return {v: out[v] for v in _sorted_versions(out)}

    @property
    def unique_versions(self) -> list[str]:
        return _sorted_versions({u.version or UNKNOWN for u in self.uses})

    @property
    def fragmented(self) -> bool:
        return len(self.unique_versions) > 1

    @property
    def any_deprecated(self) -> bool:
        return any(u.deprecated for u in self.uses)


@dataclass
class Rollup:
    components: list[ComponentRollup]
    record_count: int = 0
    skipped: int = 0

    @property
    def repos(self) -> set[str]:
        out: set[str] = set()
        for c in self.components:
            out |= c.repos
        return out

    @property
    def apis(self) -> list[ComponentRollup]:
        return [c for c in self.components if c.is_api]

    @property
    def libraries(self) -> list[ComponentRollup]:
        return [c for c in self.components if not c.is_api]


# ------------------------------------------------------------------ build


def _sorted_versions(versions) -> list[str]:
    """Best-effort high-to-low version sort; UNKNOWN sinks to the end."""
    from ..version import normalize

    def key(v: str):
        if v == UNKNOWN:
            return (1, ())
        nums = tuple(int(p) if p.isdigit() else -1 for p in normalize(v).split("."))
        return (0, tuple(-n for n in nums))  # negate for high-to-low

    return sorted(set(versions), key=key)


def records_from_dir(directory) -> tuple[list[InventoryRecord], int]:
    """Read the scan report JSON files in a directory (one per repo/branch) and project each to an
    InventoryRecord via the fleet ingest. Skips unreadable files and reports with no repo identity
    (no `source.remote` -- run `scan` with provenance / --source-repo). Returns (records, skipped)."""
    directory = Path(directory).expanduser()
    records: list[InventoryRecord] = []
    skipped = 0
    for p in sorted(directory.glob("*.json")):
        try:
            body = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue
        if not (isinstance(body, dict) and "findings" in body):
            skipped += 1
            continue
        try:
            records.append(project(body, tenant="cli"))
        except ValueError:
            skipped += 1  # no repo identity -- fail closed rather than collapse onto "unknown"
    return records, skipped


def rollup(records: list[InventoryRecord], *, skipped: int = 0) -> Rollup:
    groups: dict[tuple[str, str], ComponentRollup] = {}
    for rec in records:
        for c in rec.components:
            k = (c.ecosystem, c.name.lower())
            g = groups.get(k)
            if g is None:
                g = groups[k] = ComponentRollup(ecosystem=c.ecosystem, name=c.name)
            g.uses.append(Use(repo=rec.repo, ref=rec.ref, version=c.version, deprecated=c.deprecated))
    components = sorted(
        groups.values(),
        key=lambda c: (-c.repo_count, -len(c.uses), c.ecosystem, c.name.lower()),
    )
    return Rollup(components=components, record_count=len(records), skipped=skipped)


# --------------------------------------------------------------- rendering


def to_dict(r: Rollup) -> dict[str, Any]:
    def comp(c: ComponentRollup) -> dict[str, Any]:
        return {
            "ecosystem": c.ecosystem,
            "name": c.name,
            "is_api": c.is_api,
            "repo_count": c.repo_count,
            "unique_versions": c.unique_versions,
            "fragmented": c.fragmented,
            "deprecated": c.any_deprecated,
            "versions": {
                v: [{"repo": u.repo, "ref": u.ref} for u in us]
                for v, us in c.by_version.items()
            },
        }

    return {
        "record_count": r.record_count,
        "skipped_reports": r.skipped,
        "repo_count": len(r.repos),
        "package_count": len(r.libraries),
        "api_count": len(r.apis),
        "components": [comp(c) for c in r.components],
    }


def to_json(r: Rollup) -> str:
    return json.dumps(to_dict(r), indent=2)


def to_markdown(r: Rollup) -> str:
    L: list[str] = ["# driftscope estate rollup", ""]
    L.append(
        f"Aggregated **{r.record_count}** report(s) across **{len(r.repos)}** repo(s): "
        f"{len(r.libraries)} package(s), {len(r.apis)} API(s)."
        + (f" ({r.skipped} skipped.)" if r.skipped else "")
    )
    L.append("")

    L += ["## Package adoption", ""]
    if not r.libraries:
        L.append("_No package findings._")
    else:
        L += ["| Package | Ecosystem | Repos | Versions in use |", "|---|---|--:|---|"]
        for c in r.libraries:
            L.append(f"| `{c.name}` | {c.ecosystem} | {c.repo_count} | {len(c.unique_versions)} |")
    L.append("")

    frag = [c for c in r.libraries if c.fragmented and c.repo_count > 1]
    L += ["## Version fragmentation", "",
          "_Packages the estate runs on more than one version — the upgrade backlog._", ""]
    if not frag:
        L.append("_None — every package is on a single version across the estate._")
    else:
        for c in frag:
            L.append(f"### `{c.name}` ({c.ecosystem}) — {len(c.unique_versions)} versions, {c.repo_count} repos")
            L += ["", "| Version | Repos / branches |", "|---|---|"]
            for v, us in c.by_version.items():
                L.append(f"| `{v}` | {', '.join(sorted(u.where for u in us))} |")
            L.append("")

    L += ["## API versions in use", ""]
    if not r.apis:
        L.append("_No API findings._")
    else:
        L += ["| API | Ecosystem | Repos | Unique api-versions |", "|---|---|--:|---|"]
        for c in r.apis:
            vers = ", ".join(f"`{v}`" for v in c.unique_versions)
            L.append(f"| `{c.name}` | {c.ecosystem} | {c.repo_count} | {vers} |")
    L.append("")
    return "\n".join(L) + "\n"


def to_text(r: Rollup) -> str:
    lines = [
        f"estate rollup: {r.record_count} report(s), {len(r.repos)} repo(s), "
        f"{len(r.libraries)} package(s), {len(r.apis)} API(s)"
    ]
    frag = [c for c in r.libraries if c.fragmented and c.repo_count > 1]
    if frag:
        lines.append(f"fragmented (multiple versions across repos): {len(frag)}")
        for c in frag[:15]:
            lines.append(f"  {c.name:40} {c.repo_count:>3} repos  {', '.join(c.unique_versions)}")
        if len(frag) > 15:
            lines.append(f"  ...{len(frag) - 15} more")
    return "\n".join(lines) + "\n"

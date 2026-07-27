"""Estate history: persist runs and diff them.

Upstream velocity answers "which way is the dependency moving". This answers the
other half: **which way are we moving** -- is the estate catching up or falling
behind. A single scan cannot say; two can.

Findings are keyed by `(ecosystem, name)` rather than by version, so a component
that moved 2.0 -> 3.0 is recognised as the same thing improving rather than as one
finding vanishing and another appearing. Location is deliberately not part of the
key: moving a dependency between files is not a change in drift.

Runs are stored as the ordinary JSON report, one file per run, so the history
directory is inspectable with `jq` and needs no migration story.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import severity_rank

RUN_RE = re.compile(r"^\d{8}T\d{6}Z\.json$")


def _key(finding: dict) -> tuple[str, str]:
    c = finding.get("component") or {}
    return (c.get("ecosystem") or "", (c.get("name") or "").lower())


def _sev(finding: dict) -> str:
    return finding.get("severity") or "unknown"


def _index(report: dict) -> dict[tuple[str, str], dict]:
    """Key findings, keeping the worst when a name appears more than once."""
    out: dict[tuple[str, str], dict] = {}
    for f in report.get("findings") or []:
        k = _key(f)
        prev = out.get(k)
        if prev is None or severity_rank(_sev(f)) < severity_rank(_sev(prev)):
            out[k] = f
    return out


# ------------------------------------------------------------------ storage


def save(report_json: str, directory: Path, *, now: datetime | None = None) -> Path:
    """Write one run. Returns the path written."""
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stamp}.json"
    # A second run inside the same second must not clobber the first.
    n = 1
    while path.exists():
        path = directory / f"{stamp}-{n}.json"
        n += 1
    path.write_text(report_json)
    return path


def list_runs(directory: Path) -> list[Path]:
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        return []
    runs = [p for p in directory.glob("*.json") if p.is_file()]
    return sorted(runs, key=lambda p: p.name)


def load(path: Path) -> dict[str, Any] | None:
    try:
        body = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def previous_run(directory: Path, *, before: Path | None = None) -> Path | None:
    """The most recent run, optionally excluding one path (the run in progress)."""
    runs = [p for p in list_runs(directory) if p != before]
    return runs[-1] if runs else None


# --------------------------------------------------------------------- diff


@dataclass
class Change:
    ecosystem: str
    name: str
    from_severity: str | None = None
    to_severity: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    from_latest: str | None = None
    to_latest: str | None = None

    @property
    def moved_version(self) -> bool:
        return bool(
            self.from_version and self.to_version
            and self.from_version != self.to_version
        )


@dataclass
class Diff:
    from_run: str | None = None
    to_run: str | None = None
    from_generated: str | None = None
    to_generated: str | None = None
    appeared: list[Change] = field(default_factory=list)
    resolved: list[Change] = field(default_factory=list)
    worsened: list[Change] = field(default_factory=list)
    improved: list[Change] = field(default_factory=list)
    unchanged: int = 0
    from_counts: dict[str, int] = field(default_factory=dict)
    to_counts: dict[str, int] = field(default_factory=dict)
    note: str | None = None

    @property
    def net(self) -> int:
        """Negative is progress: fewer actionable findings than last time."""
        return _actionable(self.to_counts) - _actionable(self.from_counts)

    @property
    def direction(self) -> str:
        if self.net < 0:
            return "improving"
        if self.net > 0:
            return "falling behind"
        return "flat"

    @property
    def is_empty(self) -> bool:
        return not (self.appeared or self.resolved or self.worsened or self.improved)


def _actionable(counts: dict[str, int]) -> int:
    """Findings at MEDIUM or worse -- the ones the report asks you to act on."""
    return sum(counts.get(s, 0) for s in ("critical", "high", "medium"))


def _change(k: tuple[str, str], old: dict | None, new: dict | None) -> Change:
    oc = (old or {}).get("component") or {}
    nc = (new or {}).get("component") or {}
    orr = (old or {}).get("resolution") or {}
    nr = (new or {}).get("resolution") or {}
    return Change(
        ecosystem=k[0],
        name=nc.get("name") or oc.get("name") or k[1],
        from_severity=_sev(old) if old else None,
        to_severity=_sev(new) if new else None,
        from_version=oc.get("current"),
        to_version=nc.get("current"),
        from_latest=orr.get("latest_stable"),
        to_latest=nr.get("latest_stable"),
    )


def diff(old: dict, new: dict, *, from_name: str = "", to_name: str = "") -> Diff:
    d = Diff(
        from_run=from_name or None,
        to_run=to_name or None,
        from_generated=old.get("generated_at"),
        to_generated=new.get("generated_at"),
        from_counts=old.get("summary") or {},
        to_counts=new.get("summary") or {},
    )
    if old.get("root") and new.get("root") and old["root"] != new["root"]:
        d.note = (
            f"comparing different scan roots ({old['root']} vs {new['root']}); "
            "the diff may not be meaningful"
        )

    a, b = _index(old), _index(new)
    for k in sorted(set(a) | set(b)):
        o, n = a.get(k), b.get(k)
        ch = _change(k, o, n)
        if o is None:
            d.appeared.append(ch)
        elif n is None:
            d.resolved.append(ch)
        else:
            delta = severity_rank(_sev(n)) - severity_rank(_sev(o))
            if delta < 0:
                d.worsened.append(ch)  # lower rank == more severe
            elif delta > 0:
                d.improved.append(ch)
            elif ch.moved_version:
                # Same severity but a different pin: still upgraded, and worth
                # showing, because "we bumped it and it is still behind" is a
                # different story from "nobody touched it".
                d.improved.append(ch)
            else:
                d.unchanged += 1

    worst = lambda c: severity_rank(c.to_severity or c.from_severity or "unknown")
    d.appeared.sort(key=worst)
    d.worsened.sort(key=worst)
    d.improved.sort(key=lambda c: severity_rank(c.from_severity or "unknown"))
    d.resolved.sort(key=lambda c: severity_rank(c.from_severity or "unknown"))
    return d


def diff_latest(directory: Path, current: dict, *, current_path: Path | None = None) -> Diff | None:
    """Diff a just-completed report against the most recent stored run."""
    prev = previous_run(directory, before=current_path)
    if prev is None:
        return None
    old = load(prev)
    if old is None:
        return None
    return diff(old, current, from_name=prev.name, to_name=(current_path.name if current_path else "current"))


def to_text(d: Diff) -> str:
    lines = [
        f"{d.from_run or 'previous'} -> {d.to_run or 'current'}",
        f"actionable findings: {_actionable(d.from_counts)} -> "
        f"{_actionable(d.to_counts)} ({d.net:+d}, {d.direction})",
        "",
    ]
    for label, items in (
        ("worsened", d.worsened),
        ("new", d.appeared),
        ("improved", d.improved),
        ("resolved", d.resolved),
    ):
        if not items:
            continue
        lines.append(f"{label} ({len(items)}):")
        for c in items[:20]:
            sev = f"{c.from_severity or '-'} -> {c.to_severity or 'gone'}"
            ver = f"{c.from_version or '-'} -> {c.to_version or '-'}" if c.moved_version else (c.to_version or c.from_version or "")
            lines.append(f"  {c.name:44} {sev:26} {ver}")
        if len(items) > 20:
            lines.append(f"  ...{len(items) - 20} more")
        lines.append("")
    if d.unchanged:
        lines.append(f"unchanged: {d.unchanged}")
    if d.note:
        lines.append(f"note: {d.note}")
    return "\n".join(lines) + "\n"

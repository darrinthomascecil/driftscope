"""Fleet comparison: how drift differs across several repos.

Given the per-repo Reports the single-repo scanner already produces, join their
findings by `(ecosystem, name)` and surface where the repos *disagree* -- the same
component pinned to different versions in different repos (the "one repo on ioredis
4.3, another on 4.4" case). Pure aggregation over existing data: no scanning,
network, or resolver logic lives here.

Honesty carries over from the rest of the tool: this compares the *declared* pin
(`component.current`) per repo and flags divergence, but it does not claim which pin
is "older" unless the version scheme is comparable -- the same caution that the Go
`+incompatible` inversion taught. A repo can also pin one component at several
versions internally (a monorepo), so a repo's entry is a *set* of versions, not one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Lag, Report, SEV_UNKNOWN, severity_rank
from .version import comparable as _comparable, compare as _compare, parse as _parse


@dataclass
class RepoPin:
    """What one repo pins for one component."""

    versions: list[str] = field(default_factory=list)  # distinct, in first-seen order
    severity: str = SEV_UNKNOWN                         # worst severity for it in this repo
    lag: Lag | None = None                              # deepest lag among this repo's pins


@dataclass
class ComponentRow:
    ecosystem: str
    name: str
    latest: str | None
    pins: dict[str, RepoPin]   # repo label -> pin; only repos that use the component
    diverges: bool             # repos pin more than one distinct version between them
    worst: str                 # worst severity across the repos that use it
    align_to: str | None = None  # the newest pin already in the fleet: a proven
                                 # consensus target for the laggards (None if not orderable)
    worst_lag: Lag | None = None  # deepest lag across the repos (drives the "Behind" column)
    fleet_debt: int = 0        # sum of releases-behind across comparable pins: blast x depth
    depth_comparable: bool = False  # at least one pin's scheme was orderable, so depth is real

    @property
    def repo_count(self) -> int:
        return len(self.pins)


@dataclass
class FleetComparison:
    repos: list[str]                      # repo labels, in input order
    rows: list[ComponentRow]              # sorted: divergent first, then by severity
    errors: dict[str, str] = field(default_factory=dict)  # repo label -> failure reason
    posture: dict[str, dict[str, int]] = field(default_factory=dict)  # repo -> {severity: count}

    @property
    def divergent(self) -> list[ComponentRow]:
        return [r for r in self.rows if r.diverges]

    @property
    def shared(self) -> list[ComponentRow]:
        return [r for r in self.rows if r.repo_count > 1]


def _all_versions(row_repos: dict[str, RepoPin]) -> set[str]:
    return {v for rp in row_repos.values() for v in rp.versions}


def newest_pin(versions, ecosystem: str) -> str | None:
    """The most-current pin among a set, or None when the scheme is not comparable.

    Used only to point out which repo is furthest behind; when versions cannot be
    ordered on the same scheme (calver, floating tags, +incompatible mixes) it
    returns None and the report says "differs" rather than asserting an order.
    """
    parsed = []
    for v in versions:
        p = _parse(v)
        if not p.valid:
            return None
        parsed.append((v, p))
    for i in range(1, len(parsed)):
        ok, _ = _comparable(parsed[0][1], parsed[i][1])
        if not ok:
            return None
    return max(parsed, key=lambda t: t[1].sort_key())[0]


def _depth_key(lag: Lag | None) -> tuple[int, int, int, int]:
    """How deep a lag is, for picking the worst. Non-comparable schemes have no
    depth (calver, floating tags): they count toward blast radius but never invent
    a distance -- the same caution the Go +incompatible inversion taught."""
    if lag is None or not lag.comparable:
        return (0, 0, 0, 0)
    return (lag.behind_major, lag.behind_minor, lag.behind_patch,
            lag.stable_releases_behind or lag.releases_behind)


_UNITS = {"major": "majors", "minor": "minors", "patch": "patches"}


def lag_label(lag: Lag | None) -> str:
    """Human 'how far behind': '3 majors', '2 minors', '1 patch', or '' when level.

    Leads with the coarsest non-zero step, because a major behind dominates any
    number of patches. Empty when the pin is current or the scheme isn't orderable.
    """
    if lag is None or not lag.comparable:
        return ""
    for n, unit in ((lag.behind_major, "major"), (lag.behind_minor, "minor"),
                    (lag.behind_patch, "patch")):
        if n:
            return f"{n} {_UNITS[unit] if n > 1 else unit}"
    return f"{lag.releases_behind} releases" if lag.releases_behind else ""


def compare_reports(named_reports, errors=None) -> FleetComparison:
    """Build the fleet comparison from `[(repo_label, Report), ...]`."""
    repos = [name for name, _ in named_reports]
    posture: dict[str, dict[str, int]] = {}
    groups: dict[tuple[str, str], dict] = {}
    for name, report in named_reports:
        counts = posture.setdefault(name, {})
        for f in report.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
            key = (f.component.ecosystem, f.component.name)
            g = groups.setdefault(key, {"latest": None, "repos": {}})
            if f.resolution.latest_stable and not g["latest"]:
                g["latest"] = f.resolution.latest_stable
            rp = g["repos"].setdefault(name, RepoPin())
            cur = f.component.current or "unpinned"
            if cur not in rp.versions:
                rp.versions.append(cur)
            if severity_rank(f.severity) < severity_rank(rp.severity):
                rp.severity = f.severity
            if rp.lag is None or _depth_key(f.lag) > _depth_key(rp.lag):
                rp.lag = f.lag

    rows: list[ComponentRow] = []
    for (eco, nm), g in groups.items():
        pins = g["repos"]
        all_vers = _all_versions(pins)
        worst = min((rp.severity for rp in pins.values()), key=severity_rank)
        diverges = len(all_vers) > 1
        # Depth: only comparable pins contribute a real distance. fleet_debt sums
        # releases-behind across them, so blast radius (more repos) and depth (further
        # behind) both push a component up -- wide-but-shallow drift ranks below narrow-
        # but-deep. worst_lag is the deepest single laggard, for the "Behind" label.
        deep = [rp.lag for rp in pins.values() if rp.lag and rp.lag.comparable]
        worst_lag = max(deep, key=_depth_key, default=None)
        fleet_debt = sum((l.stable_releases_behind or l.releases_behind) for l in deep)
        rows.append(ComponentRow(
            ecosystem=eco, name=nm, latest=g["latest"], pins=pins,
            diverges=diverges, worst=worst,
            align_to=newest_pin(all_vers, eco) if diverges else None,
            worst_lag=worst_lag, fleet_debt=fleet_debt, depth_comparable=bool(deep),
        ))

    # Rank by risk first (worst severity), then by fleet debt -- blast radius x how far
    # behind -- so the components whose upgrade buys the most sit at the top. Divergence
    # is no longer the lead sort: at fleet scale almost everything diverges, so *depth*
    # is the signal, not the mere fact of disagreement (it stays a filter and a badge).
    rows.sort(key=lambda r: (severity_rank(r.worst), -r.fleet_debt, -r.repo_count, r.name))
    return FleetComparison(repos=repos, rows=rows, errors=errors or {}, posture=posture)

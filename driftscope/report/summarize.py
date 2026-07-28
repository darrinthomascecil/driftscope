"""Presentation-neutral summaries of the richer finding attachments.

The HTML and Markdown reporters used to each decide *what a report says* about
vulnerabilities, upstream velocity and estate trend -- which metrics exist, which
advisory sets to split out, where the display caps fall, which words describe a
direction. Those decisions were duplicated, and drifted: the JSON reporter shipped
without `trend` for a while because "add it to the output" meant editing three
places and one was missed.

This module owns the decisions. Each `summarize_*` function turns a model object
into small frozen records, and a reporter's only remaining job is to format them.

What deliberately stays in the reporters is *formatting*, because the two formats
genuinely differ and must keep differing:

  * Markdown writes `—` for an unavailable metric; HTML writes a rounded `0` when a
    change is under half a unit (so it never prints "-0").
  * Markdown wraps versions in backticks; HTML wraps them in a span.
  * Markdown renders a sparkline with block characters; HTML renders `<i>` bars.

So the records below carry raw values and semantic roles, never preformatted
display strings. A record that held `"—"` would have forced both reporters to
agree on something they should not.
"""

from __future__ import annotations

from dataclasses import dataclass

# Display caps. Shared because they are judgements about how much detail is useful,
# not formatting: both reporters showed 6 advisories and 12 trend rows, and the two
# numbers drifting apart would be a bug rather than a style difference.
VULN_DETAIL_CAP = 6
TREND_ROW_CAP = 12
VELOCITY_NOTE_CAP = 3


# ------------------------------------------------------------------ vulnerabilities


@dataclass(frozen=True)
class VulnSection:
    """One advisory grouping: fixed by the upgrade, still present, or introduced."""

    title: str
    count: int
    shown: tuple  # up to VULN_DETAIL_CAP Vuln objects
    hidden: int
    role: str  # good | critical | unknown -- a status role, not a colour


@dataclass(frozen=True)
class VulnSummary:
    package: str
    # None when the query did not discriminate between the two versions, in which
    # case there is no "affecting <version>" claim to make.
    headline_version: str | None
    headline_count: int
    worst_severity: str
    approximate: bool
    indiscriminate: bool
    unmatched: bool
    sections: tuple = ()
    # True when every known advisory survives the upgrade, which needs saying
    # outright: the obvious remedy does not work here.
    upgrade_clears_nothing: bool = False
    note: str | None = None


def summarize_vulns(r) -> VulnSummary:
    """Decide which advisory groupings to show and how they are labelled.

    `unmatched` short-circuits: a zero-result approximate query means the mapping
    found nothing, not that the package is clean, so the only honest output is the
    note explaining that.
    """
    if r.unmatched:
        return VulnSummary(
            package=r.package, headline_version=r.current_version,
            headline_count=0, worst_severity="", approximate=r.approximate,
            indiscriminate=False, unmatched=True, note=r.note,
        )

    def section(title: str, vulns, count: int, role: str) -> VulnSection | None:
        if not count:
            return None
        shown = tuple(vulns[:VULN_DETAIL_CAP])
        return VulnSection(title=title, count=count, shown=shown,
                           hidden=max(0, count - len(shown)), role=role)

    sections: list[VulnSection] = []
    if r.indiscriminate:
        # The fixed/remaining split is unknowable, so only the flat list is shown.
        s = section("Advisories on record", r.remaining_after_upgrade,
                    r.remaining_count, "unknown")
        if s:
            sections.append(s)
    else:
        fixed_title = (f"Fixed by upgrading to {r.target_version}"
                       if r.target_version else "Fixed by upgrading")
        for s in (
            section(fixed_title, r.fixed_by_upgrade, r.fixed_count, "good"),
            section("Still present after upgrade", r.remaining_after_upgrade,
                    r.remaining_count, "critical"),
        ):
            if s:
                sections.append(s)
    s = section("Introduced by the upgrade", r.introduced_by_upgrade,
                r.introduced_count, "critical")
    if s:
        sections.append(s)

    return VulnSummary(
        package=r.package,
        headline_version=None if r.indiscriminate else r.current_version,
        headline_count=r.current_count,
        worst_severity=r.worst_severity,
        approximate=r.approximate,
        indiscriminate=r.indiscriminate,
        unmatched=False,
        sections=tuple(sections),
        upgrade_clears_nothing=bool(
            r.remaining_count and not r.fixed_count and not r.indiscriminate
        ),
        note=r.note,
    )


# ----------------------------------------------------------------------- velocity


@dataclass(frozen=True)
class Metric:
    """One velocity row: a series to plot, a change to judge, and a verdict."""

    label: str
    series: tuple  # float per window, oldest first
    series_labels: tuple
    # None means "not measurable for the latest window" -- e.g. no reactions at all,
    # which is different from zero frustration and must not render as 0%.
    latest: float | None
    change: float | None
    unit: str  # unit of `change`: "" absolute | "pp" percentage points | "%" relative
    # What kind of quantity `latest` is, so a reporter can format it without
    # switching on the label. "signed_delta" reads with an explicit sign (a backlog
    # that shrank is meaningfully -12), "share" reads as a percentage, "count" is a
    # plain magnitude.
    kind: str  # signed_delta | share | count
    trend: str
    why: str


@dataclass(frozen=True)
class VelocitySummary:
    repo: str
    verdict: str
    window_count: int
    window_days: int
    error: str | None = None
    metrics: tuple = ()
    latest_window: object | None = None
    notes: tuple = ()


def summarize_velocity(v) -> VelocitySummary:
    """Decide which three signals describe upstream health, and how each is scaled.

    The unit per metric is the load-bearing part. Backlog is an absolute count
    because a percentage of a signed value straddling zero is meaningless (+2 to -4
    is not "-300%"); sentiment moves in percentage *points*, because showing a
    relative +64% beside a "steady" verdict read as a contradiction when the
    absolute move was 4pp.
    """
    if v.error:
        return VelocitySummary(repo=v.repo, verdict=v.verdict,
                               window_count=len(v.windows),
                               window_days=v.window_days, error=v.error)

    labels = tuple(w.label for w in v.windows)
    last = v.windows[-1] if v.windows else None

    backlog = tuple(float(w.backlog_delta) for w in v.windows)
    frustration = tuple(
        (w.frustration * 100) if w.frustration is not None else 0.0 for w in v.windows
    )
    releases = tuple(float(w.releases) for w in v.windows)

    metrics = (
        Metric(
            label="Issue backlog", series=backlog, series_labels=labels,
            latest=float(last.backlog_delta) if last else None,
            change=v.backlog_change_abs, unit="", kind="signed_delta",
            trend=v.issue_trend,
            why="opened minus closed per window; positive means the backlog grew. "
                "Shown as an absolute change because a percentage of a signed value "
                "straddling zero is meaningless",
        ),
        Metric(
            label="Negative reactions", series=frustration, series_labels=labels,
            latest=(last.frustration * 100)
            if (last and last.frustration is not None) else None,
            change=v.frustration_change_pts, unit="pp", kind="share",
            trend=v.sentiment_trend,
            why="share of \N{THUMBS DOWN SIGN}/\N{CONFUSED FACE} among all reactions "
                "on the most-reacted issues of each window",
        ),
        Metric(
            label="Releases shipped", series=releases, series_labels=labels,
            latest=float(last.releases) if last else None,
            change=v.release_change_pct, unit="%", kind="count",
            trend=v.release_trend,
            why="releases published per window",
        ),
    )
    return VelocitySummary(
        repo=v.repo, verdict=v.verdict, window_count=len(v.windows),
        window_days=v.window_days, error=None, metrics=metrics,
        latest_window=last, notes=tuple(v.notes[:VELOCITY_NOTE_CAP]),
    )


# -------------------------------------------------------------------------- trend


@dataclass(frozen=True)
class TrendTile:
    label: str
    count: int
    role: str


@dataclass(frozen=True)
class TrendGroup:
    """One change category, already capped, with its overflow count."""

    label: str
    role: str
    shown: tuple
    overflow: int


@dataclass(frozen=True)
class TrendSummary:
    direction: str
    net: int
    from_run: str
    to_run: str
    tiles: tuple = ()
    groups: tuple = ()
    is_empty: bool = True
    note: str | None = None

    @property
    def has_rows(self) -> bool:
        return any(g.shown for g in self.groups)


# Ordered worst-news-first: a reader scanning the top of the table should see what
# got worse before what got better.
_TREND_CATEGORIES = (
    ("worsened", "worsened", "critical"),
    ("appeared", "new", "serious"),
    ("improved", "improved", "good"),
    ("resolved", "resolved", "good"),
)


def summarize_trend(d) -> TrendSummary:
    """Decide the change categories, their order, their display cap and their roles."""
    groups = []
    tiles = []
    for attr, label, role in _TREND_CATEGORIES:
        items = getattr(d, attr) or []
        shown = tuple(items[:TREND_ROW_CAP])
        groups.append(TrendGroup(label=label, role=role, shown=shown,
                                 overflow=max(0, len(items) - len(shown))))
        tiles.append(TrendTile(label=label, count=len(items), role=role))
    tiles.append(TrendTile(label="unchanged", count=d.unchanged, role="low"))

    return TrendSummary(
        direction=d.direction, net=d.net,
        from_run=d.from_run or "previous", to_run=d.to_run or "current",
        tiles=tuple(tiles), groups=tuple(groups),
        is_empty=d.is_empty, note=d.note,
    )


# --------------------------------------------------------------------- sparklines


def spark_ratios(values) -> list[float]:
    """Each value's magnitude as a 0.0-1.0 fraction of the series' own peak.

    This is the shared half of a sparkline: both reporters scale by *absolute* peak
    (so a series straddling zero still shows relative magnitude) and a zero-peak
    series must not divide by zero. Mapping a ratio to something visible is not
    shared, because the two targets are not comparable -- Markdown picks one of
    eight block glyphs, HTML sets a pixel height with a minimum so a near-zero bar
    stays visible. Returning the ratio keeps the arithmetic in one place without
    pretending those two are the same operation.
    """
    if not values:
        return []
    peak = max((abs(v) for v in values), default=0) or 1
    return [abs(v) / peak for v in values]


# ------------------------------------------------------------------- sections

# A report grouped by ecosystem reads far better than one long severity-sorted
# list, because the sections map onto who fixes them: a platform team owns the
# IaC and the images, an application team owns its dependencies and runtime.
#
# This is a strict *partition*, deliberately not reusing `cli.ECOSYSTEM_GROUPS`.
# Those groups overlap on purpose -- `nuget` is in both `deps` and `dotnet` so
# `--ecosystems dotnet` picks it up -- and a report built on overlapping groups
# would print the same finding twice.
SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("iac", "Infrastructure as code",
     ("terraform-provider", "terraform-module", "azure-api-version")),
    ("containers", "Containers and orchestration",
     ("docker-image", "helm-chart", "k8s-api", "k8s-cluster")),
    ("runtimes", "Runtimes and toolchains",
     ("dotnet-sdk", "dotnet-tfm", "jvm-toolchain", "ruby-runtime")),
    ("deps", "Application dependencies",
     ("npm", "pypi", "maven", "nuget", "go-module", "rubygems")),
    ("ci", "CI and build", ("github-action",)),
    ("cloud", "Cloud resources", ("azure-sku", "azure-node-image")),
)

# Anything not listed above lands here rather than vanishing. A newly registered
# ecosystem that quietly failed to appear in the report would be the same class
# of bug the resolver contract test guards against on the other side.
OTHER_SECTION = ("other", "Other")

_SECTION_OF = {eco: key for key, _, ecos in SECTIONS for eco in ecos}
_LABELS = {key: label for key, label, _ in SECTIONS} | {OTHER_SECTION[0]: OTHER_SECTION[1]}


def section_for(ecosystem: str) -> str:
    return _SECTION_OF.get(ecosystem, OTHER_SECTION[0])


@dataclass(frozen=True)
class FindingSection:
    key: str
    label: str
    findings: list
    counts: dict[str, int]
    worst: str

    @property
    def actionable(self) -> int:
        """Findings that are behind -- everything except current and unknown."""
        return sum(
            n for s, n in self.counts.items() if s not in ("current", "unknown")
        )


def group_findings(findings, *, severity_rank) -> list[FindingSection]:
    """Partition findings into report sections, worst section first.

    Ordering is by the section's worst severity, then by how many findings sit
    at that severity, so the section demanding attention leads. Sections with no
    findings are omitted entirely: printing "Containers: none" would be a claim
    about an estate that may simply have no containers in it.
    """
    buckets: dict[str, list] = {}
    for f in findings:
        buckets.setdefault(section_for(f.component.ecosystem), []).append(f)

    out: list[FindingSection] = []
    for key, items in buckets.items():
        counts: dict[str, int] = {}
        for f in items:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        worst = min((f.severity for f in items), key=lambda s: severity_rank(s))
        out.append(
            FindingSection(
                key=key,
                label=_LABELS.get(key, key),
                findings=sorted(
                    items,
                    key=lambda f: (severity_rank(f.severity), f.component.ecosystem,
                                   f.component.name),
                ),
                counts=counts,
                worst=worst,
            )
        )
    out.sort(key=lambda s: (severity_rank(s.worst), -s.counts.get(s.worst, 0), s.label))
    return out

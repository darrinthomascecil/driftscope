"""Core data model.

A run flows: Component (found by a scanner) -> Resolution (latest version, from a
resolver) -> Lag (how far behind) -> Advice (how to upgrade safely) = Finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from typing import Any


# Ecosystem identifiers. Each maps to exactly one resolver.
TERRAFORM_PROVIDER = "terraform-provider"
TERRAFORM_MODULE = "terraform-module"
AZURE_API = "azure-api-version"
DOCKER = "docker-image"
HELM = "helm-chart"
K8S_API = "k8s-api"
K8S_CLUSTER = "k8s-cluster"
PYPI = "pypi"
NPM = "npm"
GO = "go-module"
AZURE_SKU = "azure-sku"
AZURE_NODE_IMAGE = "azure-node-image"
NUGET = "nuget"
MAVEN = "maven"
DOTNET_TFM = "dotnet-tfm"
DOTNET_SDK = "dotnet-sdk"
JVM_TOOLCHAIN = "jvm-toolchain"

SEV_CRITICAL = "critical"
SEV_HIGH = "high"
SEV_MEDIUM = "medium"
SEV_LOW = "low"
SEV_CURRENT = "current"
SEV_UNKNOWN = "unknown"

_SEV_ORDER = {
    SEV_CRITICAL: 0,
    SEV_HIGH: 1,
    SEV_MEDIUM: 2,
    SEV_LOW: 3,
    SEV_CURRENT: 4,
    SEV_UNKNOWN: 5,
}


def severity_rank(sev: str) -> int:
    return _SEV_ORDER.get(sev, 99)


@dataclass
class Component:
    """A versioned thing found in a repo (or in a live cloud account)."""

    ecosystem: str
    name: str
    current: str | None
    source: str  # file path, or "azure://<resource-id>" for live findings
    line: int | None = None
    raw: str | None = None  # original text, e.g. ">=3.0,<4.0"
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.ecosystem}:{self.name}"


@dataclass
class Release:
    version: str
    published: datetime | None = None
    prerelease: bool = False
    yanked: bool = False


@dataclass
class Resolution:
    """What the upstream registry says exists."""

    latest_stable: str | None = None
    releases: list[Release] = field(default_factory=list)
    # "owner/name" on GitHub, "org/project/repo" on Azure DevOps.
    repo: str | None = None
    # Which forge `repo` is addressed on. Defaulting to GitHub means every existing
    # resolver keeps working untouched, and the JSON schema change is additive.
    forge: str = "github"
    homepage: str | None = None
    deprecated: bool = False
    note: str | None = None
    error: str | None = None
    # Resolver-supplied extras, e.g. the AgentBaker tag a node image maps to.
    extra: dict[str, Any] = field(default_factory=dict)

    def release_for(self, version: str) -> Release | None:
        from .version import normalize

        want = normalize(version)
        for r in self.releases:
            if normalize(r.version) == want:
                return r
        return None


@dataclass
class Lag:
    """How far behind `current` is from `latest_stable`."""

    behind_major: int = 0
    behind_minor: int = 0
    behind_patch: int = 0
    releases_behind: int = 0
    stable_releases_behind: int = 0
    days_behind: int | None = None
    current_age_days: int | None = None
    # True when the pinned version predates the registry's version window, so its
    # date is unknown and the age figures are lower bounds ("at least N days").
    age_lower_bound: bool = False
    comparable: bool = True
    reason: str | None = None  # why not comparable


@dataclass
class IssueSignal:
    """One GitHub issue/PR surfaced in the version window."""

    number: int
    title: str
    url: str
    state: str
    labels: list[str] = field(default_factory=list)
    created_at: str | None = None
    kind: str = "issue"  # issue | pr


@dataclass
class GitHubDelta:
    repo: str | None = None
    from_ref: str | None = None
    to_ref: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    window_resolved: bool = False
    bug_issues: list[IssueSignal] = field(default_factory=list)
    open_bug_count: int = 0
    total_bug_count: int = 0
    # False when the issue-count queries failed (rate limit), so a zero is
    # "unknown" rather than "none".
    counts_available: bool = True
    breaking_notes: list[str] = field(default_factory=list)
    releases_in_window: list[str] = field(default_factory=list)
    error: str | None = None
    note: str | None = None


POSITIVE_REACTIONS = ("+1", "heart", "hooray", "rocket", "laugh")
NEGATIVE_REACTIONS = ("-1", "confused")

TREND_IMPROVING = "improving"
TREND_STEADY = "steady"
TREND_DETERIORATING = "deteriorating"
TREND_UNKNOWN = "unknown"


@dataclass
class Window:
    start: date
    end: date
    opened: int = 0
    closed: int = 0
    sampled_issues: int = 0
    positive: int = 0
    negative: int = 0
    comments: int = 0
    releases: int = 0
    # False when the issue walk did not reach back far enough to cover this
    # window. An undercounted old window makes the latest one look like it is
    # accelerating, so incomplete windows are excluded from the trend baseline.
    complete: bool = True

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"

    @property
    def backlog_delta(self) -> int:
        """Positive means the open-issue count grew during this window."""
        return self.opened - self.closed

    @property
    def close_ratio(self) -> float | None:
        """closed/opened. >1 means the project is catching up."""
        return (self.closed / self.opened) if self.opened else None

    @property
    def total_reactions(self) -> int:
        return self.positive + self.negative

    @property
    def frustration(self) -> float | None:
        """Negative share of all reactions, 0.0-1.0."""
        t = self.total_reactions
        return (self.negative / t) if t else None

    @property
    def comments_per_issue(self) -> float | None:
        return (self.comments / self.sampled_issues) if self.sampled_issues else None


@dataclass
class Velocity:
    repo: str
    windows: list[Window] = field(default_factory=list)
    window_days: int = 90
    issue_trend: str = TREND_UNKNOWN
    sentiment_trend: str = TREND_UNKNOWN
    release_trend: str = TREND_UNKNOWN
    verdict: str = TREND_UNKNOWN
    # Percentage change of the latest window vs the mean of prior windows.
    backlog_change_pct: float | None = None
    # Percent change of a signed value straddling zero is meaningless (+2 -> -4 is
    # "-300%"), so the backlog also carries an absolute issues-per-window delta,
    # which is what the report shows.
    backlog_change_abs: float | None = None
    frustration_change_pct: float | None = None
    # Percentage-POINT change, which is what the trend threshold actually tests.
    # Showing the relative % beside a "steady" verdict read as a contradiction
    # (+35% relative was only +4pp absolute).
    frustration_change_pts: float | None = None
    release_change_pct: float | None = None
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def latest(self) -> Window | None:
        return self.windows[-1] if self.windows else None


@dataclass
class Advice:
    risk: str = SEV_UNKNOWN
    strategy: str = ""
    steps: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    target_version: str | None = None
    intermediate_versions: list[str] = field(default_factory=list)


@dataclass
class Finding:
    component: Component
    resolution: Resolution
    lag: Lag
    severity: str = SEV_UNKNOWN
    advice: Advice | None = None
    delta: GitHubDelta | None = None
    velocity: Velocity | None = None
    vulns: Any | None = None  # vuln.VulnReport

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # datetimes -> ISO strings
        for r in d["resolution"].get("releases", []):
            if isinstance(r.get("published"), datetime):
                r["published"] = r["published"].isoformat()
        d["severity"] = self.severity
        # Window's derived numbers are properties, which asdict() drops. They are
        # the whole point of the velocity block, so materialise them.
        if self.velocity and d.get("velocity"):
            for w_obj, w_dict in zip(self.velocity.windows, d["velocity"]["windows"]):
                w_dict["label"] = w_obj.label
                w_dict["backlog_delta"] = w_obj.backlog_delta
                w_dict["close_ratio"] = w_obj.close_ratio
                w_dict["total_reactions"] = w_obj.total_reactions
                w_dict["frustration"] = w_obj.frustration
                w_dict["comments_per_issue"] = w_obj.comments_per_issue
        return d


@dataclass
class ScanStats:
    files_scanned: int = 0
    components_found: int = 0
    resolved: int = 0
    resolve_errors: int = 0
    github_queries: int = 0
    cache_hits: int = 0
    started: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    duration_s: float = 0.0


@dataclass
class Report:
    root: str
    findings: list[Finding] = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)
    errors: list[str] = field(default_factory=list)
    # Set when the scan target was a cloned remote: remote/ref/commit.
    source: dict[str, Any] | None = None
    # Set by --history: movement since the previous stored run (a history.Diff).
    trend: Any | None = None

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (
                severity_rank(f.severity),
                -(f.lag.behind_major or 0),
                -(f.lag.days_behind or 0),
                f.component.ecosystem,
                f.component.name,
            ),
        )

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

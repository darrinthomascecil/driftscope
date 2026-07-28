"""Scan orchestration: scanners -> resolvers -> lag -> severity -> advice -> delta.

Resolution is threaded, because a scan is almost entirely network wait. The GitHub
delta is the expensive step (3+ API calls per component) so it runs only for
findings that matter, controlled by --delta-threshold.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import advisor
from .ghub import delta as ghdelta
from .http import Cache, Client, GitHub, default_cache_dir
from .models import (
    AZURE_API,
    AZURE_NODE_IMAGE,
    Finding,
    Report,
    ScanStats,
    SEV_CRITICAL,
    SEV_HIGH,
    severity_rank,
)
from .resolvers import Context, resolve
from .scanners import scan_tree
from .version import lag_between, K8SAPI
from .models import K8S_API


def _hint_for(ecosystem: str) -> str | None:
    return K8SAPI if ecosystem == K8S_API else None


def _oldest_dated(releases):
    """The earliest-published release in the list, or None."""
    dated = [r for r in releases if r.published]
    return min(dated, key=lambda r: r.published) if dated else None


def _sorts_below(version: str, other: str, hint: str | None) -> bool:
    """True when `version` is strictly older than `other` on the same scheme."""
    from .version import comparable as _cmpable, compare, parse

    a, b = parse(version, hint=hint), parse(other, hint=hint)
    ok, _ = _cmpable(a, b)
    return bool(ok and compare(a, b) < 0)


def build_context(
    *,
    offline: bool = False,
    no_github: bool = False,
    no_az: bool = False,
    cache_dir: Path | None = None,
    cache_ttl: int | None = None,
    no_cache: bool = False,
) -> Context:
    cache = Cache(
        root=cache_dir or default_cache_dir(),
        ttl=cache_ttl if cache_ttl is not None else 6 * 3600,
        enabled=not no_cache,
    )
    client = Client(cache=cache, offline=offline)
    gh = GitHub(cache=cache, offline=offline, enabled=not no_github)

    # Both forges share the disk cache and the offline switch, so --offline and
    # --no-github (now --no-forge) mean the same thing regardless of where a
    # component's upstream lives.
    from .forge import AZURE_DEVOPS, GITHUB
    from .forge.ado import AdoForge
    from .forge.github import GitHubForge

    forges = {
        GITHUB: GitHubForge(gh),
        AZURE_DEVOPS: AdoForge(cache, offline=offline, enabled=not no_github),
    }
    return Context(client=client, github=gh, allow_az=not no_az, forges=forges)


def run_scan(
    root: Path,
    *,
    ctx: Context,
    ecosystems: set[str] | None = None,
    workers: int = 8,
    delta_threshold: str = SEV_HIGH,
    max_delta: int = 25,
    include_live_azure: bool = False,
    live_subscription: str | None = None,
    live_resource_group: str | None = None,
    include_vulns: bool = True,
    include_velocity: bool = False,
    velocity_threshold: str = SEV_HIGH,
    velocity_windows: int = 4,
    velocity_window_days: int = 90,
    velocity_max_pages: int = 20,
    max_velocity: int = 6,
) -> Report:
    started = time.monotonic()
    report = Report(root=str(root), stats=ScanStats())

    components, files = scan_tree(root, ecosystems)
    report.stats.files_scanned = files

    if include_live_azure:
        from .live import azure as live_azure

        if not live_azure.az_available():
            report.errors.append("live Azure scan requested but `az` is not on PATH")
        elif not live_azure.logged_in():
            report.errors.append("live Azure scan requested but `az account show` failed (run `az login`)")
        else:
            live_components, live_errors = live_azure.scan_subscription(
                subscription=live_subscription, resource_group=live_resource_group
            )
            components += live_components
            report.errors += [f"live-azure: {e}" for e in live_errors]

    if ecosystems:
        components = [c for c in components if c.ecosystem in ecosystems]
    report.stats.components_found = len(components)

    now = datetime.now(timezone.utc)

    def work(c):
        res = resolve(c, ctx)
        hint = _hint_for(c.ecosystem)
        cur_rel = res.release_for(c.current) if c.current else None
        lat_rel = res.release_for(res.latest_stable) if res.latest_stable else None

        # Registries truncate their version lists (Artifact Hub, the GitHub
        # releases page cap). When the pin predates the window, the *oldest listed*
        # release's date is an upper bound on the pin's date, so age becomes a
        # lower bound -- far more useful than reporting nothing for the stalest pins.
        bound = False
        cur_published = cur_rel.published if cur_rel else None
        # Only valid when the release list is version-complete. Docker Hub pages by
        # recency and truncates, so its "oldest listed" tag is simply the least
        # recently pushed of the newest few hundred -- not an older release at all,
        # and using it produced absurd results like "node:14 is 13 days behind".
        if cur_published is None and c.current and not res.extra.get("list_truncated"):
            oldest = _oldest_dated(res.releases)
            if oldest is not None and _sorts_below(c.current, oldest.version, hint):
                cur_published, bound = oldest.published, True

        lag = lag_between(
            c.current,
            res.latest_stable,
            releases=res.releases,
            hint=hint,
            current_published=cur_published,
            latest_published=lat_rel.published if lat_rel else None,
            now=now,
        )
        lag.age_lower_bound = bound
        sev = advisor.score(c, res, lag)
        return Finding(component=c, resolution=res, lag=lag, severity=sev)

    findings: list[Finding] = []
    if components:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for f in pool.map(work, components):
                findings.append(f)
                if f.resolution.error:
                    report.stats.resolve_errors += 1
                elif f.resolution.latest_stable:
                    report.stats.resolved += 1

    # GitHub delta only for the findings that justify the API budget.
    threshold = severity_rank(delta_threshold)
    candidates = [
        f
        for f in findings
        if severity_rank(f.severity) <= threshold
        and f.resolution.repo
        and f.component.current
        and f.resolution.latest_stable
        and f.component.ecosystem != AZURE_API  # ARM providers are not on GitHub
    ]
    # Node images have no upstream "latest" unless AKS or AgentBaker supplied one,
    # but their age alone justifies a changelog lookup, so admit them separately.
    # Dedupe by identity: `f not in candidates` compared Findings *structurally*,
    # so each test walked every nested Release on both sides -- quadratic in the
    # finding count and multiplied by the release-list length.
    already = {id(f) for f in candidates}
    candidates += [
        f
        for f in findings
        if f.component.ecosystem == AZURE_NODE_IMAGE
        and f.component.current
        and severity_rank(f.severity) <= threshold
        and id(f) not in already
    ]
    candidates.sort(key=lambda f: (severity_rank(f.severity), -(f.lag.behind_major or 0)))
    candidates = candidates[:max_delta]

    if candidates and ctx.github.enabled:
        def do_delta(f: Finding):
            if f.component.ecosystem == AZURE_NODE_IMAGE:
                # Node image versions are not tags in any repo, so the generic
                # tag-matching path cannot work. Route via AgentBaker, which maps
                # build dates to the releases that produced them.
                from .ghub import agentbaker as ab
                from .resolvers.azure import parse_node_image_date

                f.delta = ab.node_image_delta(
                    ctx.github,
                    f.component.current,
                    parse_node_image_date(f.component.current),
                    f.resolution.latest_stable,
                    parse_node_image_date(f.resolution.latest_stable),
                )
                return f
            forge = ctx.forge_for(f.resolution.forge)
            if forge is None or not forge.enabled:
                return f
            f.delta = ghdelta.compute_delta(
                forge,
                f.resolution.repo,
                f.component.current,
                f.resolution.latest_stable,
                f.resolution,
            )
            return f

        with ThreadPoolExecutor(max_workers=min(4, max(1, workers))) as pool:
            list(pool.map(do_delta, candidates))

    # ---- known vulnerabilities, and what an upgrade would clear
    # A single batched OSV call covers every component at both its current and its
    # target version, so "what would upgrading fix" is a set difference over
    # advisory data rather than an inference from version numbers.
    if include_vulns:
        from . import vuln as vuln_mod

        pairs = [
            (f.component, f.resolution.latest_stable)
            for f in findings
            if f.component.current
        ]
        try:
            reports = vuln_mod.assess(pairs, ctx.client)
        except Exception as e:  # advisory lookup must never fail a scan
            report.errors.append(f"vulnerability lookup failed: {type(e).__name__}: {e}")
            reports = {}
        for f in findings:
            r = reports.get(f.component.key)
            if r is not None:
                f.vulns = r
        # A known-vulnerable pin outranks version distance: escalate severity.
        for f in findings:
            if f.vulns and f.vulns.current_count and not f.vulns.indiscriminate:
                worst = f.vulns.worst_severity
                if worst in ("CRITICAL", "HIGH") and severity_rank(f.severity) > severity_rank(SEV_CRITICAL):
                    f.severity = SEV_CRITICAL
                elif severity_rank(f.severity) > severity_rank(SEV_HIGH):
                    f.severity = SEV_HIGH

    # ---- upstream velocity, deduplicated by repo
    # Many components share one upstream (every jackson artifact -> FasterXML), and
    # a measurement costs ~13 API calls, so measure each repo once and share it.
    if include_velocity and ctx.github.enabled:
        from .ghub import velocity as vel

        # Keyed by (forge, repo): the same "owner/name" on two forges is two
        # different projects, and measuring one would attribute the other's health.
        repos: dict[tuple[str, str], list[Finding]] = {}
        for f in findings:
            repo = f.resolution.repo
            if (
                repo
                and severity_rank(f.severity) <= severity_rank(velocity_threshold)
                and f.component.ecosystem != AZURE_API
            ):
                repos.setdefault((f.resolution.forge, repo), []).append(f)
        # Truncating by insertion order dropped the most severe packages; rank
        # repos by their worst finding so the budget is spent where it matters.
        ranked = sorted(
            repos.items(),
            key=lambda kv: min(severity_rank(f.severity) for f in kv[1]),
        )
        selected = ranked[:max_velocity]
        if len(repos) > max_velocity:
            report.errors.append(
                f"velocity: measured {max_velocity} of {len(repos)} eligible repos "
                f"(--max-velocity); the rest were skipped"
            )

        def measure(item):
            (forge_kind, repo), fs = item
            forge = ctx.forge_for(forge_kind)
            if forge is None or not forge.enabled:
                return repo
            v = vel.measure(
                forge,
                repo,
                windows=velocity_windows,
                window_days=velocity_window_days,
                max_pages=velocity_max_pages,
            )
            for f in fs:
                f.velocity = v
            return repo

        # Sequential on purpose. Each repo costs ~12 *search* calls, and GitHub's
        # search endpoint is limited to roughly 30/minute -- fanning out gets
        # everything throttled, which previously surfaced as "no activity".
        for item in selected:
            measure(item)

    for f in findings:
        f.advice = advisor.advise(
            f.component, f.resolution, f.lag, f.severity, f.delta
        )

    report.findings = findings
    report.stats.github_queries = ctx.github.queries
    report.stats.cache_hits = ctx.client.cache.hits
    report.stats.duration_s = round(time.monotonic() - started, 2)
    return report


def exit_code_for(report: Report, fail_on: str | None) -> int:
    """0 clean, 1 threshold breached, 2 scan errors only."""
    if not fail_on:
        return 0
    limit = severity_rank(fail_on)
    for f in report.findings:
        if severity_rank(f.severity) <= limit:
            return 1
    return 0

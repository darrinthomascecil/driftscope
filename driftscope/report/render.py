"""Reporters: JSON for machines, Markdown for humans."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime

from ..models import (
    Report,
    SEV_CRITICAL,
    SEV_CURRENT,
    SEV_HIGH,
    SEV_LOW,
    SEV_MEDIUM,
    SEV_UNKNOWN,
    severity_rank,
)
from .summarize import (
    spark_ratios,
    summarize_trend,
    summarize_velocity,
    summarize_vulns,
)

SCHEMA_VERSION = "driftscope/1"

_BADGE = {
    SEV_CRITICAL: "CRITICAL",
    SEV_HIGH: "HIGH",
    SEV_MEDIUM: "MEDIUM",
    SEV_LOW: "LOW",
    SEV_CURRENT: "current",
    SEV_UNKNOWN: "unknown",
}

ECO_LABEL = {
    "terraform-provider": "Terraform provider",
    "terraform-module": "Terraform module",
    "azure-api-version": "Azure API version",
    "docker-image": "Container image",
    "helm-chart": "Helm chart",
    "k8s-api": "Kubernetes API",
    "k8s-cluster": "Kubernetes cluster",
    "pypi": "Python package",
    "npm": "npm package",
    "go-module": "Go module",
    "github-action": "GitHub Action",
    "azure-sku": "Azure SKU",
    "azure-node-image": "AKS node image",
    "nuget": "NuGet package",
    "maven": "Maven artifact",
    "dotnet-tfm": ".NET target framework",
    "dotnet-sdk": ".NET SDK",
    "jvm-toolchain": "JVM toolchain",
    "rubygems": "Ruby gem",
    "ruby-runtime": "Ruby runtime",
}


def _default(o):
    # date must be tested before datetime is not needed (datetime IS a date),
    # so check datetime first; both serialise to ISO-8601.
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def to_json(report: Report, *, indent: int = 2, include_releases: bool = False) -> str:
    payload = {
        "schema": SCHEMA_VERSION,
        "root": report.root,
        "source": report.source,
        # HTML and Markdown both render the trend; omitting it here made the
        # machine-readable format the only one missing it.
        "trend": (asdict(report.trend) if report.trend is not None else None),
        "generated_at": datetime.now().astimezone().isoformat(),
        "stats": report.stats.__dict__,
        "summary": report.counts(),
        "errors": report.errors,
        "findings": [],
    }
    for f in report.sorted_findings():
        d = f.to_dict()
        if not include_releases:
            # The full release list is large and rarely useful downstream.
            d["resolution"]["release_count"] = len(d["resolution"].get("releases", []))
            d["resolution"].pop("releases", None)
        payload["findings"].append(d)
    return json.dumps(payload, indent=indent, default=_default)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _lag_phrase(f) -> str:
    from ..version import CALVER, parse

    lag = f.lag
    if not lag.comparable:
        return "not comparable"
    bits = []
    # Calendar versions (Azure API versions) have no majors: the first segment is
    # a year, so "3 major" would read as nonsense.
    is_calver = parse(f.component.current or "").kind == CALVER
    if is_calver:
        if lag.behind_major:
            bits.append(_plural(lag.behind_major, "year"))
        if lag.stable_releases_behind:
            bits.append(_plural(lag.stable_releases_behind, "api-version"))
        if lag.days_behind:
            bits.append(_plural(lag.days_behind, "day"))
        return ", ".join(bits) if bits else "up to date"
    if lag.behind_major:
        bits.append(f"{lag.behind_major} major")
    if lag.behind_minor:
        bits.append(f"{lag.behind_minor} minor")
    if lag.behind_patch:
        bits.append(f"{lag.behind_patch} patch")
    if lag.stable_releases_behind:
        bits.append(_plural(lag.stable_releases_behind, "release"))
    if lag.days_behind:
        bits.append(_plural(lag.days_behind, "day"))
    return ", ".join(bits) if bits else "up to date"


SPARK_BLOCKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def _sparkline(values) -> str:
    """A text sparkline. Markdown has no SVG, but block characters read fine in a
    terminal, in a PR comment and in a pasted email."""
    top = len(SPARK_BLOCKS) - 1
    return "".join(SPARK_BLOCKS[min(top, int(r * top))] for r in spark_ratios(values))


def _md_latest(m) -> str:
    """The latest-window value. `None` means unmeasurable, which must not read as 0."""
    if m.latest is None:
        return "\u2014"
    if m.kind == "signed_delta":
        return f"{int(m.latest):+d}"
    if m.kind == "share":
        return f"{m.latest:.0f}%"
    return f"{int(m.latest)}"


def _md_change(m) -> str:
    return "\u2014" if m.change is None else f"{m.change:+.0f}{m.unit}"


def _md_trend(d) -> list[str]:
    """Movement since the previous stored run."""
    s = summarize_trend(d)
    arrow = {"improving": "improving", "falling behind": "falling behind",
             "flat": "unchanged"}.get(s.direction, s.direction)
    out = ["## Since the last run", ""]
    out.append(
        f"**{arrow}** &middot; {s.net:+d} actionable findings "
        f"(`{s.from_run}` -> `{s.to_run}`)"
    )
    out.append("")
    out.append("| Change | Count |")
    out.append("|---|---:|")
    for tile in s.tiles:
        out.append(f"| {tile.label} | {tile.count} |")
    out.append("")
    if s.has_rows:
        out.append("| Change | Component | Severity | Version |")
        out.append("|---|---|---|---|")
        for g in s.groups:
            for c in g.shown:
                sev = f"{c.from_severity or '-'} -> {c.to_severity or 'gone'}"
                ver = (f"`{c.from_version or '-'}` -> `{c.to_version or '-'}`"
                       if c.moved_version else f"`{c.to_version or c.from_version or ''}`")
                out.append(f"| {g.label} | `{c.name}` | {sev} | {ver} |")
            if g.overflow:
                out.append(f"| {g.label} | _+{g.overflow} more_ | | |")
        out.append("")
    if s.note:
        out.append(f"_{s.note}_")
        out.append("")
    out.append(
        "_Findings are matched by ecosystem and name, so a bumped version reads as "
        "the same component moving rather than one disappearing and another "
        "appearing. \"Actionable\" counts MEDIUM and worse._"
    )
    out.append("")
    return out


def _md_vulns(r) -> list[str]:
    """Known advisories and, where knowable, what an upgrade would clear."""
    s = summarize_vulns(r)
    if s.unmatched:
        return [f"_Vulnerability lookup: {s.note}_", ""]
    head = (f"{s.headline_count} on record" if s.headline_version is None
            else f"{s.headline_count} affecting `{s.headline_version}`")
    out = [f"**Known vulnerabilities** &middot; {head} &middot; "
           f"`{s.package}` via OSV{' (approximate)' if s.approximate else ''}", ""]
    for sec in s.sections:
        out.append(f"- **{sec.title}: {sec.count}**")
        for v in sec.shown:
            # `--` where no CVSS v3 vector exists: unscored is not the same
            # claim as harmless, so it must not render as a number.
            sc = f"{v.score}" if v.score is not None else "--"
            out.append(f"  - `{sc:>3}/100` [{v.cve}]({v.url}) "
                       f"`{v.severity or 'unrated'}` {v.summary[:120]}")
        if sec.hidden:
            out.append(f"  - _+{sec.hidden} more not described_")
    if s.upgrade_clears_nothing:
        out.append("")
        out.append("  _The upgrade clears none of these: they affect the target "
                   "version too, so this needs a different remedy._")
    out.append("")
    if s.note:
        out.append(f"_{s.note}_")
        out.append("")
    return out


def _md_velocity(v) -> list[str]:
    """Upstream issue, sentiment and release velocity."""
    s = summarize_velocity(v)
    out = [f"**Upstream velocity** &middot; {s.verdict} &middot; `{s.repo}` "
           f"({s.window_count} x {s.window_days}-day windows)", ""]
    if s.error:
        out += [f"_{s.error}_", ""]
        return out
    out.append("| Metric | Trend | Latest | Change | Verdict |")
    out.append("|---|---|---:|---:|---|")
    for m in s.metrics:
        out.append(f"| {m.label} | `{_sparkline(m.series)}` | {_md_latest(m)} "
                   f"| {_md_change(m)} | {m.trend} |")
    out.append("")
    latest = s.latest_window
    if latest:
        out.append(
            f"_Latest window {latest.label}: {latest.opened} opened, "
            f"{latest.closed} closed, {latest.total_reactions} reactions across "
            f"{latest.sampled_issues} sampled issues, {latest.releases} release(s)._"
        )
        out.append("")
    for n in s.notes:
        out.append(f"_{n}_")
        out.append("")
    return out


def to_markdown(report: Report, *, max_detail: int = 40) -> str:
    counts = report.counts()
    lines: list[str] = []
    a = lines.append

    a("# Version drift report")
    a("")
    a(f"**Scanned:** `{report.root}`  ")
    if report.source:
        s = report.source
        a(f"**Commit:** `{(s.get('commit') or '')[:12]}` on `{s.get('ref')}`  ")
    a(f"**Generated:** {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}  ")
    s = report.stats
    a(
        f"**Coverage:** {s.components_found} versioned components across "
        f"{s.files_scanned} files - {s.resolved} resolved, {s.resolve_errors} unresolved "
        f"({s.duration_s}s, {s.github_queries} GitHub queries)"
    )
    a("")

    a("## Summary")
    a("")
    a("| Severity | Count | Meaning |")
    a("|---|---:|---|")
    meanings = {
        SEV_CRITICAL: "Deprecated/removed upstream, or 2+ majors behind - will break",
        SEV_HIGH: "One major behind, or 2+ years stale",
        SEV_MEDIUM: "Several minors behind, over a year old, or unpinned",
        SEV_LOW: "Minor or patch behind",
        SEV_CURRENT: "At the latest stable version",
        SEV_UNKNOWN: "Could not be assessed - see notes",
    }
    for sev in (SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, SEV_LOW, SEV_CURRENT, SEV_UNKNOWN):
        if counts.get(sev):
            a(f"| **{_BADGE[sev]}** | {counts[sev]} | {meanings[sev]} |")
    a("")

    if report.trend is not None:
        lines.extend(_md_trend(report.trend))

    by_eco: dict[str, list] = {}
    for f in report.sorted_findings():
        by_eco.setdefault(f.component.ecosystem, []).append(f)
    a("## By ecosystem")
    a("")
    a("| Ecosystem | Components | Worst |")
    a("|---|---:|---|")
    for eco, fs in sorted(by_eco.items(), key=lambda kv: severity_rank(kv[1][0].severity)):
        a(f"| {ECO_LABEL.get(eco, eco)} | {len(fs)} | {_BADGE[fs[0].severity]} |")
    a("")

    actionable = [
        f for f in report.sorted_findings()
        if severity_rank(f.severity) <= severity_rank(SEV_MEDIUM)
    ]
    a("## Drift table")
    a("")
    if not actionable:
        a("_Nothing at MEDIUM or above._")
    else:
        a("| Sev | Component | Current | Latest stable | Behind | Where |")
        a("|---|---|---|---|---|---|")
        for f in actionable:
            c = f.component
            where = f"`{c.source}`" + (f":{c.line}" if c.line else "")
            a(
                f"| {_BADGE[f.severity]} | `{c.name}` "
                f"<br/><sub>{ECO_LABEL.get(c.ecosystem, c.ecosystem)}</sub> "
                f"| `{c.current or '-'}` | `{f.resolution.latest_stable or '?'}` "
                f"| {_lag_phrase(f)} | {where} |"
            )
    a("")

    a("## Upgrade guidance")
    a("")
    shown = 0
    for f in report.sorted_findings():
        if severity_rank(f.severity) > severity_rank(SEV_MEDIUM):
            continue
        if shown >= max_detail:
            a(f"_...{len(actionable) - shown} more omitted; see the JSON report._")
            break
        shown += 1
        c, res, adv = f.component, f.resolution, f.advice
        a(f"### {_BADGE[f.severity]} - `{c.name}` ({ECO_LABEL.get(c.ecosystem, c.ecosystem)})")
        a("")
        a(f"- **Current:** `{c.current or 'unpinned'}`" + (f" (constraint `{c.raw}`)" if c.raw and c.raw != c.current else ""))
        a(f"- **Latest stable:** `{res.latest_stable or 'unknown'}`")
        a(f"- **Behind by:** {_lag_phrase(f)}")
        # Age of the pinned version is a different number from the gap to latest,
        # and usually the more persuasive one: "your pin is 8.8 years old".
        if f.lag.current_age_days:
            years = f.lag.current_age_days / 365.25
            age = f"{years:.1f} years" if years >= 1 else _plural(f.lag.current_age_days, "day")
            if f.lag.age_lower_bound:
                a(f"- **Age of your version:** at least {age} — this version predates "
                  f"the registry's version window, so its exact publish date is unavailable")
            else:
                a(f"- **Age of your version:** {age} ({_plural(f.lag.current_age_days, 'day')} since publish)")
        a(f"- **Declared at:** `{c.source}`" + (f":{c.line}" if c.line else ""))
        also = c.meta.get("also_at") or []
        if also:
            a(f"- **Also at:** {', '.join(f'`{x}`' for x in also[:8])}"
              + (f" (+{len(also) - 8} more)" if len(also) > 8 else ""))
        if res.repo:
            a(f"- **Source:** https://github.com/{res.repo}")
        if res.homepage:
            a(f"- **Docs:** {res.homepage}")
        a("")
        if adv:
            a(f"**Strategy.** {adv.strategy}")
            a("")
            if adv.intermediate_versions:
                a(f"**Staged path:** `{c.current}` -> "
                  + " -> ".join(f"`{v}`" for v in adv.intermediate_versions)
                  + f" -> `{res.latest_stable}`")
                a("")
            if adv.steps:
                a("**Steps**")
                a("")
                for i, st in enumerate(adv.steps, 1):
                    a(f"{i}. {st}")
                a("")
            if adv.cautions:
                a("**Cautions**")
                a("")
                for ct in adv.cautions:
                    a(f"- {ct}")
                a("")
        d = f.delta
        if d and (d.breaking_notes or d.bug_issues or d.releases_in_window):
            a("<details>")
            a(f"<summary>Upstream delta {d.from_ref} -> {d.to_ref}"
              f"{f' ({len(d.releases_in_window)} releases)' if d.releases_in_window else ''}</summary>")
            a("")
            if d.breaking_notes:
                a("**Breaking-change notes from release bodies**")
                a("")
                for n in d.breaking_notes[:12]:
                    a(f"- {n}")
                a("")
            if d.bug_issues:
                a(f"**Open bug-labelled issues filed in this window** "
                  f"({d.open_bug_count} open of {d.total_bug_count} total)")
                a("")
                for i in d.bug_issues[:10]:
                    a(f"- [#{i.number}]({i.url}) {i.title}")
                a("")
            if d.note:
                a(f"_{d.note}_")
                a("")
            a("</details>")
            a("")
        if f.vulns is not None and f.vulns.has_data:
            for ln in _md_vulns(f.vulns):
                a(ln)
        if f.velocity:
            for ln in _md_velocity(f.velocity):
                a(ln)

    unknown = [f for f in report.findings if f.severity == SEV_UNKNOWN]
    if unknown:
        a("## Not assessed")
        a("")
        a("| Component | Reason |")
        a("|---|---|")
        for f in unknown[:40]:
            why = f.resolution.error or f.lag.reason or f.resolution.note or "unknown"
            a(f"| `{f.component.name}` <sub>{ECO_LABEL.get(f.component.ecosystem, f.component.ecosystem)}</sub> | {why} |")
        if len(unknown) > 40:
            a(f"| _+{len(unknown) - 40} more_ | |")
        a("")

    if report.errors:
        a("## Scan errors")
        a("")
        for e in report.errors:
            a(f"- {e}")
        a("")

    a("---")
    a("")
    a("**How to read this.** Severity is rule-based and inspectable: deprecated or "
      "2+ majors behind is CRITICAL, one major behind or 2+ years stale is HIGH. "
      "Runtimes and AKS node images are scored on support expiry and age instead of "
      "version distance.")
    a("")
    a("Issue counts in an upstream delta are *issues filed during the version "
      "window* -- a signal, not proof of a regression; the breaking-change notes "
      "quoted from release bodies are the higher-confidence input. Velocity "
      "percentages compare the latest window with the mean of the preceding ones, "
      "so they read as acceleration rather than level, and reaction mix is a proxy "
      "for sentiment rather than a measurement of it.")
    a("")
    a("Vulnerability data comes from OSV. \"Fixed by upgrading\" is the set of "
      "advisories affecting the current version but not the target, computed from "
      "advisory ranges. For container images the query is approximate and says so: "
      "without an image scan there is no package inventory, and where a distro "
      "version scheme does not line up with an image tag the report declines to "
      "split fixed from remaining rather than guess.")
    return "\n".join(lines) + "\n"


def to_text(report: Report) -> str:
    """Compact terminal summary."""
    counts = report.counts()
    out = []
    order = (SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, SEV_LOW, SEV_CURRENT, SEV_UNKNOWN)
    head = "  ".join(f"{_BADGE[s]}:{counts[s]}" for s in order if counts.get(s))
    out.append(f"{report.stats.components_found} components in {report.stats.files_scanned} files -> {head or 'nothing found'}")
    out.append("")
    for f in report.sorted_findings():
        if severity_rank(f.severity) > severity_rank(SEV_MEDIUM):
            continue
        c = f.component
        loc = f"{c.source}:{c.line}" if c.line else c.source
        out.append(
            f"[{_BADGE[f.severity]:8}] {c.ecosystem:20} {c.name:44} "
            f"{str(c.current or '-'):16} -> {str(f.resolution.latest_stable or '?'):16} "
            f"({_lag_phrase(f)})  {loc}"
        )
    if report.errors:
        out.append("")
        out.append("errors:")
        out += [f"  - {e}" for e in report.errors]
    return "\n".join(out) + "\n"

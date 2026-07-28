"""driftscope command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .engine import build_context, exit_code_for, run_scan
from .source import CloneError, open_source
from .models import (
    AZURE_API,
    DOCKER,
    GO,
    HELM,
    DOTNET_SDK,
    DOTNET_TFM,
    JVM_TOOLCHAIN,
    K8S_API,
    MAVEN,
    NPM,
    NUGET,
    PYPI,
    RUBYGEMS,
    RUBY_RUNTIME,
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_LOW,
    SEV_MEDIUM,
    TERRAFORM_MODULE,
    TERRAFORM_PROVIDER,
)
from .report import render
from .scanners.cicd import GH_ACTION

# Sentinel for "derive the filename from the scan target".
AUTO = "\x00auto"


def resolve_out_path(value: str, src, ext: str) -> Path:
    """Turn an output flag value into a concrete path.

    AUTO or a directory both yield `drift-<slug>.<ext>`, where the slug names the
    repository or directory scanned.
    """
    from .source import slug_for

    name = f"drift-{slug_for(getattr(src, 'display', '') or '')}.{ext}"
    if value == AUTO:
        return Path(name)
    p = Path(value).expanduser()
    if p.is_dir() or value.endswith(("/", "\\")):
        return p / name
    return p


ECOSYSTEM_GROUPS = {
    "terraform": {TERRAFORM_PROVIDER, TERRAFORM_MODULE},
    "azure": {AZURE_API},
    "containers": {DOCKER, HELM, K8S_API},
    "deps": {PYPI, NPM, GO, NUGET, MAVEN, RUBYGEMS},
    "dotnet": {NUGET, DOTNET_TFM, DOTNET_SDK},
    "jvm": {MAVEN, JVM_TOOLCHAIN},
    "ruby": {RUBYGEMS, RUBY_RUNTIME},
    "ci": {GH_ACTION},
}


def _parse_ecosystems(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    out: set[str] = set()
    for v in values:
        for part in v.split(","):
            part = part.strip()
            if not part:
                continue
            if part in ECOSYSTEM_GROUPS:
                out |= ECOSYSTEM_GROUPS[part]
            else:
                out.add(part)
    return out or None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="driftscope",
        description=(
            "Measure how far behind latest stable a codebase's infrastructure, IaC "
            "and dependencies are, and how safely each gap can be closed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  driftscope scan .                                     scan the current tree
  driftscope scan https://github.com/org/repo           clone a repo, scan it, clean up
  driftscope scan org/repo --ref release-2.1            shorthand, pinned to a ref
  driftscope scan ./infra --ecosystem terraform,azure   IaC only
  driftscope scan . --markdown drift.md --json drift.json
  driftscope scan . --fail-on high                      CI gate: exit 1 past threshold
  driftscope scan . --live-azure --resource-group rg     add deployed-resource drift
  driftscope scan . --offline                           cache only, no network

  driftscope ecosystems                                 list ecosystem filter names
""",
    )
    p.add_argument("--version", action="version", version=f"driftscope {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser(
        "scan",
        help="scan a local directory or a remote git repository",
        description=(
            "Scan a target for versioned components, resolve each against upstream, "
            "and report how far behind latest stable it is plus how to close the gap "
            "safely. The target may be a local directory, a git URL, or an "
            "owner/repo shorthand (cloned to a temporary directory and removed "
            "afterwards)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""notes:
  Remote targets are shallow-cloned (--depth 1), scanned, then deleted. Reports are
  attributed to the repo and commit rather than the temporary path.

  --delta-threshold and --max-delta bound GitHub API spend: the upstream delta costs
  3+ calls per component, so the defaults keep a scan to roughly a dozen queries.

  Exit codes: 0 clean (or no --fail-on), 1 threshold breached, 2 bad target.
""",
    )
    s.add_argument(
        "path", nargs="?", default=".",
        help="directory to scan, or a git URL / owner-repo to clone and scan",
    )
    s.add_argument("--ref", metavar="REF", help="branch or tag to clone (remote targets only)")
    s.add_argument("--keep-clone", action="store_true", help="do not delete the temporary clone")
    s.add_argument(
        "--ado-org", metavar="ORG",
        help="read a bare project/repo target as Azure DevOps on this organisation "
             "(a full dev.azure.com URL needs no flag)",
    )
    s.add_argument(
        "--ecosystem",
        action="append",
        metavar="NAME",
        help="limit to ecosystems or groups: terraform, azure, containers, deps, ci",
    )
    # Each output flag takes an optional value. Omit it, or give a directory, and
    # the filename is derived from what was scanned -- so reports from different
    # repositories never overwrite each other.
    s.add_argument("--json", nargs="?", const=AUTO, metavar="FILE",
                   help="write JSON report (- for stdout, omit value to auto-name)")
    s.add_argument("--markdown", nargs="?", const=AUTO, metavar="FILE",
                   help="write Markdown report (- for stdout, omit value to auto-name)")
    s.add_argument("--html", nargs="?", const=AUTO, metavar="FILE",
                   help="write a self-contained HTML report (omit value to auto-name)")
    s.add_argument("--open", action="store_true",
                   help="open the HTML report in the default browser when done")
    s.add_argument("--quiet", action="store_true", help="suppress the terminal summary")
    s.add_argument("--history", metavar="DIR",
                   help="save this run to DIR and report the change since the last one")
    s.add_argument(
        "--fail-on",
        choices=[SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, SEV_LOW],
        help="exit 1 if any finding is at or above this severity (for CI gating)",
    )

    g = s.add_argument_group("upstream lookups")
    g.add_argument("--offline", action="store_true", help="serve from cache only, never open a socket")
    g.add_argument("--no-github", action="store_true", help="skip all GitHub queries (no issue delta)")
    g.add_argument("--no-az", action="store_true", help="skip the az CLI (Azure API versions unresolved)")
    g.add_argument("--no-cache", action="store_true", help="bypass the on-disk cache")
    g.add_argument("--cache-dir", metavar="DIR", help="override the cache directory")
    g.add_argument("--cache-ttl", type=int, metavar="SECONDS", help="cache lifetime (default 21600)")
    g.add_argument("--workers", type=int, default=8, metavar="N", help="parallel resolvers (default 8)")
    g.add_argument(
        "--delta-threshold",
        choices=[SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, SEV_LOW],
        default=SEV_HIGH,
        help="minimum severity that earns a GitHub issue delta (default high)",
    )
    g.add_argument("--max-delta", type=int, default=25, metavar="N",
                   help="cap on components given a GitHub delta (default 25)")

    g.add_argument("--no-vulns", action="store_true",
                   help="skip the OSV advisory lookup (no CVE data, no "
                        "\"fixed by upgrading\" analysis)")

    v = s.add_argument_group("upstream project health (velocity)")
    v.add_argument("--velocity", action="store_true",
                   help="measure upstream issue/sentiment/release velocity per finding")
    v.add_argument("--velocity-threshold",
                   choices=[SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, SEV_LOW],
                   default=SEV_HIGH,
                   help="minimum severity that earns a velocity measurement (default high)")
    v.add_argument("--velocity-windows", type=int, default=4, metavar="N",
                   help="number of comparison windows (default 4)")
    v.add_argument("--velocity-window-days", type=int, default=90, metavar="D",
                   help="length of each window in days (default 90)")
    v.add_argument("--max-velocity", type=int, default=6, metavar="N",
                   help="cap on distinct upstream repos measured (default 6)")
    v.add_argument("--velocity-max-pages", type=int, default=20, metavar="N",
                   help="issue-history pages per repo (default 20 = 2000 issues); "
                        "windows the walk cannot reach are excluded from the trend")

    lz = s.add_argument_group("live Azure introspection")
    lz.add_argument("--live-azure", action="store_true", help="also inspect deployed Azure resources (read-only)")
    lz.add_argument("--subscription", metavar="ID", help="Azure subscription for the live scan")
    lz.add_argument("--resource-group", metavar="NAME", help="limit the live scan to one resource group")

    tr = sub.add_parser(
        "trend",
        help="compare stored runs to show whether the estate is catching up",
        description=(
            "Diff two runs saved by `scan --history DIR`. Findings are matched by "
            "ecosystem and name, so a component that moved 2.0 -> 3.0 reads as the "
            "same component improving rather than as one finding vanishing and "
            "another appearing."
        ),
    )
    tr.add_argument("directory", help="history directory written by --history")
    tr.add_argument("--from", dest="from_run", metavar="FILE",
                    help="baseline run (default: second most recent)")
    tr.add_argument("--to", dest="to_run", metavar="FILE",
                    help="comparison run (default: most recent)")
    tr.add_argument("--json", metavar="FILE", help="write the diff as JSON (- for stdout)")
    tr.add_argument("--list", action="store_true", help="list stored runs and exit")

    sub.add_parser(
        "ecosystems",
        help="list the names accepted by --ecosystem",
        description="List ecosystem groups and the individual ecosystems they cover.",
    )
    return p


def cmd_ecosystems() -> int:
    print("groups:")
    for name, members in sorted(ECOSYSTEM_GROUPS.items()):
        print(f"  {name:12} {', '.join(sorted(members))}")
    print("\nlive-only (require --live-azure):")
    print("  azure-sku, azure-node-image, k8s-cluster")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    try:
        with open_source(args.path, ref=args.ref, keep=args.keep_clone,
                         ado_org=args.ado_org) as src:
            return _scan_source(args, src)
    except CloneError as e:
        print(f"driftscope: {e}", file=sys.stderr)
        return 2


def _scan_source(args: argparse.Namespace, src) -> int:
    root = src.path
    if src.cloned:
        print(
            f"cloned {src.remote} @ {(src.commit or '')[:8]} ({src.ref})",
            file=sys.stderr,
        )

    ctx = build_context(
        offline=args.offline,
        no_github=args.no_github,
        no_az=args.no_az,
        cache_dir=Path(args.cache_dir).expanduser() if args.cache_dir else None,
        cache_ttl=args.cache_ttl,
        no_cache=args.no_cache,
    )

    report = run_scan(
        root,
        ctx=ctx,
        ecosystems=_parse_ecosystems(args.ecosystem),
        workers=args.workers,
        delta_threshold=args.delta_threshold,
        max_delta=args.max_delta,
        include_live_azure=args.live_azure,
        live_subscription=args.subscription,
        live_resource_group=args.resource_group,
        include_vulns=not args.no_vulns,
        include_velocity=args.velocity,
        velocity_threshold=args.velocity_threshold,
        velocity_windows=args.velocity_windows,
        velocity_window_days=args.velocity_window_days,
        velocity_max_pages=args.velocity_max_pages,
        max_velocity=args.max_velocity,
    )

    # Attribute the report to what the user asked for, not a temp directory.
    report.root = src.display
    if src.cloned:
        report.source = {
            "remote": src.remote, "ref": src.ref, "commit": src.commit,
        }

    # ---- history: save this run, then report movement since the previous one
    if args.history:
        from . import history

        hist_dir = Path(args.history).expanduser()
        payload = render.to_json(report)
        saved = history.save(payload, hist_dir)
        print(f"saved run to {saved}", file=sys.stderr)
        d = history.diff_latest(hist_dir, json.loads(payload), current_path=saved)
        if d is None:
            print("no earlier run to compare against yet", file=sys.stderr)
        else:
            report.trend = d


    if args.json:
        text = render.to_json(report)
        if args.json == "-":
            print(text)
        else:
            out = resolve_out_path(args.json, src, "json")
            out.write_text(text)
            print(f"wrote {out}", file=sys.stderr)
    if args.markdown:
        text = render.to_markdown(report)
        if args.markdown == "-":
            print(text)
        else:
            out = resolve_out_path(args.markdown, src, "md")
            out.write_text(text)
            print(f"wrote {out}", file=sys.stderr)
    if args.html:
        from .report import html as html_report

        out = resolve_out_path(args.html, src, "html")
        out.write_text(html_report.to_html(report))
        print(f"wrote {out}", file=sys.stderr)
        if args.open:
            import subprocess

            opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
            try:
                subprocess.run([opener, str(out)], check=False)
            except OSError as exc:
                print(f"could not open browser: {exc}", file=sys.stderr)
    elif args.open:
        print("driftscope: --open requires --html", file=sys.stderr)

    if not args.quiet and args.json != "-" and args.markdown != "-":
        sys.stdout.write(render.to_text(report))
        if report.trend is not None:
            from . import history as _h

            sys.stdout.write("\n" + _h.to_text(report.trend))

    return exit_code_for(report, args.fail_on)


def cmd_trend(args: argparse.Namespace) -> int:
    from . import history

    directory = Path(args.directory).expanduser()
    runs = history.list_runs(directory)
    if args.list:
        if not runs:
            print(f"no runs in {directory}", file=sys.stderr)
            return 2
        for p in runs:
            body = history.load(p) or {}
            counts = body.get("summary") or {}
            total = sum(counts.values())
            print(f"{p.name}  {total:>4} findings  {counts}")
        return 0
    if len(runs) < 2 and not (args.from_run and args.to_run):
        print(
            f"need at least two runs to compare; {directory} has {len(runs)}",
            file=sys.stderr,
        )
        return 2

    old_p = Path(args.from_run).expanduser() if args.from_run else runs[-2]
    new_p = Path(args.to_run).expanduser() if args.to_run else runs[-1]
    old, new = history.load(old_p), history.load(new_p)
    if old is None or new is None:
        print("could not read one of the runs", file=sys.stderr)
        return 2

    d = history.diff(old, new, from_name=old_p.name, to_name=new_p.name)
    if args.json:
        from dataclasses import asdict

        text = json.dumps(asdict(d), indent=2)
        if args.json == "-":
            print(text)
        else:
            # `trend` has no Source: it reads a history directory, not a repo.
            # Passing the undefined `src` here was a NameError on every
            # `trend --json <file>` -- the path `--json -` never touches.
            out = Path(args.json).expanduser()
            if out.is_dir():
                out = out / f"drift-trend-{directory.name}.json"
            out.write_text(text)
            print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(history.to_text(d))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ecosystems":
        return cmd_ecosystems()
    if args.command == "trend":
        return cmd_trend(args)
    if args.command == "scan":
        try:
            return cmd_scan(args)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

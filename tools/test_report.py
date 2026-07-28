"""Render a detailed HTML report from a test run: results, coverage, mutations.

    python3 tools/test_report.py [-o report.html]

Gathers three things the terminal shows separately and a reader has to hold in
their head at once:

  pytest --junitxml   per-test outcome and duration
  coverage json       per-module line coverage
  tools/mutation_test per-mutation kill/survive

Colour follows the job, not taste. Pass/fail and kill/survive are **status**, so
they use the reserved status palette and always ship an icon plus a label -- never
hue alone. Coverage is a **magnitude**, so its bars are one sequential hue, with a
separate status dot for the band. Those are different jobs and must not share an
encoding, or "80%" and "passing" would read as the same claim.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv/bin/python"


# --------------------------------------------------------------------- gather


def run_tests(tmp: Path) -> dict:
    junit = tmp / "junit.xml"
    subprocess.run(
        [str(PY), "-m", "pytest", "tests/", "-q", "--junitxml", str(junit),
         "-p", "no:randomly"],
        cwd=ROOT, capture_output=True, text=True, timeout=900,
    )
    root = ET.parse(junit).getroot()
    suite = root[0] if root.tag == "testsuites" else root

    files: dict[str, dict] = defaultdict(
        lambda: {"passed": 0, "failed": 0, "skipped": 0, "time": 0.0, "cases": []})
    for case in suite.iter("testcase"):
        # classname is "tests.test_forge" or "tests.test_forge.ClassName"
        mod = (case.get("classname") or "").split(".")
        fname = mod[1] if len(mod) > 1 else (mod[0] or "unknown")
        rec = files[fname]
        t = float(case.get("time") or 0.0)
        rec["time"] += t
        if case.find("failure") is not None or case.find("error") is not None:
            rec["failed"] += 1
            outcome = "failed"
        elif case.find("skipped") is not None:
            rec["skipped"] += 1
            outcome = "skipped"
        else:
            rec["passed"] += 1
            outcome = "passed"
        rec["cases"].append({"name": case.get("name"), "time": t, "outcome": outcome})
    return {
        "total": int(suite.get("tests") or 0),
        "failures": int(suite.get("failures") or 0),
        "errors": int(suite.get("errors") or 0),
        "skipped": int(suite.get("skipped") or 0),
        "time": float(suite.get("time") or 0.0),
        "files": dict(files),
    }


def run_coverage(tmp: Path) -> dict:
    out = tmp / "cov.json"
    subprocess.run([str(PY), "-m", "coverage", "run", "-m", "pytest", "tests/", "-q"],
                   cwd=ROOT, capture_output=True, text=True, timeout=900)
    subprocess.run([str(PY), "-m", "coverage", "json", "-o", str(out),
                    "--include=driftscope/*"],
                   cwd=ROOT, capture_output=True, text=True, timeout=300)
    data = json.loads(out.read_text())
    mods = []
    for path, f in sorted(data["files"].items()):
        s = f["summary"]
        if s["num_statements"] == 0:
            continue
        mods.append({
            "name": path.replace("driftscope/", ""),
            "pct": s["percent_covered"],
            "statements": s["num_statements"],
            "missing": s["missing_lines"],
        })
    return {"total": data["totals"]["percent_covered"], "modules": mods}


def read_mutations(text: str) -> dict:
    killed, survived, skipped = [], [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("killed   "):
            killed.append(s[len("killed   "):])
        elif s.startswith("SURVIVED "):
            survived.append(s[len("SURVIVED "):])
        elif s.startswith("SKIP    "):
            skipped.append(s[len("SKIP    "):])
    return {"killed": killed, "survived": survived, "skipped": skipped}


def git_info() -> dict:
    def g(*args):
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        return (r.stdout or "").strip()
    return {"branch": g("rev-parse", "--abbrev-ref", "HEAD"),
            "commit": g("rev-parse", "--short", "HEAD"),
            "subject": g("log", "-1", "--pretty=%s")}


# --------------------------------------------------------------------- render

E = lambda s: html.escape(str(s), quote=True)


def band(pct: float) -> tuple[str, str, str]:
    """(status role, icon, label) for a coverage percentage.

    Coverage gets a status *dot plus label* beside its magnitude bar rather than a
    colour-coded bar: the bar answers "how much", the dot answers "is that ok".
    Conflating them would make a long bar and a good verdict the same mark.
    """
    if pct >= 80:
        return "good", "●", "good"
    if pct >= 50:
        return "warning", "▲", "thin"
    return "critical", "■", "weak"


def bar(pct: float, role: str) -> str:
    # 4px rounded data-end anchored to the baseline; the track is a hairline
    # surface so a near-zero bar is still visibly a bar rather than nothing.
    return (f"<span class='track'><i class='fill {role}' "
            f"style='width:{max(pct, 1.2):.4g}%'></i></span>")


def render(tests: dict, cov: dict, mut: dict, git: dict) -> str:
    passed = tests["total"] - tests["failures"] - tests["errors"] - tests["skipped"]
    all_green = tests["failures"] == 0 and tests["errors"] == 0
    mk, ms = len(mut["killed"]), len(mut["survived"])
    mtotal = mk + ms

    files = sorted(tests["files"].items(),
                   key=lambda kv: -(kv[1]["passed"] + kv[1]["failed"]))
    biggest = max((v["passed"] + v["failed"] + v["skipped"] for _, v in files),
                  default=1)
    mods = sorted(cov["modules"], key=lambda m: m["pct"])

    def tile(value, label, role, icon, sub=""):
        return (f"<div class='tile'><div class='n {role}-ink'>{E(value)}</div>"
                f"<div class='k'><span class='dot {role}'>{icon}</span>{E(label)}</div>"
                + (f"<div class='sub'>{E(sub)}</div>" if sub else "") + "</div>")

    tiles = [
        tile(f"{passed:,}", "tests passed", "good", "●",
             f"{tests['time']:.2f}s"),
        tile(tests["failures"] + tests["errors"], "failed",
             "good" if all_green else "critical", "●" if all_green else "✕",
             "no failures" if all_green else "see below"),
        tile(f"{cov['total']:.0f}%", "line coverage", *band(cov["total"])[:2],
             f"{len(mods)} modules"),
        tile(f"{mk}/{mtotal}", "mutations killed",
             "good" if ms == 0 else "critical", "●" if ms == 0 else "✕",
             "injected bugs caught"),
    ]

    file_rows = []
    for name, rec in files:
        n = rec["passed"] + rec["failed"] + rec["skipped"]
        role = "critical" if rec["failed"] else "good"
        icon = "✕" if rec["failed"] else "●"
        file_rows.append(
            f"<tr><td class='name'>{E(name)}.py</td>"
            f"<td class='v'>{n:,}</td>"
            f"<td class='barcell'>{bar(100 * n / biggest, 'series')}</td>"
            f"<td class='v mut'>{rec['time']:.3f}s</td>"
            f"<td><span class='badge {role}'><span class='g'>{icon}</span>"
            f"{'all passed' if not rec['failed'] else str(rec['failed']) + ' failed'}"
            f"</span></td></tr>")

    mod_rows = []
    for m in mods:
        role, icon, word = band(m["pct"])
        mod_rows.append(
            f"<tr><td class='name'>{E(m['name'])}</td>"
            f"<td class='v'>{m['pct']:.0f}%</td>"
            f"<td class='barcell'>{bar(m['pct'], 'series')}</td>"
            f"<td class='v mut'>{m['statements']}</td>"
            f"<td class='v mut'>{m['missing'] or ''}</td>"
            f"<td><span class='badge {role}'><span class='g'>{icon}</span>"
            f"{E(word)}</span></td></tr>")

    mut_rows = []
    for label in mut["killed"]:
        mut_rows.append(
            f"<tr><td class='name'>{E(label)}</td>"
            f"<td><span class='badge good'><span class='g'>●</span>killed</span>"
            f"</td></tr>")
    for label in mut["survived"]:
        mut_rows.append(
            f"<tr><td class='name'>{E(label)}</td>"
            f"<td><span class='badge critical'><span class='g'>✕</span>"
            f"SURVIVED</span></td></tr>")

    generated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return TEMPLATE.format(
        tiles="".join(tiles),
        file_rows="".join(file_rows),
        mod_rows="".join(mod_rows),
        mut_rows="".join(mut_rows),
        n_files=len(files),
        n_mods=len(mods),
        n_mut=mtotal,
        branch=E(git["branch"]), commit=E(git["commit"]),
        subject=E(git["subject"]), generated=E(generated),
        verdict="all green" if all_green and ms == 0 else "attention needed",
        verdict_role="good" if all_green and ms == 0 else "critical",
        verdict_icon="●" if all_green and ms == 0 else "✕",
    )


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>driftscope &middot; test report</title>
<style>
  :root {{
    color-scheme: light;
    --plane:#f9f9f7; --surface:#fcfcfb;
    --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7;
    --series:#2a78d6; --track:#eceae4;
    --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) {{
      color-scheme: dark;
      --plane:#0d0d0d; --surface:#1a1a19;
      --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835;
      --series:#3987e5; --track:#262624;
    }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--plane); color:var(--ink);
    font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 64px; }}
  header {{ margin-bottom:24px; }}
  h1 {{ font-size:22px; margin:0 0 6px; letter-spacing:-.01em; }}
  h2 {{ font-size:15px; margin:0 0 2px; letter-spacing:-.005em; }}
  .meta {{ color:var(--ink-2); font-size:13px; }}
  .meta code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    background:var(--surface); border:1px solid var(--grid);
    border-radius:4px; padding:1px 5px; }}
  .card {{ background:var(--surface); border:1px solid var(--grid);
    border-radius:10px; padding:18px 20px; margin:16px 0; }}
  .note {{ color:var(--muted); font-size:12.5px; margin-top:10px; }}

  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
    gap:12px; margin:16px 0; }}
  .tile {{ background:var(--surface); border:1px solid var(--grid);
    border-radius:10px; padding:16px 18px; }}
  .tile .n {{ font-size:30px; font-weight:600; letter-spacing:-.02em;
    font-variant-numeric:tabular-nums; line-height:1.1; }}
  .tile .k {{ color:var(--ink-2); font-size:13px; margin-top:6px;
    display:flex; align-items:center; gap:6px; }}
  .tile .sub {{ color:var(--muted); font-size:12px; margin-top:2px; }}

  /* Status is never colour alone: every dot ships beside its label. */
  .dot {{ font-size:10px; line-height:1; }}
  .dot.good, .good-ink {{ color:var(--good); }}
  .dot.warning, .warning-ink {{ color:var(--warning); }}
  .dot.critical, .critical-ink {{ color:var(--critical); }}
  .badge {{ display:inline-flex; align-items:center; gap:5px;
    font-size:12px; color:var(--ink-2); white-space:nowrap; }}
  .badge .g {{ font-size:9px; line-height:1; }}
  .badge.good .g {{ color:var(--good); }}
  .badge.warning .g {{ color:var(--warning); }}
  .badge.critical .g {{ color:var(--critical); font-size:11px; }}

  table {{ width:100%; border-collapse:collapse; margin-top:12px; }}
  th {{ text-align:left; font-weight:500; font-size:12px; color:var(--muted);
    padding:0 10px 8px 0; border-bottom:1px solid var(--grid); }}
  td {{ padding:7px 10px 7px 0; border-bottom:1px solid var(--grid);
    vertical-align:middle; }}
  tr:last-child td {{ border-bottom:none; }}
  .name {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    font-size:12.5px; }}
  .v {{ text-align:right; font-variant-numeric:tabular-nums; width:1%;
    white-space:nowrap; }}
  .mut {{ color:var(--muted); }}
  .barcell {{ width:38%; }}

  /* Thin mark, 4px rounded data-end, anchored to a hairline track. */
  .track {{ display:block; height:8px; background:var(--track);
    border-radius:4px; overflow:hidden; }}
  .fill {{ display:block; height:100%; border-radius:4px;
    background:var(--series); }}
  .fill.series {{ background:var(--series); }}

  details summary {{ cursor:pointer; color:var(--ink-2); font-size:13px;
    padding:4px 0; }}
  .legend {{ display:flex; gap:16px; flex-wrap:wrap; margin-top:10px;
    color:var(--ink-2); font-size:12.5px; }}
</style></head>
<body><div class="wrap">
<header>
  <h1>driftscope &middot; test report</h1>
  <div class="meta">
    <code>{branch}</code> at <code>{commit}</code> &mdash; {subject}<br>
    Generated {generated} &middot;
    <span class="badge {verdict_role}"><span class="g">{verdict_icon}</span>{verdict}</span>
  </div>
</header>

<div class="tiles">{tiles}</div>

<div class="card">
  <h2>Tests by file</h2>
  <div class="meta">{n_files} files. Bar length is the file's share of the largest
  suite &mdash; a magnitude, not a score.</div>
  <table>
    <thead><tr><th>File</th><th class="v">Tests</th><th>Relative size</th>
      <th class="v">Time</th><th>Result</th></tr></thead>
    <tbody>{file_rows}</tbody>
  </table>
</div>

<div class="card">
  <h2>Coverage by module</h2>
  <div class="meta">{n_mods} modules, lowest first. The bar is <em>how much</em> is
  covered; the badge beside it is <em>whether that is enough</em>. They are separate
  encodings on purpose &mdash; a long bar and a good verdict are different claims.</div>
  <table>
    <thead><tr><th>Module</th><th class="v">Covered</th><th>Coverage</th>
      <th class="v">Stmts</th><th class="v">Missing</th><th>Band</th></tr></thead>
    <tbody>{mod_rows}</tbody>
  </table>
  <div class="legend">
    <span class="badge good"><span class="g">&#9679;</span>good &mdash; 80% and above</span>
    <span class="badge warning"><span class="g">&#9650;</span>thin &mdash; 50&ndash;79%</span>
    <span class="badge critical"><span class="g">&#9632;</span>weak &mdash; under 50%</span>
  </div>
</div>

<div class="card">
  <h2>Mutation testing</h2>
  <div class="meta">{n_mut} deliberate bugs injected into a throwaway copy, each
  targeting an invariant this project has been burned by. <strong>Killed</strong>
  means the suite noticed. <strong>SURVIVED</strong> means the bug shipped and every
  test still passed &mdash; those are the holes.</div>
  <details open><summary>All {n_mut} mutations</summary>
  <table><tbody>{mut_rows}</tbody></table>
  </details>
  <div class="note">A green suite proves the tests pass, not that they would catch
  anything. This is the check on the check.</div>
</div>

</div></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", default="test-report.html")
    args = ap.parse_args()

    tmp = ROOT / ".report-tmp"
    tmp.mkdir(exist_ok=True)
    print("running tests…")
    tests = run_tests(tmp)
    print("measuring coverage…")
    cov = run_coverage(tmp)
    print("running mutations…")
    r = subprocess.run([sys.executable, str(ROOT / "tools/mutation_test.py")],
                       cwd=ROOT, capture_output=True, text=True, timeout=2400)
    mut = read_mutations(r.stdout or "")

    out = Path(args.out)
    out.write_text(render(tests, cov, mut, git_info()))
    passed = tests["total"] - tests["failures"] - tests["errors"] - tests["skipped"]
    print(f"\n{passed} passed, {tests['failures'] + tests['errors']} failed, "
          f"{cov['total']:.0f}% coverage, "
          f"{len(mut['killed'])}/{len(mut['killed']) + len(mut['survived'])} mutations killed")
    print(f"wrote {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

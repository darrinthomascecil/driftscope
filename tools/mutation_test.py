"""Mutation test: inject a real bug, see whether the suite notices.

A green suite proves the tests pass, not that they would catch anything. Each
mutation below is a plausible edit -- the kind a refactor or a "simplification"
would produce -- aimed at an invariant this project has already been burned by.

SURVIVED means the bug was introduced and every test still passed. Those are the
holes.

Runs against a throwaway copy; the real repo is never touched.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.gettempdir()) / "driftscope-mutate"

# (label, file, find, replace, why it matters)
MUTATIONS = [
    # --- version algebra: the comparison core
    ("version: flip behind-major direction", "driftscope/version.py",
     "behind_major=max(0, lv.major - cv.major)",
     "behind_major=max(0, cv.major - lv.major)",
     "every 'N majors behind' number inverts"),
    ("version: treat prereleases as stable", "driftscope/version.py",
     "if not p.is_prerelease and (best_stable is None or k >= best_stable[0]):",
     "if (best_stable is None or k >= best_stable[0]):",
     "recommends upgrading to an rc/beta"),
    ("version: ignore docker variant mismatch", "driftscope/version.py",
     'if a.kind == SEMVER and a.variant != b.variant:',
     'if False:',
     "compares 3.12-slim against 3.13-alpine"),
    ("version: drop the calver branch", "driftscope/version.py",
     "m = _CALVER_RE.match(s)",
     "m = None",
     "Azure API dates parse as semver"),

    # --- severity: the rules a customer will argue with
    ("advisor: node images use generic scale", "driftscope/advisor.py",
     "if component.ecosystem == AZURE_NODE_IMAGE:",
     "if False:",
     "a 6-month-old node image rates LOW"),
    ("advisor: runtimes scored on distance", "driftscope/advisor.py",
     "if component.ecosystem in (JVM_TOOLCHAIN, DOTNET_TFM, DOTNET_SDK):",
     "if False:",
     "Java 17 LTS rates CRITICAL"),
    ("advisor: floating tags rate CURRENT", "driftscope/advisor.py",
     'if component.meta.get("floating") or component.meta.get("untagged"):\n        return SEV_MEDIUM',
     'if component.meta.get("floating") or component.meta.get("untagged"):\n        return SEV_CURRENT',
     "unpinned images stop being findings"),
    ("advisor: age never escalates", "driftscope/advisor.py",
     "if days >= STALE_DAYS_HIGH and sev in (SEV_LOW, SEV_MEDIUM):",
     "if False:",
     "a 3-year-old pin stays LOW"),
    ("advisor: k8s removed == deprecated", "driftscope/advisor.py",
     'if "removed in" in note:\n            return SEV_CRITICAL',
     'if "removed in" in note:\n            return SEV_HIGH',
     "a removed API reads as merely deprecated"),

    # --- the sentinel discipline: 'unknown' collapsing into 'zero'
    ("forge: NOT_SAMPLED becomes 0", "driftscope/forge/__init__.py",
     "NOT_SAMPLED = -1",
     "NOT_SAMPLED = 0",
     "unsampled sentiment reads as zero frustration"),
    ("forge: unreadable project returns []", "driftscope/forge/ado.py",
     "        if ids is None:\n            # Unreadable: missing project, no permission, or the call failed.\n            return IssueWalk(items=None)",
     "        if ids is None:\n            return IssueWalk(items=[])",
     "a 403 reads as a project with no work"),
    ("forge: failed count returns 0", "driftscope/forge/ado.py",
     "        if total_ids is None:\n            return None, None",
     "        if total_ids is None:\n            return 0, 0",
     "a failed query reads as a clean project"),
    ("forge: hardcode 'Closed' state", "driftscope/forge/ado.py",
     "return self._state_categories(org, project, wi_type).get(state) in CLOSED_CATEGORIES",
     'return state == "Closed"',
     "Scrum/Agile projects report zero closures"),
    ("forge: stop filtering test artifacts", "driftscope/forge/ado.py",
     'if (f.get("System.WorkItemType") or "").lower() in NON_ISSUE_TYPES:',
     "if False:",
     "test cases inflate the backlog"),
    ("forge: lightweight tag dated from now", "driftscope/forge/ado.py",
     "            published, message = None, \"\"",
     "            published, message = date(2026, 1, 1), \"\"",
     "an undated tag gets a fabricated date"),

    # --- vulnerabilities: the redis/postgres lessons
    ("vuln: drop the indiscriminate guard", "driftscope/vuln.py",
     "elif rep.approximate and remaining and not fixed and not introduced:",
     "elif False:",
     "72 CVEs claimed as 'still present after upgrade'"),
    ("vuln: drop the unmatched guard", "driftscope/vuln.py",
     "if rep.approximate and not (remaining or fixed or introduced):",
     "if False:",
     "a zero-result query reads as a clean image"),

    # --- reporting: what the customer actually reads
    ("delta: quote section headings again", "driftscope/ghub/delta.py",
     "            if _is_section_heading(s):\n                continue",
     "            pass",
     "'DEPRECATIONS:' counted as a breaking change"),
    ("summarize: vuln detail cap 6 -> 3", "driftscope/report/summarize.py",
     "VULN_DETAIL_CAP = 6",
     "VULN_DETAIL_CAP = 3",
     "silently shows less than the report claims"),
    ("summarize: indiscriminate splits anyway", "driftscope/report/summarize.py",
     "        headline_version=None if r.indiscriminate else r.current_version,",
     "        headline_version=r.current_version,",
     "asserts 'N affecting <version>' on unusable data"),

    # --- source/permalinks
    ("source: stop stripping userinfo", "driftscope/source.py",
     "    return _USERINFO_RE.sub(r\"\\1\", url) if url else url",
     "    return url",
     "a credential-shaped string appears in reports"),
    ("html: permalink to branch not commit", "driftscope/report/html.py",
     'ref = src.get("commit") or src.get("ref") or "HEAD"',
     'ref = src.get("ref") or "HEAD"',
     "links rot the moment the branch moves"),
    # --- resolvers: the layer the contract test was added to cover
    ("resolver: invent latest from the pin", "driftscope/resolvers/packages.py",
     "        latest_stable=latest_stable(usable) or declared,",
     "        latest_stable=latest_stable(usable) or declared or c.current,",
     "a dead registry makes every package look current"),
    ("resolver: emit unparseable declared version", "driftscope/resolvers/packages.py",
     "    if declared is not None and not parse(str(declared)).valid:\n        declared = None",
     "    pass",
     "junk from the registry lands in the report"),
    ("resolver: swallow transport errors silently", "driftscope/resolvers/packages.py",
     "    except HttpError as e:\n        return Resolution(error=str(e))\n    info = body.get",
     "    except HttpError as e:\n        return Resolution()\n    info = body.get",
     "a 503 renders as a clean resolution with no error"),
    ("dispatcher: let resolver bugs escape", "driftscope/resolvers/__init__.py",
     "    except Exception as e:  # resolver bugs must not abort the run\n        return Resolution(error=f\"{type(e).__name__}: {e}\")",
     "    except None:\n        pass",
     "one bad resolver aborts the entire scan"),
]



def run() -> tuple[int, str]:
    r = subprocess.run(
        [str(WORK / ".venv/bin/python"), "-m", "pytest", "tests/", "-q",
         "--no-header", "-x", "-p", "no:randomly"],
        cwd=WORK, capture_output=True, text=True, timeout=600,
    )
    return r.returncode, (r.stdout or "")[-400:]


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    subprocess.run(["cp", "-R", str(SRC), str(WORK)], check=True)
    print(f"working copy: {WORK}\n")

    code, out = run()
    if code != 0:
        print("BASELINE IS RED -- aborting\n", out)
        return 1
    print(f"baseline: green ({re.search(r'(\d+) passed', out).group(1)} tests)\n")

    killed, survived, skipped = [], [], []
    for label, rel, find, repl, why in MUTATIONS:
        p = WORK / rel
        original = p.read_text()
        if find not in original:
            skipped.append((label, "pattern not found"))
            print(f"  SKIP    {label}  (pattern not found)")
            continue
        p.write_text(original.replace(find, repl, 1))
        try:
            code, out = run()
        finally:
            p.write_text(original)
        if code == 0:
            survived.append((label, why))
            print(f"  SURVIVED {label}")
        else:
            killed.append(label)
            print(f"  killed   {label}")

    total = len(killed) + len(survived)
    print(f"\n{'='*70}")
    print(f"killed {len(killed)}/{total}"
          + (f"   ({len(skipped)} skipped)" if skipped else ""))
    if survived:
        print("\nSURVIVED -- bug introduced, suite still green:")
        for label, why in survived:
            print(f"  - {label}\n      consequence: {why}")
    for label, reason in skipped:
        print(f"\nskipped: {label} ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

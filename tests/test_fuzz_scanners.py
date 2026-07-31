"""Regression guard against catastrophic parser inputs (ReDoS / superlinear / parser bombs).

The scanners parse UNTRUSTED repo files. Fuzzing found FIVE superlinear bugs -- ruby (long gem
line + O(gems x text) group lookup), containers (`^\\s*FROM` scanning across newlines), terraform
(`_ENTRY` backtracking a huge required_providers body), and langdeps (a hostile pyproject.toml
driving tomllib O(n^2) into a multi-MINUTE hang). Each was 4-5s (or worse) on a small hostile file.

Two guards:
  * explicit known-pathological inputs reproducing each fixed bug (bounded so a revert is slow,
    not an infinite hang), asserted under a budget;
  * a DETERMINISTIC fuzz slice run in a SUBPROCESS with a hard timeout -- because an in-process
    fuzzer cannot interrupt a C-level regex or a tomllib O(n^2) parse, so a hanging input would
    freeze the whole suite (which is exactly how this failed once). In a subprocess, a hang is a
    clean timeout failure and a slow-but-returning input is a non-zero exit; both fail the test.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import fuzz_scanners as F

_REPO = Path(__file__).parent.parent
# Generous on purpose: catch CATASTROPHIC (superlinear) regressions -- 4-5s locally, 15s+ on CI's
# slower shared runners -- without policing CI hardware variance on bounded inputs. Fixed cases
# run well under this; a reverted fix blows far past it.
_BUDGET = 4.0

# Reproduce each FIXED bug with a BOUNDED input, so a revert is slow-but-returning (caught by the
# budget) rather than an infinite hang that would freeze this in-process test.
_KNOWN_BAD = [
    ("Gemfile", b'gem "x", "' + b"a" * 80_000 + b'"\n'),        # ruby _spec_from long line
    ("Gemfile", b'gem "a", "1.0"\n' * 6000),                     # ruby _group_at O(gems x text)
    ("Dockerfile", b"FROM a\n" + b"RUN x\n" * 20_000 + b"FROM b AS c\n"),  # containers cross-line
    ("main.tf", b"required_providers {\n" + b"a" * 80_000 + b"\n"),        # terraform _ENTRY body
    ("pyproject.toml", b"a" + b".a" * 30_000 + b" = 1\n"),       # langdeps tomllib bomb (skipped)
]


@pytest.mark.parametrize("name,payload", _KNOWN_BAD)
def test_known_pathological_inputs_are_fast(name, payload, tmp_path):
    p = tmp_path / name
    p.write_bytes(payload)
    for sname, fn in F._SCANNERS.items():
        t0 = time.perf_counter()
        try:
            fn(p, tmp_path)
        except Exception:  # noqa: BLE001 -- crashes are contained by scan_tree; we test timing
            pass
        dt = time.perf_counter() - t0
        assert dt < _BUDGET, f"{sname} took {dt*1000:.0f}ms on {name} ({len(payload)} bytes)"


def test_fuzz_finds_no_catastrophic_input():
    # Run the fuzzer in a SUBPROCESS with a hard timeout so a hanging input is a clean failure,
    # not a frozen suite. Internal budget 15s: catches truly-catastrophic blowups (15s+ on CI)
    # while tolerating bounded-but-slow inputs across CI hardware variance.
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "tests.fuzz_scanners", "5000", "15"],
            cwd=_REPO, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("fuzzer hung (>180s) -- a scanner has an unbounded/exponential input")
    assert proc.returncode == 0, (
        f"fuzzer found a catastrophic input (exit {proc.returncode}):\n{proc.stdout[-2000:]}"
    )

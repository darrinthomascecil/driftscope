"""Regression guard against catastrophic parser inputs (ReDoS / superlinear).

The scanners parse UNTRUSTED repo files. Fuzzing found four superlinear bugs -- ruby (long gem
line + O(gems x text) group lookup), containers (`^\\s*FROM` scanning across newlines), and
terraform (`_ENTRY` backtracking a huge required_providers body) -- each 4-5s on a small hostile
file. This runs a bounded, DETERMINISTIC slice of the fuzzer (fixed seed) so a revert of any fix
regenerates the same slow input and pushes a `scan_file` over budget, failing here.

Plus explicit known-pathological inputs, so the guard survives even if the fuzzer's generation
changes. Budget 1.0s: the fixed worst is ~0.6s; the bugs were 4-5s.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from tests import fuzz_scanners as F

_BUDGET = 1.0


# Directly reproduce the shape of each fixed bug -- these must all parse fast.
_KNOWN_BAD = [
    ("Gemfile", b'gem "x", "' + b"a" * 80_000 + b'"\n'),        # ruby _spec_from long line
    ("Gemfile", b'gem "a", "1.0"\n' * 6000),                     # ruby _group_at O(gems x text)
    ("Dockerfile", b"FROM a\n" + b"RUN x\n" * 20_000 + b"FROM b AS c\n"),  # containers cross-line
    ("main.tf", b"required_providers {\n" + b"a" * 80_000 + b"\n"),        # terraform _ENTRY body
    ("requirements.txt", b"pkg==1.0\n" * 100_000),              # langdeps: linear but large
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
    # Same seed that surfaced the original bugs -> a reverted fix re-triggers the slow input.
    res = F.run_fuzz(iterations=6000, seed=7, budget=_BUDGET, tmp=Path(tempfile.mkdtemp()))
    assert not res.slow, f"scan_file exceeded {_BUDGET}s: {res.slow[:3]}  ({res.summary()})"
    assert "MemoryError" not in res.crashes, f"unbounded allocation: {res.summary()}"

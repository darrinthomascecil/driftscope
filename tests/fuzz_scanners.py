"""Fuzz the scanner parsers, which parse UNTRUSTED repo files.

`scan_tree` already contains parser *crashes* (a per-file try/except), so the residual risks
this hunts are the ones that are NOT contained: a **hang** (e.g. catastrophic-backtracking
regex) or a runaway **allocation**. It feeds each `scanner.scan_file` mutated real manifests
and format-agnostic adversarial payloads (deep nesting, huge repetition, long lines, NULs,
bad encodings), measures per-call wall-time, and records anything that exceeds a budget or
raises. Seeds come from the fixtures, whose filenames trigger every scanner branch.

Run a heavy pass directly:  python -m tests.fuzz_scanners 20000
The bounded, deterministic slice runs in the suite as test_fuzz_scanners.py.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from driftscope.scanners import azure_iac, cicd, containers, dotnet, jvm, langdeps, ruby, terraform

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_SCANNERS = {
    "terraform": terraform.scan_file, "azure_iac": azure_iac.scan_file,
    "containers": containers.scan_file, "langdeps": langdeps.scan_file,
    "cicd": cicd.scan_file, "dotnet": dotnet.scan_file, "jvm": jvm.scan_file,
    "ruby": ruby.scan_file,
}


def seeds() -> list[tuple[str, bytes]]:
    """(relative-path, bytes) for every fixture file -- real, correctly-named manifests that
    reach each scanner's parser (scanners gate on file name/suffix)."""
    out = []
    for p in sorted(_FIXTURES.rglob("*")):
        if p.is_file():
            out.append((str(p.relative_to(_FIXTURES)), p.read_bytes()))
    return out


def _adversarial() -> list[tuple[str, bytes]]:
    """Format-agnostic payloads, each paired with every seed filename so every parser sees it.
    Sized under the scanners' 2 MB read cap so they actually reach the parser."""
    big = 200_000
    return [
        ("deep-braces", b"{" * big),
        ("deep-brackets", b"[" * big + b"]" * big),
        ("deep-parens", b"(" * big),
        ("deep-quotes", b'"' * big),
        ("nested-toml", b"[" + b"a." * 4000 + b"a]\n"),
        ("nested-json", b"[" * 60_000 + b"]" * 60_000),
        ("one-huge-token", b"a" * 1_500_000),
        ("many-lines", b"x = 1\n" * 40_000),
        ("long-lines", (b"a" * 65_000 + b"\n") * 20),
        ("version-spamm", b"v = " + b"1." * 300_000),
        ("from-bomb", b"FROM " + b"a" * 1_000_000 + b"\n"),
        ("image-bomb", b"image: " + b"a" * 1_000_000 + b"\n"),
        ("nuls", b"\x00" * 300_000),
        ("all-bytes", bytes(range(256)) * 6000),
        ("utf16-bom", b"\xff\xfe" + b"A\x00" * 300_000),
        ("crlf-spam", b"a:1\r\n" * 40_000),
        ("colon-spam", b":" * 500_000),
        ("equals-spam", b"=" * 500_000),
        ("dotted-name", b"a" + b".a" * 100_000 + b" = 1\n"),
    ]


def _mutate(data: bytes, rng: random.Random) -> bytes:
    """One random mutation of `data`: bit flips, truncation, region duplication, or splicing
    an adversarial run into it."""
    if not data:
        data = b"x = 1\n"
    b = bytearray(data)
    op = rng.randrange(5)
    if op == 0:  # bit flips
        for _ in range(rng.randint(1, 32)):
            i = rng.randrange(len(b))
            b[i] ^= 1 << rng.randrange(8)
    elif op == 1:  # truncate
        b = b[: rng.randrange(len(b) + 1)]
    elif op == 2:  # duplicate a region (grow, but bounded by the 2 MB read cap)
        i = rng.randrange(len(b))
        chunk = bytes(b[i : i + rng.randint(1, 4096)])
        for _ in range(rng.randint(1, 200)):
            b += chunk
        b = b[:1_900_000]
    elif op == 3:  # insert a run of one byte
        i = rng.randrange(len(b) + 1)
        b[i:i] = bytes([rng.randrange(256)]) * rng.randint(1, 50_000)
    else:  # splice bytes from another position
        i, j = rng.randrange(len(b)), rng.randrange(len(b))
        b[i:i] = b[j : j + rng.randint(1, 1000)]
    return bytes(b[:1_900_000])


@dataclass
class Findings:
    calls: int = 0
    slow: list = field(default_factory=list)        # (seconds, scanner, label)
    crashes: dict = field(default_factory=dict)      # exc-type name -> count
    worst: float = 0.0

    def summary(self) -> str:
        crash = ", ".join(f"{k}:{v}" for k, v in sorted(self.crashes.items())) or "none"
        return (f"{self.calls} scan_file calls | worst {self.worst*1000:.1f}ms | "
                f"slow(>budget)={len(self.slow)} | exceptions(contained): {crash}")


def run_fuzz(iterations: int = 2000, seed: int = 1234, budget: float = 2.0,
             tmp: Path | None = None) -> Findings:
    """Drive mutated + adversarial inputs through every scanner. `budget` is the per-call
    wall-clock ceiling; anything slower is a `slow` finding (a hang / pathological input)."""
    import tempfile
    rng = random.Random(seed)
    base = tmp or Path(tempfile.mkdtemp(prefix="fuzz-scan-"))
    f = Findings()
    corpus_names = [n for n, _ in seeds()]
    adv = _adversarial()

    def drive(relpath: str, content: bytes, label: str):
        p = base / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        for sname, fn in _SCANNERS.items():
            t0 = time.perf_counter()
            try:
                fn(p, base)
            except Exception as e:  # noqa: BLE001 -- fuzzing: any exception is data
                f.crashes[type(e).__name__] = f.crashes.get(type(e).__name__, 0) + 1
            dt = time.perf_counter() - t0
            f.calls += 1
            f.worst = max(f.worst, dt)
            if dt > budget:
                f.slow.append((dt, sname, f"{relpath}:{label}"))

    # 1. every adversarial payload against every seed filename (all parser branches)
    for relpath in corpus_names:
        for label, payload in adv:
            drive(relpath, payload, label)
    # 2. random mutations of real seeds
    src = seeds()
    for _ in range(iterations):
        relpath, data = src[rng.randrange(len(src))]
        drive(relpath, _mutate(data, rng), "mut")
    return f


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    print(f"fuzzing scanners: {n} mutation iterations + adversarial corpus ...")
    res = run_fuzz(iterations=n)
    print(res.summary())
    for dt, sc, where in sorted(res.slow, reverse=True)[:20]:
        print(f"  SLOW {dt*1000:.0f}ms  {sc}  {where}")

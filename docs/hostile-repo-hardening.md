# Hostile-repo hardening

driftscope's unique threat surface: `POST /scan` **clones and parses an untrusted repository**.
A hostile repo shouldn't be able to exhaust the node, hang a worker, or (now that it's
multi-tenant) starve another tenant's scan sharing the pod. This is that hardening.

## What was already defended

The scanners were already reasonably careful, and this is worth stating so the defenses aren't
re-litigated:

- **Symlink escape** — `walk_files` skips symlinks (no reading host files outside the clone).
- **Huge single file** — `read_text` caps at **2 MB**; larger files are skipped.
- **Millions of files** — `walk_files` caps at **20 000 files** and prunes vendor/VCS dirs.
- **Decompression bombs** — no archive handling exists (nothing to bomb).
- **Clone** — `--depth 1 --single-branch --no-tags`, hooks disabled, `GIT_TERMINAL_PROMPT=0`,
  300 s timeout.
- **Parser crashes** — `scan_tree` wraps each file in `try/except`, so a malformed file can't
  abort a scan.
- **Pod** — non-root, read-only rootfs, dropped capabilities, cpu/memory limits.

## What this adds

### 1. Clone disk bound (infra)

The clone lands in a `/tmp` `emptyDir`, which had **no `sizeLimit`** — a 5 GB repo could fill the
node's ephemeral storage during `git clone`. `deploy/base/deployment.yaml` now sets a `sizeLimit`
on the `tmp` (2 Gi), `cache` (1 Gi), and `home` (64 Mi) volumes, so writing past the limit fails
with **ENOSPC** and the scan errors cleanly instead of taking the node down.

### 2. Per-scan wall-clock deadline

`run_scan(..., scan_deadline=…)` bounds the untrusted-file **parse phase**: `scan_tree` checks an
absolute `time.monotonic()` deadline between files and raises `ScanTimeout` once passed. The server
sets it from **`DRIFTSCOPE_SCAN_TIMEOUT`** seconds (default **120**, `0` disables) and returns
**422** on timeout. This backstops the aggregate CPU one scan can spend, so a large tree can't
monopolise a worker (the server has a small concurrency pool). A single file is already bounded by
the 2 MB cap plus the parser fixes below, so between-files granularity is sufficient.

### 3. Parser hardening — the four superlinear bugs fuzzing found

Fuzzing (`tests/fuzz_scanners.py`) feeds each `scanner.scan_file` mutated real manifests +
adversarial payloads and measures per-call wall-time. It found **four catastrophic cases**, each
4–5 s on a *small* (30–45 KB) hostile file — a static regex audit had missed them, which is the
point of fuzzing:

| Scanner | Bug | Fix |
|---|---|---|
| ruby | `_spec_from` ran the version regexes over a 45 KB gem-line tail (quadratic) | cap the tail to 4 KB (a real spec is tiny) |
| ruby | `_group_at` re-scanned the whole file per gem — O(gems × text) | index groups once, binary-search per gem |
| containers | `^\s*FROM` under `re.M`: `\s` matches `\n`, so each line-start scanned forward across the file — O(n²) | `[^\S\n]` (horizontal whitespace only) |
| terraform | `_ENTRY` backtracked over a huge unbalanced `required_providers` body | possessive quantifiers (3.11+) + cap the block body to 8 KB |

After the fixes the worst measured `scan_file` dropped from ~5.5 s to ~0.6 s. `langdeps` on a
200 K-line file is ~0.6 s but **linear** (bounded by the 2 MB read cap), so it is left as-is and
backstopped by the deadline.

## Regression guard

`tests/test_fuzz_scanners.py` runs in the normal CI suite:

- **explicit known-pathological inputs** reproducing each fixed bug, asserting each `scan_file`
  stays under a 1 s budget;
- a **deterministic fuzz slice** (fixed seed) — the seed that surfaced the originals, so reverting
  any fix regenerates the slow input and fails the test.

Run a heavy pass locally: `python -m tests.fuzz_scanners 20000`.

## Deferred (architectural)

Per-scan **memory isolation** (running each scan in a subprocess with `setrlimit`) is not done —
it would restructure the threaded server. Today a runaway allocation is bounded by the pod's
memory limit (an OOM kills the pod, not just the scan). That's the next step if per-tenant memory
isolation becomes a requirement.

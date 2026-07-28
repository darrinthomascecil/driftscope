"""Static scanners: walk a tree and emit Components.

Scanners never touch the network. They answer only "what versions does this repo
claim to use", which keeps them fast, credential-free and unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from ..models import Component

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".terraform",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tox",
    ".idea",
    ".gradle",
    "vendor",
    "site-packages",
}

# Virtualenv directory names, including the one the AI-103 course uses.
SKIP_DIR_MARKERS = ("venv", "tutorials", ".venv", "env")


def _is_skipped(d: Path) -> bool:
    n = d.name
    if n in SKIP_DIRS:
        return True
    if n.startswith(".") and n not in (".github",):
        return True
    # A directory is a virtualenv if it looks like one, not merely by name.
    if n in SKIP_DIR_MARKERS or n.lower() in SKIP_DIR_MARKERS:
        if (d / "pyvenv.cfg").exists() or (d / "bin" / "activate").exists():
            return True
        if (d / "Scripts" / "activate.bat").exists():
            return True
    return False


def walk_files(root: Path, *, max_files: int = 20000) -> Iterator[Path]:
    """Yield candidate files, pruning vendor/venv/VCS directories."""
    count = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = sorted(cur.iterdir())
        except (PermissionError, OSError):
            continue
        for e in entries:
            if e.is_symlink():
                continue
            if e.is_dir():
                if not _is_skipped(e):
                    stack.append(e)
                continue
            if e.is_file():
                count += 1
                if count > max_files:
                    return
                yield e


# Byte-order marks, longest first so UTF-32 is not mistaken for UTF-16.
_BOMS = (
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xff\xfe", "utf-16-le"),
)


def read_text(p: Path, limit: int = 2_000_000) -> str | None:
    """Read a text file, honouring its encoding.

    Windows tooling writes UTF-16 by default -- `pip freeze > requirements.txt` in
    PowerShell produces a UTF-16LE file with a BOM. Forcing UTF-8 on those yields
    NUL-interleaved mojibake that silently matches nothing, so the file looks empty
    rather than misparsed. Sniff the BOM first.
    """
    try:
        if p.stat().st_size > limit:
            return None
        raw = p.read_bytes()
    except OSError:
        return None
    if not raw:
        return ""
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(encoding, errors="replace")
            except (LookupError, UnicodeDecodeError):
                break
    # No BOM: UTF-16 without one still shows as alternating NUL bytes.
    if b"\x00" in raw[:512]:
        nul_even = raw[1:512:2].count(0)
        nul_odd = raw[0:512:2].count(0)
        if nul_even > nul_odd and nul_even > 8:
            return raw.decode("utf-16-le", errors="replace")
        if nul_odd > nul_even and nul_odd > 8:
            return raw.decode("utf-16-be", errors="replace")
    return raw.decode("utf-8", errors="replace")


def scan_tree(
    root: Path, ecosystems: set[str] | None = None
) -> tuple[list[Component], int]:
    """Run every scanner over the tree. Returns (components, files_scanned)."""
    from . import (azure_iac, cicd, containers, dotnet, jvm, langdeps, ruby,
                   terraform)

    scanners = (
        terraform.scan_file,
        azure_iac.scan_file,
        containers.scan_file,
        langdeps.scan_file,
        cicd.scan_file,
        dotnet.scan_file,
        jvm.scan_file,
        ruby.scan_file,
    )
    out: list[Component] = []
    n = 0
    for path in walk_files(root):
        n += 1
        for fn in scanners:
            try:
                found = fn(path, root)
            except Exception:  # a malformed file must not abort the scan
                continue
            if found:
                out.extend(found)
    if ecosystems:
        out = [c for c in out if c.ecosystem in ecosystems]
    return _dedupe(out), n


def _dedupe(components: Iterable[Component]) -> list[Component]:
    """Collapse identical (ecosystem, name, current) triples, keeping first site."""
    seen: dict[tuple[str, str, str | None], Component] = {}
    order: list[tuple[str, str, str | None]] = []
    for c in components:
        k = (c.ecosystem, c.name.lower(), c.current)
        if k not in seen:
            seen[k] = c
            order.append(k)
        else:
            prev = seen[k]
            extra = prev.meta.setdefault("also_at", [])
            loc = f"{c.source}:{c.line}" if c.line else c.source
            if loc not in extra and len(extra) < 25:
                extra.append(loc)
    return [seen[k] for k in order]


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)

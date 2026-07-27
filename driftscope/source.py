"""Scan targets: a local directory, or a remote git repository.

A URL target is shallow-cloned into a temporary directory, scanned, then removed.
The clone is `--depth 1 --single-branch` because only the current state of the
working tree matters -- history is irrelevant to "what versions does this pin".

Nothing here executes repository code: it clones and reads files. Hooks are
disabled explicitly so a hostile repo cannot run anything during clone.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# https://host/owner/repo(.git), git@host:owner/repo(.git), ssh://, git://
_URL_RE = re.compile(
    r"^(?:https?://|git@|ssh://|git://)", re.I
)
# Bare "owner/repo" shorthand -- only honoured when no such local path exists.
_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*$")


class CloneError(Exception):
    pass


def slug_for(display: str) -> str:
    """A filesystem-safe name identifying what was scanned.

    Remote targets become `owner-repo` so reports from different repositories
    never collide; local paths become the directory name.
    """
    s = display.strip().rstrip("/")
    m = re.search(r"(?:https?://|git@|ssh://|git://)[^/:]+[/:]([^/]+)/([^/]+?)(?:\.git)?$", s)
    if m:
        parts = [m.group(1), m.group(2)]
    else:
        parts = [Path(s).name or "scan"]
    slug = "-".join(parts)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-.")
    return (slug or "scan")[:80]


@dataclass
class Source:
    """Where a scan's files came from, for attribution in the report."""

    display: str  # what the user asked for
    path: Path  # where the files actually are
    remote: str | None = None
    ref: str | None = None
    commit: str | None = None
    cloned: bool = False
    meta: dict = field(default_factory=dict)


def looks_remote(target: str) -> bool:
    if _URL_RE.match(target):
        return True
    # Treat owner/repo as remote only if it is not a real local path, so a
    # directory literally named "foo/bar" still wins.
    if _SHORTHAND_RE.match(target) and not Path(target).exists():
        return True
    return False


def normalize_remote(target: str) -> str:
    if _URL_RE.match(target):
        return target
    return f"https://github.com/{target}"


def _git(args: list[str], cwd: Path | None = None, timeout: int = 300):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        # Never let a clone run repository-supplied code, and never hang waiting
        # for credentials on a private repo -- fail fast instead.
        env={
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "true",
            "GCM_INTERACTIVE": "never",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(Path.home()),
        },
    )


@contextmanager
def open_source(target: str, *, ref: str | None = None, keep: bool = False):
    """Yield a Source for `target`, cloning it first if it is remote."""
    if not looks_remote(target):
        p = Path(target).expanduser().resolve()
        if not p.exists():
            raise CloneError(f"{target}: no such path")
        yield Source(display=str(p), path=p if p.is_dir() else p.parent)
        return

    if shutil.which("git") is None:
        raise CloneError("git is not installed, cannot scan a remote repository")

    remote = normalize_remote(target)
    tmp = Path(tempfile.mkdtemp(prefix="driftscope-clone-"))
    dest = tmp / "repo"
    args = ["clone", "--depth", "1", "--single-branch", "--no-tags", "-c",
            "core.hooksPath=/dev/null"]
    if ref:
        args += ["--branch", ref]
    args += [remote, str(dest)]

    try:
        r = _git(args)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            detail = err[-1] if err else "git clone failed"
            hint = ""
            if "Authentication failed" in " ".join(err) or "could not read Username" in " ".join(err):
                hint = " (private repo? authenticate git, or clone it yourself and scan the path)"
            raise CloneError(f"could not clone {remote}: {detail}{hint}")

        head = _git(["rev-parse", "HEAD"], cwd=dest)
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest)
        src = Source(
            display=remote,
            path=dest,
            remote=remote,
            ref=(branch.stdout or "").strip() or ref,
            commit=(head.stdout or "").strip()[:40] or None,
            cloned=True,
        )
        yield src
    finally:
        if keep:
            print(f"clone retained at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

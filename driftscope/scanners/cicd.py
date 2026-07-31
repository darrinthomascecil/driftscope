"""CI/CD pipeline scanner: GitHub Actions.

Actions are almost always open-source GitHub repos, which makes them the single
best-suited ecosystem for release-note and issue-delta analysis. They are also
routinely years stale because nothing fails when they age -- until a runner
deprecation breaks every workflow at once.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Component
from . import read_text, rel

GH_ACTION = "github-action"

# uses: actions/checkout@v4  |  uses: docker://image:tag  |  uses: ./local
# `[^\S\n]` (horizontal whitespace only): a leading `\s*` under re.M matches newlines, so each
# line-start's match scans forward across the file hunting for the keyword -- O(n^2), a multi-
# second hang on a hostile workflow (worse on Python 3.11's engine). Directives are within one
# line, so keep the whitespace non-newline and the scan stays linear.
_USES = re.compile(
    r"""^[^\S\n]*-?[^\S\n]*uses:[^\S\n]*['"]?(?P<ref>[^\s'"#]+)""", re.M
)
_RUNS_ON = re.compile(r"""^[^\S\n]*runs-on:[^\S\n]*['"]?(?P<img>[\w.-]+)""", re.M)

# Runner labels that are deprecated or heading that way, with successor.
RUNNER_SUCCESSORS = {
    "ubuntu-18.04": "ubuntu-24.04",
    "ubuntu-20.04": "ubuntu-24.04",
    "ubuntu-22.04": "ubuntu-24.04",
    "windows-2016": "windows-2022",
    "windows-2019": "windows-2022",
    "macos-11": "macos-14",
    "macos-12": "macos-14",
    "macos-13": "macos-14",
}


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _is_workflow(path: Path) -> bool:
    if path.suffix not in (".yml", ".yaml"):
        return False
    parts = {p.lower() for p in path.parts}
    return ".github" in parts and "workflows" in parts


def scan_file(path: Path, root: Path) -> list[Component]:
    if not _is_workflow(path):
        return []
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []

    for m in _USES.finditer(text):
        ref = m.group("ref")
        if ref.startswith((".", "/")) or ref.startswith("docker://"):
            continue  # local composite action or container action
        if "@" not in ref:
            continue
        repo_path, _, ver = ref.rpartition("@")
        # Sub-path actions: owner/repo/dir@ref -> the versioned unit is owner/repo.
        parts = repo_path.split("/")
        if len(parts) < 2:
            continue
        repo = "/".join(parts[:2])
        subpath = "/".join(parts[2:]) or None
        pinned_sha = bool(re.fullmatch(r"[0-9a-f]{40}", ver))
        # Strip the `v`: "v2" would otherwise parse as a Kubernetes-style apiVersion
        # and refuse to compare against the numeric latest major.
        out.append(
            Component(
                ecosystem=GH_ACTION,
                name=repo,
                current=None if pinned_sha else ver.lstrip("vV"),
                source=src,
                line=_line_of(text, m.start()),
                raw=ref,
                meta={
                    "subpath": subpath,
                    "sha_pinned": pinned_sha,
                    "sha": ver if pinned_sha else None,
                },
            )
        )

    for m in _RUNS_ON.finditer(text):
        img = m.group("img")
        if img.startswith("$") or img in ("self-hosted",):
            continue
        out.append(
            Component(
                ecosystem=GH_ACTION,
                name=f"runner/{img}",
                current=img,
                source=src,
                line=_line_of(text, m.start()),
                raw=img,
                meta={
                    "runner": True,
                    "successor": RUNNER_SUCCESSORS.get(img),
                    "latest_label": img.endswith("-latest"),
                },
            )
        )
    return out

"""Language dependency scanner: Python, Node, Go.

Highest-volume ecosystem and the best-served by upstream metadata (release dates,
changelogs, GitHub repos), so it produces the most actionable findings.

Only direct/declared dependencies are reported. Lock files are read for *exact*
versions where a manifest gives a range, because the resolved version is what
actually ships.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from ..models import GO, NPM, PYPI, Component
from ..version import constraint_floor, is_pinned
from . import read_text, rel

# name[extra]==1.2.3 ; python_version < "3.12"
_REQ_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][\w.\-]*)\s*(?P<extras>\[[^\]]*\])?\s*"
    r"(?P<spec>(?:[=<>!~^]=?|===)\s*[^;#\s]+(?:\s*,\s*[=<>!~^]=?\s*[^;#\s]+)*)?"
)
_GOMOD_REQUIRE_BLOCK = re.compile(r"require\s*\((?P<body>.*?)\n\)", re.S)
_GOMOD_SINGLE = re.compile(
    r"^\s*require\s+(?P<mod>[^\s]+)\s+(?P<ver>v[^\s]+)(?P<rest>.*)$", re.M
)
_GOMOD_ENTRY = re.compile(r"^\s*(?P<mod>[^\s/][^\s]*)\s+(?P<ver>v[^\s]+)(?P<rest>.*)$", re.M)
_GO_DIRECTIVE = re.compile(r"^\s*go\s+(?P<ver>\d+\.\d+(?:\.\d+)?)\s*$", re.M)


def scan_file(path: Path, root: Path) -> list[Component]:
    n = path.name.lower()
    if n.startswith("requirements") and path.suffix in (".txt", ".in"):
        return _scan_requirements(path, root)
    if n == "pyproject.toml":
        return _scan_pyproject(path, root)
    if n == "package.json":
        return _scan_package_json(path, root)
    if n == "go.mod":
        return _scan_gomod(path, root)
    return []


def _scan_requirements(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []
    for i, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("-") or s.startswith("--"):
            continue  # -r includes, --index-url, --hash
        if "://" in s or s.startswith("."):
            continue  # VCS/URL/local installs have no registry version
        s = s.split("#")[0].split(";")[0].strip()
        m = _REQ_LINE.match(s)
        if not m or not m.group("name"):
            continue
        spec = (m.group("spec") or "").strip()
        out.append(
            Component(
                ecosystem=PYPI,
                name=m.group("name"),
                current=constraint_floor(spec),
                source=src,
                line=i,
                raw=spec or None,
                meta={"constraint": spec or None, "pinned": is_pinned(spec)},
            )
        )
    return out


def _scan_pyproject(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    try:
        doc = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    src = rel(path, root)
    out: list[Component] = []

    def add(spec: str, group: str | None = None) -> None:
        m = _REQ_LINE.match(spec.strip())
        if not m or not m.group("name"):
            return
        s = (m.group("spec") or "").strip()
        out.append(
            Component(
                ecosystem=PYPI,
                name=m.group("name"),
                current=constraint_floor(s),
                source=src,
                raw=s or None,
                meta={"constraint": s or None, "group": group, "pinned": is_pinned(s)},
            )
        )

    project = doc.get("project") or {}
    for dep in project.get("dependencies") or []:
        if isinstance(dep, str):
            add(dep)
    for group, deps in (project.get("optional-dependencies") or {}).items():
        for dep in deps or []:
            if isinstance(dep, str):
                add(dep, group)
    # Poetry
    poetry = ((doc.get("tool") or {}).get("poetry") or {})
    for group, deps in (("main", poetry.get("dependencies") or {}),):
        for name, spec in deps.items():
            if name.lower() == "python":
                continue
            if isinstance(spec, str):
                add(f"{name}{spec if spec[0] in '<>=!~^' else '==' + spec}", group)
    return out


def _scan_package_json(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(doc, dict):
        return []
    src = rel(path, root)
    out: list[Component] = []
    lock = _npm_lock_versions(path)
    for group in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (doc.get(group) or {}).items():
            if not isinstance(spec, str):
                continue
            if spec.startswith(("file:", "link:", "git+", "github:", "workspace:", "npm:")):
                continue
            resolved = lock.get(name)
            out.append(
                Component(
                    ecosystem=NPM,
                    name=name,
                    current=resolved or constraint_floor(spec),
                    source=src,
                    raw=spec,
                    meta={
                        "constraint": spec,
                        "group": group,
                        "locked": bool(resolved),
                        "pinned": is_pinned(spec),
                    },
                )
            )
    engines = (doc.get("engines") or {}).get("node")
    if isinstance(engines, str):
        out.append(
            Component(
                ecosystem=NPM,
                name="node",
                current=constraint_floor(engines),
                source=src,
                raw=engines,
                meta={"runtime": True, "constraint": engines},
            )
        )
    return out


def _npm_lock_versions(pkg_json: Path) -> dict[str, str]:
    """Read resolved top-level versions from package-lock.json, if present."""
    lock = pkg_json.with_name("package-lock.json")
    text = read_text(lock) if lock.exists() else None
    if not text:
        return {}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for key, meta in (doc.get("packages") or {}).items():
        if not key.startswith("node_modules/"):
            continue
        name = key[len("node_modules/") :]
        if "/node_modules/" in name:
            continue  # nested/transitive
        if isinstance(meta, dict) and isinstance(meta.get("version"), str):
            out[name] = meta["version"]
    for name, meta in (doc.get("dependencies") or {}).items():  # lockfileVersion 1
        if isinstance(meta, dict) and isinstance(meta.get("version"), str):
            out.setdefault(name, meta["version"])
    return out


def _scan_gomod(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []
    seen: set[str] = set()

    def add(mod: str, ver: str, indirect: bool) -> None:
        if mod in seen:
            return
        seen.add(mod)
        out.append(
            Component(
                ecosystem=GO,
                name=mod,
                current=ver,
                source=src,
                raw=ver,
                meta={"indirect": indirect},
            )
        )

    for block in _GOMOD_REQUIRE_BLOCK.finditer(text):
        for m in _GOMOD_ENTRY.finditer(block.group("body")):
            mod, ver, rest = m.group("mod"), m.group("ver"), m.group("rest") or ""
            if mod in ("require", "module", "go"):
                continue
            add(mod, ver, "// indirect" in rest)
    for m in _GOMOD_SINGLE.finditer(text):
        add(m.group("mod"), m.group("ver"), "// indirect" in (m.group("rest") or ""))

    gm = _GO_DIRECTIVE.search(text)
    if gm:
        add("golang.org/toolchain", gm.group("ver"), False)
    return out

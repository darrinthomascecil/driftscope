""".NET scanner: NuGet packages, SDK pins, target frameworks.

Uses ElementTree rather than regex -- MSBuild project files are real XML, and the
same package can be declared with the version as an attribute or a child element,
which a regex handles badly.

Central Package Management complicates this: a `PackageReference` may carry no
version at all, with the real version living in `Directory.Packages.props`. Those
references are recorded as version-less and reconciled against the props file so
they are not reported as "unpinned" when they are in fact centrally pinned.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..models import Component
from ..version import constraint_floor, is_pinned
from . import read_text, rel

NUGET = "nuget"
DOTNET_SDK = "dotnet-sdk"
DOTNET_TFM = "dotnet-tfm"

_PROJ_SUFFIXES = (".csproj", ".fsproj", ".vbproj")

# net6.0 -> 6.0 ; netcoreapp3.1 -> 3.1 ; net48 -> 4.8 ; netstandard2.1 -> 2.1
_TFM_RE = re.compile(
    r"^(?:net(?P<modern>\d+\.\d+)|netcoreapp(?P<core>\d+\.\d+)"
    r"|netstandard(?P<std>\d+\.\d+)|net(?P<fw>\d{2,3}))$"
)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text_of(el, name: str) -> str | None:
    for child in el:
        if _strip_ns(child.tag).lower() == name.lower():
            return (child.text or "").strip() or None
    return None


def scan_file(path: Path, root: Path) -> list[Component]:
    n = path.name.lower()
    if path.suffix.lower() in _PROJ_SUFFIXES:
        return _scan_project(path, root)
    if n == "directory.packages.props":
        return _scan_central(path, root)
    if n == "global.json":
        return _scan_global_json(path, root)
    if n == "packages.config":
        return _scan_packages_config(path, root)
    return []


def _parse_xml(path: Path):
    text = read_text(path)
    if not text:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def _resolve_property(value: str, props: dict[str, str]) -> str | None:
    """Expand a single $(Prop) reference; give up on anything more complex."""
    m = re.fullmatch(r"\$\(([^)]+)\)", value.strip())
    if not m:
        return value
    return props.get(m.group(1))


def _collect_properties(rootel) -> dict[str, str]:
    props: dict[str, str] = {}
    for group in rootel.iter():
        if _strip_ns(group.tag) != "PropertyGroup":
            continue
        for child in group:
            name = _strip_ns(child.tag)
            if child.text and child.text.strip():
                props.setdefault(name, child.text.strip())
    return props


def _scan_project(path: Path, root: Path) -> list[Component]:
    el = _parse_xml(path)
    if el is None:
        return []
    src = rel(path, root)
    props = _collect_properties(el)
    lock = _lock_versions(path)
    out: list[Component] = []

    for ref in el.iter():
        if _strip_ns(ref.tag) != "PackageReference":
            continue
        name = ref.get("Include") or ref.get("Update")
        if not name:
            continue
        raw = ref.get("Version") or ref.get("VersionOverride") or _text_of(ref, "Version")
        resolved_prop = None
        if raw:
            resolved_prop = _resolve_property(raw, props)
        version = lock.get(name.lower()) or constraint_floor(resolved_prop or "")
        out.append(
            Component(
                ecosystem=NUGET,
                name=name,
                current=version,
                source=src,
                raw=raw,
                meta={
                    "constraint": raw,
                    "central": raw is None,  # version comes from Directory.Packages.props
                    "locked": name.lower() in lock,
                    "pinned": is_pinned(resolved_prop or ""),
                    **({"unresolved_expr": raw} if raw and resolved_prop is None else {}),
                },
            )
        )

    # Target frameworks are runtime versions and go EOL on Microsoft's schedule.
    for key in ("TargetFramework", "TargetFrameworks"):
        val = props.get(key)
        if not val:
            continue
        for tfm in val.split(";"):
            tfm = tfm.strip()
            if not tfm:
                continue
            ver = _tfm_version(tfm)
            out.append(
                Component(
                    ecosystem=DOTNET_TFM,
                    name=tfm,
                    current=ver,
                    source=src,
                    raw=tfm,
                    meta={"runtime": True, "moniker": tfm},
                )
            )
    return out


def _tfm_version(tfm: str) -> str | None:
    m = _TFM_RE.match(tfm.lower())
    if not m:
        return None
    if m.group("modern"):
        return m.group("modern")
    if m.group("core"):
        return m.group("core")
    if m.group("std"):
        return m.group("std")
    fw = m.group("fw")
    if fw:  # net48 -> 4.8, net472 -> 4.7.2
        return ".".join(fw) if len(fw) <= 3 else fw
    return None


def _scan_central(path: Path, root: Path) -> list[Component]:
    el = _parse_xml(path)
    if el is None:
        return []
    src = rel(path, root)
    props = _collect_properties(el)
    out: list[Component] = []
    for ref in el.iter():
        if _strip_ns(ref.tag) != "PackageVersion":
            continue
        name = ref.get("Include")
        raw = ref.get("Version") or _text_of(ref, "Version")
        if not name:
            continue
        resolved = _resolve_property(raw, props) if raw else None
        out.append(
            Component(
                ecosystem=NUGET,
                name=name,
                current=constraint_floor(resolved or ""),
                source=src,
                raw=raw,
                meta={
                    "constraint": raw,
                    "central_definition": True,
                    "pinned": is_pinned(resolved or ""),
                },
            )
        )
    return out


def _scan_global_json(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(doc, dict):
        return []
    sdk = (doc.get("sdk") or {}) if isinstance(doc.get("sdk"), dict) else {}
    version = sdk.get("version")
    if not isinstance(version, str):
        return []
    return [
        Component(
            ecosystem=DOTNET_SDK,
            name="dotnet-sdk",
            current=version,
            source=rel(path, root),
            raw=version,
            meta={"rollForward": sdk.get("rollForward"), "runtime": True},
        )
    ]


def _scan_packages_config(path: Path, root: Path) -> list[Component]:
    """Legacy (pre-PackageReference) format -- still common in older estates."""
    el = _parse_xml(path)
    if el is None:
        return []
    src = rel(path, root)
    out: list[Component] = []
    for pkg in el.iter():
        if _strip_ns(pkg.tag) != "package":
            continue
        name, version = pkg.get("id"), pkg.get("version")
        if not name:
            continue
        out.append(
            Component(
                ecosystem=NUGET,
                name=name,
                current=version,
                source=src,
                raw=version,
                meta={"legacy_format": True, "pinned": True},
            )
        )
    return out


def _lock_versions(proj: Path) -> dict[str, str]:
    """Resolved versions from packages.lock.json next to the project."""
    lock = proj.with_name("packages.lock.json")
    text = read_text(lock) if lock.exists() else None
    if not text:
        return {}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for _tfm, deps in (doc.get("dependencies") or {}).items():
        if not isinstance(deps, dict):
            continue
        for name, meta in deps.items():
            if isinstance(meta, dict):
                v = meta.get("resolved") or meta.get("requested")
                if isinstance(v, str) and meta.get("type") != "Transitive":
                    out[name.lower()] = v
    return out

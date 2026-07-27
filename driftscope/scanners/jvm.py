"""JVM scanner: Maven and Gradle dependencies, plus toolchain versions.

`pom.xml` is parsed with ElementTree. Maven's property indirection is the main
complication: versions are routinely `${spring.version}`, defined in `<properties>`
or inherited from a parent POM. Properties declared in the same file are expanded;
anything inherited is reported unresolved rather than guessed, because guessing a
parent-managed version is how you end up telling a customer to upgrade something
they do not control.

Gradle build scripts are not XML and have no stable machine format, so those are
regex-matched for the conventional `group:artifact:version` string form only.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..models import Component
from ..version import constraint_floor, is_pinned
from . import read_text, rel

MAVEN = "maven"
JVM_TOOLCHAIN = "jvm-toolchain"

_PROP_RE = re.compile(r"^\$\{([^}]+)\}$")

# implementation 'g:a:v'  |  implementation("g:a:v")  |  api "g:a:v"
_GRADLE_DEP = re.compile(
    r"""(?:implementation|api|compileOnly|runtimeOnly|testImplementation|"""
    r"""testCompileOnly|annotationProcessor|kapt|classpath)\s*"""
    r"""[\s(]*['"](?P<g>[\w.\-]+):(?P<a>[\w.\-]+):(?P<v>[^'"]+)['"]""",
)
_GRADLE_WRAPPER = re.compile(r"gradle-(?P<v>\d+(?:\.\d+)*)-(?:bin|all)\.zip")
_JAVA_VERSION = re.compile(
    r"""(?:sourceCompatibility|targetCompatibility|JavaLanguageVersion\.of)\s*"""
    r"""[=(]\s*['"]?(?P<v>\d+(?:\.\d+)?)""",
)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(el, name: str) -> str | None:
    for c in el:
        if _strip_ns(c.tag) == name:
            return (c.text or "").strip() or None
    return None


def scan_file(path: Path, root: Path) -> list[Component]:
    n = path.name.lower()
    if n == "pom.xml":
        return _scan_pom(path, root)
    if n in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
        return _scan_gradle(path, root)
    if n == "gradle-wrapper.properties":
        return _scan_wrapper(path, root)
    return []


def _scan_pom(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    try:
        el = ET.fromstring(text)
    except ET.ParseError:
        return []
    src = rel(path, root)

    props: dict[str, str] = {}
    for group in el:
        if _strip_ns(group.tag) == "properties":
            for c in group:
                if c.text and c.text.strip():
                    props[_strip_ns(c.tag)] = c.text.strip()

    def resolve(v: str | None) -> tuple[str | None, str | None]:
        """Return (resolved, unresolved_expression)."""
        if not v:
            return None, None
        m = _PROP_RE.match(v)
        if not m:
            return v, None
        key = m.group(1)
        # project.version / parent-inherited properties are not in this file.
        return (props.get(key), None) if key in props else (None, v)

    out: list[Component] = []

    # Parent POM is itself a versioned artifact and often the real upgrade lever.
    for child in el:
        if _strip_ns(child.tag) != "parent":
            continue
        g, a = _child_text(child, "groupId"), _child_text(child, "artifactId")
        v, unresolved = resolve(_child_text(child, "version"))
        if g and a:
            out.append(
                Component(
                    ecosystem=MAVEN,
                    name=f"{g}:{a}",
                    current=constraint_floor(v or ""),
                    source=src,
                    raw=v,
                    meta={"parent": True,
                          **({"unresolved_expr": unresolved} if unresolved else {})},
                )
            )

    seen: set[str] = set()
    for dep in el.iter():
        if _strip_ns(dep.tag) != "dependency":
            continue
        g, a = _child_text(dep, "groupId"), _child_text(dep, "artifactId")
        if not g or not a:
            continue
        v, unresolved = resolve(_child_text(dep, "version"))
        scope = _child_text(dep, "scope")
        key = f"{g}:{a}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Component(
                ecosystem=MAVEN,
                name=key,
                current=constraint_floor(v or ""),
                source=src,
                raw=v,
                meta={
                    "scope": scope,
                    "constraint": v,
                    "pinned": is_pinned(v or ""),
                    # dependencyManagement entries declare versions for children
                    # rather than adding a dependency themselves.
                    **({"unresolved_expr": unresolved} if unresolved else {}),
                },
            )
        )

    for key in ("maven.compiler.release", "maven.compiler.source", "java.version"):
        if key in props:
            out.append(
                Component(
                    ecosystem=JVM_TOOLCHAIN,
                    name="java",
                    current=props[key],
                    source=src,
                    raw=props[key],
                    meta={"runtime": True, "property": key},
                )
            )
            break
    return out


def _scan_gradle(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []
    seen: set[str] = set()
    for m in _GRADLE_DEP.finditer(text):
        v = m.group("v")
        if v.startswith("$"):
            continue  # version catalog / variable, not resolvable here
        key = f"{m.group('g')}:{m.group('a')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Component(
                ecosystem=MAVEN,
                name=key,
                current=constraint_floor(v),
                source=src,
                line=text.count("\n", 0, m.start()) + 1,
                raw=v,
                meta={"build_system": "gradle", "pinned": is_pinned(v)},
            )
        )
    jm = _JAVA_VERSION.search(text)
    if jm:
        out.append(
            Component(
                ecosystem=JVM_TOOLCHAIN,
                name="java",
                current=jm.group("v"),
                source=src,
                raw=jm.group("v"),
                meta={"runtime": True, "build_system": "gradle"},
            )
        )
    return out


def _scan_wrapper(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    m = _GRADLE_WRAPPER.search(text)
    if not m:
        return []
    return [
        Component(
            ecosystem=JVM_TOOLCHAIN,
            name="gradle",
            current=m.group("v"),
            source=rel(path, root),
            raw=m.group(0),
            meta={"runtime": True, "tool": "gradle"},
        )
    ]

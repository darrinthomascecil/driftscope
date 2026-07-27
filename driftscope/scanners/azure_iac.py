"""Bicep and ARM template scanner.

The versioned thing in Azure IaC is the resource-provider API version, which is a
date (`2025-06-01`) rather than a semver. Bicep declares it inline on the type
string; ARM JSON puts it in an `apiVersion` field. Both also pin the Bicep/ARM
template schema itself, which is worth reporting separately.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import AZURE_API, Component
from . import read_text, rel

# resource x 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
_BICEP_RESOURCE = re.compile(
    r"""(?:resource|existing)?\s*[\w]*\s*['"]"""
    r"""(?P<type>[A-Za-z][\w.]*(?:/[\w.]+)+)@(?P<api>\d{4}-\d{2}-\d{2}(?:-preview)?)['"]"""
)
# Any quoted type@date pair (covers module outputs, getSecret, nested resources).
_BICEP_ANY = re.compile(
    r"""['"](?P<type>[A-Za-z][\w.]*(?:/[\w.]+)+)@(?P<api>\d{4}-\d{2}-\d{2}(?:-preview)?)['"]"""
)
# $schema": ".../2019-04-01/deploymentTemplate.json#"
_ARM_SCHEMA = re.compile(r"schemas/(\d{4}-\d{2}-\d{2})/[\w.]+\.json")


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def scan_file(path: Path, root: Path) -> list[Component]:
    if path.suffix == ".bicep":
        return _scan_bicep(path, root)
    if path.suffix == ".json":
        return _scan_arm(path, root)
    return []


def _scan_bicep(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []
    seen: set[tuple[str, str]] = set()
    for m in _BICEP_ANY.finditer(text):
        rtype, api = m.group("type"), m.group("api")
        if "/" not in rtype:
            continue
        key = (rtype.lower(), api)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Component(
                ecosystem=AZURE_API,
                name=rtype,
                current=api,
                source=src,
                line=_line_of(text, m.start()),
                raw=api,
                meta={"format": "bicep", "preview": api.endswith("-preview")},
            )
        )
    return out


def _walk_json(node, out: list[tuple[str, str]]) -> None:
    """Collect (type, apiVersion) pairs from an ARM template tree."""
    if isinstance(node, dict):
        t, a = node.get("type"), node.get("apiVersion")
        if isinstance(t, str) and isinstance(a, str) and "/" in t:
            out.append((t, a))
        for v in node.values():
            _walk_json(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, out)


def _scan_arm(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text or '"resources"' not in text and "deploymentTemplate.json" not in text:
        return []
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(doc, dict):
        return []
    schema = doc.get("$schema")
    is_arm = isinstance(schema, str) and "deploymentTemplate" in schema
    if not is_arm and "resources" not in doc:
        return []

    src = rel(path, root)
    out: list[Component] = []
    pairs: list[tuple[str, str]] = []
    _walk_json(doc.get("resources", []), pairs)

    seen: set[tuple[str, str]] = set()
    for rtype, api in pairs:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(-preview)?", api):
            continue  # skip expressions like "[variables('api')]"
        key = (rtype.lower(), api)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Component(
                ecosystem=AZURE_API,
                name=rtype,
                current=api,
                source=src,
                line=None,
                raw=api,
                meta={"format": "arm", "preview": api.endswith("-preview")},
            )
        )

    if isinstance(schema, str):
        m = _ARM_SCHEMA.search(schema)
        if m:
            out.append(
                Component(
                    ecosystem=AZURE_API,
                    name="ARM/deploymentTemplateSchema",
                    current=m.group(1),
                    source=src,
                    raw=schema,
                    meta={"format": "arm", "schema": True},
                )
            )
    return out

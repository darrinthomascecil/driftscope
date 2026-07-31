"""Terraform scanner: providers, modules, and the lock file.

HCL has no standard-library parser and we deliberately take no dependencies, so
this is regex-based block extraction. That is sufficient for version metadata --
`required_providers`, `module` sources and `.terraform.lock.hcl` are all simple,
highly-conventional shapes -- but it will not evaluate variables or interpolation.
A `version = var.provider_version` is reported as unresolved rather than guessed.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import TERRAFORM_MODULE, TERRAFORM_PROVIDER, Component
from ..version import constraint_floor
from . import read_text, rel

# required_providers { azurerm = { source = "hashicorp/azurerm", version = "~> 3.0" } }
_RP_BLOCK = re.compile(r"required_providers\s*\{", re.I)
# Possessive quantifiers (Python 3.11+) stop `[\w-]*` / `\s*` from backtracking; combined with
# the body cap in scan_file this keeps a hostile, unbalanced required_providers block from
# driving this into O(n^2) (a 28 KB block was measured at ~4.4s, ~88ms after both fixes).
_ENTRY = re.compile(
    r"(?P<alias>[A-Za-z_][\w-]*+)\s*+=\s*+\{(?P<body>[^{}]*+)\}",
    re.S,
)
# A required_providers block is tiny in practice (~60 bytes/provider); cap the body a regex
# scans so a malformed brace (which makes _balanced_block run to EOF) can't be a DoS.
_MAX_BLOCK = 8192
_SOURCE = re.compile(r"""source\s*=\s*["']([^"']+)["']""")
_VERSION = re.compile(r"""version\s*=\s*["']([^"']+)["']""")
_UNQUOTED_VERSION = re.compile(r"version\s*=\s*([A-Za-z_][\w.\[\]\"']*)")

# module "x" { source = "..." version = "..." }
_MODULE = re.compile(
    r"""module\s+["'](?P<name>[^"']+)["']\s*\{(?P<body>.*?)\n\}""", re.S
)

# terraform { required_version = ">= 1.5" }
_REQUIRED_VERSION = re.compile(r"""required_version\s*=\s*["']([^"']+)["']""")

# .terraform.lock.hcl: provider "registry.terraform.io/hashicorp/azurerm" { version = "3.75.0" }
_LOCK_PROVIDER = re.compile(
    r"""provider\s+["'](?P<addr>[^"']+)["']\s*\{(?P<body>.*?)\n\}""", re.S
)

# Registry addresses are exactly <NAMESPACE>/<NAME>/<PROVIDER> with no dots and no
# leading path characters -- that is what separates them from "../modules/local"
# and from git/https sources.
_REGISTRY_MODULE = re.compile(r"^[A-Za-z0-9][\w-]*/[A-Za-z0-9][\w-]*/[A-Za-z0-9][\w-]*$")


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _balanced_block(text: str, open_idx: int) -> tuple[str, int]:
    """Return the body of a brace block starting at the '{' index."""
    depth = 0
    for i in range(open_idx, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i
    return text[open_idx + 1 :], len(text)


def scan_file(path: Path, root: Path) -> list[Component]:
    name = path.name
    if name == ".terraform.lock.hcl":
        return _scan_lock(path, root)
    if path.suffix not in (".tf", ".tofu"):
        return []
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []

    # ---- providers
    for m in _RP_BLOCK.finditer(text):
        brace = text.index("{", m.start())
        body, _ = _balanced_block(text, brace)
        for e in _ENTRY.finditer(body[:_MAX_BLOCK]):
            alias = e.group("alias")
            inner = e.group("body")
            sm = _SOURCE.search(inner)
            source = sm.group(1) if sm else f"hashicorp/{alias}"
            if source.count("/") == 0:
                source = f"hashicorp/{source}"
            source = source.removeprefix("registry.terraform.io/")
            vm = _VERSION.search(inner)
            raw = vm.group(1) if vm else None
            unresolved = None
            if raw is None and _UNQUOTED_VERSION.search(inner):
                unresolved = _UNQUOTED_VERSION.search(inner).group(1)
            out.append(
                Component(
                    ecosystem=TERRAFORM_PROVIDER,
                    name=source,
                    current=constraint_floor(raw),
                    source=src,
                    line=_line_of(text, e.start()),
                    raw=raw,
                    meta={
                        "alias": alias,
                        "constraint": raw,
                        **({"unresolved_expr": unresolved} if unresolved else {}),
                    },
                )
            )

    # ---- modules
    for m in _MODULE.finditer(text):
        body = m.group("body")
        sm = _SOURCE.search(body)
        if not sm:
            continue
        source = sm.group(1)
        vm = _VERSION.search(body)
        raw = vm.group(1) if vm else None
        is_registry = bool(_REGISTRY_MODULE.match(source))
        out.append(
            Component(
                ecosystem=TERRAFORM_MODULE,
                name=source,
                current=constraint_floor(raw),
                source=src,
                line=_line_of(text, m.start()),
                raw=raw,
                meta={
                    "block": m.group("name"),
                    "registry": is_registry,
                    "constraint": raw,
                },
            )
        )

    # ---- terraform core
    for m in _REQUIRED_VERSION.finditer(text):
        out.append(
            Component(
                ecosystem=TERRAFORM_PROVIDER,
                name="hashicorp/terraform",
                current=constraint_floor(m.group(1)),
                source=src,
                line=_line_of(text, m.start()),
                raw=m.group(1),
                meta={"core": True, "constraint": m.group(1)},
            )
        )
    return out


def _scan_lock(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []
    for m in _LOCK_PROVIDER.finditer(text):
        addr = m.group("addr").removeprefix("registry.terraform.io/")
        vm = _VERSION.search(m.group("body"))
        if not vm:
            continue
        out.append(
            Component(
                ecosystem=TERRAFORM_PROVIDER,
                name=addr,
                current=vm.group(1),
                source=src,
                line=_line_of(text, m.start()),
                raw=vm.group(1),
                # Lock entries are authoritative: this is what actually gets used.
                meta={"locked": True, "constraint": vm.group(1)},
            )
        )
    return out

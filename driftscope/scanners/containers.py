"""Container, Kubernetes and Helm scanner.

Covers four version surfaces:
  Dockerfile  FROM image:tag            -> docker-image
  compose     services.*.image          -> docker-image
  Helm        Chart.yaml version/deps   -> helm-chart
  K8s         apiVersion + kind         -> k8s-api  (deprecation, not "newness")

YAML is read with a deliberately small subset parser rather than a dependency;
it handles the flat key/value and simple list-of-mappings shapes these files use.
Anchors, multi-line scalars and flow mappings are not supported and are skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import DOCKER, HELM, K8S_API, Component
from . import read_text, rel

# `[^\S\n]` is horizontal whitespace only. Under re.M, a leading `\s*` (which includes `\n`)
# makes each line-start's match scan FORWARD across newlines hunting for the keyword -- O(n^2)
# over the file (a hostile Dockerfile was measured at ~5.3s). These directives are always
# within one line, so restrict the whitespace to non-newline and the scan stays linear.
_FROM = re.compile(
    r"^[^\S\n]*FROM[^\S\n]+(?:--platform=\S+[^\S\n]+)?(?P<ref>\S+)(?:[^\S\n]+AS[^\S\n]+(?P<stage>\S+))?",
    re.I | re.M,
)
_IMAGE_LINE = re.compile(r"^[^\S\n]*-?[^\S\n]*image:[^\S\n]*['\"]?(?P<ref>[^\s'\"#]+)", re.M)
_APIVERSION_LINE = re.compile(r"^[^\S\n]*apiVersion:[^\S\n]*['\"]?(?P<v>[\w./-]+)", re.M)
_KIND_LINE = re.compile(r"^[^\S\n]*kind:[^\S\n]*['\"]?(?P<k>[\w.-]+)", re.M)

_DOC_SEP = re.compile(r"^---[ \t]*$", re.M)


def _yaml_documents(text: str):
    """Yield (document_text, offset_in_file) for each YAML document."""
    pos = 0
    for m in _DOC_SEP.finditer(text):
        yield text[pos : m.start()], pos
        pos = m.end() + 1
    yield text[pos:], pos


_DOCKERFILE_NAMES = ("dockerfile", "containerfile")
_COMPOSE_NAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def parse_image_ref(ref: str) -> tuple[str, str | None, str | None]:
    """Split an image reference into (name, tag, digest).

    Handles registry hosts with ports, digests, and untagged refs.
    """
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    tag = None
    # A colon is a tag separator only if it is after the last slash.
    if ":" in ref:
        head, maybe = ref.rsplit(":", 1)
        if "/" not in maybe:
            ref, tag = head, maybe
    return ref, tag, digest


def scan_file(path: Path, root: Path) -> list[Component]:
    low = path.name.lower()
    if low.startswith(_DOCKERFILE_NAMES) or low.endswith(".dockerfile"):
        return _scan_dockerfile(path, root)
    if low in _COMPOSE_NAMES:
        return _scan_compose(path, root)
    if low in ("chart.yaml", "chart.yml"):
        return _scan_chart(path, root)
    if path.suffix in (".yaml", ".yml"):
        return _scan_k8s(path, root)
    return []


def _scan_dockerfile(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    stages: set[str] = set()
    out: list[Component] = []
    for m in _FROM.finditer(text):
        ref = m.group("ref")
        if m.group("stage"):
            stages.add(m.group("stage").lower())
        if ref.lower() in stages or ref.startswith("$"):
            continue  # multi-stage self-reference or build arg
        name, tag, digest = parse_image_ref(ref)
        out.append(
            Component(
                ecosystem=DOCKER,
                name=name,
                current=tag,
                source=src,
                line=_line_of(text, m.start()),
                raw=ref,
                meta={
                    "digest": digest,
                    "untagged": tag is None,
                    "floating": tag in (None, "latest"),
                },
            )
        )
    return out


def _scan_compose(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []
    for m in _IMAGE_LINE.finditer(text):
        ref = m.group("ref")
        if ref.startswith(("$", "{")):
            continue
        name, tag, digest = parse_image_ref(ref)
        out.append(
            Component(
                ecosystem=DOCKER,
                name=name,
                current=tag,
                source=src,
                line=_line_of(text, m.start()),
                raw=ref,
                meta={"digest": digest, "floating": tag in (None, "latest")},
            )
        )
    return out


def _flat_keys(text: str) -> dict[str, str]:
    """Top-level `key: value` pairs (indentation zero)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip().strip("'\"")
        if v and not v.startswith(("&", "*", "|", ">")):
            out[k.strip()] = v
    return out


def _chart_dependencies(text: str) -> list[dict[str, str]]:
    """Parse a `dependencies:` list of simple mappings."""
    deps: list[dict[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if re.match(r"^dependencies:\s*$", lines[i]):
            i += 1
            cur: dict[str, str] | None = None
            while i < len(lines):
                ln = lines[i]
                if ln and not ln[0].isspace():
                    break  # dedented to a new top-level key
                item = re.match(r"^\s*-\s*(\w+):\s*(.+)$", ln)
                if item:
                    if cur:
                        deps.append(cur)
                    cur = {item.group(1): item.group(2).strip().strip("'\"")}
                else:
                    kv = re.match(r"^\s+(\w+):\s*(.+)$", ln)
                    if kv and cur is not None:
                        cur[kv.group(1)] = kv.group(2).strip().strip("'\"")
                i += 1
            if cur:
                deps.append(cur)
            break
        i += 1
    return deps


def _scan_chart(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    flat = _flat_keys(text)
    out: list[Component] = []
    chart_name = flat.get("name")
    if chart_name and flat.get("version"):
        out.append(
            Component(
                ecosystem=HELM,
                name=chart_name,
                current=flat.get("version"),
                source=src,
                raw=flat.get("version"),
                meta={
                    "self": True,
                    "appVersion": flat.get("appVersion"),
                    "chart_api": flat.get("apiVersion"),
                },
            )
        )
    for dep in _chart_dependencies(text):
        n, v = dep.get("name"), dep.get("version")
        if not n or not v:
            continue
        out.append(
            Component(
                ecosystem=HELM,
                name=n,
                current=v,
                source=src,
                raw=v,
                meta={"repository": dep.get("repository"), "dependency": True},
            )
        )
    return out


def _scan_k8s(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text or "apiVersion:" not in text:
        return []
    src = rel(path, root)
    out: list[Component] = []
    # Track each document's offset in the file. Splitting and then measuring
    # offsets inside a fragment reported every document's line as if it were the
    # first, so a manifest in the second document pointed at line 2.
    for doc, doc_offset in _yaml_documents(text):
        av = _APIVERSION_LINE.search(doc)
        kd = _KIND_LINE.search(doc)
        if not av or not kd:
            continue
        api, kind = av.group("v"), kd.group("k")
        if "/" in api:
            group, _, ver = api.rpartition("/")
        else:
            group, ver = "core", api
        if not re.fullmatch(r"v\d+(?:(?:alpha|beta)\d+)?", ver):
            continue  # not a Kubernetes-style apiVersion (e.g. a Helm chart file)
        out.append(
            Component(
                ecosystem=K8S_API,
                name=f"{group}/{kind}",
                current=ver,
                source=src,
                line=_line_of(text, doc_offset + av.start()),
                raw=api,
                meta={"group": group, "kind": kind, "apiVersion": api},
            )
        )
        for m in _IMAGE_LINE.finditer(doc):
            ref = m.group("ref")
            if ref.startswith(("$", "{")):
                continue
            name, tag, digest = parse_image_ref(ref)
            out.append(
                Component(
                    ecosystem=DOCKER,
                    name=name,
                    current=tag,
                    source=src,
                    line=_line_of(text, doc_offset + m.start()),
                    raw=ref,
                    meta={"digest": digest, "floating": tag in (None, "latest")},
                )
            )
    return out

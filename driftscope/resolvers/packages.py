"""Language package resolvers: PyPI, npm, Go module proxy.

All three expose release dates, which is what makes "you are 412 days behind"
possible rather than just "you are 3 minor versions behind".
"""

from __future__ import annotations

import re
from urllib.parse import quote

from ..http import HttpError
from ..models import Component, Release, Resolution
from ..version import latest_stable, parse
from . import Context, parse_iso

_GITHUB_URL = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+)")


def _github_repo_from_urls(*candidates: object) -> str | None:
    for c in candidates:
        if isinstance(c, dict):
            found = _github_repo_from_urls(*c.values())
            if found:
                return found
        elif isinstance(c, str):
            m = _GITHUB_URL.search(c)
            if m:
                return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
    return None


# ------------------------------------------------------------------------ PyPI


def resolve_pypi(c: Component, ctx: Context) -> Resolution:
    url = f"https://pypi.org/pypi/{quote(c.name)}/json"
    try:
        body = ctx.client.get_json(url)
    except HttpError as e:
        return Resolution(error=str(e))
    info = body.get("info") or {}
    releases: list[Release] = []
    for ver, files in (body.get("releases") or {}).items():
        published = None
        yanked = False
        if isinstance(files, list) and files:
            published = parse_iso(files[0].get("upload_time_iso_8601") or files[0].get("upload_time"))
            yanked = all(bool(f.get("yanked")) for f in files)
        releases.append(
            Release(
                version=ver,
                published=published,
                prerelease=bool(re.search(r"(a|b|rc|dev|pre)\d*$", ver)),
                yanked=yanked,
            )
        )
    usable = [r.version for r in releases if not r.yanked]
    repo = _github_repo_from_urls(
        info.get("project_urls"), info.get("home_page"), info.get("project_url")
    )
    # `info.version` backs up an unusable release list, but it is the registry's
    # free-text field and is not guaranteed to be a version at all. Emitting an
    # unparseable one puts a value in the report that nothing downstream can
    # compare against -- reporting no answer is the honest degradation.
    declared = info.get("version")
    if declared is not None and not parse(str(declared)).valid:
        declared = None
    return Resolution(
        latest_stable=latest_stable(usable) or declared,
        releases=releases,
        repo=repo,
        homepage=info.get("home_page") or info.get("package_url"),
        deprecated=bool(info.get("yanked")),
        note=_pypi_note(info),
    )


def _pypi_note(info: dict) -> str | None:
    classifiers = info.get("classifiers") or []
    for c in classifiers:
        if isinstance(c, str) and "Development Status :: 7 - Inactive" in c:
            return "package marked Inactive on PyPI"
    return None


# ------------------------------------------------------------------------- npm


def resolve_npm(c: Component, ctx: Context) -> Resolution:
    if c.meta.get("runtime") and c.name == "node":
        return _resolve_node(ctx)
    name = quote(c.name, safe="@/")
    try:
        body = ctx.client.get_json(f"https://registry.npmjs.org/{name}")
    except HttpError as e:
        return Resolution(error=str(e))
    times = body.get("time") or {}
    versions = list((body.get("versions") or {}).keys())
    deprecated_versions = {
        v for v, meta in (body.get("versions") or {}).items()
        if isinstance(meta, dict) and meta.get("deprecated")
    }
    releases = [
        Release(
            version=v,
            published=parse_iso(times.get(v)),
            prerelease=bool(re.search(r"-(alpha|beta|rc|next|canary|dev)", v, re.I)),
            yanked=v in deprecated_versions,
        )
        for v in versions
    ]
    dist_latest = ((body.get("dist-tags") or {}).get("latest"))
    repo = _github_repo_from_urls(body.get("repository"), body.get("homepage"))
    return Resolution(
        latest_stable=dist_latest or latest_stable(versions),
        releases=releases,
        repo=repo,
        homepage=body.get("homepage"),
        # A package-level `deprecated` field means the whole package is retired.
        deprecated=bool(body.get("deprecated")),
        note="package deprecated by maintainer" if body.get("deprecated") else None,
    )


def _resolve_node(ctx: Context) -> Resolution:
    """Node.js runtime: use the official release index, which carries LTS data."""
    try:
        body = ctx.client.get_json("https://nodejs.org/dist/index.json")
    except HttpError as e:
        return Resolution(error=str(e), repo="nodejs/node")
    if not isinstance(body, list):
        return Resolution(error="unexpected nodejs.org payload", repo="nodejs/node")
    releases, lts_versions = [], []
    for r in body:
        ver = (r.get("version") or "").lstrip("v")
        if not ver:
            continue
        is_lts = bool(r.get("lts"))
        releases.append(Release(version=ver, published=parse_iso(r.get("date"))))
        if is_lts:
            lts_versions.append(ver)
    return Resolution(
        # For a runtime, the meaningful target is current LTS, not absolute latest.
        latest_stable=latest_stable(lts_versions) or latest_stable([r.version for r in releases]),
        releases=releases,
        repo="nodejs/node",
        homepage="https://nodejs.org",
        note="latest_stable is the newest LTS line",
    )


# -------------------------------------------------------------------------- Go


def resolve_go(c: Component, ctx: Context) -> Resolution:
    if c.name == "golang.org/toolchain":
        return _resolve_go_toolchain(ctx)
    mod = _go_escape(c.name)
    try:
        latest = ctx.client.get_json(f"https://proxy.golang.org/{mod}/@latest")
    except HttpError as e:
        return Resolution(error=str(e), repo=_go_repo(c.name))
    try:
        listing = ctx.client.get_text(f"https://proxy.golang.org/{mod}/@v/list")
        versions = [v.strip() for v in listing.splitlines() if v.strip()]
    except HttpError:
        versions = []
    latest_v = (latest or {}).get("Version")
    if latest_v and latest_v not in versions:
        versions.append(latest_v)
    releases = [Release(version=v, prerelease="-" in v) for v in versions]
    if latest_v:
        for r in releases:
            if r.version == latest_v:
                r.published = parse_iso((latest or {}).get("Time"))

    # The @latest endpoint dates only one version, but /@v/{ver}.info dates any
    # single version for one extra request. Fetch just the pinned one so
    # "how old is what we ship" is answerable without 27 round-trips.
    if c.current:
        want = c.current if c.current.startswith("v") else f"v{c.current}"
        for r in releases:
            if r.version == want and r.published is None:
                try:
                    info = ctx.client.get_json(
                        f"https://proxy.golang.org/{mod}/@v/{want}.info"
                    )
                    r.published = parse_iso((info or {}).get("Time"))
                except HttpError:
                    pass
                break
    return Resolution(
        latest_stable=latest_stable([v for v in versions if "+incompatible" not in v]) or latest_v,
        releases=releases,
        repo=_go_repo(c.name),
        homepage=f"https://pkg.go.dev/{c.name}",
        note="Go proxy exposes dates only for the latest version",
    )


def _resolve_go_toolchain(ctx: Context) -> Resolution:
    body = ctx.github.api("repos/golang/go/tags?per_page=100")
    if not isinstance(body, list):
        return Resolution(error="could not read golang/go tags", repo="golang/go")
    versions = []
    for t in body:
        name = t.get("name") or ""
        m = re.fullmatch(r"go(\d+\.\d+(?:\.\d+)?)", name)
        if m:
            versions.append(m.group(1))
    return Resolution(
        latest_stable=latest_stable(versions),
        releases=[Release(version=v) for v in versions],
        repo="golang/go",
        homepage="https://go.dev/dl/",
    )


def _go_escape(mod: str) -> str:
    """Uppercase letters are !-escaped in module proxy paths."""
    return "".join(f"!{ch.lower()}" if ch.isupper() else ch for ch in mod)


def _go_repo(mod: str) -> str | None:
    if mod.startswith("github.com/"):
        parts = mod.split("/")
        if len(parts) >= 3:
            return f"{parts[1]}/{parts[2]}"
    return None

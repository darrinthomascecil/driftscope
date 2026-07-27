"""Terraform Registry resolver.

Providers: https://registry.terraform.io/v1/providers/{ns}/{name}
Modules:   https://registry.terraform.io/v1/modules/{ns}/{name}/{provider}

The registry does not return publish dates in the version list, so dates come from
the corresponding GitHub releases when a repo is known (which it almost always is
for providers: `{ns}/terraform-provider-{name}`).
"""

from __future__ import annotations

from ..http import HttpError
from ..models import Component, Release, Resolution
from ..version import latest_stable
from . import Context, parse_iso

REGISTRY = "https://registry.terraform.io/v1"

# Providers whose GitHub repo does not follow the standard naming convention.
_REPO_OVERRIDES = {
    "hashicorp/terraform": "hashicorp/terraform",
}


def provider_repo(name: str) -> str | None:
    if name in _REPO_OVERRIDES:
        return _REPO_OVERRIDES[name]
    if "/" not in name:
        return None
    ns, short = name.split("/", 1)
    return f"{ns}/terraform-provider-{short}"


def resolve_provider(c: Component, ctx: Context) -> Resolution:
    if c.meta.get("core") or c.name == "hashicorp/terraform":
        return _resolve_core(ctx)
    if "/" not in c.name:
        return Resolution(error=f"unqualified provider name {c.name!r}")
    ns, short = c.name.split("/", 1)
    url = f"{REGISTRY}/providers/{ns}/{short}"
    try:
        body = ctx.client.get_json(url)
    except HttpError as e:
        return Resolution(error=str(e), repo=provider_repo(c.name))
    versions = [v for v in (body.get("versions") or []) if isinstance(v, str)]
    if not versions:
        return Resolution(error="registry returned no versions", repo=provider_repo(c.name))

    repo = provider_repo(c.name)
    dates = _github_release_dates(repo, ctx) if repo else {}
    # The releases listing is capped at 100, but a provider like azurerm has 400+
    # versions -- so an old pin is usually off the end of the page and ends up
    # undated, which silently kills days_behind. Date the pinned version directly.
    if repo and c.current and c.current.lstrip("v") not in dates:
        dates.update(_github_tag_date(repo, c.current, ctx))
    releases = [
        Release(
            version=v,
            published=dates.get(v.lstrip("v")),
            prerelease=any(t in v.lower() for t in ("alpha", "beta", "rc", "pre")),
        )
        for v in versions
    ]
    return Resolution(
        latest_stable=latest_stable(versions),
        releases=releases,
        repo=repo,
        homepage=f"https://registry.terraform.io/providers/{ns}/{short}",
        note=None if dates else "release dates unavailable",
    )


def _resolve_core(ctx: Context) -> Resolution:
    """Terraform itself, via GitHub releases (there is no registry entry)."""
    body = ctx.github.api("repos/hashicorp/terraform/releases?per_page=100")
    if not isinstance(body, list) or not body:
        return Resolution(error="could not read hashicorp/terraform releases", repo="hashicorp/terraform")
    releases = [
        Release(
            version=(r.get("tag_name") or "").lstrip("v"),
            published=parse_iso(r.get("published_at")),
            prerelease=bool(r.get("prerelease")),
        )
        for r in body
        if r.get("tag_name")
    ]
    stable = [r.version for r in releases if not r.prerelease]
    return Resolution(
        latest_stable=latest_stable(stable),
        releases=releases,
        repo="hashicorp/terraform",
        homepage="https://github.com/hashicorp/terraform",
    )


def resolve_module(c: Component, ctx: Context) -> Resolution:
    if not c.meta.get("registry"):
        return Resolution(
            note="non-registry module source (git/local); no upstream version feed",
            error=None,
        )
    parts = c.name.split("/")
    if len(parts) != 3:
        return Resolution(error=f"unexpected module address {c.name!r}")
    ns, name, provider = parts
    url = f"{REGISTRY}/modules/{ns}/{name}/{provider}"
    try:
        body = ctx.client.get_json(url)
    except HttpError as e:
        return Resolution(error=str(e))
    versions = []
    for v in body.get("versions") or []:
        if isinstance(v, str):
            versions.append(v)
        elif isinstance(v, dict) and isinstance(v.get("version"), str):
            versions.append(v["version"])
    if not versions:
        versions = [body["version"]] if isinstance(body.get("version"), str) else []
    if not versions:
        return Resolution(error="registry returned no versions")

    repo = None
    src = body.get("source")
    if isinstance(src, str) and "github.com/" in src:
        repo = src.split("github.com/", 1)[1].removesuffix(".git").strip("/")
        repo = "/".join(repo.split("/")[:2])
    dates = _github_release_dates(repo, ctx) if repo else {}
    releases = [Release(version=v, published=dates.get(v.lstrip("v"))) for v in versions]
    return Resolution(
        latest_stable=latest_stable(versions),
        releases=releases,
        repo=repo,
        homepage=f"https://registry.terraform.io/modules/{ns}/{name}/{provider}",
    )


def _github_tag_date(repo: str, version: str, ctx: Context) -> dict[str, object]:
    """Date one specific version by fetching its release directly."""
    v = version.lstrip("v")
    for tag in (f"v{v}", v):
        body = ctx.github.api(f"repos/{repo}/releases/tags/{tag}")
        if isinstance(body, dict):
            dt = parse_iso(body.get("published_at"))
            if dt:
                return {v: dt}
    return {}


def _github_release_dates(repo: str | None, ctx: Context) -> dict[str, object]:
    """Map version-without-v -> published datetime, from GitHub releases."""
    if not repo:
        return {}
    body = ctx.github.api(f"repos/{repo}/releases?per_page=100")
    if not isinstance(body, list):
        return {}
    out: dict[str, object] = {}
    for r in body:
        tag = r.get("tag_name")
        if isinstance(tag, str):
            dt = parse_iso(r.get("published_at"))
            if dt:
                out[tag.lstrip("v")] = dt
    return out

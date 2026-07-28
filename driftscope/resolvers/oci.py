"""Container image and Helm chart resolvers.

Image tags are the messiest version surface in this tool: they are free-form
strings, frequently variant-qualified (`3.12-slim`), and a registry has no notion
of "latest stable". The approach is to enumerate tags, keep only those whose
variant matches the current tag, and pick the highest semver-ish among them.
Digest-pinned and `latest`-pinned images are reported as findings in their own
right rather than compared.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from ..http import HttpError
from ..models import Component, Release, Resolution
from ..version import parse as vparse, sort_versions
from . import Context, parse_iso

# Official images live under library/ on Docker Hub.
_HUB = "https://hub.docker.com/v2"

# Well-known image -> GitHub repo, for release notes and issue deltas.
IMAGE_REPOS = {
    "python": "docker-library/python",
    "node": "nodejs/docker-node",
    "nginx": "nginxinc/docker-nginx",
    "postgres": "docker-library/postgres",
    "redis": "docker-library/redis",
    "alpine": "alpinelinux/docker-alpine",
    "ubuntu": "canonical/ubuntu-docker-images",
    "golang": "docker-library/golang",
    "mysql": "docker-library/mysql",
    "mongo": "docker-library/mongo",
    "rabbitmq": "docker-library/rabbitmq",
    "traefik": "traefik/traefik",
    "grafana/grafana": "grafana/grafana",
    "prom/prometheus": "prometheus/prometheus",
    "hashicorp/terraform": "hashicorp/terraform",
}

_SKIP_TAGS = re.compile(
    r"(?:^latest$|^stable$|^edge$|^main$|^master$|^nightly$|sha-|^sha256)", re.I
)


def _hub_repo_path(name: str) -> str | None:
    """Map an image name to a Docker Hub v2 repository path, or None if not Hub."""
    if "/" not in name:
        return f"library/{name}"
    host = name.split("/", 1)[0]
    if "." in host or ":" in host:
        return None  # ghcr.io, mcr.microsoft.com, private registries
    return name


def resolve_image(c: Component, ctx: Context) -> Resolution:
    # A private registry answers for itself. Checked before the Docker Hub path
    # because `_hub_repo_path` rejects any host with a dot and would otherwise
    # send an internal image down the GitHub-releases fallback, which cannot
    # know anything about it.
    from .acr import resolve_acr_image

    private = resolve_acr_image(c, ctx)
    if private is not None:
        return private

    repo_hint = IMAGE_REPOS.get(c.name) or IMAGE_REPOS.get(c.name.split("/")[-1])

    if c.meta.get("digest") and not c.current:
        return Resolution(
            repo=repo_hint,
            note="digest-pinned; immutable by design, check for a newer digest out of band",
        )
    if c.current in (None, "latest"):
        return Resolution(
            repo=repo_hint,
            note="floating tag; the running image is unpinned and unreproducible",
        )

    path = _hub_repo_path(c.name)
    if path is None:
        return _resolve_via_github(c, repo_hint, ctx)

    tags: list[tuple[str, object]] = []
    truncated = False
    try:
        for page in (1, 2):
            body = ctx.client.get_json(
                f"{_HUB}/repositories/{path}/tags?page_size=100&page={page}&ordering=last_updated"
            )
            for t in body.get("results") or []:
                name = t.get("name")
                if isinstance(name, str):
                    tags.append((name, parse_iso(t.get("last_updated"))))
            if not body.get("next"):
                break
            if page == 2:
                # More pages exist and we stopped. This matters downstream: the
                # listing is ordered by recency, not version, so the tags we did
                # NOT fetch are arbitrary rather than "the older ones".
                truncated = True
    except HttpError as e:
        return Resolution(error=str(e), repo=repo_hint)

    if not tags:
        return Resolution(error="no tags returned", repo=repo_hint)

    cur = vparse(c.current)
    want_variant = cur.variant
    candidates = []
    for name, when in tags:
        if _SKIP_TAGS.search(name):
            continue
        pv = vparse(name)
        if not pv.valid or pv.variant != want_variant:
            continue
        # A bare major like "3" is a moving alias; ignore for "latest" purposes.
        if len(pv.release) < 2:
            continue
        candidates.append((name, when, pv))

    if not candidates:
        return Resolution(
            repo=repo_hint,
            note=f"no comparable tags for variant {want_variant or 'default'!r}",
        )
    candidates.sort(key=lambda x: x[2].sort_key())
    releases = [Release(version=n, published=w, prerelease=p.is_prerelease) for n, w, p in candidates]
    stable = [r for r in releases if not r.prerelease]
    newest = (stable or releases)[-1]

    notes = []
    if want_variant:
        notes.append(f"compared within variant {want_variant!r}")
    # Docker Hub's `last_updated` is when the tag was last *pushed*, not when the
    # version was first released: base images are rebuilt for OS patches, so an
    # old tag can carry a recent timestamp. Day counts here are weaker evidence
    # than the version distance.
    notes.append("dates are Docker Hub last-push times, not original release dates")
    if truncated:
        notes.append(
            "tag listing truncated at 200 tags (Hub pages by recency, not version), "
            "so an old pin may be absent and undatable"
        )
    found_current = any(r.version == c.current for r in releases)
    if not found_current:
        notes.append(
            f"tag {c.current!r} was not in the fetched listing; age unavailable"
        )

    return Resolution(
        latest_stable=newest.version,
        releases=releases,
        repo=repo_hint,
        homepage=f"https://hub.docker.com/_/{c.name}" if "/" not in c.name else None,
        note="; ".join(notes),
        # Blocks the age lower-bound heuristic: "oldest listed" is meaningless
        # in a recency-ordered, truncated listing.
        extra={"list_truncated": truncated, "dates_are_push_times": True},
    )


def _resolve_via_github(c: Component, repo: str | None, ctx: Context) -> Resolution:
    """Non-Hub registries: fall back to the project's GitHub releases."""
    if not repo:
        return Resolution(
            note=f"registry {c.name.split('/')[0]} not supported for tag enumeration"
        )
    body = ctx.github.api(f"repos/{repo}/releases?per_page=100")
    if not isinstance(body, list) or not body:
        return Resolution(repo=repo, error="no GitHub releases found")
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
    ordered = sort_versions(stable)
    return Resolution(
        latest_stable=ordered[-1].raw if ordered else None,
        releases=releases,
        repo=repo,
        note="resolved from GitHub releases, not the registry",
    )


# ------------------------------------------------------------------------ Helm


def resolve_chart(c: Component, ctx: Context) -> Resolution:
    """Resolve a Helm chart via Artifact Hub search."""
    if c.meta.get("self"):
        return Resolution(
            note="chart defined in this repo; version is yours to bump, no upstream"
        )
    try:
        body = ctx.client.get_json(
            f"https://artifacthub.io/api/v1/packages/search"
            f"?ts_query_web={quote(c.name)}&kind=0&limit=5&facets=false"
        )
    except HttpError as e:
        return Resolution(error=str(e))
    packages = (body or {}).get("packages") or []
    match = None
    for p in packages:
        if (p.get("name") or "").lower() == c.name.lower():
            match = p
            break
    if match is None:
        return Resolution(note=f"chart {c.name!r} not found on Artifact Hub")

    repo_name = ((match.get("repository") or {}).get("name")) or ""
    try:
        detail = ctx.client.get_json(
            f"https://artifacthub.io/api/v1/packages/helm/{quote(repo_name)}/{quote(c.name)}"
        )
    except HttpError:
        detail = match

    versions = detail.get("available_versions") or []
    releases = [
        Release(
            version=v.get("version"),
            published=parse_iso(_ts_to_iso(v.get("ts"))),
            prerelease=bool(v.get("prerelease")),
        )
        for v in versions
        if isinstance(v, dict) and v.get("version")
    ]
    if not releases and detail.get("version"):
        releases = [Release(version=detail["version"])]
    ordered = sort_versions([r.version for r in releases if not r.prerelease])
    # The chart repository URL is usually a Helm repo (charts.bitnami.com), not a
    # source repo. The GitHub link lives in `links[]` (name "source"), so check
    # there first -- without it no Helm chart ever gets an issue delta.
    repo = None
    candidates: list[str] = []
    for link in detail.get("links") or []:
        if isinstance(link, dict) and isinstance(link.get("url"), str):
            # Prefer an explicit source link over other links (docs, support).
            if (link.get("name") or "").lower() in ("source", "chart source", "github"):
                candidates.insert(0, link["url"])
            else:
                candidates.append(link["url"])
    candidates += [
        (detail.get("repository") or {}).get("url") or "",
        detail.get("home_url") or "",
    ]
    for url in candidates:
        m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)", url or "")
        if m:
            repo = f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
            break
    url = (detail.get("repository") or {}).get("url") or ""
    return Resolution(
        latest_stable=(ordered[-1].raw if ordered else detail.get("version")),
        releases=releases,
        repo=repo,
        homepage=detail.get("home_url") or url or None,
        deprecated=bool(detail.get("deprecated")),
        note="chart deprecated on Artifact Hub" if detail.get("deprecated") else None,
    )


def _ts_to_iso(ts: object) -> str | None:
    from datetime import datetime, timezone

    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

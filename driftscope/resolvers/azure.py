"""Azure resolvers: resource-provider API versions, SKUs, AKS node images.

API versions come from the live ARM provider metadata via the `az` CLI, because
there is no unauthenticated public endpoint that lists them per subscription. That
makes this the one static-scan resolver with a credential dependency; with
`--no-az` it degrades to "unresolved" rather than guessing.

Azure API versions are dates, and a `-preview` suffix is a first-class signal: a
resource pinned to a preview API is a different (and usually worse) problem than
one pinned to an old GA API, so the two are reported differently.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date

from ..models import Component, Release, Resolution
from ..version import latest_stable, parse as vparse
from . import Context

_AZ_AVAILABLE: bool | None = None


def az_available() -> bool:
    global _AZ_AVAILABLE
    if _AZ_AVAILABLE is None:
        _AZ_AVAILABLE = shutil.which("az") is not None
    return _AZ_AVAILABLE


def _az_json(args: list[str], ctx: Context, cache_key: str) -> object | None:
    cached = ctx.client.cache.get(cache_key)
    if cached is not None:
        return cached
    if not ctx.allow_az or not az_available():
        return None
    try:
        r = subprocess.run(
            ["az", *args, "-o", "json"], capture_output=True, text=True, timeout=120
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        body = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    ctx.client.cache.put(cache_key, body)
    return body


def _provider_api_versions(namespace: str, ctx: Context) -> dict[str, list[str]]:
    """Map lowercase resourceType -> apiVersions for one RP namespace."""
    body = _az_json(
        ["provider", "show", "-n", namespace], ctx, f"az:provider:{namespace.lower()}"
    )
    if not isinstance(body, dict):
        return {}
    out: dict[str, list[str]] = {}
    for rt in body.get("resourceTypes") or []:
        name = rt.get("resourceType")
        versions = rt.get("apiVersions") or []
        if isinstance(name, str) and isinstance(versions, list):
            out[name.lower()] = [v for v in versions if isinstance(v, str)]
    return out


def resolve_api_version(c: Component, ctx: Context) -> Resolution:
    if c.meta.get("schema"):
        return Resolution(
            note="ARM template schema version; rarely a functional risk",
            latest_stable=c.current,
        )
    if "/" not in c.name:
        return Resolution(error=f"malformed resource type {c.name!r}")

    namespace, _, rtype = c.name.partition("/")
    table = _provider_api_versions(namespace, ctx)
    if not table:
        reason = (
            "az CLI unavailable or not logged in"
            if not (ctx.allow_az and az_available())
            else f"provider {namespace} returned no resource types"
        )
        return Resolution(error=reason)

    versions = table.get(rtype.lower())
    inherited_from = None
    if versions is None:
        # Nested child types (e.g. accounts/deployments) are often not enumerated
        # by ARM. Fall back to the closest declared ancestor and say so.
        parts = rtype.lower().split("/")
        for i in range(len(parts) - 1, 0, -1):
            cand = "/".join(parts[:i])
            if cand in table:
                versions, inherited_from = table[cand], f"{namespace}/{cand}"
                break
    if not versions:
        return Resolution(
            error=f"resource type {c.name!r} not found in provider metadata"
        )

    stable = [v for v in versions if "preview" not in v.lower()]
    newest_stable = latest_stable(stable)
    releases = [
        Release(version=v, prerelease="preview" in v.lower()) for v in versions
    ]
    notes = []
    if inherited_from:
        notes.append(f"api-versions inherited from parent type {inherited_from}")
    if c.meta.get("preview"):
        notes.append("currently pinned to a PREVIEW api-version")
    if c.current and c.current not in versions:
        notes.append("pinned api-version not advertised by the provider (may be retired)")

    return Resolution(
        latest_stable=newest_stable,
        releases=releases,
        repo=None,  # ARM resource providers are not open source
        homepage=f"https://learn.microsoft.com/azure/templates/{namespace.lower()}/{rtype.lower()}",
        note="; ".join(notes) or None,
    )


def resolve_sku(c: Component, ctx: Context) -> Resolution:
    """SKU findings are produced by live introspection, which supplies candidates."""
    available = c.meta.get("available") or []
    if not available:
        return Resolution(note="no SKU alternatives reported for this resource")
    return Resolution(
        latest_stable=c.meta.get("recommended") or None,
        releases=[Release(version=str(v)) for v in available],
        homepage=c.meta.get("docs"),
        note=c.meta.get("note"),
    )


# AKS node image versions carry their build date in the version string, so age is
# computable with no API call at all. Three formats are in the wild:
#   AKSUbuntu-2204gen2containerd-202607.15.0   YYYYMM.DD.revision  (current)
#   AKSUbuntu-1804gen2containerd-2022.08.15    YYYY.MM.DD          (legacy)
#   AKSWindows-2022-containerd-20348.2582.240716   trailing YYMMDD (Windows)
_NI_YYYYMM_DD = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])\.(\d{1,2})(?:\.(\d+))?(?!\d)")
_NI_Y_M_D = re.compile(r"(?<!\d)(20\d{2})\.(0[1-9]|1[0-2])\.([0-3]?\d)(?!\d)")
_NI_WINDOWS = re.compile(r"\.(\d{2})(0[1-9]|1[0-2])([0-3]\d)$")


def parse_node_image_date(version: str | None) -> date | None:
    """Extract the build date embedded in an AKS node image version string."""
    if not version:
        return None
    s = version.strip()
    m = _NI_Y_M_D.search(s)  # try the unambiguous legacy form first
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _NI_YYYYMM_DD.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _NI_WINDOWS.search(s)
    if m:
        try:
            return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def resolve_node_image(c: Component, ctx: Context) -> Resolution:
    """AKS node image. Age comes from the date embedded in the version string;
    the upgrade target comes from `az aks nodepool get-upgrades` during the live
    scan, so drift is reported even when only the current version is known."""
    current_date = parse_node_image_date(c.current)
    latest = c.meta.get("latest") or None
    candidates = [str(u) for u in (c.meta.get("available") or []) if u]
    if latest and latest not in candidates:
        candidates.append(latest)

    releases: list[Release] = []
    if c.current:
        releases.append(
            Release(
                version=c.current,
                published=_as_dt(current_date),
            )
        )
    for cand in candidates:
        if cand == c.current:
            continue
        releases.append(Release(version=cand, published=_as_dt(parse_node_image_date(cand))))

    notes = []
    extra: dict = {}
    if c.meta.get("note"):
        notes.append(c.meta["note"])
    if current_date is None and c.current:
        notes.append(
            f"could not parse a build date out of {c.current!r}; age unavailable"
        )

    # The embedded date is when the VHD was *built*. Azure/AgentBaker is the repo
    # that builds it, so its release date is when the image actually shipped --
    # and it also gives us a changelog window. Prefer it over the build date.
    latest_date = parse_node_image_date(latest) if latest else None
    if current_date and ctx.github.enabled:
        from ..ghub import agentbaker as ab

        index = ab.release_index(ctx.github)
        start = ab.release_for_date(index, current_date)
        end = ab.release_for_date(index, latest_date) or ab.latest_release(index)
        if start:
            extra["agentbaker_from"] = start.tag
            extra["build_date"] = current_date.isoformat()
            extra["release_date"] = (
                start.published_date.isoformat() if start.published_date else None
            )
            # Age stays anchored to the VHD build date embedded in the version:
            # it is intrinsic and verifiable. The AgentBaker release is the code
            # that produced it and is cut *before* the build, so using its date
            # would overstate the image's age. Keep it as changelog context.
            if start.published:
                notes.append(
                    f"built {current_date.isoformat()} from AgentBaker {start.tag} "
                    f"(released {start.effective_date.isoformat()})"
                )
        if end:
            extra["agentbaker_to"] = end.tag
            extra["latest_release_date"] = (
                end.published_date.isoformat() if end.published_date else None
            )
            if not latest:
                # No AKS-reported target, but AgentBaker tells us what the newest
                # node image line is, which is enough to measure the gap.
                latest = f"(AgentBaker {end.tag})"
                if end.published:
                    releases.append(Release(version=latest, published=end.published))
                notes.append(
                    "no upgrade target reported by AKS; newest node image line "
                    f"inferred from AgentBaker release {end.tag}"
                )

    return Resolution(
        latest_stable=latest or (candidates[-1] if candidates else None),
        releases=releases,
        repo="Azure/AgentBaker",
        homepage="https://github.com/Azure/AgentBaker/releases",
        note="; ".join(notes) or None,
        extra=extra,
    )


def _as_dt(d: date | None):
    from datetime import datetime, timezone

    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None

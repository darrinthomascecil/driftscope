"""Resolvers whose upstream truth lives on GitHub: Actions, Kubernetes APIs.

Kubernetes API versions are deliberately *not* resolved by "find the highest
number". A `v1beta1` resource is not N versions behind `v1`; it is either fine,
deprecated, or removed as of a specific cluster release. That is a fixed, finite
table, so it is encoded here rather than inferred.
"""

from __future__ import annotations

import re

from ..models import Component, Release, Resolution
from ..version import sort_versions
from . import Context, parse_iso

# group/Kind -> (stable apiVersion, deprecated_in, removed_in)
# Sourced from the Kubernetes deprecated-API-migration guide.
K8S_MIGRATIONS: dict[str, tuple[str, str, str | None]] = {
    "extensions/Ingress": ("networking.k8s.io/v1", "1.14", "1.22"),
    "networking.k8s.io/Ingress": ("networking.k8s.io/v1", "1.19", "1.22"),
    "extensions/Deployment": ("apps/v1", "1.9", "1.16"),
    "extensions/DaemonSet": ("apps/v1", "1.9", "1.16"),
    "extensions/ReplicaSet": ("apps/v1", "1.9", "1.16"),
    "apps/Deployment": ("apps/v1", "1.9", "1.16"),
    "apps/StatefulSet": ("apps/v1", "1.9", "1.16"),
    "extensions/PodSecurityPolicy": ("(removed - use Pod Security Admission)", "1.21", "1.25"),
    "policy/PodSecurityPolicy": ("(removed - use Pod Security Admission)", "1.21", "1.25"),
    "batch/CronJob": ("batch/v1", "1.21", "1.25"),
    "autoscaling/HorizontalPodAutoscaler": ("autoscaling/v2", "1.23", "1.26"),
    "rbac.authorization.k8s.io/Role": ("rbac.authorization.k8s.io/v1", "1.17", "1.22"),
    "rbac.authorization.k8s.io/ClusterRole": ("rbac.authorization.k8s.io/v1", "1.17", "1.22"),
    "rbac.authorization.k8s.io/RoleBinding": ("rbac.authorization.k8s.io/v1", "1.17", "1.22"),
    "rbac.authorization.k8s.io/ClusterRoleBinding": ("rbac.authorization.k8s.io/v1", "1.17", "1.22"),
    "apiextensions.k8s.io/CustomResourceDefinition": ("apiextensions.k8s.io/v1", "1.16", "1.22"),
    "admissionregistration.k8s.io/ValidatingWebhookConfiguration": (
        "admissionregistration.k8s.io/v1", "1.16", "1.22",
    ),
    "admissionregistration.k8s.io/MutatingWebhookConfiguration": (
        "admissionregistration.k8s.io/v1", "1.16", "1.22",
    ),
    "storage.k8s.io/CSIDriver": ("storage.k8s.io/v1", "1.19", "1.22"),
    "coordination.k8s.io/Lease": ("coordination.k8s.io/v1", "1.19", "1.22"),
    "flowcontrol.apiserver.k8s.io/FlowSchema": ("flowcontrol.apiserver.k8s.io/v1", "1.29", "1.32"),
    "policy/PodDisruptionBudget": ("policy/v1", "1.21", "1.25"),
}


def resolve_k8s_api(c: Component, ctx: Context) -> Resolution:
    """Resolve a Kubernetes apiVersion against the deprecation table."""
    entry = K8S_MIGRATIONS.get(c.name)
    current_api = c.meta.get("apiVersion") or c.current or ""
    if entry is None:
        return Resolution(
            latest_stable=c.current,
            note="no known deprecation for this group/kind",
            repo="kubernetes/kubernetes",
        )
    target, deprecated_in, removed_in = entry
    # A resource already on the stable target is fine.
    if current_api == target or (target.endswith("/" + (c.current or "")) and "beta" not in (c.current or "")):
        return Resolution(
            latest_stable=c.current,
            note="already on the stable apiVersion",
            repo="kubernetes/kubernetes",
        )
    stable_ver = target.rsplit("/", 1)[-1] if "/" in target else target
    return Resolution(
        latest_stable=stable_ver,
        releases=[Release(version=stable_ver)],
        repo="kubernetes/kubernetes",
        homepage="https://kubernetes.io/docs/reference/using-api/deprecation-guide/",
        deprecated=True,
        note=(
            f"{current_api} deprecated in k8s {deprecated_in}"
            + (f", REMOVED in {removed_in}" if removed_in else "")
            + f"; migrate to {target}"
        ),
    )


def resolve_k8s_cluster(c: Component, ctx: Context) -> Resolution:
    """Latest Kubernetes version this cluster can actually move to.

    When a live scan supplied the platform's own upgrade candidates, those win:
    upstream Kubernetes ships versions before AKS/EKS offer them, so pointing at
    the upstream release would recommend a version the customer cannot deploy.
    """
    available = [str(v) for v in (c.meta.get("available") or []) if v]
    if available:
        ordered = sort_versions(available)
        newest = ordered[-1].raw if ordered else None
        return Resolution(
            latest_stable=newest,
            releases=[Release(version=v) for v in available],
            repo="kubernetes/kubernetes",
            homepage="https://kubernetes.io/releases/",
            note=(
                f"target is the newest version offered by the platform for this "
                f"cluster ({len(available)} candidate(s)); upstream Kubernetes may "
                "already be ahead of what is deployable here"
            ),
        )

    body = ctx.github.api("repos/kubernetes/kubernetes/releases?per_page=60")
    if not isinstance(body, list) or not body:
        return Resolution(error="could not read kubernetes releases", repo="kubernetes/kubernetes")
    releases = [
        Release(
            version=(r.get("tag_name") or "").lstrip("v"),
            published=parse_iso(r.get("published_at")),
            prerelease=bool(r.get("prerelease")) or bool(re.search(r"(alpha|beta|rc)", r.get("tag_name") or "")),
        )
        for r in body
        if r.get("tag_name")
    ]
    stable = sort_versions([r.version for r in releases if not r.prerelease])
    return Resolution(
        latest_stable=stable[-1].raw if stable else None,
        releases=releases,
        repo="kubernetes/kubernetes",
        homepage="https://kubernetes.io/releases/",
    )


def resolve_action(c: Component, ctx: Context) -> Resolution:
    """Resolve a GitHub Action (or a runner label) to its latest release."""
    if c.meta.get("runner"):
        return _resolve_runner(c)

    if c.meta.get("sha_pinned"):
        # SHA pinning is the security best practice; we still want to know if the
        # pinned commit is far behind, but that needs a commit->tag lookup.
        return Resolution(
            repo=c.name,
            note="pinned to a commit SHA (good for supply-chain safety); "
            "version lag not computed",
        )

    body = ctx.github.api(f"repos/{c.name}/releases?per_page=100")
    releases: list[Release] = []
    if isinstance(body, list):
        for r in body:
            tag = r.get("tag_name")
            if isinstance(tag, str):
                releases.append(
                    Release(
                        version=tag.lstrip("v"),
                        published=parse_iso(r.get("published_at")),
                        prerelease=bool(r.get("prerelease")),
                    )
                )
    if not releases:
        tags = ctx.github.api(f"repos/{c.name}/tags?per_page=100")
        if isinstance(tags, list):
            releases = [
                Release(version=(t.get("name") or "").lstrip("v"))
                for t in tags
                if t.get("name")
            ]
    if not releases:
        return Resolution(error=f"no releases or tags for {c.name}", repo=c.name)

    stable = sort_versions([r.version for r in releases if not r.prerelease])
    latest = stable[-1] if stable else None
    if latest is None:
        return Resolution(error="no stable release", repo=c.name, releases=releases)

    # Workflows normally pin a floating major tag (`v4`). Compare like-for-like:
    # report the latest major, so v3 -> v4 registers as one major behind.
    cur = (c.current or "").lstrip("vV")
    if re.fullmatch(r"\d+", cur):
        return Resolution(
            latest_stable=str(latest.major),
            releases=[Release(version=str(r.version).split(".")[0], published=r.published)
                      for r in releases if not r.prerelease],
            repo=c.name,
            homepage=f"https://github.com/{c.name}",
            note=f"comparing major tags; newest full release is {latest.raw}",
        )
    return Resolution(
        latest_stable=latest.raw,
        releases=releases,
        repo=c.name,
        homepage=f"https://github.com/{c.name}",
    )


def _resolve_runner(c: Component) -> Resolution:
    label = c.current or ""
    successor = c.meta.get("successor")
    if c.meta.get("latest_label"):
        return Resolution(
            latest_stable=label,
            note="floating '-latest' runner label; image changes under you, "
            "but never goes stale",
        )
    if successor:
        return Resolution(
            latest_stable=successor,
            releases=[Release(version=successor)],
            repo="actions/runner-images",
            homepage="https://github.com/actions/runner-images",
            deprecated=True,
            note=f"runner image {label} is deprecated or retired; move to {successor}",
        )
    return Resolution(latest_stable=label, note="runner label appears current",
                      repo="actions/runner-images")

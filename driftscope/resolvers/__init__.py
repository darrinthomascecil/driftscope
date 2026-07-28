"""Resolvers: given a Component, ask upstream what the latest stable version is.

Each resolver returns a Resolution and must never raise -- transport and parsing
failures become `Resolution.error` so one dead registry cannot fail a whole scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..http import Client, GitHub
from ..models import (
    AZURE_API,
    DOTNET_SDK,
    DOTNET_TFM,
    JVM_TOOLCHAIN,
    MAVEN,
    NUGET,
    AZURE_NODE_IMAGE,
    AZURE_SKU,
    DOCKER,
    GO,
    HELM,
    K8S_API,
    K8S_CLUSTER,
    NPM,
    PYPI,
    RUBYGEMS,
    RUBY_RUNTIME,
    TERRAFORM_MODULE,
    TERRAFORM_PROVIDER,
    Component,
    Resolution,
)
from ..scanners.cicd import GH_ACTION


@dataclass
class Context:
    client: Client
    github: GitHub
    allow_az: bool = True
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Forge clients keyed by kind. Populated by build_context; resolvers keep using
    # `.github` directly, which is why adding this breaks nothing.
    forges: dict = field(default_factory=dict)

    def forge_for(self, kind: str | None):
        """The client for a resolution's forge, defaulting to GitHub."""
        from ..forge import resolve_forge

        return resolve_forge(kind, self.forges)


def parse_iso(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    # Trim fractional seconds longer than 6 digits (npm/PyPI sometimes emit more).
    if "." in t:
        head, _, tail = t.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        rest = tail[len(digits) :] if tail[len(digits) :].startswith(("+", "-")) else ""
        t = f"{head}.{digits or '0'}{rest}"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve(component: Component, ctx: Context) -> Resolution:
    from . import (azure, dotnet_jvm as dj, github_src, oci, packages,
                   ruby as rb, terraform as tf)

    table = {
        TERRAFORM_PROVIDER: tf.resolve_provider,
        TERRAFORM_MODULE: tf.resolve_module,
        PYPI: packages.resolve_pypi,
        NPM: packages.resolve_npm,
        GO: packages.resolve_go,
        DOCKER: oci.resolve_image,
        HELM: oci.resolve_chart,
        K8S_API: github_src.resolve_k8s_api,
        K8S_CLUSTER: github_src.resolve_k8s_cluster,
        GH_ACTION: github_src.resolve_action,
        AZURE_API: azure.resolve_api_version,
        AZURE_SKU: azure.resolve_sku,
        AZURE_NODE_IMAGE: azure.resolve_node_image,
        NUGET: dj.resolve_nuget,
        MAVEN: dj.resolve_maven,
        DOTNET_TFM: dj.resolve_dotnet_tfm,
        DOTNET_SDK: dj.resolve_dotnet_sdk,
        JVM_TOOLCHAIN: dj.resolve_jvm_toolchain,
        RUBYGEMS: rb.resolve_rubygem,
        RUBY_RUNTIME: rb.resolve_ruby_runtime,
    }
    fn = table.get(component.ecosystem)
    if fn is None:
        return Resolution(error=f"no resolver for ecosystem {component.ecosystem!r}")
    try:
        return fn(component, ctx)
    except Exception as e:  # resolver bugs must not abort the run
        return Resolution(error=f"{type(e).__name__}: {e}")

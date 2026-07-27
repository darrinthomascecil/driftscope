"""Live Azure introspection (phase 2).

Static scanning tells you what the IaC *claims*. This tells you what is actually
deployed, which differs whenever someone changed something in the portal, or the
IaC was never applied. Findings here carry `source="azure://<resource-id>"` so a
report can distinguish "your repo is stale" from "your subscription is stale".

Read-only: every command is a `show`/`list`/`get-*`. Nothing here mutates.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from ..models import (
    AZURE_API,
    AZURE_NODE_IMAGE,
    AZURE_SKU,
    K8S_CLUSTER,
    Component,
)


@dataclass
class AzResult:
    ok: bool
    data: object = None
    error: str | None = None


def az_available() -> bool:
    return shutil.which("az") is not None


def _az(args: list[str], timeout: int = 120) -> AzResult:
    if not az_available():
        return AzResult(False, error="az CLI not found on PATH")
    try:
        r = subprocess.run(
            ["az", *args, "-o", "json"], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return AzResult(False, error=f"az {' '.join(args[:3])} timed out")
    except (subprocess.SubprocessError, OSError) as e:
        return AzResult(False, error=f"{type(e).__name__}: {e}")
    if r.returncode != 0:
        return AzResult(False, error=(r.stderr or "").strip()[:300] or "az returned non-zero")
    if not r.stdout.strip():
        return AzResult(True, data=None)
    try:
        return AzResult(True, data=json.loads(r.stdout))
    except json.JSONDecodeError as e:
        return AzResult(False, error=f"unparseable az output: {e}")


def logged_in() -> bool:
    return _az(["account", "show"]).ok


def scan_subscription(
    *,
    subscription: str | None = None,
    resource_group: str | None = None,
    include_aks: bool = True,
    include_cognitive: bool = True,
) -> tuple[list[Component], list[str]]:
    """Enumerate live resources whose versions we can assess."""
    components: list[Component] = []
    errors: list[str] = []
    scope: list[str] = []
    if subscription:
        scope += ["--subscription", subscription]

    if include_cognitive:
        c, e = _scan_cognitive(scope, resource_group)
        components += c
        errors += e
    if include_aks:
        c, e = _scan_aks(scope, resource_group)
        components += c
        errors += e
    return components, errors


# ------------------------------------------------------------- Cognitive / AI


def _scan_cognitive(
    scope: list[str], resource_group: str | None
) -> tuple[list[Component], list[str]]:
    """Model deployments on AI Services accounts: SKU, capacity, model version."""
    args = ["cognitiveservices", "account", "list", *scope]
    if resource_group:
        args += ["-g", resource_group]
    res = _az(args)
    if not res.ok:
        return [], [f"cognitiveservices account list: {res.error}"]
    accounts = res.data if isinstance(res.data, list) else []

    out: list[Component] = []
    errors: list[str] = []
    for acct in accounts:
        name = acct.get("name")
        rg = (acct.get("resourceGroup") or "")
        loc = acct.get("location") or ""
        if not name or not rg:
            continue
        dep = _az(
            ["cognitiveservices", "account", "deployment", "list", "-n", name, "-g", rg, *scope]
        )
        if not dep.ok:
            errors.append(f"deployment list for {name}: {dep.error}")
            continue
        catalogue = _model_catalogue(loc, scope)
        for d in dep.data if isinstance(dep.data, list) else []:
            props = d.get("properties") or {}
            model = props.get("model") or {}
            mname, mversion = model.get("name"), model.get("version")
            sku = (d.get("sku") or {}).get("name")
            capacity = (d.get("sku") or {}).get("capacity")
            rid = d.get("id") or f"{rg}/{name}/{d.get('name')}"

            if mname and mversion:
                available = sorted(
                    {
                        v
                        for (cn, v) in catalogue.get("versions", set())
                        if cn == mname
                    }
                )
                out.append(
                    Component(
                        ecosystem=AZURE_SKU,
                        name=f"model:{mname}",
                        current=mversion,
                        source=f"azure://{rid}",
                        meta={
                            "kind": "model-version",
                            "deployment": d.get("name"),
                            "account": name,
                            "resource_group": rg,
                            "region": loc,
                            "available": available,
                            "recommended": available[-1] if available else None,
                            "sku": sku,
                            "capacity": capacity,
                            "docs": "https://learn.microsoft.com/azure/ai-foundry/concepts/models",
                            "note": f"deployed model version on {name}/{d.get('name')}",
                        },
                    )
                )
            if sku and mname:
                skus = sorted(
                    {s for (cn, s) in catalogue.get("skus", set()) if cn == mname}
                )
                if skus and sku not in skus:
                    out.append(
                        Component(
                            ecosystem=AZURE_SKU,
                            name=f"sku:{mname}",
                            current=sku,
                            source=f"azure://{rid}",
                            meta={
                                "kind": "deployment-sku",
                                "available": skus,
                                "note": f"SKU {sku} is not in the current catalogue for {mname} in {loc}",
                                "region": loc,
                            },
                        )
                    )
    return out, errors


def _model_catalogue(location: str, scope: list[str]) -> dict[str, set]:
    """Model name -> available versions and SKUs in a region."""
    if not location:
        return {}
    res = _az(["cognitiveservices", "model", "list", "-l", location, *scope])
    if not res.ok or not isinstance(res.data, list):
        return {}
    versions: set[tuple[str, str]] = set()
    skus: set[tuple[str, str]] = set()
    for m in res.data:
        model = m.get("model") or {}
        n, v = model.get("name"), model.get("version")
        if n and v:
            versions.add((n, v))
        for s in model.get("skus") or []:
            if s.get("name") and n:
                skus.add((n, s["name"]))
    return {"versions": versions, "skus": skus}


# -------------------------------------------------------------------- AKS


def _scan_aks(
    scope: list[str], resource_group: str | None
) -> tuple[list[Component], list[str]]:
    args = ["aks", "list", *scope]
    if resource_group:
        args += ["-g", resource_group]
    res = _az(args)
    if not res.ok:
        return [], [f"aks list: {res.error}"]
    clusters = res.data if isinstance(res.data, list) else []

    out: list[Component] = []
    errors: list[str] = []
    for c in clusters:
        name, rg = c.get("name"), c.get("resourceGroup")
        if not name or not rg:
            continue
        rid = c.get("id") or f"{rg}/{name}"
        current_k8s = c.get("currentKubernetesVersion") or c.get("kubernetesVersion")

        up = _az(["aks", "get-upgrades", "-n", name, "-g", rg, *scope])
        candidates: list[str] = []
        if up.ok and isinstance(up.data, dict):
            for prof in up.data.get("controlPlaneProfile", {}).get("upgrades") or []:
                v = prof.get("kubernetesVersion")
                if v:
                    candidates.append(v)
        elif not up.ok:
            errors.append(f"aks get-upgrades {name}: {up.error}")

        if current_k8s:
            out.append(
                Component(
                    ecosystem=K8S_CLUSTER,
                    name=f"aks/{name}",
                    current=current_k8s,
                    source=f"azure://{rid}",
                    meta={
                        "available": candidates,
                        "resource_group": rg,
                        "note": "AKS control-plane Kubernetes version",
                        "support_plan": c.get("supportPlan"),
                    },
                )
            )

        for pool in c.get("agentPoolProfiles") or []:
            pname = pool.get("name") or "?"
            node_image = pool.get("nodeImageVersion")
            pool_k8s = pool.get("currentOrchestratorVersion") or pool.get("orchestratorVersion")
            if pool_k8s and pool_k8s != current_k8s:
                out.append(
                    Component(
                        ecosystem=K8S_CLUSTER,
                        name=f"aks/{name}/pool/{pname}",
                        current=pool_k8s,
                        source=f"azure://{rid}/agentPools/{pname}",
                        meta={
                            "available": [current_k8s] if current_k8s else [],
                            "note": "node pool trails the control plane",
                            "pool": pname,
                        },
                    )
                )
            if node_image:
                # AKS itself knows the newest image for this pool; ask it rather
                # than inferring, since availability is pool- and region-specific.
                pool_up = _az([
                    "aks", "nodepool", "get-upgrades", "-n", pname,
                    "--cluster-name", name, "-g", rg, *scope,
                ])
                latest_image = None
                if pool_up.ok and isinstance(pool_up.data, dict):
                    latest_image = pool_up.data.get("latestNodeImageVersion")
                elif not pool_up.ok:
                    errors.append(
                        f"aks nodepool get-upgrades {name}/{pname}: {pool_up.error}"
                    )
                out.append(
                    Component(
                        ecosystem=AZURE_NODE_IMAGE,
                        name=f"aks/{name}/pool/{pname}/nodeimage",
                        current=node_image,
                        source=f"azure://{rid}/agentPools/{pname}",
                        meta={
                            "available": [latest_image] if latest_image else [],
                            "latest": latest_image,
                            "os_sku": pool.get("osSKU"),
                            "vm_size": pool.get("vmSize"),
                            "pool": pname,
                            "cluster": name,
                            "resource_group": rg,
                            "note": "AKS node image version",
                        },
                    )
                )
            vm = pool.get("vmSize")
            if vm:
                out.append(
                    Component(
                        ecosystem=AZURE_SKU,
                        name=f"vmsize:{vm}",
                        current=vm,
                        source=f"azure://{rid}/agentPools/{pname}",
                        meta={
                            "kind": "vm-size",
                            "generation": _vm_generation(vm),
                            "available": [],
                            "note": _vm_note(vm),
                        },
                    )
                )
    return out, errors


# Azure VM series generation hints: v2/v3 families are old; v5/v6 are current.
def _vm_generation(size: str) -> str | None:
    import re

    m = re.search(r"_v(\d)$", size)
    return f"v{m.group(1)}" if m else None


def _vm_note(size: str) -> str | None:
    gen = _vm_generation(size)
    if gen in ("v1", "v2", "v3"):
        return (
            f"VM size {size} is a {gen} family; v5/v6 equivalents are typically "
            "cheaper per vCPU and remain in support longer"
        )
    return None

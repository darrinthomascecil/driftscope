"""Regression guard: the cloud overlay must run the app UNDER Workload Identity.

History (why this test exists): the cloud overlay was first created (commit 3e51039) as a
public-repo-only, *open* endpoint and deliberately STRIPPED workload identity to avoid a
confused-deputy. That rationale lapsed once the endpoint moved behind oauth2-proxy SSO + the
Front Door origin lock, and forge-auth (docs/forge-auth.md) now needs the pod's Workload Identity
to sign GitHub App JWTs and read ADO client secrets from Key Vault. The strip was never revisited,
so KV-backed auth silently could not work in cloud. These checks fail loudly if anyone re-adds a
strip or drops the client-id wiring.

No YAML dependency (driftscope is stdlib-only): the checks are on file text, keyed on the
JSON-pointer escape `~1` that only a remove/replace patch uses, so they are specific, not fuzzy.
"""

from __future__ import annotations

from pathlib import Path

DEPLOY = Path(__file__).parent.parent / "deploy"
BASE = DEPLOY / "base"
CLOUD = DEPLOY / "overlays" / "cloud"


def test_base_pod_opts_into_workload_identity():
    dep = (BASE / "deployment.yaml").read_text()
    assert 'azure.workload.identity/use: "true"' in dep, \
        "base Deployment must opt the pod into the workload-identity webhook"


def test_base_sa_carries_the_wi_client_id_annotation():
    sa = (BASE / "serviceaccount.yaml").read_text()
    assert "azure.workload.identity/client-id" in sa, \
        "base ServiceAccount must declare the WI client-id annotation (set per overlay)"


def test_cloud_overlay_does_not_strip_workload_identity():
    kust = (CLOUD / "kustomization.yaml").read_text()
    # A JSON6902 patch that removes the WI annotation/label references the escaped pointer
    # `azure.workload.identity~1...`; a normal annotation never contains `~1`. So its presence
    # here means someone re-added a strip.
    assert "azure.workload.identity~1" not in kust, (
        "cloud overlay must NOT strip Workload Identity -- the endpoint is SSO-gated and "
        "forge-auth needs the pod's identity for Key Vault (see this module's docstring)"
    )


def test_cloud_overlay_wires_the_sa_client_id():
    kust = (CLOUD / "kustomization.yaml").read_text()
    assert "patch-sa.yaml" in kust, "cloud overlay must apply patch-sa.yaml"
    patch = (CLOUD / "patch-sa.yaml").read_text()
    assert "azure.workload.identity/client-id" in patch, \
        "patch-sa.yaml must set the SA's workload-identity client-id"


def test_committed_overlay_is_a_placeholder_template():
    """ADR 0001: the committed cloud overlay is a TEMPLATE -- real per-deployment identifiers
    (host, tenant/client/group ids, vault) are injected at deploy time by the pipeline, never
    committed. Guards against a regression that re-commits a live id -- which once silently
    deployed REPLACE-* and broke Workload Identity + External Secrets.
    """
    import re

    uuid = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
    for path in sorted(CLOUD.glob("**/*.yaml")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]  # comments may legitimately mention ids / "REPLACE-*"
            assert not uuid.search(code), (
                f"{path.relative_to(DEPLOY)}:{i} commits a live id -- must be a REPLACE-* "
                f"placeholder, injected at deploy time (ADR 0001)"
            )
    for name in ("params.yaml", "patch-sa.yaml", "external-secrets.yaml"):
        assert "REPLACE-" in (CLOUD / name).read_text(), \
            f"{name} must hold REPLACE-* placeholders bound at deploy time"

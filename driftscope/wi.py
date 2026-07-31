"""Azure Workload Identity: trade a projected federated token for an AAD token.

In a federated pod the azure-workload-identity webhook projects an OIDC token and
sets AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_FEDERATED_TOKEN_FILE (and
AZURE_AUTHORITY_HOST). Entra exchanges that token -- signed by the cluster, validated
against its published JWKS -- for a token scoped to any Azure resource, with no stored
secret. This is the single primitive the Azure DevOps clone path and the Key
Vault-backed GitHub App path both build on, so it lives here rather than in either.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def federated_token_for(
    scope: str, *, client_id: str | None = None, tenant: str | None = None
) -> str | None:
    """An AAD access token for `scope` (e.g. ``"<resource>/.default"``), or None.

    Returns None unless a projected federated token file exists and both a client id and
    tenant are known (from the arguments or, failing that, AZURE_CLIENT_ID /
    AZURE_TENANT_ID), and the exchange succeeds -- so a process that is not federated
    simply gets None and the caller falls through to another credential source.

    `client_id` / `tenant` let a caller exchange the pod's *one* projected token for a
    token on a *different* Entra app -- how a multi-tenant server mints an ADO token for a
    tenant's own app (`source._entra_ado_credential`). They default to the pod's own
    AZURE_* env, so every existing call (the GitHub App Key Vault path, the legacy ADO
    federated clone) is unchanged. The federated *assertion* is always the pod's projected
    service-account token regardless -- that is what the tenant app federates trust to.
    """
    token_file = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
    client_id = client_id or os.environ.get("AZURE_CLIENT_ID")
    tenant = tenant or os.environ.get("AZURE_TENANT_ID")
    if not (token_file and client_id and tenant):
        return None
    try:
        assertion = Path(token_file).read_text().strip()
    except OSError:
        return None
    if not assertion:
        return None
    # AZURE_AUTHORITY_HOST is injected too, so this follows the pod into sovereign
    # clouds (Government, China) instead of hard-coding the public login endpoint.
    authority = (os.environ.get("AZURE_AUTHORITY_HOST")
                 or "https://login.microsoftonline.com").rstrip("/")
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "scope": scope,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
    }).encode()
    req = urllib.request.Request(f"{authority}/{tenant}/oauth2/v2.0/token", data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp).get("access_token") or None
    except (urllib.error.URLError, OSError, ValueError):
        return None


_KV_API_VERSION = "7.4"


def get_key_vault_secret(vault_uri: str, secret_name: str, secret_version: str = "") -> str | None:
    """Read a Key Vault secret's value with the pod's workload identity, or None.

    Used to fetch an ADO Entra app's client secret at mint time so it is never stored in
    driftscope's config -- only referenced. Reached with the *server's* federated identity
    (not the tenant's), so the server's managed identity needs Key Vault Secrets User on
    the referenced vault. Returns None on any failure so the caller falls through rather
    than erroring; the value is never logged.
    """
    kv_token = federated_token_for("https://vault.azure.net/.default")
    if not kv_token:
        return None
    path = f"secrets/{secret_name}/{secret_version}" if secret_version else f"secrets/{secret_name}"
    url = f"{vault_uri.rstrip('/')}/{path}?api-version={_KV_API_VERSION}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {kv_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp).get("value") or None
    except (urllib.error.URLError, OSError, ValueError):
        return None

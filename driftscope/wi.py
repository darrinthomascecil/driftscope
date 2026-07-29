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


def federated_token_for(scope: str) -> str | None:
    """An AAD access token for `scope` (e.g. ``"<resource>/.default"``), or None.

    Returns None unless AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_FEDERATED_TOKEN_FILE
    are all present and the exchange succeeds, so a process that is not federated
    simply gets None and the caller falls through to another credential source rather
    than erroring.
    """
    token_file = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
    client_id = os.environ.get("AZURE_CLIENT_ID")
    tenant = os.environ.get("AZURE_TENANT_ID")
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

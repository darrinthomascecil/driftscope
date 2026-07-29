"""GitHub App authentication -- the secure, short-lived alternative to a PAT.

A GitHub App authenticates with a private key to mint installation access tokens that
last at most an hour and carry only the App's least-privilege permissions. This module
produces one on demand, caching it until it is close to expiry.

The private key is the one real secret. Two ways to hold it, in order of preference:

  Model A -- Key Vault sign-in-place (recommended): the key lives in Azure Key Vault as
    a *key*, and the App JWT is signed by Key Vault's `sign` operation, reached with the
    pod's workload identity. The private key never leaves the vault, and driftscope
    needs no crypto library. Configure GITHUB_APP_KEY_VAULT_URI + GITHUB_APP_KEY_NAME.

  Model B -- local sign: the PEM is on disk (GITHUB_APP_PRIVATE_KEY_FILE) or in the
    environment (GITHUB_APP_PRIVATE_KEY), and the JWT is signed by shelling to
    `openssl`. Simpler, but the key sits in the pod.

Both always need GITHUB_APP_ID and GITHUB_APP_INSTALLATION_ID. When none of the App
configuration is present, `installation_token()` returns None and the caller falls
through to a `GITHUB_TOKEN`/`gh` credential -- so this is inert until opted into.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

_KV_API_VERSION = "7.4"
# Cache the installation token across a run; refresh once it is within the skew of
# expiry. Tokens last ~1h and minting costs two HTTPS calls, so this matters on a
# multi-repo scan.
_REFRESH_SKEW_SECS = 300
_CACHE: dict[str, object] = {}  # {"token": str, "exp": float}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _configured() -> bool:
    return bool(os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_APP_INSTALLATION_ID"))


def _sign(signing_input: str) -> str | None:
    """RS256-sign the JWT signing input, returning the base64url signature or None."""
    vault = os.environ.get("GITHUB_APP_KEY_VAULT_URI")
    key_name = os.environ.get("GITHUB_APP_KEY_NAME")
    if vault and key_name:
        return _sign_via_key_vault(signing_input, vault, key_name)
    if os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE") or os.environ.get("GITHUB_APP_PRIVATE_KEY"):
        return _sign_via_openssl(signing_input)
    return None


def _sign_via_key_vault(signing_input: str, vault: str, key_name: str) -> str | None:
    """Model A: sign the SHA-256 digest with Key Vault, reached via workload identity.

    Key Vault's `sign` takes the *digest*, not the data, and returns the signature as
    base64url -- which is exactly the JWT's third segment, so it is used verbatim.
    """
    from .wi import federated_token_for

    kv_token = federated_token_for("https://vault.azure.net/.default")
    if not kv_token:
        return None
    digest = hashlib.sha256(signing_input.encode()).digest()
    version = os.environ.get("GITHUB_APP_KEY_VERSION", "")
    path = f"keys/{key_name}/{version}/sign" if version else f"keys/{key_name}/sign"
    url = f"{vault.rstrip('/')}/{path}?api-version={_KV_API_VERSION}"
    body = json.dumps({"alg": "RS256", "value": _b64url(digest)}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {kv_token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp).get("value") or None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _sign_via_openssl(signing_input: str) -> str | None:
    """Model B: RS256-sign locally by shelling to openssl over the PEM."""
    key_file = os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE")
    tmp = None
    try:
        if not key_file:
            pem = (os.environ.get("GITHUB_APP_PRIVATE_KEY") or "").replace("\\n", "\n")
            if not pem.strip():
                return None
            tmp = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
            tmp.write(pem)
            tmp.close()
            key_file = tmp.name
        r = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_file],
            input=signing_input.encode(), capture_output=True, timeout=15,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        return _b64url(r.stdout)
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def _app_jwt() -> str | None:
    """A short-lived JWT that authenticates as the App itself, or None if unsignable."""
    app_id = os.environ.get("GITHUB_APP_ID")
    if not app_id:
        return None
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    # iat backdated 60s for clock skew; exp 9 min out (GitHub caps the App JWT at 10).
    claims = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    signing_input = f"{header}.{claims}"
    signature = _sign(signing_input)
    return f"{signing_input}.{signature}" if signature else None


def _exchange_for_installation_token(app_jwt: str) -> tuple[str, float] | None:
    """Trade the App JWT for an installation token; return (token, expiry_epoch)."""
    installation = os.environ.get("GITHUB_APP_INSTALLATION_ID")
    url = f"https://api.github.com/app/installations/{installation}/access_tokens"
    req = urllib.request.Request(url, data=b"", headers={
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "driftscope",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    token = data.get("token")
    if not token:
        return None
    exp = data.get("expires_at")
    try:
        exp_epoch = datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        exp_epoch = time.time() + 3600  # GitHub's documented default lifetime
    return token, exp_epoch


def installation_token() -> str | None:
    """A GitHub App installation token, minted on demand and cached until near expiry.

    Returns None when the App is not configured or any step fails, so the caller falls
    through to another GitHub credential rather than erroring.

    REVOCATION LAG -- a security caveat. The token is cached until ~REFRESH_SKEW_SECS
    before its ~1h expiry, and driftscope does NOT re-check the installation's current
    repositories or permissions on each scan; it reuses the cached token. GitHub scopes
    an installation token at mint time, and *narrowing* an installation (removing a repo,
    reducing permissions) does not reliably invalidate tokens already issued. So a token
    minted while access was broader can keep granting that broader access for up to the
    cache window (~55 min) after the permission is revoked. Uninstalling the App does
    revoke immediately; narrowing repos does not, so it is not a fast kill-switch. If
    prompt revocation matters, shorten REFRESH_SKEW_SECS toward the token lifetime (mint
    per scan), or restart the process to drop the cache -- which is what a redeploy does.
    """
    if not _configured():
        return None
    cached_token = _CACHE.get("token")
    cached_exp = _CACHE.get("exp")
    if cached_token and isinstance(cached_exp, (int, float)) and time.time() < cached_exp - _REFRESH_SKEW_SECS:
        return cached_token  # type: ignore[return-value]

    app_jwt = _app_jwt()
    if not app_jwt:
        return None
    minted = _exchange_for_installation_token(app_jwt)
    if not minted:
        return None
    token, exp = minted
    _CACHE["token"] = token
    _CACHE["exp"] = exp
    return token

"""Caller authentication for the HTTP scan service: validate the Entra ID token the
edge (oauth2-proxy) forwards, and extract the caller's **app roles**.

This is the *authorization principal* half of multi-tenant safety. The bring-your-own
forge registry (`forge/identity.py`) decides *which* app a scan runs under; without knowing
*who* is asking, a caller could name any tenant's identity (a confused deputy). This module
answers "who is asking" from a cryptographically verified token, and `resolve()` then only
hands back identities whose `principals` (app-role values) the caller actually holds.

Why validate the token in-app rather than trust a forwarded header: a forged
`X-Auth-Request-Groups` header only fails if every mesh invariant holds (the app is
unreachable except via oauth2-proxy, headers are stripped on the check request, no
auth-exempt path leaks). A forged *token* fails signature verification unconditionally. So
the security-critical claim rests on crypto, not on six deployment invariants all holding.

Design notes:
  * **App roles, not groups.** The `roles` claim is emitted for users *and* service
    principals (CI) and never hits the Entra group-overage cliff -- see docs/caller-authz.md.
    The coarse "member of the access group" gate stays at oauth2-proxy, unchanged; this is
    the finer, in-app check on top.
  * **Inert until configured.** `AuthzConfig.from_env()` returns None unless
    DRIFTSCOPE_OIDC_AUDIENCE is set, and `authenticate()` then returns None -- meaning
    "authorization disabled", so a single-tenant deployment behaves exactly as before.
  * **Stdlib only.** RS256 verification is plain modular exponentiation (`pow`) plus a
    PKCS#1 v1.5 comparison -- no crypto dependency, matching the rest of the codebase.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit


class TokenError(Exception):
    """The presented token is missing, malformed, unverifiable, or expired."""


# ------------------------------------------------------------- RS256 (pure stdlib)

# EMSA-PKCS1-v1_5 DigestInfo prefix for SHA-256 (RFC 8017 §9.2).
_SHA256_ASN1 = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64url_to_int(s: str) -> int:
    return int.from_bytes(_b64url_decode(s), "big")


def _rs256_verify(signing_input: bytes, signature: bytes, n: int, e: int) -> bool:
    """Verify an RS256 (RSASSA-PKCS1-v1_5, SHA-256) signature with only `pow`.

    RSA verification is `s^e mod n`; the result must be the PKCS#1 v1.5 encoded digest
    `0x00 0x01 [0xFF..] 0x00 || DigestInfo || SHA256(signing_input)`. Comparing the full
    encoded block in constant time also rejects Bleichenbacher-style padding forgeries that
    a lenient "find the hash somewhere" parser would accept.
    """
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        return False
    em = pow(sig_int, e, n).to_bytes(k, "big")
    t = _SHA256_ASN1 + hashlib.sha256(signing_input).digest()
    ps_len = k - len(t) - 3
    if ps_len < 8:  # PKCS#1 requires at least 8 padding bytes
        return False
    expected = b"\x00\x01" + b"\xff" * ps_len + b"\x00" + t
    return hmac.compare_digest(em, expected)


# --------------------------------------------------------------------- JWKS cache

# {jwks_uri: (keys, expiry_epoch)}. Entra rotates signing keys; a short TTL keeps a fresh set
# without a network round-trip on every request.
_JWKS_CACHE: dict[str, tuple[list, float]] = {}
_JWKS_TTL = 3600.0


def _fetch_jwks(jwks_uri: str) -> list:
    # The JWKS is the root of trust for every token decision, so it is fetched over https ONLY.
    # `urllib.request.urlopen` would otherwise happily open file://, http://, ftp://, ... -- a
    # misconfiguration or a future change feeding it less-trusted input could then turn this into
    # a local-file read or a plaintext/SSRF hop. https-only closes that off.
    if urlsplit(jwks_uri).scheme != "https":
        raise TokenError(f"JWKS URI must be https, got {jwks_uri!r}")
    hit = _JWKS_CACHE.get(jwks_uri)
    if hit and time.time() < hit[1]:
        return hit[0]
    with urllib.request.urlopen(jwks_uri, timeout=10) as resp:
        keys = json.load(resp).get("keys", [])
    _JWKS_CACHE[jwks_uri] = (keys, time.time() + _JWKS_TTL)
    return keys


def reset_jwks_cache() -> None:
    _JWKS_CACHE.clear()


# ------------------------------------------------------------------ JWT validation


def _verify_jwt(token: str, keys: list, *, audience: str, issuer: str,
                leeway: int = 60, now: int | None = None) -> dict:
    """Verify an Entra RS256 JWT and return its claims, or raise TokenError.

    Checks: three segments, alg=RS256, a JWKS key matching `kid`, a valid signature over
    the signing input, and `exp`/`nbf`/`aud`/`iss`. `leeway` tolerates minor clock skew.
    """
    now = now if now is not None else int(time.time())
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("malformed token")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except (ValueError, json.JSONDecodeError) as e:
        raise TokenError(f"undecodable token: {e}") from e

    if header.get("alg") != "RS256":  # never accept alg=none or a symmetric alg
        raise TokenError(f"unsupported alg {header.get('alg')!r}")
    kid = header.get("kid")
    jwk = next((k for k in keys if k.get("kid") == kid and k.get("kty") == "RSA"), None)
    if jwk is None:
        raise TokenError("no matching signing key (kid)")
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    if not _rs256_verify(signing_input, signature, _b64url_to_int(jwk["n"]), _b64url_to_int(jwk["e"])):
        raise TokenError("bad signature")

    if int(payload.get("exp", 0)) <= now - leeway:
        raise TokenError("token expired")
    if int(payload.get("nbf", 0)) > now + leeway:
        raise TokenError("token not yet valid")
    aud = payload.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if audience not in auds:
        raise TokenError("audience mismatch")
    if issuer and payload.get("iss") != issuer:
        raise TokenError("issuer mismatch")
    return payload


# ---------------------------------------------------------------------- config/API


@dataclass(frozen=True)
class AuthzConfig:
    """Where and how to validate caller tokens. Built from env; None when unconfigured."""

    audience: str      # the app registration the id_token is FOR (oauth2-proxy's client id)
    issuer: str        # https://login.microsoftonline.com/{tenant}/v2.0
    jwks_uri: str
    roles_claim: str = "roles"

    @classmethod
    def from_env(cls) -> "AuthzConfig | None":
        audience = (os.environ.get("DRIFTSCOPE_OIDC_AUDIENCE") or "").strip()
        if not audience:
            return None  # authorization disabled -> caller checks are skipped entirely
        tenant = (os.environ.get("DRIFTSCOPE_OIDC_TENANT")
                  or os.environ.get("AZURE_TENANT_ID") or "").strip()
        authority = (os.environ.get("DRIFTSCOPE_OIDC_AUTHORITY")
                     or "https://login.microsoftonline.com").rstrip("/")
        issuer = (os.environ.get("DRIFTSCOPE_OIDC_ISSUER") or f"{authority}/{tenant}/v2.0")
        jwks_uri = (os.environ.get("DRIFTSCOPE_OIDC_JWKS_URI")
                    or f"{authority}/{tenant}/discovery/v2.0/keys")
        roles_claim = os.environ.get("DRIFTSCOPE_OIDC_ROLES_CLAIM") or "roles"
        return cls(audience=audience, issuer=issuer, jwks_uri=jwks_uri, roles_claim=roles_claim)


@dataclass(frozen=True)
class Caller:
    """The authenticated caller: the app-role values they hold, and whether auth succeeded.

    `ok=False` (with `reason`) means a token was required but absent/invalid -> the caller is
    unauthenticated; `resolve()` turns that into a 401 only when the target identity actually
    requires a role, so an unauthenticated probe of an unrestricted identity is unaffected.
    """

    roles: frozenset[str]
    ok: bool
    reason: str | None = None


def _bearer(headers) -> str | None:
    """Extract the Bearer token oauth2-proxy forwards (`--set-authorization-header=true`)."""
    raw = headers.get("Authorization") or headers.get("authorization") or ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def authenticate(headers, config: "AuthzConfig | None" = None) -> "Caller | None":
    """Authenticate the caller from request `headers`.

    Returns None when authorization is disabled (no DRIFTSCOPE_OIDC_AUDIENCE) -- the single-
    tenant default, where the oauth2-proxy access-group gate is the only control. Otherwise a
    `Caller`: verified roles on success, or `ok=False` with a reason when the token is
    missing/invalid. `headers` is anything with a `.get(name)` (the handler's headers, or a dict).
    """
    config = config if config is not None else AuthzConfig.from_env()
    if config is None:
        return None
    token = _bearer(headers)
    if not token:
        return Caller(frozenset(), False, "no bearer token")
    try:
        keys = _fetch_jwks(config.jwks_uri)
        payload = _verify_jwt(token, keys, audience=config.audience, issuer=config.issuer)
    except TokenError as e:
        return Caller(frozenset(), False, str(e))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return Caller(frozenset(), False, f"token validation unavailable: {e}")
    raw_roles = payload.get(config.roles_claim) or []
    roles = frozenset(raw_roles) if isinstance(raw_roles, list) else frozenset()
    return Caller(roles, True)

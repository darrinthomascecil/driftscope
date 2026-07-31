"""Caller token validation. Uses a real RSA key (generated via openssl) to sign test JWTs,
so the pure-stdlib RS256 verifier is exercised against genuine signatures -- no crypto
dependency, no network (JWKS is injected).
"""

from __future__ import annotations

import base64
import json
import subprocess
import time

import pytest

from driftscope import authz


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def rsa_key(tmp_path_factory):
    """An RSA keypair + its JWKS (kid 'test'), generated once for the module."""
    d = tmp_path_factory.mktemp("rsa")
    key = d / "key.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True,
                   capture_output=True)
    # Modulus (n) as hex; e is openssl's default 65537 == AQAB.
    mod = subprocess.run(["openssl", "rsa", "-in", str(key), "-noout", "-modulus"],
                         check=True, capture_output=True, text=True).stdout.strip()
    n_hex = mod.split("=", 1)[1]
    jwk = {"kty": "RSA", "kid": "test", "n": _b64url(bytes.fromhex(n_hex)),
           "e": _b64url((65537).to_bytes(3, "big"))}
    return {"key": key, "jwks": [jwk]}


def _sign(key_path, header: dict, payload: dict) -> str:
    seg = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key_path)],
                         input=seg.encode(), check=True, capture_output=True).stdout
    return f"{seg}.{_b64url(sig)}"


def _claims(**over):
    now = int(time.time())
    c = {"aud": "app-123", "iss": "https://issuer/v2.0", "exp": now + 600, "nbf": now - 60,
         "roles": ["tenant.acme"]}
    c.update(over)
    return c


AUD, ISS = "app-123", "https://issuer/v2.0"


def test_valid_token_verifies_and_yields_roles(rsa_key):
    tok = _sign(rsa_key["key"], {"alg": "RS256", "kid": "test"}, _claims())
    payload = authz._verify_jwt(tok, rsa_key["jwks"], audience=AUD, issuer=ISS)
    assert payload["roles"] == ["tenant.acme"]


def test_tampered_payload_fails_signature(rsa_key):
    tok = _sign(rsa_key["key"], {"alg": "RS256", "kid": "test"}, _claims())
    head, payload, sig = tok.split(".")
    forged = base64.urlsafe_b64encode(
        json.dumps(_claims(roles=["operator"])).encode()).rstrip(b"=").decode()
    with pytest.raises(authz.TokenError):
        authz._verify_jwt(f"{head}.{forged}.{sig}", rsa_key["jwks"], audience=AUD, issuer=ISS)


def test_expired_token_rejected(rsa_key):
    tok = _sign(rsa_key["key"], {"alg": "RS256", "kid": "test"}, _claims(exp=int(time.time()) - 3600))
    with pytest.raises(authz.TokenError, match="expired"):
        authz._verify_jwt(tok, rsa_key["jwks"], audience=AUD, issuer=ISS)


def test_wrong_audience_rejected(rsa_key):
    tok = _sign(rsa_key["key"], {"alg": "RS256", "kid": "test"}, _claims(aud="someone-else"))
    with pytest.raises(authz.TokenError, match="audience"):
        authz._verify_jwt(tok, rsa_key["jwks"], audience=AUD, issuer=ISS)


def test_wrong_issuer_rejected(rsa_key):
    tok = _sign(rsa_key["key"], {"alg": "RS256", "kid": "test"}, _claims(iss="https://evil/v2.0"))
    with pytest.raises(authz.TokenError, match="issuer"):
        authz._verify_jwt(tok, rsa_key["jwks"], audience=AUD, issuer=ISS)


def test_alg_none_rejected(rsa_key):
    # alg=none with an empty signature must never be accepted.
    seg = f"{_b64url(json.dumps({'alg': 'none', 'kid': 'test'}).encode())}.{_b64url(json.dumps(_claims()).encode())}"
    with pytest.raises(authz.TokenError, match="alg"):
        authz._verify_jwt(f"{seg}.", rsa_key["jwks"], audience=AUD, issuer=ISS)


def test_unknown_kid_rejected(rsa_key):
    tok = _sign(rsa_key["key"], {"alg": "RS256", "kid": "other"}, _claims())
    with pytest.raises(authz.TokenError, match="kid"):
        authz._verify_jwt(tok, rsa_key["jwks"], audience=AUD, issuer=ISS)


# ---- authenticate() end to end (JWKS injected, no network) ----

@pytest.fixture(autouse=True)
def _clear_jwks():
    authz.reset_jwks_cache()


def _cfg():
    return authz.AuthzConfig(audience=AUD, issuer=ISS, jwks_uri="https://jwks")


def test_authenticate_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("DRIFTSCOPE_OIDC_AUDIENCE", raising=False)
    assert authz.authenticate({"Authorization": "Bearer x"}) is None  # authz off


def test_authenticate_valid(monkeypatch, rsa_key):
    monkeypatch.setattr(authz, "_fetch_jwks", lambda uri: rsa_key["jwks"])
    tok = _sign(rsa_key["key"], {"alg": "RS256", "kid": "test"}, _claims(roles=["tenant.acme", "x"]))
    caller = authz.authenticate({"Authorization": f"Bearer {tok}"}, _cfg())
    assert caller.ok and caller.roles == frozenset({"tenant.acme", "x"})


def test_authenticate_missing_token(rsa_key):
    caller = authz.authenticate({}, _cfg())
    assert caller.ok is False and "no bearer" in caller.reason


def test_authenticate_bad_token(monkeypatch, rsa_key):
    monkeypatch.setattr(authz, "_fetch_jwks", lambda uri: rsa_key["jwks"])
    caller = authz.authenticate({"Authorization": "Bearer not.a.jwt"}, _cfg())
    assert caller.ok is False and caller.reason


def test_config_from_env(monkeypatch):
    monkeypatch.delenv("DRIFTSCOPE_OIDC_AUDIENCE", raising=False)
    assert authz.AuthzConfig.from_env() is None
    monkeypatch.setenv("DRIFTSCOPE_OIDC_AUDIENCE", "app-xyz")
    monkeypatch.setenv("DRIFTSCOPE_OIDC_TENANT", "tid-1")
    cfg = authz.AuthzConfig.from_env()
    assert cfg.audience == "app-xyz"
    assert cfg.issuer == "https://login.microsoftonline.com/tid-1/v2.0"
    assert cfg.jwks_uri == "https://login.microsoftonline.com/tid-1/discovery/v2.0/keys"

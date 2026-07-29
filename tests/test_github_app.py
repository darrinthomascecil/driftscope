"""GitHub App installation-token flow. No network, no openssl -- both are mocked."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

import driftscope.github_app as ga
from driftscope import wi as wi_mod


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _future_iso(minutes=55):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def clear_state(monkeypatch):
    ga._CACHE.clear()
    for v in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_KEY_VAULT_URI",
              "GITHUB_APP_KEY_NAME", "GITHUB_APP_KEY_VERSION", "GITHUB_APP_PRIVATE_KEY",
              "GITHUB_APP_PRIVATE_KEY_FILE"):
        monkeypatch.delenv(v, raising=False)


def test_returns_none_when_app_not_configured():
    assert ga.installation_token() is None


def test_local_sign_mints_a_token_and_caches_it(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "999")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN KEY-----x-----END KEY-----")
    monkeypatch.setattr(ga, "_sign", lambda si: "c2ln")  # don't actually run openssl

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        assert "app/installations/999/access_tokens" in req.full_url
        auth = req.headers["Authorization"]
        assert auth.startswith("Bearer ") and auth.count(".") == 2   # a JWT bearer
        return _Resp({"token": "ghs_installtoken", "expires_at": _future_iso()})

    monkeypatch.setattr(ga.urllib.request, "urlopen", fake_urlopen)

    assert ga.installation_token() == "ghs_installtoken"
    # second call is served from cache -- no second mint
    assert ga.installation_token() == "ghs_installtoken"
    assert calls["n"] == 1


def test_key_vault_sign_flow_uses_workload_identity_and_signs_remotely(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "999")
    monkeypatch.setenv("GITHUB_APP_KEY_VAULT_URI", "https://kv.vault.azure.net")
    monkeypatch.setenv("GITHUB_APP_KEY_NAME", "gh-app-key")
    # the Key Vault token is obtained via the workload-identity exchange
    monkeypatch.setattr(wi_mod, "federated_token_for",
                        lambda scope: "kvtoken" if "vault.azure.net" in scope else None)

    seen = {}

    def fake_urlopen(req, timeout=None):
        u = req.full_url
        if "vault.azure.net" in u and u.rstrip("?").endswith("/sign") or "/sign?" in u:
            seen["kv_body"] = json.loads(req.data.decode())
            seen["kv_auth"] = req.headers["Authorization"]
            return _Resp({"kid": "https://kv/keys/gh-app-key/1", "value": "c2lnbmVk"})
        if "access_tokens" in u:
            seen["gh_auth"] = req.headers["Authorization"]
            return _Resp({"token": "ghs_kv", "expires_at": _future_iso()})
        raise AssertionError(f"unexpected url {u}")

    monkeypatch.setattr(ga.urllib.request, "urlopen", fake_urlopen)

    assert ga.installation_token() == "ghs_kv"
    assert seen["kv_body"]["alg"] == "RS256"          # signed the SHA-256 digest, RS256
    assert seen["kv_auth"] == "Bearer kvtoken"        # reached KV via workload identity
    # the JWT's signature segment IS Key Vault's returned value -- the key never left
    assert seen["gh_auth"].endswith(".c2lnbmVk")


def test_app_jwt_claims_are_short_lived(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "777")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "1")
    monkeypatch.setattr(ga, "_sign", lambda si: "sig")

    jwt = ga._app_jwt()
    seg = jwt.split(".")[1]
    seg += "=" * (-len(seg) % 4)
    claims = json.loads(base64.urlsafe_b64decode(seg))
    assert claims["iss"] == "777"
    assert 0 < claims["exp"] - claims["iat"] <= 600   # GitHub caps the App JWT at 10 min


def test_unsignable_key_yields_no_token(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "2")
    # no key source configured -> _sign returns None -> no token, no crash
    assert ga.installation_token() is None

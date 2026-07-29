"""Azure Workload Identity token exchange. No network -- urlopen is mocked."""

from __future__ import annotations

import json

import pytest

from driftscope import wi


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for v in ("AZURE_FEDERATED_TOKEN_FILE", "AZURE_CLIENT_ID", "AZURE_TENANT_ID",
              "AZURE_AUTHORITY_HOST"):
        monkeypatch.delenv(v, raising=False)


def test_returns_none_when_not_federated():
    assert wi.federated_token_for("x/.default") is None  # no AZURE_* set


def test_exchanges_the_federated_token_for_the_scope(monkeypatch, tmp_path):
    f = tmp_path / "tok"
    f.write_text("eyJ.federated.assertion")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(f))
    monkeypatch.setenv("AZURE_CLIENT_ID", "the-client")
    monkeypatch.setenv("AZURE_TENANT_ID", "the-tenant")

    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode()
        return _Resp({"access_token": "AAD.token"})

    monkeypatch.setattr(wi.urllib.request, "urlopen", fake)

    tok = wi.federated_token_for("https://vault.azure.net/.default")
    assert tok == "AAD.token"
    assert "the-tenant/oauth2/v2.0/token" in seen["url"]
    assert "vault.azure.net%2F.default" in seen["body"]        # the scope, urlencoded
    assert "client_assertion=eyJ.federated.assertion" in seen["body"]


def test_missing_token_file_returns_none(monkeypatch):
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/no/such/token")
    monkeypatch.setenv("AZURE_CLIENT_ID", "c")
    monkeypatch.setenv("AZURE_TENANT_ID", "t")
    assert wi.federated_token_for("x/.default") is None


def test_authority_host_follows_the_pod_to_sovereign_clouds(monkeypatch, tmp_path):
    f = tmp_path / "tok"
    f.write_text("assertion")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(f))
    monkeypatch.setenv("AZURE_CLIENT_ID", "c")
    monkeypatch.setenv("AZURE_TENANT_ID", "t")
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.us/")

    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp({"access_token": "x"})

    monkeypatch.setattr(wi.urllib.request, "urlopen", fake)
    wi.federated_token_for("s")
    assert seen["url"].startswith("https://login.microsoftonline.us/t/oauth2")

"""Bring-your-own forge identities: registry loading, resolution, and per-identity
token isolation across GitHub Apps and ADO Entra apps. No network -- Key Vault, the
Entra token endpoint, and the GitHub API are all mocked.
"""

from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

import driftscope.github_app as ga
import driftscope.source as src
from driftscope import cli, server
from driftscope import wi as wi_mod
from driftscope.forge.identity import (
    ADO_RESOURCE,
    AdoEntraAuth,
    ForgeIdentity,
    GitHubAppAuth,
    IdentityForbidden,
    IdentityMatch,
    IdentityRegistry,
    KeyVaultKeyRef,
    KeyVaultSecretRef,
    RegistryError,
    UnknownIdentity,
    env_default_identity,
    target_selector,
)
from driftscope.source import AdoCredential, Source

FIXTURE = Path(__file__).parent.parent / "fixtures" / "stale-estate"
HERMETIC = {"offline": True, "no_az": True, "no_github": True}

SAMPLE = {
    "version": 1,
    "identities": [
        {"name": "acme-github", "forge": "github",
         "match": {"hosts": ["github.com"], "owners": ["acme-corp", "acme-labs"]},
         "github": {"app_id": "123", "installation_id": "789",
                    "signing_key": {"vault_uri": "https://acme-kv.vault.azure.net",
                                    "key_name": "acme-key"}}},
        {"name": "globex-ado", "forge": "ado",
         "match": {"hosts": ["dev.azure.com", "globex.visualstudio.com"], "owners": ["globex"]},
         "ado": {"client_id": "gcid", "tenant_id": "gtid",
                 "credential": {"kind": "client_secret",
                                "secret": {"vault_uri": "https://g-kv.vault.azure.net",
                                           "secret_name": "globex-secret"}}}},
        {"name": "initech-ado", "forge": "ado",
         "match": {"hosts": ["dev.azure.com"], "owners": ["initech"]},
         "ado": {"client_id": "icid", "tenant_id": "itid",
                 "credential": {"kind": "federated"}}},
    ],
}


def _write(tmp_path, doc) -> str:
    p = tmp_path / "forge-auth.json"
    p.write_text(json.dumps(doc))
    return str(p)


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
def _clear_state(monkeypatch):
    ga._CACHE.clear()
    src._ADO_CRED_CACHE.clear()
    src._ADO_CRED = False
    for v in ("DRIFTSCOPE_FORGE_AUTH", "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID",
              "GITHUB_APP_KEY_VAULT_URI", "GITHUB_APP_KEY_NAME", "GITHUB_APP_KEY_VERSION",
              "GITHUB_TOKEN"):
        monkeypatch.delenv(v, raising=False)


# --------------------------------------------------------------------- loader

def test_loads_a_valid_registry(tmp_path):
    reg = IdentityRegistry.load(_write(tmp_path, SAMPLE))
    assert reg.get("acme-github").github.app_id == "123"
    assert reg.get("acme-github").github.signing_key.key_name == "acme-key"
    assert reg.get("globex-ado").ado.secret.secret_name == "globex-secret"
    assert reg.get("initech-ado").ado.federated is True
    assert reg.get("initech-ado").ado.resource == ADO_RESOURCE


def test_missing_file_yields_no_config_identities():
    reg = IdentityRegistry.load(None)  # no path, no env
    assert reg.resolve("github.com", "anyone", forge="github") is None


def test_env_var_path_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIFTSCOPE_FORGE_AUTH", _write(tmp_path, SAMPLE))
    reg = IdentityRegistry.load()
    assert reg.get("acme-github") is not None


def test_duplicate_name_is_rejected(tmp_path):
    doc = {"version": 1, "identities": [SAMPLE["identities"][0], SAMPLE["identities"][0]]}
    with pytest.raises(RegistryError):
        IdentityRegistry.load(_write(tmp_path, doc))


def test_unknown_version_is_rejected(tmp_path):
    with pytest.raises(RegistryError):
        IdentityRegistry.load(_write(tmp_path, {"version": 2, "identities": []}))


def test_bad_ado_credential_kind_is_rejected(tmp_path):
    doc = {"version": 1, "identities": [
        {"name": "x", "forge": "ado", "match": {},
         "ado": {"client_id": "c", "tenant_id": "t", "credential": {"kind": "bogus"}}}]}
    with pytest.raises(RegistryError):
        IdentityRegistry.load(_write(tmp_path, doc))


def test_malformed_json_is_rejected(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(RegistryError):
        IdentityRegistry.load(str(p))


# ------------------------------------------------------------------- resolver

def test_resolves_by_host_and_owner(tmp_path):
    reg = IdentityRegistry.load(_write(tmp_path, SAMPLE))
    assert reg.resolve("github.com", "acme-corp", forge="github").name == "acme-github"
    assert reg.resolve("github.com", "acme-labs", forge="github").name == "acme-github"
    assert reg.resolve("dev.azure.com", "globex", forge="ado").name == "globex-ado"
    assert reg.resolve("dev.azure.com", "initech", forge="ado").name == "initech-ado"


def test_legacy_visualstudio_host_matches(tmp_path):
    reg = IdentityRegistry.load(_write(tmp_path, SAMPLE))
    assert reg.resolve("globex.visualstudio.com", "globex", forge="ado").name == "globex-ado"


def test_unmatched_owner_returns_none(tmp_path):
    reg = IdentityRegistry.load(_write(tmp_path, SAMPLE))
    assert reg.resolve("github.com", "stranger", forge="github") is None


def test_unknown_name_raises(tmp_path):
    reg = IdentityRegistry.load(_write(tmp_path, SAMPLE))
    with pytest.raises(UnknownIdentity):
        reg.resolve("github.com", "acme-corp", name="does-not-exist")


def test_name_outside_its_match_is_forbidden(tmp_path):
    reg = IdentityRegistry.load(_write(tmp_path, SAMPLE))
    with pytest.raises(IdentityForbidden):
        reg.resolve("github.com", "stranger", name="acme-github")


# --------------------------------------------------------- env default & selector

def test_env_default_identity_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "999")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "111")
    d = env_default_identity()
    assert d.name == "default" and d.source == "env-default"
    assert d.github.app_id == "999" and d.github.signing_key is None  # PEM path


def test_env_default_absent_without_app_env():
    assert env_default_identity() is None


def test_explicit_default_name_resolves_env_default(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "999")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "111")
    reg = IdentityRegistry.load(None)
    assert reg.resolve("github.com", "whoever", name="default").github.app_id == "999"


def test_env_default_not_auto_selected_on_implicit_path(monkeypatch):
    # An unconfigured target returns None, so github_token's own precedence applies.
    monkeypatch.setenv("GITHUB_APP_ID", "999")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "111")
    reg = IdentityRegistry.load(None)
    assert reg.resolve("github.com", "whoever", forge="github") is None


def test_target_selector_github_and_ado():
    assert target_selector("https://github.com/acme/repo") == ("github.com", "acme", "github")
    assert target_selector("https://dev.azure.com/globex/proj/_git/repo") == (
        "dev.azure.com", "globex", "ado")


# ------------------------------------------------- cache-key isolation invariant

def test_github_cache_keys_distinct_for_two_apps_on_same_host():
    a = GitHubAppAuth("1", "10", KeyVaultKeyRef("https://kv", "ka"))
    b = GitHubAppAuth("2", "20", KeyVaultKeyRef("https://kv", "kb"))
    assert a.cache_key() != b.cache_key()


def test_ado_cache_keys_distinct_for_two_apps():
    a = AdoEntraAuth("c1", "t1", federated=True)
    b = AdoEntraAuth("c2", "t2", secret=KeyVaultSecretRef("https://kv", "s"))
    assert a.cache_key() != b.cache_key()


# ------------------------------------------ GitHub App per-identity token isolation

def test_two_github_identities_get_isolated_tokens(monkeypatch):
    monkeypatch.setattr(wi_mod, "federated_token_for", lambda scope, **k: "kvtok")

    def fake_urlopen(req, timeout=None):
        u = req.full_url
        if "/sign" in u:
            return _Resp({"value": "sig"})
        if "installations/10/" in u:
            return _Resp({"token": "tokA", "expires_at": "2999-01-01T00:00:00Z"})
        if "installations/20/" in u:
            return _Resp({"token": "tokB", "expires_at": "2999-01-01T00:00:00Z"})
        raise AssertionError(f"unexpected url {u}")

    monkeypatch.setattr(ga.urllib.request, "urlopen", fake_urlopen)
    A = GitHubAppAuth("1", "10", KeyVaultKeyRef("https://kv", "ka"))
    B = GitHubAppAuth("2", "20", KeyVaultKeyRef("https://kv", "kb"))

    assert ga.installation_token(A) == "tokA"
    assert ga.installation_token(B) == "tokB"
    # distinct cache entries -> tenant A never receives tenant B's token
    assert ga._CACHE[A.cache_key()]["token"] == "tokA"
    assert ga._CACHE[B.cache_key()]["token"] == "tokB"
    assert A.cache_key() != B.cache_key()


# ---------------------------------------------------- ADO Entra app minting

def test_ado_client_secret_mint(monkeypatch):
    monkeypatch.setattr("driftscope.wi.get_key_vault_secret", lambda *a, **k: "s3cr3t")

    def fake_urlopen(req, timeout=None):
        assert "gtid/oauth2/v2.0/token" in req.full_url
        body = req.data.decode()
        assert "grant_type=client_credentials" in body and "s3cr3t" in body
        assert f"{ADO_RESOURCE}%2F.default" in body or f"{ADO_RESOURCE}/.default" in body
        return _Resp({"access_token": "eyJado", "expires_in": 3600})

    monkeypatch.setattr(src.urllib.request, "urlopen", fake_urlopen)
    ident = ForgeIdentity("globex-ado", "ado", IdentityMatch(),
                          ado=AdoEntraAuth("gcid", "gtid",
                                           secret=KeyVaultSecretRef("https://kv", "s")))
    cred = src.ado_credential(ident)
    assert cred.scheme == "Bearer" and cred.origin == "entra-client-secret"
    assert cred.token == "eyJado"
    assert ident.cache_key() in src._ADO_CRED_CACHE  # cached per identity


def test_ado_federated_mint_passes_tenant_client_id(monkeypatch):
    seen = {}

    def fake_fed(scope, *, client_id=None, tenant=None):
        seen.update(scope=scope, client_id=client_id, tenant=tenant)
        return "eyJfed"

    monkeypatch.setattr("driftscope.wi.federated_token_for", fake_fed)
    ident = ForgeIdentity("initech-ado", "ado", IdentityMatch(),
                          ado=AdoEntraAuth("icid", "itid", federated=True))
    cred = src.ado_credential(ident)
    assert cred.origin == "entra-federated" and cred.token == "eyJfed"
    assert seen["client_id"] == "icid" and seen["tenant"] == "itid"
    assert seen["scope"] == f"{ADO_RESOURCE}/.default"


def test_reset_credential_cache_evicts_only_that_identity():
    a = ForgeIdentity("a", "ado", IdentityMatch(), ado=AdoEntraAuth("c1", "t1", federated=True))
    b = ForgeIdentity("b", "ado", IdentityMatch(), ado=AdoEntraAuth("c2", "t2", federated=True))
    src._ADO_CRED_CACHE[a.cache_key()] = (AdoCredential("x", "Bearer", "e"), 9e18)
    src._ADO_CRED_CACHE[b.cache_key()] = (AdoCredential("y", "Bearer", "e"), 9e18)
    src.reset_credential_cache(a)
    assert a.cache_key() not in src._ADO_CRED_CACHE
    assert b.cache_key() in src._ADO_CRED_CACHE  # the other tenant is untouched


# ------------------------------------- clone auth-env selection per identity

def test_auth_env_uses_the_identity_and_never_leaks_github_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "operator-pat")  # must NOT reach a tenant clone
    monkeypatch.setattr("driftscope.github_app.installation_token",
                        lambda auth=None: "ghs_tenant" if auth is not None else None)
    ident = ForgeIdentity("acme", "github",
                          IdentityMatch(hosts=("github.com",), owners=("acme",)),
                          github=GitHubAppAuth("1", "10", KeyVaultKeyRef("https://kv", "ka")))
    env = src._auth_env_for("https://github.com/acme/repo", ident)
    expected = base64.b64encode(b"x-access-token:ghs_tenant").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected}"
    assert "operator-pat" not in env["GIT_CONFIG_VALUE_0"]


def test_auth_env_is_empty_when_tenant_mint_fails(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "operator-pat")
    monkeypatch.setattr("driftscope.github_app.installation_token", lambda auth=None: None)
    ident = ForgeIdentity("acme", "github", IdentityMatch(),
                          github=GitHubAppAuth("1", "10", KeyVaultKeyRef("https://kv", "ka")))
    # No token from the tenant app, and NO fall back to GITHUB_TOKEN -> no auth header.
    assert src._auth_env_for("https://github.com/acme/repo", ident) == {}


def test_auth_env_uses_entra_bearer_for_ado(monkeypatch):
    monkeypatch.setattr(src, "ado_credential",
                        lambda identity=None: AdoCredential("eyJado", "Bearer", "entra-federated"))
    ident = ForgeIdentity("globex", "ado", IdentityMatch(),
                          ado=AdoEntraAuth("c", "t", federated=True))
    env = src._auth_env_for("https://dev.azure.com/globex/proj/_git/repo", ident)
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer eyJado"


# ------------------------------------------------ wi.py parameterization

def test_federated_token_for_kwargs_override_env(monkeypatch, tmp_path):
    tf = tmp_path / "tok"
    tf.write_text("assertion-jwt")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(tf))
    monkeypatch.setenv("AZURE_CLIENT_ID", "env-cid")
    monkeypatch.setenv("AZURE_TENANT_ID", "env-tid")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode()
        return _Resp({"access_token": "tok"})

    monkeypatch.setattr(wi_mod.urllib.request, "urlopen", fake_urlopen)
    assert wi_mod.federated_token_for("s/.default", client_id="arg-cid", tenant="arg-tid") == "tok"
    assert "arg-tid/oauth2/v2.0/token" in seen["url"]  # tenant from the arg, not env
    assert "client_id=arg-cid" in seen["body"]


def test_get_key_vault_secret(monkeypatch):
    monkeypatch.setattr(wi_mod, "federated_token_for", lambda scope, **k: "kvtok")

    def fake_urlopen(req, timeout=None):
        assert req.full_url.startswith("https://kv.vault.azure.net/secrets/mysecret")
        assert req.headers["Authorization"] == "Bearer kvtok"
        return _Resp({"value": "the-secret"})

    monkeypatch.setattr(wi_mod.urllib.request, "urlopen", fake_urlopen)
    assert wi_mod.get_key_vault_secret("https://kv.vault.azure.net", "mysecret") == "the-secret"


# --------------------------------------------------- server & CLI edges (E2E)

def test_server_resolves_identity_into_the_scan(monkeypatch, tmp_path):
    reg = IdentityRegistry.load(_write(tmp_path, SAMPLE))
    captured = {}

    @contextmanager
    def fake_open(target, *, ref=None, keep=False, ado_org=None, identity=None):
        captured["identity"] = identity
        yield Source(display=target, path=FIXTURE)

    monkeypatch.setattr(server, "open_source", fake_open)
    status, report, err = server.produce_report(
        {"repo": "https://github.com/acme-corp/app", **HERMETIC}, registry=reg)
    assert status == 200 and report is not None
    assert captured["identity"].name == "acme-github"


def test_server_unknown_identity_is_400(tmp_path):
    reg = IdentityRegistry.load(_write(tmp_path, SAMPLE))
    status, _, err = server.produce_report(
        {"repo": "https://github.com/acme-corp/app", "identity": "nope"}, registry=reg)
    assert status == 400 and "unknown identity" in err


def test_server_forbidden_identity_is_403(tmp_path):
    reg = IdentityRegistry.load(_write(tmp_path, SAMPLE))
    status, _, err = server.produce_report(
        {"repo": "https://github.com/stranger/app", "identity": "acme-github"}, registry=reg)
    assert status == 403


def test_cli_scan_threads_identity(monkeypatch, tmp_path):
    reg_path = _write(tmp_path, SAMPLE)
    captured = {}

    @contextmanager
    def fake_open(target, *, ref=None, keep=False, ado_org=None, identity=None):
        captured["open_identity"] = identity
        yield Source(display=target, path=FIXTURE)

    monkeypatch.setattr(cli, "open_source", fake_open)
    monkeypatch.setattr(
        cli, "_scan_source",
        lambda args, src_, identity=None: captured.update(scan_identity=identity) or 0)

    args = cli.build_parser().parse_args(
        ["scan", "https://github.com/acme-corp/app", "--forge-auth", reg_path,
         "--identity", "acme-github"])
    assert cli.cmd_scan(args) == 0
    assert captured["open_identity"].name == "acme-github"
    assert captured["scan_identity"].name == "acme-github"

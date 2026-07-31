"""Caller authorization: app-role gating in resolve() and the server's 401/403 mapping.
Uses a constructed `authz.Caller` (no token — token verification is covered in test_authz).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from driftscope import server
from driftscope.authz import Caller
from driftscope.forge.identity import (
    ForgeIdentity,
    GitHubAppAuth,
    IdentityForbidden,
    IdentityMatch,
    IdentityRegistry,
    KeyVaultKeyRef,
    RegistryError,
    Unauthenticated,
)
from driftscope.source import Source

FIXTURE = Path(__file__).parent.parent / "fixtures" / "stale-estate"
HERMETIC = {"offline": True, "no_az": True, "no_github": True}


def _gh_identity(name="acme", owners=("acme-corp",), principals=()):
    return ForgeIdentity(
        name=name, forge="github",
        match=IdentityMatch(hosts=("github.com",), owners=owners),
        github=GitHubAppAuth("1", "10", KeyVaultKeyRef("https://kv", "k")),
        principals=tuple(principals),
    )


def _registry(identities, strict=False):
    return IdentityRegistry(identities, default=None, require_authorization=strict)


# --------------------------------------------------------------- resolve() authz

def test_authorized_caller_resolves_the_identity():
    reg = _registry([_gh_identity(principals=("tenant.acme",))])
    caller = Caller(frozenset({"tenant.acme"}), ok=True)
    assert reg.resolve("github.com", "acme-corp", forge="github", caller=caller).name == "acme"


def test_caller_missing_the_role_is_forbidden():
    reg = _registry([_gh_identity(principals=("tenant.acme",))])
    caller = Caller(frozenset({"tenant.other"}), ok=True)
    with pytest.raises(IdentityForbidden):
        reg.resolve("github.com", "acme-corp", forge="github", caller=caller)


def test_unauthenticated_caller_on_a_restricted_identity_is_401():
    reg = _registry([_gh_identity(principals=("tenant.acme",))])
    caller = Caller(frozenset(), ok=False, reason="no bearer token")
    with pytest.raises(Unauthenticated):
        reg.resolve("github.com", "acme-corp", forge="github", caller=caller)


def test_caller_none_disables_the_check():
    # authz disabled -> principals are ignored (single-tenant default).
    reg = _registry([_gh_identity(principals=("tenant.acme",))])
    assert reg.resolve("github.com", "acme-corp", forge="github", caller=None).name == "acme"


def test_identity_without_principals_is_unrestricted_when_lenient():
    reg = _registry([_gh_identity(principals=())])
    caller = Caller(frozenset(), ok=False)  # even an unauthenticated caller
    assert reg.resolve("github.com", "acme-corp", forge="github", caller=caller).name == "acme"


def test_auto_match_then_authorize_fails_closed():
    # No name given: auto-match finds acme by owner, then authorizes -> 403 without the role.
    reg = _registry([_gh_identity(principals=("tenant.acme",))])
    caller = Caller(frozenset({"nope"}), ok=True)
    with pytest.raises(IdentityForbidden):
        reg.resolve("github.com", "acme-corp", forge="github", caller=caller)


# ------------------------------------------------------------------- strict mode

def test_strict_mode_denies_principal_less_env_default():
    # A principal-less identity (e.g. the env default) is never usable under strict mode.
    default = ForgeIdentity(name="default", forge="github",
                            match=IdentityMatch(hosts=("github.com",)),
                            github=GitHubAppAuth("9", "99"), source="env-default")
    reg = IdentityRegistry([], default=default, require_authorization=True)
    caller = Caller(frozenset({"anything"}), ok=True)
    with pytest.raises(IdentityForbidden):
        reg.resolve("github.com", "whoever", name="default", caller=caller)


def test_load_rejects_principal_less_identity_in_strict_mode(tmp_path):
    import json
    doc = {"version": 1, "require_authorization": True, "identities": [
        {"name": "acme", "forge": "github", "match": {"owners": ["acme"]},
         "github": {"app_id": "1", "installation_id": "2",
                    "signing_key": {"vault_uri": "https://kv", "key_name": "k"}}}]}  # no principals
    p = tmp_path / "fa.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(RegistryError, match="principals"):
        IdentityRegistry.load(str(p))


def test_load_parses_principals(tmp_path):
    import json
    doc = {"version": 1, "require_authorization": True, "identities": [
        {"name": "acme", "forge": "github", "match": {"owners": ["acme"]},
         "principals": ["tenant.acme"],
         "github": {"app_id": "1", "installation_id": "2",
                    "signing_key": {"vault_uri": "https://kv", "key_name": "k"}}}]}
    p = tmp_path / "fa.json"
    p.write_text(json.dumps(doc))
    reg = IdentityRegistry.load(str(p))
    assert reg.require_authorization is True
    assert reg.get("acme").principals == ("tenant.acme",)


# ------------------------------------------------------- server 401/403/200 (E2E)

@pytest.fixture
def _fake_clone(monkeypatch):
    @contextmanager
    def fake_open(target, *, ref=None, keep=False, ado_org=None, identity=None):
        yield Source(display=target, path=FIXTURE)

    monkeypatch.setattr(server, "open_source", fake_open)


def _scan(reg, caller):
    return server.produce_report(
        {"repo": "https://github.com/acme-corp/app", **HERMETIC}, registry=reg, caller=caller)


def test_server_authorized_caller_gets_200(_fake_clone):
    reg = _registry([_gh_identity(principals=("tenant.acme",))])
    status, report, err = _scan(reg, Caller(frozenset({"tenant.acme"}), ok=True))
    assert status == 200 and report is not None


def test_server_unauthenticated_is_401(_fake_clone):
    reg = _registry([_gh_identity(principals=("tenant.acme",))])
    status, _, err = _scan(reg, Caller(frozenset(), ok=False, reason="no bearer token"))
    assert status == 401 and "authentication required" in err


def test_server_wrong_role_is_403(_fake_clone):
    reg = _registry([_gh_identity(principals=("tenant.acme",))])
    status, _, err = _scan(reg, Caller(frozenset({"tenant.globex"}), ok=True))
    assert status == 403


def test_server_authz_disabled_is_unchanged(_fake_clone):
    reg = _registry([_gh_identity(principals=("tenant.acme",))])
    status, report, err = _scan(reg, None)  # caller None -> authz off
    assert status == 200 and report is not None

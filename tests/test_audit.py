"""Audit logging: the emitter, and that produce_report records EVERY outcome (allow + deny)
with the right fields and no secrets."""

from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from driftscope import audit, server
from driftscope.authz import Caller
from driftscope.forge.identity import (
    ForgeIdentity,
    GitHubAppAuth,
    IdentityMatch,
    IdentityRegistry,
    KeyVaultKeyRef,
)
from driftscope.source import Source

FIXTURE = Path(__file__).parent.parent / "fixtures" / "stale-estate"
HERMETIC = {"offline": True, "no_az": True, "no_github": True}


@pytest.fixture
def sink():
    buf = io.StringIO()
    audit.set_sink(buf)
    yield buf
    audit.set_sink(sys.stdout)  # restore the default


def _events(buf: io.StringIO):
    return [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]


# ------------------------------------------------------------------- the emitter

def test_emit_writes_one_json_line(sink):
    audit.emit("scan", status=200, caller="oid-1", extra=None)
    (ev,) = _events(sink)
    assert ev["audit"] is True and ev["event"] == "scan" and ev["status"] == 200
    assert ev["caller"] == "oid-1"
    assert "ts" in ev and "extra" not in ev  # None fields dropped


def test_emit_never_raises(sink):
    class Boom:
        def write(self, *_):
            raise OSError("sink down")
        def flush(self):
            pass
    audit.set_sink(Boom())
    audit.emit("scan", status=200)  # must not raise into the request path
    audit.set_sink(sink)


def test_caller_fields():
    assert audit.caller_fields(None) == {"caller": "authz-disabled", "authenticated": None}
    ok = audit.caller_fields(Caller(frozenset({"tenant.acme"}), ok=True, subject="oid-42", tenant="t1"))
    assert ok == {"caller": "oid-42", "authenticated": True, "roles": ["tenant.acme"], "tenant": "t1"}
    anon = audit.caller_fields(Caller(frozenset(), ok=False, reason="no bearer token"))
    assert anon["caller"] == "anonymous" and anon["authenticated"] is False


# ------------------------------------------------------- end to end via produce_report

def _gh_identity(principals=("tenant.acme",)):
    return ForgeIdentity(
        name="acme-github", forge="github",
        match=IdentityMatch(hosts=("github.com",), owners=("acme-corp",)),
        github=GitHubAppAuth("1", "10", KeyVaultKeyRef("https://kv", "k")),
        principals=tuple(principals),
    )


@pytest.fixture
def fake_clone(monkeypatch):
    @contextmanager
    def fake(target, *, ref=None, keep=False, ado_org=None, identity=None):
        yield Source(display=target, path=FIXTURE)
    monkeypatch.setattr(server, "open_source", fake)


def _scan(reg, caller, request_id="req-1"):
    return server.produce_report(
        {"repo": "https://github.com/acme-corp/app", **HERMETIC},
        registry=reg, caller=caller, request_id=request_id)


def test_allowed_scan_is_audited(sink, fake_clone):
    reg = IdentityRegistry([_gh_identity()])
    status, report, _ = _scan(reg, Caller(frozenset({"tenant.acme"}), ok=True, subject="oid-acme"))
    assert status == 200
    (ev,) = _events(sink)
    assert ev["outcome"] == "allowed" and ev["status"] == 200
    assert ev["caller"] == "oid-acme" and ev["authenticated"] is True
    assert ev["identity"] == "acme-github" and ev["forge"] == "github"
    assert ev["repo"] == "https://github.com/acme-corp/app"
    assert ev["request_id"] == "req-1" and ev["findings"] >= 1


def test_forbidden_scan_is_audited_as_denied(sink, fake_clone):
    reg = IdentityRegistry([_gh_identity()])
    status, _, _ = _scan(reg, Caller(frozenset({"tenant.globex"}), ok=True, subject="oid-globex"))
    assert status == 403
    (ev,) = _events(sink)
    assert ev["outcome"] == "denied" and ev["status"] == 403
    assert ev["caller"] == "oid-globex" and "role" in ev["error"].lower()
    assert "identity" not in ev  # nothing resolved/ran
    assert "requested_identity" not in ev  # auto-match (no name requested) -> None -> dropped


def test_unauthenticated_scan_is_audited_as_anonymous(sink, fake_clone):
    reg = IdentityRegistry([_gh_identity()])
    status, _, _ = _scan(reg, Caller(frozenset(), ok=False, reason="no bearer token"))
    assert status == 401
    (ev,) = _events(sink)
    assert ev["outcome"] == "denied" and ev["status"] == 401 and ev["caller"] == "anonymous"


def test_audit_never_carries_a_token_or_secret(sink, fake_clone):
    # A token/secret must never reach the log even if a caller crafts a weird identity name.
    reg = IdentityRegistry([_gh_identity()])
    _scan(reg, Caller(frozenset({"tenant.acme"}), ok=True, subject="oid-acme"))
    raw = sink.getvalue().lower()
    for forbidden in ("bearer", "authorization", "client_secret", "private_key", "signing_key", "vault"):
        assert forbidden not in raw, f"audit leaked {forbidden!r}: {raw}"

"""Scan-target parsing: which forge a URL addresses, and what to call the report.

No network and no cloning -- these are all pure string functions.
"""

from __future__ import annotations

import base64
import json

import pytest

from pathlib import Path

from driftscope import source as src_mod
from driftscope import wi as wi_mod
from driftscope.source import (
    AdoCredential,
    AdoRef,
    _auth_env_for,
    looks_remote,
    normalize_remote,
    parse_ado_url,
    slug_for,
    strip_userinfo,
)

# The three shapes that turn up in the wild. The middle one is what the ADO
# "Clone" button copies to the clipboard, so it is the one users actually paste.
ADO_URLS = [
    ("https://dev.azure.com/contoso/Platform/_git/payments",
     AdoRef("contoso", "Platform", "payments")),
    ("https://contoso@dev.azure.com/contoso/Platform/_git/payments",
     AdoRef("contoso", "Platform", "payments")),
    ("https://contoso.visualstudio.com/Platform/_git/payments",
     AdoRef("contoso", "Platform", "payments")),
]


@pytest.mark.parametrize("url,expected", ADO_URLS)
def test_parse_ado_url_shapes(url, expected):
    assert parse_ado_url(url) == expected


def test_ado_url_tolerates_git_suffix_and_trailing_slash():
    ref = AdoRef("contoso", "Platform", "payments")
    assert parse_ado_url("https://dev.azure.com/contoso/Platform/_git/payments.git") == ref
    assert parse_ado_url("https://dev.azure.com/contoso/Platform/_git/payments/") == ref


def test_ado_url_without_project_falls_back_to_repo_name():
    """ADO permits omitting the project when it matches the repo name, and the web
    UI resolves it the same way."""
    assert parse_ado_url("https://dev.azure.com/contoso/_git/payments") == AdoRef(
        "contoso", "payments", "payments"
    )


def test_legacy_host_tolerates_default_collection():
    assert parse_ado_url(
        "https://contoso.visualstudio.com/DefaultCollection/Platform/_git/payments"
    ) == AdoRef("contoso", "Platform", "payments")


@pytest.mark.parametrize("url", [
    "https://github.com/org/repo",
    "https://gitlab.com/org/repo",
    "git@github.com:org/repo.git",
    "org/repo",
    "/local/path",
    "",
    None,
])
def test_non_ado_urls_are_not_claimed(url):
    assert parse_ado_url(url) is None


def test_userinfo_is_stripped():
    """An ADO clone URL embeds `{org}@`, which reads as a credential in a report."""
    assert (strip_userinfo("https://contoso@dev.azure.com/contoso/P/_git/r")
            == "https://dev.azure.com/contoso/P/_git/r")
    # Anything without userinfo is returned untouched.
    assert (strip_userinfo("https://github.com/org/repo")
            == "https://github.com/org/repo")
    assert strip_userinfo(None) is None


# ------------------------------------------------------------------------ slug


def test_ado_slug_is_org_project_repo():
    """Regression: the generic remote regex reads an ADO path as `{project}/_git`,
    so every repo in a project produced the same `drift-Platform-_git` filename."""
    assert slug_for("https://dev.azure.com/contoso/Platform/_git/payments") == (
        "contoso-Platform-payments"
    )


def test_ado_slug_distinguishes_repos_in_one_project():
    a = slug_for("https://dev.azure.com/contoso/Platform/_git/payments")
    b = slug_for("https://dev.azure.com/contoso/Platform/_git/ledger")
    assert a != b


def test_github_slug_unchanged():
    assert slug_for("https://github.com/darrinthomascecil/driftscope-testapp") == (
        "darrinthomascecil-driftscope-testapp"
    )


def test_local_path_slug_unchanged():
    assert slug_for("/tmp/estate/") == "estate"


# ------------------------------------------------------------- normalize_remote


def test_bare_shorthand_still_means_github():
    """Changing this silently would break every existing invocation."""
    assert normalize_remote("org/repo") == "https://github.com/org/repo"


def test_ado_org_opts_shorthand_into_azure_devops():
    assert normalize_remote("Platform/payments", ado_org="contoso") == (
        "https://dev.azure.com/contoso/Platform/_git/payments"
    )


def test_ado_org_with_single_segment_uses_it_as_both():
    assert normalize_remote("payments", ado_org="contoso") == (
        "https://dev.azure.com/contoso/payments/_git/payments"
    )


def test_full_url_ignores_ado_org():
    url = "https://github.com/org/repo"
    assert normalize_remote(url, ado_org="contoso") == url


def test_ado_url_looks_remote():
    assert looks_remote("https://dev.azure.com/contoso/Platform/_git/payments")


# ------------------------------------------------------------------ clone auth


ADO_URL = "https://dev.azure.com/contoso/Platform/_git/payments"


@pytest.fixture
def fake_token(monkeypatch):
    """Stand in for `az account get-access-token` -- tests never shell out."""
    monkeypatch.setattr(
        src_mod, "ado_credential",
        lambda identity=None: AdoCredential("TOKEN123", "Bearer", "az"),
    )


def test_ado_clone_gets_a_bearer_header(fake_token):
    env = _auth_env_for(ADO_URL)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer TOKEN123"


def test_token_never_reaches_argv(fake_token):
    """argv is world-readable via `ps`; environment is not. The token must travel
    in GIT_CONFIG_VALUE_0, never as a `-c` argument."""
    env = _auth_env_for(ADO_URL)
    assert not any(k.startswith("-c") or "TOKEN123" in k for k in env)
    assert "TOKEN123" in env["GIT_CONFIG_VALUE_0"]


def test_github_clone_injects_nothing_without_a_github_token(monkeypatch):
    """No resolvable GitHub token -> fall back to git's own credential helper; and it
    must never reach for the ADO credential the way the old code implicitly could."""
    import driftscope.http as http_mod

    monkeypatch.setattr(http_mod, "github_token", lambda identity=None: None)
    assert _auth_env_for("https://github.com/org/repo") == {}


def test_github_clone_injects_token_as_basic_x_access_token(monkeypatch):
    """A resolvable GitHub token (App installation token under WI, or a PAT) is how a
    private GitHub repo clones in a pod with no credential helper. GitHub's git-over-
    HTTPS convention is basic auth with the token as the password and any username;
    `x-access-token` is what a GitHub App installation token expects."""
    import driftscope.http as http_mod

    monkeypatch.setattr(http_mod, "github_token", lambda identity=None: "ghs_installtoken")
    env = _auth_env_for("https://github.com/org/repo")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    expected = "Basic " + base64.b64encode(b"x-access-token:ghs_installtoken").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: {expected}"
    # the ADO branch must not have run
    assert "TOKEN123" not in env["GIT_CONFIG_VALUE_0"]


def test_no_token_means_no_auth_env(monkeypatch):
    """`az` missing or logged out falls back to whatever git is configured with,
    rather than producing a header with an empty token."""
    monkeypatch.setattr(src_mod, "ado_credential", lambda identity=None: None)
    assert _auth_env_for(ADO_URL) == {}


# ------------------------------------------------------- credential resolution

# A structurally valid JWT: `{"alg":"none"}` / `{"sub":"x"}` / empty signature.
# Only the shape matters -- nothing here verifies or decodes it.
JWT = ("eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.")
PAT = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnop"


@pytest.fixture(autouse=True)
def clear_cred_cache(monkeypatch):
    """`ado_credential` memoises for the process; each test resolves afresh."""
    monkeypatch.setattr(src_mod, "_ADO_CRED", False)
    for name, _ in src_mod._ADO_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_az(monkeypatch):
    """No `az` on PATH, so only the environment can supply a credential."""
    monkeypatch.setattr(src_mod.shutil, "which", lambda _: None)


def test_a_pat_authenticates_as_basic_not_bearer(monkeypatch, no_az):
    """The bug this whole type exists to prevent. ADO answers a PAT-as-Bearer
    with 401, and `AdoClient._call` turns 401 into None -- so getting this wrong
    produces a report with fewer findings instead of an error anyone would see."""
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", PAT)
    cred = src_mod.ado_credential()
    assert cred.scheme == "Basic"
    assert cred.header.startswith("Basic ")
    # Empty username, PAT as password -- what ADO expects.
    assert base64.b64decode(cred.header.split(" ", 1)[1]).decode() == f":{PAT}"


def test_pipelines_system_accesstoken_is_a_bearer_token(monkeypatch, no_az):
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", JWT)
    cred = src_mod.ado_credential()
    assert (cred.scheme, cred.origin) == ("Bearer", "SYSTEM_ACCESSTOKEN")
    assert cred.header == f"Bearer {JWT}"


@pytest.mark.parametrize("token,scheme", [(JWT, "Bearer"), (PAT, "Basic")])
def test_the_ambiguous_variable_is_detected_not_assumed(monkeypatch, no_az, token, scheme):
    """AZURE_DEVOPS_TOKEN has no single owner defining its meaning, so the scheme
    comes from the token's own shape -- a JWT is an AAD token, anything else is
    treated as a PAT."""
    monkeypatch.setenv("AZURE_DEVOPS_TOKEN", token)
    assert src_mod.ado_credential().scheme == scheme


def test_env_beats_az(monkeypatch):
    """Setting the variable is an act of intent, and probing a logged-out `az`
    costs a 30s timeout to learn nothing."""
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", JWT)

    def explode(*a, **k):  # pragma: no cover -- reaching this is the failure
        raise AssertionError("shelled out to az despite an env credential")

    monkeypatch.setattr(src_mod.shutil, "which", lambda _: "/usr/bin/az")
    monkeypatch.setattr(src_mod.subprocess, "run", explode)
    assert src_mod.ado_credential().origin == "SYSTEM_ACCESSTOKEN"


def test_precedence_among_env_vars(monkeypatch, no_az):
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", PAT)
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", JWT)
    monkeypatch.setenv("AZURE_DEVOPS_TOKEN", JWT)
    assert src_mod.ado_credential().origin == "AZURE_DEVOPS_EXT_PAT"


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_an_empty_variable_means_unset(monkeypatch, no_az, value):
    """CI systems export variables as the empty string constantly. That is
    "nothing supplied it", not "authenticate with nothing" -- a header built from
    it would 401 and read as a permissions problem."""
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", value)
    assert src_mod.ado_credential() is None


def test_surrounding_whitespace_is_stripped(monkeypatch, no_az):
    """A token piped through a file or a shell substitution keeps its newline."""
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", f"  {JWT}\n")
    assert src_mod.ado_credential().token == JWT


def test_nothing_anywhere_yields_none(no_az):
    assert src_mod.ado_credential() is None
    assert src_mod.ado_token() is None


def test_ado_token_still_returns_the_raw_value(monkeypatch, no_az):
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", JWT)
    assert src_mod.ado_token() == JWT


def test_resolution_is_cached_for_the_process(monkeypatch, no_az):
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", JWT)
    first = src_mod.ado_credential()
    monkeypatch.delenv("SYSTEM_ACCESSTOKEN")
    assert src_mod.ado_credential() is first


# --------------------------------------------------------------- cache writes


def test_concurrent_writers_to_one_url_do_not_collide(tmp_path):
    """Regression: the temp filename was derived from the key alone, so two threads
    writing the same URL raced -- the first replace() consumed the shared temp file
    and the second raised FileNotFoundError.

    Reachable in normal use: resolution runs eight workers, and a real scan
    resolves rails/rails from five components and kubernetes/kubernetes from four,
    all hitting one URL. The exception escaped put(), propagated out of the
    resolver, and the dispatcher turned it into a `unknown` finding -- intermittent
    and with no cause visible in the report.
    """
    import threading

    from driftscope.http import Cache

    cache = Cache(root=tmp_path / "c")
    key = "https://registry.example/shared"
    errors: list[str] = []

    def writer(i: int) -> None:
        try:
            for _ in range(40):
                cache.put(key, {"writer": i, "payload": "x" * 4000})
                got = cache.get(key)
                if got is not None and "writer" not in got:
                    errors.append(f"torn read: {sorted(got)[:3]}")
        except Exception as e:  # noqa: BLE001 -- the point is that none escape
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors[:3]
    # And the entry is intact afterwards, not a half-written file.
    assert "writer" in cache.get(key)


def test_a_failed_cache_write_never_fails_the_caller(tmp_path, monkeypatch):
    """The answer is already in hand by the time we write it; losing the cache
    entry is a slower next run, not a failed scan."""
    from driftscope.http import Cache

    cache = Cache(root=tmp_path / "c")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    cache.put("k", {"a": 1})  # must not raise


# ------------------------------------------------ temp-clone cleanup (cloud hygiene)

def test_sweep_stale_clones_removes_orphans_and_leaves_fresh(tmp_path):
    import os
    import time

    from driftscope.source import CLONE_PREFIX, sweep_stale_clones

    old = tmp_path / f"{CLONE_PREFIX}old"; old.mkdir()
    fresh = tmp_path / f"{CLONE_PREFIX}fresh"; fresh.mkdir()
    unrelated = tmp_path / "something-else"; unrelated.mkdir()
    past = time.time() - 7200
    os.utime(old, (past, past))  # age it beyond the cutoff

    removed = sweep_stale_clones(older_than_secs=3600, root=tmp_path)

    assert removed == 1
    assert not old.exists()           # the orphan is gone
    assert fresh.exists()             # a possibly-live clone is spared
    assert unrelated.exists()         # only our own prefix is touched


def test_startup_sweep_reaps_everything_present(tmp_path):
    from driftscope.source import CLONE_PREFIX, sweep_stale_clones

    for n in ("a", "b", "c"):
        (tmp_path / f"{CLONE_PREFIX}{n}").mkdir()
    # older_than=0 is how the server sweeps at boot: nothing can be live yet.
    assert sweep_stale_clones(0, root=tmp_path) == 3
    assert not list(tmp_path.glob(f"{CLONE_PREFIX}*"))


def test_open_source_removes_temp_dir_when_clone_fails(monkeypatch):
    from driftscope.source import CloneError, open_source

    created = {}
    real_mkdtemp = src_mod.tempfile.mkdtemp

    def spy(*a, **k):
        d = real_mkdtemp(*a, **k)
        created["dir"] = Path(d)
        return d

    monkeypatch.setattr(src_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(src_mod.tempfile, "mkdtemp", spy)

    class _Failed:
        returncode = 1
        stderr = "fatal: repository 'x' not found"
        stdout = ""

    monkeypatch.setattr(src_mod, "_git", lambda *a, **k: _Failed())

    with pytest.raises(CloneError):
        with open_source("https://github.com/o/nope"):
            pass  # pragma: no cover

    assert created["dir"] and not created["dir"].exists()  # cleaned up despite failure


# ------------------------------------------- workload identity ADO auth (no `az`)

def _clear_ado_env(monkeypatch):
    monkeypatch.setattr(src_mod, "_ADO_CRED", False)  # reset the resolve-once cache
    for v in ("AZURE_DEVOPS_EXT_PAT", "SYSTEM_ACCESSTOKEN", "AZURE_DEVOPS_TOKEN",
              "AZURE_FEDERATED_TOKEN_FILE", "AZURE_CLIENT_ID", "AZURE_TENANT_ID",
              "AZURE_AUTHORITY_HOST"):
        monkeypatch.delenv(v, raising=False)


class _FakeResp:
    def __init__(self, payload): self._payload = payload
    def read(self): return json.dumps(self._payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_workload_identity_exchange_yields_a_bearer_credential(monkeypatch, tmp_path):
    _clear_ado_env(monkeypatch)
    tokfile = tmp_path / "azure-identity-token"
    tokfile.write_text("eyJhbGc.federated.assertion")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(tokfile))
    monkeypatch.setenv("AZURE_CLIENT_ID", "5d507c31-client")
    monkeypatch.setenv("AZURE_TENANT_ID", "f493fd7e-tenant")

    seen = {}
    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode()
        return _FakeResp({"access_token": "eyJhbGc.ado.access", "token_type": "Bearer"})
    monkeypatch.setattr(wi_mod.urllib.request, "urlopen", fake_urlopen)

    cred = src_mod.ado_credential()
    assert cred is not None
    assert cred.scheme == "Bearer" and cred.origin == "workload-identity"
    assert cred.header == "Bearer eyJhbGc.ado.access"
    # exchanged the federated assertion for the ADO-scoped token at the tenant endpoint
    assert "f493fd7e-tenant/oauth2/v2.0/token" in seen["url"]
    assert f"{src_mod.ADO_RESOURCE}%2F.default" in seen["body"]
    assert "client-assertion-type" in seen["body"]


def test_explicit_env_token_beats_workload_identity(monkeypatch, tmp_path):
    _clear_ado_env(monkeypatch)
    tokfile = tmp_path / "azure-identity-token"
    tokfile.write_text("federated.assertion.here")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(tokfile))
    monkeypatch.setenv("AZURE_CLIENT_ID", "c"); monkeypatch.setenv("AZURE_TENANT_ID", "t")
    monkeypatch.setenv("AZURE_DEVOPS_TOKEN", "eyJexplicit.jwt.token")  # an intentional override

    # If WI were consulted, this would blow up -- proving precedence stops earlier.
    def boom(*a, **k): raise AssertionError("workload identity must not be reached")
    monkeypatch.setattr(wi_mod.urllib.request, "urlopen", boom)

    cred = src_mod.ado_credential()
    assert cred.origin == "AZURE_DEVOPS_TOKEN" and cred.scheme == "Bearer"


def test_partial_workload_identity_falls_through_without_erroring(monkeypatch):
    _clear_ado_env(monkeypatch)
    monkeypatch.setenv("AZURE_CLIENT_ID", "c")  # tenant + token file missing
    monkeypatch.setattr(src_mod.shutil, "which", lambda name: None)  # no `az` either
    assert src_mod.ado_credential() is None  # no crash, just "nothing supplies one"


# ----------------------------------------- per-scan credential re-resolution

def test_reset_credential_cache_forces_re_resolution(monkeypatch, no_az):
    """A workload-identity/az token is only ~1h valid, so a scan must re-resolve
    rather than reuse a credential cached for the whole process."""
    monkeypatch.setenv("AZURE_DEVOPS_TOKEN", JWT)
    assert src_mod.ado_credential().token == JWT
    monkeypatch.setenv("AZURE_DEVOPS_TOKEN", JWT + "rotated")
    assert src_mod.ado_credential().token == JWT            # still the cached one
    src_mod.reset_credential_cache()
    assert src_mod.ado_credential().token == JWT + "rotated"  # re-resolved fresh


def test_open_source_re_resolves_credentials_each_scan(tmp_path):
    """open_source drops the cached credential on entry, so every scan (once per repo)
    gets a fresh token instead of reusing a stale process-lifetime one."""
    src_mod._ADO_CRED = AdoCredential("stale-token", "Bearer", "workload-identity")
    d = tmp_path / "local-repo"
    d.mkdir()
    with src_mod.open_source(str(d)):     # local path: no clone, but the reset still runs
        pass
    assert src_mod._ADO_CRED is False     # next ado_credential() will re-resolve

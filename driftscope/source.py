"""Scan targets: a local directory, or a remote git repository.

A URL target is shallow-cloned into a temporary directory, scanned, then removed.
The clone is `--depth 1 --single-branch` because only the current state of the
working tree matters -- history is irrelevant to "what versions does this pin".

Nothing here executes repository code: it clones and reads files. Hooks are
disabled explicitly so a hostile repo cannot run anything during clone.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# https://host/owner/repo(.git), git@host:owner/repo(.git), ssh://, git://
_URL_RE = re.compile(
    r"^(?:https?://|git@|ssh://|git://)", re.I
)
# Bare "owner/repo" shorthand -- only honoured when no such local path exists.
_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*$")

# Azure DevOps, in the three shapes that turn up in the wild:
#   https://dev.azure.com/{org}/{project}/_git/{repo}
#   https://{org}@dev.azure.com/{org}/{project}/_git/{repo}   <- what "Clone" copies
#   https://{org}.visualstudio.com/{project}/_git/{repo}      <- legacy, still common
# The project segment is optional on the modern host: ADO permits omitting it when
# the repository and project share a name.
_ADO_MODERN_RE = re.compile(
    r"^(?:https?://)?(?:[^@/]+@)?dev\.azure\.com/(?P<org>[^/]+)"
    r"(?:/(?P<project>(?!_git/)[^/]+))?/_git/(?P<repo>[^/?#]+)",
    re.I,
)
_ADO_LEGACY_RE = re.compile(
    r"^(?:https?://)?(?:[^@/]+@)?(?P<org>[^./@]+)\.visualstudio\.com/"
    r"(?:DefaultCollection/)?(?:(?P<project>(?!_git/)[^/]+)/)?_git/(?P<repo>[^/?#]+)",
    re.I,
)
# A userinfo prefix is how ADO's Clone button formats a URL. Echoing it back in a
# report would print something credential-shaped, so it is stripped on the way in.
_USERINFO_RE = re.compile(r"^(https?://)[^@/]+@", re.I)


class CloneError(Exception):
    pass


@dataclass(frozen=True)
class AdoRef:
    """An Azure DevOps repository, decomposed into the parts its URLs need."""

    org: str
    project: str
    repo: str

    @property
    def web_base(self) -> str:
        return f"https://dev.azure.com/{self.org}/{self.project}/_git/{self.repo}"


def parse_ado_url(url: str | None) -> AdoRef | None:
    """An `AdoRef` when `url` addresses an Azure DevOps repo, else None.

    Both live hosts and the legacy `*.visualstudio.com` one are accepted, with or
    without the `{org}@` userinfo prefix the Clone button emits. When the project
    segment is omitted -- which ADO permits when repo and project share a name --
    the repo name stands in for it, which is what the web UI resolves to as well.
    """
    if not url:
        return None
    s = url.strip().rstrip("/")
    for pattern in (_ADO_MODERN_RE, _ADO_LEGACY_RE):
        m = pattern.match(s)
        if m:
            repo = m.group("repo").removesuffix(".git")
            project = m.group("project") or repo
            return AdoRef(org=m.group("org"), project=project, repo=repo)
    return None


def strip_userinfo(url: str | None) -> str | None:
    """Drop a `user@` prefix so a report never echoes a credential-shaped string."""
    return _USERINFO_RE.sub(r"\1", url) if url else url


def slug_for(display: str) -> str:
    """A filesystem-safe name identifying what was scanned.

    Remote targets become `owner-repo` so reports from different repositories
    never collide; local paths become the directory name.
    """
    s = display.strip().rstrip("/")
    ado = parse_ado_url(s)
    if ado:
        # The generic pattern below would read an ADO path as `{project}/_git`,
        # producing `drift-project-_git.html` for every repo in the project.
        parts = [ado.org, ado.project, ado.repo]
    else:
        m = re.search(r"(?:https?://|git@|ssh://|git://)[^/:]+[/:]([^/]+)/([^/]+?)(?:\.git)?$", s)
        if m:
            parts = [m.group(1), m.group(2)]
        else:
            parts = [Path(s).name or "scan"]
    slug = "-".join(parts)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-.")
    return (slug or "scan")[:80]


@dataclass
class Source:
    """Where a scan's files came from, for attribution in the report."""

    display: str  # what the user asked for
    path: Path  # where the files actually are
    remote: str | None = None
    ref: str | None = None
    commit: str | None = None
    cloned: bool = False
    meta: dict = field(default_factory=dict)


def looks_remote(target: str) -> bool:
    if _URL_RE.match(target):
        return True
    # Treat owner/repo as remote only if it is not a real local path, so a
    # directory literally named "foo/bar" still wins.
    if _SHORTHAND_RE.match(target) and not Path(target).exists():
        return True
    return False


def normalize_remote(target: str, *, ado_org: str | None = None) -> str:
    """Expand a shorthand target to a clone URL.

    Bare `owner/repo` still means GitHub, because that is what it means everywhere
    else and silently changing it would break existing invocations. `--ado-org`
    opts a run into reading the same shorthand as `{project}/{repo}` on that
    organisation.
    """
    if _URL_RE.match(target):
        return target
    if ado_org:
        project, _, repo = target.partition("/")
        return f"https://dev.azure.com/{ado_org}/{project}/_git/{repo or project}"
    return f"https://github.com/{target}"


# Azure DevOps' fixed application ID. `az account get-access-token --resource <this>`
# mints a token scoped to ADO, which is how a private repo is cloned without a PAT.
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"

@dataclass(frozen=True)
class AdoCredential:
    """An Azure DevOps credential together with *how* it must be presented.

    The scheme is not decoration. Azure DevOps accepts two kinds of credential
    and authenticates them differently:

        AAD / OAuth access token  ->  Authorization: Bearer {token}
        Personal Access Token     ->  Authorization: Basic base64(":" + pat)

    Sending a PAT as a Bearer token returns 401. That matters more here than it
    would elsewhere, because `AdoClient._call` turns a 401 into `None`, which
    propagates as `counts_available = False` -- so the mistake surfaces as a
    report with fewer findings rather than as an error. Carrying the scheme
    alongside the token is what keeps that from being a guess.

    `origin` records which rule supplied the credential, so a failure can be
    diagnosed without re-deriving the precedence order.
    """

    token: str
    scheme: str  # "Bearer" | "Basic"
    origin: str  # "az" | the environment variable name

    @property
    def header(self) -> str:
        """The full `Authorization` header value."""
        if self.scheme == "Basic":
            # ADO expects an empty username and the PAT as the password.
            return "Basic " + base64.b64encode(f":{self.token}".encode()).decode()
        return f"Bearer {self.token}"


# Azure DevOps' fixed application ID. `az account get-access-token --resource <this>`
# mints a token scoped to ADO, which is how a private repo is cloned without a PAT.
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"

# Environment variables checked before shelling out to `az`, in this order, with
# the scheme each one implies.
#
# Explicit environment beats `az` deliberately. Setting one of these is an act of
# intent -- in a container or a CI job there may be no `az` at all, and probing a
# logged-out `az` costs a 30-second timeout before returning nothing.
#
#   AZURE_DEVOPS_EXT_PAT  what the `az devops` CLI extension reads; always a PAT
#   SYSTEM_ACCESSTOKEN    Azure Pipelines' built-in OAuth token, when a job opts in
#   AZURE_DEVOPS_TOKEN    no single owner defines this one, so its scheme is
#                         detected from the token's shape rather than assumed
_ADO_ENV_VARS: tuple[tuple[str, str | None], ...] = (
    ("AZURE_DEVOPS_EXT_PAT", "Basic"),
    ("SYSTEM_ACCESSTOKEN", "Bearer"),
    ("AZURE_DEVOPS_TOKEN", None),
)

_ADO_CRED: AdoCredential | None | bool = False  # False = not yet looked up


def _scheme_for(token: str) -> str:
    """Bearer or Basic, from the token's own shape.

    An AAD access token is a JWT: three base64url segments separated by dots,
    the first decoding to a JSON header, so it always begins `eyJ`. A PAT is an
    opaque string with no dots. The two are therefore distinguishable by
    inspection, which is why the ambiguous variable can be supported at all --
    were they not, the honest option would be to refuse it rather than pick.
    """
    return "Bearer" if token.startswith("eyJ") and token.count(".") == 2 else "Basic"


def _federated_ado_token() -> str | None:
    """The Azure Workload Identity path for Azure DevOps: exchange the pod's projected
    federated token for an ADO-scoped access token, with no stored secret and no `az`.

    The exchange itself lives in `wi.federated_token_for` because the Key Vault-backed
    GitHub App path needs the identical flow for a different resource. Returns None
    when the pod is not federated, so the caller falls through to `az` or nothing.
    """
    from .wi import federated_token_for

    return federated_token_for(f"{ADO_RESOURCE}/.default")


def ado_credential() -> AdoCredential | None:
    """The Azure DevOps credential for this process, resolved once.

    Mirrors `http.gh_token()`: prefer a credential that already exists on the
    machine so there is nothing to configure. Returns None when nothing supplies
    one, in which case the caller falls back to whatever git credentials the user
    has configured -- never a header built from an empty token.
    """
    global _ADO_CRED
    if _ADO_CRED is not False:
        return _ADO_CRED  # type: ignore[return-value]
    _ADO_CRED = None

    for name, scheme in _ADO_ENV_VARS:
        # CI systems routinely export a variable as the empty string; that means
        # "unset", not "authenticate with nothing".
        raw = (os.environ.get(name) or "").strip()
        if raw:
            _ADO_CRED = AdoCredential(raw, scheme or _scheme_for(raw), name)
            return _ADO_CRED

    # Workload identity: a federated pod, no secret and no `az`. Comes after the
    # explicit env tokens (setting one of those is an override) but before `az`,
    # which is the last resort and may not be installed at all.
    wi = _federated_ado_token()
    if wi:
        _ADO_CRED = AdoCredential(wi, "Bearer", "workload-identity")
        return _ADO_CRED

    if shutil.which("az"):
        try:
            r = subprocess.run(
                ["az", "account", "get-access-token", "--resource", ADO_RESOURCE,
                 "--query", "accessToken", "-o", "tsv"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0 and r.stdout.strip():
                _ADO_CRED = AdoCredential(r.stdout.strip(), "Bearer", "az")
        except (subprocess.SubprocessError, OSError):
            pass
    return _ADO_CRED  # type: ignore[return-value]


def ado_token() -> str | None:
    """The raw token, for callers that do not build a header themselves."""
    cred = ado_credential()
    return cred.token if cred else None


def _auth_env_for(remote: str) -> dict[str, str]:
    """Git config, as environment, that authenticates a clone of `remote`.

    Azure DevOps gets its AAD/PAT credential; github.com gets a GitHub token when one
    is resolvable (a GitHub App installation token under workload identity, or a
    `GITHUB_TOKEN`/`gh` token otherwise) -- which is the only way a private GitHub repo
    clones in a pod with no credential helper. Anything else, or no token, gets `{}`
    and falls back to whatever git is already configured with.

    The token travels in `GIT_CONFIG_VALUE_0` rather than a `-c` argument on purpose --
    argv is world-readable via `ps`, environment is not.
    """
    header = None
    if parse_ado_url(remote):
        cred = ado_credential()
        if cred:
            header = cred.header
    elif "github.com" in remote:
        from .http import github_token

        tok = github_token()
        if tok:
            # GitHub's git-over-HTTPS convention: any username, token as password.
            # x-access-token is what a GitHub App installation token expects.
            header = "Basic " + base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    if not header:
        return {}
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: {header}",
    }


def _git(args: list[str], cwd: Path | None = None, timeout: int = 300,
         extra_env: dict[str, str] | None = None):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        # Never let a clone run repository-supplied code, and never hang waiting
        # for credentials on a private repo -- fail fast instead.
        env={
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "true",
            "GCM_INTERACTIVE": "never",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(Path.home()),
            **(extra_env or {}),
        },
    )


CLONE_PREFIX = "driftscope-clone-"


def sweep_stale_clones(older_than_secs: float = 3600, root=None) -> int:
    """Remove clone temp dirs abandoned by a killed process, returning the count.

    open_source cleans up its own clone in a `finally`, but a SIGKILL runs no
    `finally` -- a container scaled in or evicted mid-scan leaves its clone behind,
    and on a persistent/mounted temp those accumulate until the disk fills. Sweep any
    `driftscope-clone-*` older than `older_than_secs`; called at server startup with
    0 (nothing can be a live scan before the server accepts a request).
    """
    import time

    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    cutoff = time.time() - older_than_secs
    removed = 0
    try:
        candidates = list(base.glob(f"{CLONE_PREFIX}*"))
    except OSError:
        return 0
    for d in candidates:
        try:
            if d.is_dir() and d.stat().st_mtime <= cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


@contextmanager
def open_source(
    target: str,
    *,
    ref: str | None = None,
    keep: bool = False,
    ado_org: str | None = None,
):
    """Yield a Source for `target`, cloning it first if it is remote."""
    if not looks_remote(target):
        p = Path(target).expanduser().resolve()
        if not p.exists():
            raise CloneError(f"{target}: no such path")
        yield Source(display=str(p), path=p if p.is_dir() else p.parent)
        return

    if shutil.which("git") is None:
        raise CloneError("git is not installed, cannot scan a remote repository")

    remote = normalize_remote(target, ado_org=ado_org)
    auth_env = _auth_env_for(remote)
    # Create the temp dir last, and wrap everything after it in try/finally, so no
    # code path between its creation and the cleanup guard can leak it.
    tmp = Path(tempfile.mkdtemp(prefix=CLONE_PREFIX))
    try:
        dest = tmp / "repo"
        args = ["clone", "--depth", "1", "--single-branch", "--no-tags", "-c",
                "core.hooksPath=/dev/null"]
        if ref:
            args += ["--branch", ref]
        args += [remote, str(dest)]

        r = _git(args, extra_env=auth_env)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            detail = err[-1] if err else "git clone failed"
            hint = ""
            joined = " ".join(err)
            if "Authentication failed" in joined or "could not read Username" in joined:
                if parse_ado_url(remote) and not auth_env:
                    # The specific, actionable cause: `az` could have supplied a
                    # token and did not, so say that rather than the generic hint.
                    hint = (" (Azure DevOps: run `az login`, or authenticate git "
                            "for dev.azure.com)")
                else:
                    hint = " (private repo? authenticate git, or clone it yourself and scan the path)"
            raise CloneError(f"could not clone {remote}: {detail}{hint}")

        head = _git(["rev-parse", "HEAD"], cwd=dest)
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest)
        # The clone needs any userinfo the user supplied, but the report must not
        # carry it: an ADO Clone URL embeds `{org}@`, which reads as a credential.
        src = Source(
            display=strip_userinfo(remote),
            path=dest,
            remote=strip_userinfo(remote),
            ref=(branch.stdout or "").strip() or ref,
            commit=(head.stdout or "").strip()[:40] or None,
            cloned=True,
        )
        yield src
    finally:
        if keep:
            print(f"clone retained at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

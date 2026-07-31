"""Named forge identities: bring-your-own GitHub App / Azure DevOps Entra app.

driftscope's credential seams (`http.github_token`, `source.ado_credential`) historically
read one app's configuration from process-global environment and cached one token per
process. That is fine for a single-tenant CLI, but a hosted server scans repositories
belonging to *different* tenants, each of whom brings their **own** GitHub App or ADO
Entra app. This module is the registry that lets one process hold many, select the right
one per scan by the repository's host+owner, and keep their tokens isolated.

Three deliberate constraints, each reflected in the types here:

  * **Secrets are Key Vault references only.** A GitHub App identity carries a KV *key*
    reference (the App JWT is signed by Key Vault's `sign`, so the private key never
    leaves the vault); an ADO identity carries a KV *secret* reference or a federated
    credential (no stored secret at all). There is no field in which a raw PEM or client
    secret could be written, so tenant key material can never land in the config file or
    the pod. The env-default identity is the sole exception -- it can fall back to a local
    PEM -- because that path predates this module and is single-tenant by nature.

  * **Isolation is keyed on the credential, not the host.** `ForgeIdentity.cache_key()`
    folds in the app/installation/vault-ref (GitHub) or client/tenant/secret-ref (ADO),
    never the host alone. Two tenants on github.com therefore get distinct cache entries;
    one can never be handed the other's minted token.

  * **Resolution fails closed.** An explicitly requested name that does not exist is a
    hard error (`UnknownIdentity` -> 400), and a named identity whose match rules exclude
    the target is refused (`IdentityForbidden` -> 403). Only the *implicit* path (no name
    requested) falls through to the env default and then to ambient git/gh credentials --
    which is exactly today's behaviour, preserved for backward compatibility.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Azure DevOps' fixed application id; `<this>/.default` scopes an AAD token to ADO.
# Duplicated from source.ADO_RESOURCE (a well-known constant) to keep this module free of
# an import cycle with source.py, which imports identity types for its own signatures.
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"

FORGE_GITHUB = "github"
FORGE_ADO = "ado"


class RegistryError(Exception):
    """The forge-auth config is unreadable, malformed, or self-contradictory.

    Raised at load time so a bad registry fails the server/CLI startup loudly rather
    than silently degrading to anonymous access mid-scan.
    """


class UnknownIdentity(Exception):
    """A scan requested an identity name that the registry does not define (-> 400)."""


class IdentityForbidden(Exception):
    """A named identity's match rules do not permit the requested target (-> 403)."""


@dataclass(frozen=True)
class KeyVaultKeyRef:
    """Points at a Key Vault *key* used to sign a GitHub App JWT (never exported)."""

    vault_uri: str
    key_name: str
    key_version: str = ""


@dataclass(frozen=True)
class KeyVaultSecretRef:
    """Points at a Key Vault *secret* holding an ADO Entra app's client secret."""

    vault_uri: str
    secret_name: str
    secret_version: str = ""


@dataclass(frozen=True)
class GitHubAppAuth:
    """A GitHub App's identity: the ids, and how its JWT is signed.

    `signing_key` is a Key Vault key reference for a configured tenant identity. It is
    `None` only for the env-default identity, whose JWT is signed by local openssl over a
    PEM (`github_app._sign_via_openssl`) -- a path the config schema cannot express.
    """

    app_id: str
    installation_id: str
    signing_key: KeyVaultKeyRef | None = None

    def cache_key(self) -> str:
        sk = self.signing_key
        sig = f"{sk.vault_uri}/{sk.key_name}/{sk.key_version}" if sk else "pem"
        return f"gh:{self.app_id}:{self.installation_id}:{sig}"


@dataclass(frozen=True)
class AdoEntraAuth:
    """An Azure DevOps Entra app: mints an ADO-scoped AAD token per tenant.

    Exactly one of two credential kinds is set:
      * `secret` -- a Key Vault secret reference (client-credentials grant), or
      * `federated=True` -- no stored secret; the server's projected federated token is
        exchanged for a token on *this* app via its client_id/tenant.
    """

    client_id: str
    tenant_id: str
    resource: str = ADO_RESOURCE
    secret: KeyVaultSecretRef | None = None
    federated: bool = False

    def cache_key(self) -> str:
        cred = "fed" if self.federated else (self.secret.secret_name if self.secret else "none")
        return f"ado:{self.client_id}:{self.tenant_id}:{cred}"


@dataclass(frozen=True)
class IdentityMatch:
    """Which targets an identity is allowed to serve.

    An empty tuple means "any" for that dimension, so `IdentityMatch()` matches every
    target (used by the env default). Matching is case-insensitive because hosts and
    GitHub owners are.
    """

    hosts: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()

    def permits(self, host: str, owner: str | None) -> bool:
        host_ok = (not self.hosts) or host.lower() in {h.lower() for h in self.hosts}
        owner_ok = (not self.owners) or (
            owner is not None and owner.lower() in {o.lower() for o in self.owners}
        )
        return host_ok and owner_ok


@dataclass(frozen=True)
class ForgeIdentity:
    """A named credential for one forge, selected per scan by host+owner."""

    name: str
    forge: str  # FORGE_GITHUB | FORGE_ADO
    match: IdentityMatch
    github: GitHubAppAuth | None = None
    ado: AdoEntraAuth | None = None
    source: str = "config"  # "config" | "env-default"

    def cache_key(self) -> str:
        """Stable, secret-free token-cache key. Never host-alone (isolation invariant)."""
        if self.github is not None:
            return self.github.cache_key()
        if self.ado is not None:
            return self.ado.cache_key()
        return f"id:{self.name}"


# --------------------------------------------------------------------------- parsing


def _require(d: dict, key: str, ctx: str):
    v = d.get(key)
    if v is None or v == "":
        raise RegistryError(f"{ctx}: missing required field {key!r}")
    return v


def _parse_kv_key(d: dict, ctx: str) -> KeyVaultKeyRef:
    return KeyVaultKeyRef(
        vault_uri=_require(d, "vault_uri", ctx),
        key_name=_require(d, "key_name", ctx),
        key_version=(d.get("key_version") or ""),
    )


def _parse_kv_secret(d: dict, ctx: str) -> KeyVaultSecretRef:
    return KeyVaultSecretRef(
        vault_uri=_require(d, "vault_uri", ctx),
        secret_name=_require(d, "secret_name", ctx),
        secret_version=(d.get("secret_version") or ""),
    )


def _parse_identity(d: dict) -> ForgeIdentity:
    name = _require(d, "name", "identity")
    ctx = f"identity {name!r}"
    forge = _require(d, "forge", ctx)
    if forge not in (FORGE_GITHUB, FORGE_ADO):
        raise RegistryError(f"{ctx}: unknown forge {forge!r} (expected 'github' or 'ado')")
    m = d.get("match") or {}
    match = IdentityMatch(hosts=tuple(m.get("hosts") or ()), owners=tuple(m.get("owners") or ()))

    github = ado = None
    if forge == FORGE_GITHUB:
        g = d.get("github") or {}
        signing = g.get("signing_key")
        github = GitHubAppAuth(
            app_id=str(_require(g, "app_id", ctx)),
            installation_id=str(_require(g, "installation_id", ctx)),
            signing_key=_parse_kv_key(signing, f"{ctx} signing_key") if signing else None,
        )
    else:
        a = d.get("ado") or {}
        cred = a.get("credential") or {}
        kind = cred.get("kind")
        if kind not in ("client_secret", "federated"):
            raise RegistryError(
                f"{ctx}: ado credential.kind must be 'client_secret' or 'federated'"
            )
        secret = _parse_kv_secret(cred.get("secret") or {}, f"{ctx} secret") \
            if kind == "client_secret" else None
        ado = AdoEntraAuth(
            client_id=str(_require(a, "client_id", ctx)),
            tenant_id=str(_require(a, "tenant_id", ctx)),
            resource=(a.get("resource") or ADO_RESOURCE),
            secret=secret,
            federated=(kind == "federated"),
        )
    return ForgeIdentity(name=name, forge=forge, match=match, github=github, ado=ado)


def env_default_identity() -> ForgeIdentity | None:
    """The legacy single-app GitHub App, modelled as an identity for backward compat.

    Built from `GITHUB_APP_ID` / `GITHUB_APP_INSTALLATION_ID` (+ the KV key vars, or a
    local PEM when they are absent). Returns None when the App env is not configured, in
    which case the implicit path falls straight through to `GITHUB_TOKEN`/`gh`.

    The ADO env/az/federated legacy path is deliberately NOT modelled here: it stays the
    `identity is None` fallback inside `source.ado_credential`, so the existing tests and
    the zero-config developer flow are untouched.
    """
    app_id = os.environ.get("GITHUB_APP_ID")
    installation = os.environ.get("GITHUB_APP_INSTALLATION_ID")
    if not (app_id and installation):
        return None
    vault = os.environ.get("GITHUB_APP_KEY_VAULT_URI")
    key_name = os.environ.get("GITHUB_APP_KEY_NAME")
    signing = KeyVaultKeyRef(vault, key_name, os.environ.get("GITHUB_APP_KEY_VERSION", "") or "") \
        if (vault and key_name) else None
    return ForgeIdentity(
        name="default",
        forge=FORGE_GITHUB,
        match=IdentityMatch(hosts=("github.com",)),
        github=GitHubAppAuth(app_id=app_id, installation_id=installation, signing_key=signing),
        source="env-default",
    )


# ------------------------------------------------------------------------- selection

# Extract (host, owner) from an already-normalized remote. `normalize_remote` yields
# https URLs for both github and ADO, so urlsplit handles the common case; the scp-like
# `git@host:owner/repo` form is covered for completeness.
_SCP_RE = re.compile(r"^(?:ssh://|git://)?(?:[^@/]+@)?([^/:]+)[/:]([^/]+)", re.I)


def _host_owner(remote: str) -> tuple[str, str | None]:
    if remote.startswith(("http://", "https://")):
        parts = urlsplit(remote)
        host = (parts.hostname or "").lower()
        segs = [s for s in parts.path.split("/") if s]
        return host, (segs[0] if segs else None)
    m = _SCP_RE.match(remote)
    if m:
        return m.group(1).lower(), m.group(2)
    return "", None


def target_selector(remote: str) -> tuple[str, str | None, str]:
    """(host, owner, forge) for a normalized remote, so an identity can be resolved.

    ADO is recognised via `source.parse_ado_url` (imported lazily to avoid a cycle), so
    the owner is the *org* and the legacy `*.visualstudio.com` host is handled too.
    """
    from ..source import parse_ado_url

    ado = parse_ado_url(remote)
    host, owner = _host_owner(remote)
    if ado:
        return host or "dev.azure.com", ado.org, FORGE_ADO
    return host, owner, FORGE_GITHUB


class IdentityRegistry:
    """The set of configured identities plus the env default, with a per-target resolver."""

    def __init__(self, identities: list[ForgeIdentity], default: ForgeIdentity | None = None):
        self._identities = list(identities)
        self._by_name = {i.name: i for i in self._identities}
        self._default = default

    @classmethod
    def load(cls, path: str | None = None) -> "IdentityRegistry":
        """Load from `path` (or `$DRIFTSCOPE_FORGE_AUTH`); attach the env default.

        A missing/unset path yields a registry with only the env default (if any) --
        i.e. today's behaviour. A present-but-malformed file raises RegistryError so the
        process fails fast rather than silently losing tenant access.
        """
        path = path or os.environ.get("DRIFTSCOPE_FORGE_AUTH")
        identities: list[ForgeIdentity] = []
        if path:
            try:
                raw = Path(path).read_text()
            except OSError as e:
                raise RegistryError(f"cannot read forge-auth file {path!r}: {e}") from e
            try:
                doc = json.loads(raw)
            except ValueError as e:
                raise RegistryError(f"forge-auth file {path!r} is not valid JSON: {e}") from e
            version = doc.get("version")
            if version != 1:
                raise RegistryError(f"forge-auth: unsupported version {version!r} (expected 1)")
            seen: set[str] = set()
            for entry in (doc.get("identities") or []):
                ident = _parse_identity(entry)
                if ident.name in seen:
                    raise RegistryError(f"forge-auth: duplicate identity name {ident.name!r}")
                seen.add(ident.name)
                identities.append(ident)
        return cls(identities, env_default_identity())

    def get(self, name: str) -> ForgeIdentity | None:
        if name in self._by_name:
            return self._by_name[name]
        return self._default if (self._default and self._default.name == name) else None

    def resolve(
        self, host: str, owner: str | None, *, name: str | None = None, forge: str | None = None
    ) -> ForgeIdentity | None:
        """The identity for a target, by precedence:

        1. `name` given -> exact lookup (incl. the env `default` by name); unknown ->
           UnknownIdentity; found but the target is outside its match rules ->
           IdentityForbidden.
        2. no name -> the first configured identity (of the right forge) whose match permits.
        3. else None.

        The env default is deliberately NOT auto-selected on the implicit path: returning
        None lets the caller fall through to `github_token`/`ado_credential`'s existing
        precedence (which already includes the env App, `GITHUB_TOKEN`, and `gh`), so an
        unconfigured target behaves exactly as it did before this module existed. The env
        default is still reachable by explicitly requesting `name="default"`.
        """
        if name is not None:
            ident = self.get(name)
            if ident is None:
                raise UnknownIdentity(name)
            if not ident.match.permits(host, owner):
                raise IdentityForbidden(
                    f"identity {name!r} may not access {owner or '?'} on {host}"
                )
            return ident
        for ident in self._identities:
            if forge and ident.forge != forge:
                continue
            if ident.match.permits(host, owner):
                return ident
        return None

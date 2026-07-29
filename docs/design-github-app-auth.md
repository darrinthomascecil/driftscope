# Design: GitHub App authentication (as secretless as GitHub allows)

Status: **proposed** — reviewed, not yet implemented.
Related: the Azure Workload Identity ADO path (`source._federated_ado_token`, shipped
in the WI branch) is the model this mirrors for GitHub.

## Why

driftscope authenticates to Azure DevOps / ARM / ACR secretlessly via Azure Workload
Identity. GitHub **cannot** be federated the same way: GitHub is an OIDC *issuer* (so
Actions can federate *into* clouds), not a relying party that accepts a cluster's
ServiceAccount tokens. So a workload needs a GitHub-native credential.

The industry standard for machine GitHub access is a **GitHub App**, not a personal
access token. It authenticates with a private key to mint **short-lived (≤1h),
least-privilege installation tokens**, is attributable in the audit log, and rotates
independently of any human. Ranked least→most secure: classic PAT ❌ → fine-grained
PAT ⚠️ → GitHub App ✅.

**Goal:** driftscope obtains GitHub installation tokens whose single root secret (the
App private key) lives in **Azure Key Vault**, reached only through the **workload
identity** we already have — so there is no long-lived secret anywhere in the cluster
or image, and the GitHub tokens themselves are ephemeral and narrowly scoped.

## The GitHub App token flow

```
App private key ──sign──▶ App JWT (RS256, iss=app_id, exp ≤10 min)
App JWT ──POST /app/installations/{id}/access_tokens──▶ installation token (≤1h, scoped)
installation token ──▶ GitHub API (Bearer) and git clone (x-access-token)
```

## What exists today (and what changes)

| Consumer | Today | After |
|---|---|---|
| GitHub **API** (`http.GitHub.api`, http.py:278) | `Authorization: Bearer <gh auth token>` via `gh_token()` (http.py:198) | Bearer `<installation token>` via a new `github_token()` |
| Private GitHub **clone** (`source.open_source`) | git's own credential helper; `_auth_env_for` injects a header only for **ADO** | inject the installation token as an `http.extraheader` for `github.com` remotes too |

Two consumers, one new resolver feeds both.

## Proposed design

### 1. A unified `github_token()` resolver (precedence)

Mirror `ado_credential()`'s "resolve once, prefer what already exists" shape:

1. `GITHUB_TOKEN` env — explicit override (unchanged behaviour for CI today).
2. **GitHub App** — if App vars are configured, mint/refresh an installation token.
3. `gh auth token` — the zero-config dev fallback (unchanged).

Cache the installation token with its `expires_at`; refresh when <5 min remain
(tokens last 1h, minting is a couple of HTTPS calls). When no App is configured the
function behaves exactly as `gh_token()` does now — **no behaviour change unless opted
in**.

### 2. Signing the App JWT — the one real design decision

The App JWT must be signed **RS256**. Python's stdlib has SHA hashing but **no RSA
private-key signing**, so doing it in-process would break the project's
zero-dependency rule (or export the private key into the pod). Two ways out:

- **Model A — Key Vault sign-in-place (recommended).** Store the App key in Key Vault
  as a *key* (not a secret). The pod signs the JWT via Key Vault's
  `POST /keys/{name}/{version}/sign` (RS256) using its **workload identity**. The
  private key **never leaves the vault**, and driftscope needs no crypto library. Most
  secure, and it composes perfectly with what we built.
- **Model B — fetch + local sign (fallback).** Store the PEM as a KV *secret*, fetch
  it via WI, sign locally by shelling to `openssl` (the project already shells to
  `git`/`az`). Simpler to set up, but the key lives in pod memory, and the image must
  carry `openssl`.

Default to **A**; keep **B** as an option for setups without a KV key or that prefer a
plain PAT-in-secret.

### 3. Reaching Key Vault via workload identity

Generalise the federated-token exchange already in `source._federated_ado_token`: it
hard-codes the ADO resource, but the flow (federated token → Entra → resource token)
is identical for `https://vault.azure.net`. Factor out
`federated_token_for(resource) -> str | None` and call it with the Key Vault resource,
then hit the KV REST endpoint (sign, or secret-get) with that Bearer token.

### 4. Exchanging the App JWT for an installation token

`POST https://api.github.com/app/installations/{installation_id}/access_tokens` with
`Authorization: Bearer <app_jwt>`. Optionally send a body to **narrow** the token:
`{"repositories": [...], "permissions": {"contents": "read", "metadata": "read"}}`.
Returns `{"token": "...", "expires_at": "..."}`.

### 5. Wiring the consumers

- `GitHub.api()` (http.py:299–306): swap `gh_token()` → `github_token()`. Drop-in.
- Clone auth (`source._auth_env_for`): for `github.com` remotes with an App token,
  inject `http.extraheader: Authorization: Basic base64("x-access-token:" + token)`
  — the same mechanism the ADO path uses, just GitHub's basic-auth convention.

## Configuration surface

| Var | Meaning |
|---|---|
| `GITHUB_TOKEN` | explicit token; highest precedence (unchanged) |
| `GITHUB_APP_ID` | the App's id |
| `GITHUB_APP_INSTALLATION_ID` | the installation to mint tokens for |
| `GITHUB_APP_KEY_VAULT_URI` + `GITHUB_APP_KEY_NAME` | Model A: KV key to sign with |
| `GITHUB_APP_KEY_VAULT_URI` + `GITHUB_APP_SECRET_NAME` | Model B: KV secret holding the PEM |
| `AZURE_*` (from the WI webhook) | reused to reach Key Vault — nothing new |

If the App vars are unset, driftscope authenticates to GitHub exactly as it does today.

## Least privilege

- App permissions: **Contents: Read**, **Metadata: Read** (add **Issues: Read** only
  if the issue-history feature is enabled).
- Install the App on **only the repos to be scanned**, not the whole org.
- Optionally scope each installation token to a subset of repos/permissions at mint
  time (the body above).

## Why this is the most secure option available

- **No long-lived secret** in the cluster or image.
- The **only** root secret is the App key, in a hardened, audited, versioned vault;
  with Model A it **never leaves the vault** (remote sign).
- **Access to the vault is gated by workload identity** — there is nothing to steal
  that grants it.
- GitHub tokens are **ephemeral (≤1h), least-privilege, and attributable** to the App.
- **Rotation** is a vault operation (new key version), no redeploy.

## Implementation phases

1. `github_token()` resolver + expiry-aware cache; wire `GitHub.api()`. No behaviour
   change when App unset. Tests for precedence + refresh.
2. Factor `federated_token_for(resource)` out of `source._federated_ado_token`.
3. Model A: KV sign → App JWT → installation-token exchange. Tests with mocked KV +
   GitHub endpoints (no network).
4. Clone auth: inject `x-access-token` header for `github.com` remotes.
5. Model B (KV secret + `openssl` sign) as an optional fallback.
6. Docs + a deploy example: KV `Key Vault Crypto User` (Model A) or `Key Vault Secrets
   User` (Model B) role for the UAMI, and the env wiring.

## Testing (hermetic, mirrors the WI ADO tests)

Monkeypatch `urllib.request.urlopen`; no real Key Vault or GitHub. Assert:
- precedence: `GITHUB_TOKEN` beats App beats `gh`.
- installation token is cached until near expiry, then re-minted.
- App JWT claims: `iss == app_id`, `exp - iat ≤ 600s`.
- Model A calls the KV `sign` endpoint and never handles a private key locally.
- clone injects the `x-access-token` Basic header for a `github.com` remote and not
  for others.

## Open questions

- **Model A vs B** — recommend A; confirm the KV key is RSA and grants `sign`.
- Scope installation tokens **per-scan** (tighter) or once per run (fewer calls)?
- Does the deployed feature set need **Issues: Read** (issue-history)? If so, add it.
- Model B only: the zero-dep image would need `openssl` added.

## Non-goals

- Registering/installing the GitHub App (a one-time manual step in GitHub's UI).
- Provisioning Key Vault or the role assignment (a deploy concern, documented not
  automated here).

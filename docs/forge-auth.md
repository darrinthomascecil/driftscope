◊# Bring your own GitHub App / Azure DevOps app (`forge-auth`)

driftscope clones and reads source, and queries the forge (GitHub / Azure DevOps) for
issue and release history. By default it uses one credential for the whole process
(`GITHUB_APP_*` env, `GITHUB_TOKEN`, `gh`, or the ADO env/`az`/workload-identity chain).

A **forge-auth registry** lets one driftscope process hold **many** apps — one per tenant,
org, or host — select the right one per scan, and keep their tokens isolated. Tenants
bring their **own** GitHub App or ADO Entra app; driftscope authenticates each scan as
that app.

## How it's selected

- **Server:** the scan request names an identity — `POST /scan {"repo": "...", "identity": "acme-github"}`.
  If no `identity` is given, driftscope auto-matches by the repo's host + owner/org. An
  unknown name is **400**; a name whose match rules exclude the target is **403**.
- **CLI:** `driftscope scan <url> --forge-auth registry.json --identity acme-github`
  (or omit `--identity` to auto-match).
- **Config path:** `--forge-auth FILE` on `scan`/`serve`, or `$DRIFTSCOPE_FORGE_AUTH`.
◊
If no registry is configured and no identity is requested, behaviour is exactly as before
(the process-default credential).◊

## Secrets are Key Vault references only

The registry file contains **no secret material** — only *references*:

- A GitHub App's private key is a Key Vault **key**; the App JWT is signed by Key Vault's
  `sign` operation, so the key never leaves the vault.
- An ADO Entra app's client secret is a Key Vault **secret**, read at mint time — or the
  app uses a **federated credential** and needs no stored secret at all.

Both are reached with **driftscope's own** workload identity (the server's managed
identity), not the tenant's. So a tenant only grants driftscope's identity access to their
vault; they never hand over key material.

## Schema

`forge-auth.json` (JSON — stdlib only, no YAML dependency):

```json
{
  "version": 1,
  "identities": [
    {
      "name": "acme-github",
      "forge": "github",
      "match": { "hosts": ["github.com"], "owners": ["acme-corp", "acme-labs"] },
      "github": {
        "app_id": "123456",
        "installation_id": "7890123",
        "signing_key": {
          "vault_uri": "https://acme-kv.vault.azure.net",
          "key_name": "acme-gh-app-signing",
          "key_version": ""
        }
      }
    },
    {
      "name": "globex-ado",
      "forge": "ado",
      "match": { "hosts": ["dev.azure.com", "globex.visualstudio.com"], "owners": ["globex"] },
      "ado": {
        "client_id": "1111-aaaa-...",
        "tenant_id":  "2222-bbbb-...",
        "credential": {
          "kind": "client_secret",
          "secret": {
            "vault_uri": "https://globex-kv.vault.azure.net",
            "secret_name": "globex-ado-entra-secret"
          }
        }
      }
    },
    {
      "name": "initech-ado",
      "forge": "ado",
      "match": { "hosts": ["dev.azure.com"], "owners": ["initech"] },
      "ado": {
        "client_id": "3333-cccc-...",
        "tenant_id": "4444-dddd-...",
        "credential": { "kind": "federated" }
      }
    }
  ]
}
```

Field notes:

- **`match`** — `hosts` and `owners` are allowlists (empty = "any" for that dimension).
  `owners` is the GitHub owner (first path segment) or the ADO org. Matching is
  case-insensitive. `owners` is enforced even when an identity is named explicitly, as
  defense-in-depth (a **403** if the target is outside it).
- **`github.signing_key`** — a Key Vault *key* reference. Omit `key_version` to sign with
  the current version. (There is no field for a raw PEM: tenant private keys cannot be
  written into this file.)
- **`ado.credential.kind`** — `client_secret` (with a Key Vault *secret* reference) or
  `federated` (no stored secret). `resource` defaults to Azure DevOps'
  `499b84ac-1321-427f-aa17-267ca6975798`.
- The loader **fails fast**: an unreadable file, invalid JSON, unknown `version`, duplicate
  `name`, or bad `credential.kind` aborts startup rather than degrading silently.

## Azure setup (per tenant identity)

The server's managed identity needs access to each referenced vault:

| Credential | Grant on the tenant's Key Vault (RBAC role) |
|---|---|
| GitHub App (`signing_key`) | **Key Vault Crypto User** (to `sign`) |
| ADO `client_secret` | **Key Vault Secrets User** (to `get` the secret) |

For ADO **`federated`**, additionally register a **federated credential on the tenant's
Entra app** trusting the server's OIDC subject (the AKS service-account issuer +
`system:serviceaccount:<namespace>:<sa>`, or the server's user-assigned managed identity).
This is the "no stored secret" path; `client_secret` avoids that trust setup at the cost of
a secret in the tenant's vault.

> These are cross-tenant infrastructure grants — the multi-tenant model needs a small
> amount of Azure configuration outside driftscope itself.

## In the deployed cluster (onboarding a tenant)

The server reads its registry from `$DRIFTSCOPE_FORGE_AUTH`. In the cloud overlay this is wired to a
ConfigMap mounted read-only at `/etc/driftscope/forge-auth.json`:

- **`deploy/overlays/cloud/forge-auth.yaml`** — the `driftscope-forge-auth` ConfigMap. It ships
  **empty** (`{"version":1,"identities":[]}`) — a deployment onboards no tenants by default, so
  behaviour is the ambient single-credential flow until you add one. The ConfigMap + the Deployment
  mount/env (a patch in `kustomization.yaml`) are the *plumbing*; the mount requires the ConfigMap to
  exist, so it can't be omitted.

**To onboard a tenant** (GitOps — no live edits; Argo `selfHeal` would revert a `kubectl edit`):

1. In the tenant's Azure: store their GitHub App key (a KV **key**) or ADO client secret (a KV
   **secret**) in a Key Vault, and grant **driftscope's** app identity access to it (the roles in
   *Azure setup* above — `app-identity.tf` already grants driftscope's own vault).
2. Append a KV-**reference** identity to `identities` in `forge-auth.yaml` (schema above — no secret
   material, only refs + `match` rules) and open a PR. On merge, Argo reconciles it and the server
   picks it up; the CI deploy (continuous deployment) rolls a pod that mounts the updated ConfigMap.
3. Select it per scan by `match` (host + owner/org) or an explicit `"identity"` in the request.

Because the registry holds only references, it is safe to commit and review in git; the secrets stay
in Key Vault, reached by driftscope's own workload identity.

## Token isolation & revocation

Minted tokens are cached keyed by the app/installation/vault-ref (GitHub) or
client/tenant/secret-ref (ADO) — never by host — so one tenant can never be served
another's token. GitHub App installation tokens are re-checked near expiry (~1h); see the
revocation-lag note in `driftscope/github_app.py` for the narrow window where a *narrowed*
installation's already-issued token stays valid. ADO tokens are minted fresh per scan.

# Caller authorization (Entra app roles)

The bring-your-own forge registry (`docs/forge-auth.md`) decides *which* app a scan runs
under. This layer decides *who may ask* — so a caller can't name another tenant's identity
and have their private repos scanned under it (a confused deputy).

> **Invariant:** the forge identity a scan runs under is authorized by the **authenticated
> caller** — never chosen freely by the request. The client can at most *disambiguate* among
> identities it already holds a role for; it can never escalate.

This sits **on top of** your existing access control, which is unchanged.

## Two layers, one token

| Layer | Question | Where | Claim |
|---|---|---|---|
| **Access gate (unchanged)** | may you enter driftscope at all? | oauth2-proxy (`ALLOWED_GROUP_ID`) | `groups` |
| **Tenant authorization (this)** | whose scanner may you use? | the app, in `resolve()` | `roles` (app roles) |

A single id_token carries both `groups` (directory membership → the access gate) and `roles`
(app-role assignments → this check). Keep your group gate exactly as-is; app roles are additive.

**Why app roles, not groups:** the `roles` claim is emitted for **users *and* service
principals** (so CI callers work) and never hits Entra's group-overage cliff. Assigning a role
to a **group** needs Entra ID **P1/P2**; per-user/per-SP assignment is free.

## Setup

### 1. Define app roles on the app registration

On the app whose id_token the server validates (the oauth2-proxy relying party), add roles —
`value` is what lands in the token:

```json
"appRoles": [
  { "value": "tenant.acme",   "allowedMemberTypes": ["User","Application"], "displayName": "Acme",   "id": "…" },
  { "value": "tenant.globex", "allowedMemberTypes": ["User","Application"], "displayName": "Globex", "id": "…" }
]
```

Assign `tenant.acme` to Acme's group (P1/P2) or its users/CI service principals; likewise per tenant.

### 2. Point the server at the token (env)

| Var | Value |
|---|---|
| `DRIFTSCOPE_OIDC_AUDIENCE` | the app registration id the id_token is *for* (oauth2-proxy's client id). **Presence of this var turns authorization on.** |
| `DRIFTSCOPE_OIDC_TENANT` | tenant id (defaults to `AZURE_TENANT_ID`) — derives issuer + JWKS |
| `DRIFTSCOPE_OIDC_ROLES_CLAIM` | optional; defaults to `roles` |

The forwarded token reaches the app via oauth2-proxy's `--set-authorization-header=true`
(`Authorization: Bearer <id_token>`) — ensure the ext_authz provider's
`headersToUpstreamOnAllow` includes `authorization`.

### 3. Authorize identities in the registry

Add `principals` (the app-role values) to each identity, and optionally flip strict mode:

```json
{ "version": 1, "require_authorization": true, "identities": [
  { "name": "acme-github", "forge": "github", "principals": ["tenant.acme"],
    "match": { "owners": ["acme-corp"] }, "github": { … } }
]}
```

- `principals` present → caller must hold an intersecting role (else 401 unauthenticated / 403
  wrong role). Applies to **explicit name, auto-match, and every repo of a `/scan-multi`** — all
  fail closed.
- `principals` absent → no per-caller restriction (the access-group gate is the control).
- `require_authorization: true` → **every** identity must declare `principals` (load fails
  otherwise), and a principal-less identity (e.g. the env default) is never usable. The server
  **refuses to start** in this mode unless `DRIFTSCOPE_OIDC_AUDIENCE` is set — it won't run
  "strict" with validation silently off.

## Backward compatible / when you need it

With `DRIFTSCOPE_OIDC_AUDIENCE` unset, authorization is **disabled** and behavior is exactly as
before — the oauth2-proxy group gate is the control. You only need this once a **second tenant**
can authenticate against a registry holding multiple tenants' identities; until then the single
group gate suffices. The mechanism is inert until you turn it on.

## Security notes

- **Token validated in-app, not header-trusted.** The server verifies the `Bearer` id_token
  (RS256 against Entra's JWKS, checking `exp`/`aud`/`iss`) and reads `roles` from the *verified*
  payload — a forged token fails signature verification, so the boundary rests on crypto, not on
  every mesh invariant holding. Pure stdlib (`pow` + PKCS#1), no crypto dependency.
- **Fail closed.** Missing/invalid token on a restricted identity → 401; authenticated but
  lacking the role → 403; never a silent fall-through to an unauthenticated clone.
- **Scope boundary:** this authorizes *triggering* a scan. Scans are synchronous with no stored
  results, so there is nothing to read cross-tenant yet. When scan history/DB arrives (Phase 2),
  per-tenant *read* isolation is a **separate** control — this is not it.
- **Disambiguation edge:** if two identities match the same host+owner, auto-match takes the
  first then authorizes; a caller authorized only for the second would 403. Rare (why would two
  identities match one repo?), and pass an explicit `identity` to disambiguate.

# Feature: Entra group-gated access with a custom no-access page

**Goal:** only members of a specific Entra security group (`driftscope`) in the tenant may use
the app. Any other user who authenticates lands on a branded "you don't have access" page
instead of getting in. The gated group is parameterized so it can be swapped without code
changes.

## How it works

Access was already behind Entra SSO (oauth2-proxy as an Istio ext_authz provider). This adds an
**authorization** step on top of authentication:

```
user → Entra login → token now carries a `groups` claim (security-group object ids)
     → oauth2-proxy checks the token's groups against OAUTH2_PROXY_ALLOWED_GROUPS
         ├─ contains the driftscope group  → 200, into the app
         └─ does not                        → 403, render /templates/error.html (no-access page)
```

Two independent pieces make it work:
1. **Entra emits the group claim.** The app registration is set to `groupMembershipClaims:
   SecurityGroup`, so ID tokens include the object ids of the user's security groups.
2. **oauth2-proxy enforces it.** `--allowed-group` (via `OAUTH2_PROXY_ALLOWED_GROUPS`) restricts
   logins to members of that group id; `--oidc-groups-claim=groups` tells it where to look.

## Entra setup (tenant state — not in git)

```sh
APP=2fe61316-6a56-4808-968c-0785ffa97aac        # the driftscope-sso app registration

# 1. the security group
az ad group create --display-name driftscope --mail-nickname driftscope-access
GID=$(az ad group show --group driftscope --query id -o tsv)   # 82cb7474-d489-4fd7-b7e4-d7d6fad74d0c

# 2. add a member (the app-login user; guests work too)
az ad group member add --group driftscope --member-id <user-object-id>

# 3. make the app emit security-group claims in tokens
az ad app update --id $APP --set groupMembershipClaims=SecurityGroup
```

## Kubernetes side (`deploy/overlays/cloud/`)

| Object | Role |
|---|---|
| `driftscope-access` ConfigMap | **the parameter** — `ALLOWED_GROUP_ID` (object id that gates access) + `ALLOWED_GROUP_NAME` (page text) |
| `oauth2-proxy` Deployment | `OAUTH2_PROXY_ALLOWED_GROUPS` ← ConfigMap, `OIDC_GROUPS_CLAIM=groups`, `--custom-templates-dir=/templates` |
| `oauth2-proxy-templates` ConfigMap | `error.html` — the theme-aware no-access page (mounted at `/templates`) |

The no-access page (`error.html`) renders on any oauth2-proxy error; the common case is the
group denial. It names the required group and offers "Sign in with a different account"
(`/oauth2/sign_out?rd=%2F`).

## Parameterization — changing who gets in

- **Different group:** edit `ALLOWED_GROUP_ID` in the `driftscope-access` ConfigMap (and
  `ALLOWED_GROUP_NAME` for the page), then `kubectl -n driftscope rollout restart deployment
  oauth2-proxy`. No code or image change.
- **Add/remove a user:** `az ad group member add/remove --group driftscope --member-id <id>`.
- **Multiple groups:** `OAUTH2_PROXY_ALLOWED_GROUPS` is comma-separated.

## Verification (done live)

| Case | Result |
|---|---|
| In-group user, fresh login | **`200`** — into the app (`/` and `/trace`) |
| Not in group (token without the claim) | **`403`** — the custom no-access page rendered |

## Operational notes & caveats

- **Revocation isn't instant.** The `groups` claim is baked into the session token at login, and
  oauth2-proxy checks the *stored session*. Removing a user from the group doesn't lock them out
  until their session expires (cookie `expiry:168h`) or they re-login. For immediate revocation,
  shorten the cookie lifetime or force re-auth.
- **Existing sessions break on enablement.** Turning the gate on denies sessions minted *before*
  group claims existed (they carry no `groups`), until the user re-logs-in. Expected one-time.
- **Groups overage (>200 groups).** If a user belongs to >200 groups, Entra sends an overage
  *link* instead of the list and oauth2-proxy can't evaluate it — they'd be denied. To avoid,
  scope claims to groups assigned to the app, or use App Roles instead.
- **Alternative — App Roles + "assignment required".** Entra can block non-members at the IdP
  (they never get a token), showing Entra's own page. Cleaner enforcement, but not a *custom*
  page — which is why this feature uses the oauth2-proxy group check.

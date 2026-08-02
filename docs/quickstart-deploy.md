# Quickstart: clone → running on Azure

Stand up your **own** DriftScope — your Azure subscription, your Entra tenant, your domain (or
none). This is Phase 0.5 of the business-readiness roadmap: the reproducible, deliverable package.

Since Phase 0.6d there are **three isolated environments** (dev/staging/prod). Stand up the **shared**
layer (one ACR + DNS zone + state backend) once, then each environment:

```sh
git clone <this repo> && cd driftscope
cp terraform/shared/terraform.tfvars.example terraform/shared/terraform.tfvars   # ACR + DNS zone
cp terraform/envs/dev/dev.tfvars.example     terraform/envs/dev/dev.tfvars        # fill it in (below)
az login
make shared-up            # ONCE: the shared ACR + DNS zone + state backend
make up ENV=dev           # then each environment (default ENV=dev; also staging, prod)
```

`make up ENV=<env>` prints that env's URL when it's done (dev → `dev.driftscope.<domain>`, staging →
`staging.…`, prod → the apex `driftscope.<domain>`). Open it, sign in with an account in that env's
access group, and you're in. **Day-2 changes flow git → CD:** merge to main deploys dev; a `git tag`
promotes to staging; a GitHub release promotes to prod — the **same digest** each hop
(`docs/runbook-promotion.md`).

---

## Prerequisites

**Tools** (all on PATH): `az`, `terraform`, `kubectl`, `helm`, `kustomize`, `jq`, `openssl`,
`git`. `make preflight` checks these and fails early if anything's missing.

**Azure**: a subscription you can create resources in, and rights in your Entra tenant to
create an app registration + security group (Application Administrator, or equivalent). `az login`
first; the active subscription is used unless you set `subscription_id` in tfvars.

**Cost**: per environment roughly ~$150–250/mo left running (1× B4ms AKS node, Front Door Standard
~$35, Key Vault, a public IP), plus the shared ACR Basic + DNS zone. `make down ENV=<env>` destroys
one env; lower envs default to `node_count=1`, and `az aks stop` on idle staging/prod keeps ~1 warm.

---

## Configure (per-env `tfvars`)

The **shared** layer (`terraform/shared/terraform.tfvars`) names the one ACR (+ optional DNS zone):

```hcl
subscription_id = "<your-sub-guid>"
acr_name        = "driftscopeacr12345"   # globally unique, alphanumeric -- the ONE shared registry
# base_domain   = "acme.com"             # -> shared zone driftscope.acme.com (OMIT for demo mode)
```

Each **env** (`terraform/envs/<env>/<env>.tfvars`) references the shared ACR + sets its own unique
names:

```hcl
subscription_id       = "<your-sub-guid>"
shared_resource_group = "driftscope-shared"          # where the shared ACR + zone live
acr_name              = "driftscopeacr12345"          # the shared ACR (same in every env)
key_vault_name        = "driftscope-kv-dev-12345"     # PER-ENV, globally unique (<=24 chars)
gateway_dns_label     = "driftscope-gw-dev-12345"     # PER-ENV, unique per region
github_repo           = "your-org/driftscope"
admin_members         = ["you@your-tenant.onmicrosoft.com"]   # added to this env's access group
node_count            = 1                             # prod defaults to 2
```

Leave `base_domain` unset (shared) to run every env on a Front Door `*.azurefd.net` hostname — no
domain, no DNS delegation, HTTP on the internal Front Door→origin leg (see
[`design-edge-origin-cert.md`](design-edge-origin-cert.md)). Set it to serve prod at the apex
`driftscope.acme.com` and lower envs at `dev.`/`staging.` with real certs end-to-end. All other
settings have defaults (see the example files).

---

## What `make up` does

`make shared-up` (once) applies `terraform/shared` — the one **ACR** + **DNS zone** every env shares —
and provisions the state backend. Then `make up ENV=<env>` runs the per-env pipeline; each step is
also a standalone target (`make <step> ENV=<env>`):

| Step | Target | What |
|---|---|---|
| 1 | `preflight` | tools + `az login` + this env's tfvars present |
| 2 | `kv-recover` | recovers this env's soft-deleted Key Vault if a prior teardown reserved the name (purge protection) |
| 3 | `infra` | `terraform -chdir=terraform/envs/<env> apply`: AKS (managed Istio, external gateway, Cilium), Key Vault, Entra app+group, Front Door + WAF, and this env's DNS **records** in the shared zone. References the shared ACR + zone (data sources). |
| 4 | `repo-config` | wires GitHub Actions for this env: env-scoped `AZURE_CLIENT_ID`/`TFVARS` (deploy) + repo-level shared vars + drift inputs |
| 5 | `image` | builds the app image **in the shared ACR** (`az acr build`, no local Docker) at this env's `newTag` |
| 6 | `cluster` | kubeconfig; ext_authz provider; namespace label; BYO public IP; cert-manager (custom-domain only) + **ESO** + **Argo CD** (its `driftscope-deploy-config` carries `DRIFTSCOPE_ENV=<env>` so its CMP renders `overlays/<env>`) |
| 7 | `deploy` | renders `overlays/<env>` + applies the app + ESO `SecretStore`/`ExternalSecret`, mesh hardening (STRICT mTLS + default-deny), the SSO gate, and the edge (origin cert + `X-Azure-FDID` lock); prints the URL |

The oauth2-proxy credentials (client id/secret + a Terraform-generated cookie secret) live in Key
Vault and are pulled into the cluster by External Secrets via Workload Identity — there is **no**
manual `create secret` step. (`make secret` remains only as a fallback for clusters without ESO.)

Unattended infra: `make up ENV=<env> AUTO=-auto-approve` (otherwise `terraform apply` prompts once).

---

## Custom domain: one-time delegation (shared)

If you set `base_domain` in the shared tfvars, the **shared** zone `driftscope.<base_domain>` is
delegated **once** — it is long-lived and never changes on an env teardown:

```sh
make shared-up
terraform -chdir=terraform/shared output dns_name_servers   # add ONE `driftscope` NS record at your registrar/Cloudflare
```

Front Door's managed cert (browser leg) and cert-manager's DNS-01 (origin leg) both need the
delegation live. Give it a few minutes to propagate, then stand up each env (`make up ENV=<env>`);
`deploy` waits on the origin certificate (5-min timeout) and warns — rather than failing — if DNS
hasn't propagated yet; re-run `make deploy ENV=<env>` once it has.

---

## After it's up

- **URL**: `make url` (or the last line of `make deploy`).
- **Access is group-gated.** Only members of the Entra access group (`allowed_group_name`,
  default `driftscope`) get in; everyone else sees the no-access page. `admin_members` seeds it
  with you — add others in Entra or via `azuread_group_member`.
- **Logs / debugging**: see [`design-observability.md`](design-observability.md) and
  [`runbook-edge-to-app.md`](runbook-edge-to-app.md).

## Teardown

```sh
make down ENV=dev     # destroy ONE env (its AKS, KV, Front Door, Entra app+group, DNS records)
                      # -- the SHARED ACR + DNS zone (+ registrar NS delegation) are untouched
make shared-down      # DANGER: destroy the shared ACR (all images) + DNS zone -- full decommission only
```

---

## Notes on state & secrets

- Terraform **state is remote and per-env**: one shared Azure Storage account, isolated keys
  (`driftscope-<env>.tfstate` + `shared.tfstate`), AAD-only/keyless, versioned + soft-deleted. `make
  state` provisions it and writes a `backend.hcl` (gitignored) into each root. State holds the Entra
  client secret and is never committed; so are the per-env `*.tfvars`. See [`remote-state.md`](remote-state.md).
- The `oauth2-proxy` Secret lives only in the cluster, synced from Key Vault by External Secrets
  (Workload Identity, read-only). It is never written to disk or git, and no human handles the values.
- The image registry (`newName`) is bound at render time from the shared ACR (ADR-0001); the image
  **tag** is the per-env `overlays/<env>/kustomization.yaml` `newTag`, moved by the promotion pipeline
  — not by hand. See [`runbook-promotion.md`](runbook-promotion.md).

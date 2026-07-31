# Quickstart: clone → running on Azure

Stand up your **own** DriftScope — your Azure subscription, your Entra tenant, your domain (or
none) — with two files of config and one command. This is Phase 0.5 of the business-readiness
roadmap: the reproducible, deliverable package.

```sh
git clone <this repo> && cd driftscope
cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # fill it in (below)
az login
make up
```

`make up` prints the URL when it's done. Open it, sign in with an account in the access group,
and you're in.

---

## Prerequisites

**Tools** (all on PATH): `az`, `terraform`, `kubectl`, `helm`, `kustomize`, `jq`, `openssl`,
`git`. `make preflight` checks these and fails early if anything's missing.

**Azure**: a subscription you can create resources in, and rights in your Entra tenant to
create an app registration + security group (Application Administrator, or equivalent). `az login`
first; the active subscription is used unless you set `subscription_id` in tfvars.

**Cost**: roughly ~$150–250/mo left running (1× B4ms AKS node, Front Door Standard ~$35, ACR
Basic, Key Vault, a public IP). `make down` destroys all of it.

---

## Configure (`terraform.tfvars`)

Minimum:

```hcl
subscription_id   = "<your-sub-guid>"
acr_name          = "driftscopeacr12345"   # globally unique, alphanumeric
key_vault_name    = "driftscope-kv-12345"  # globally unique
gateway_dns_label = "driftscope-gw-12345"  # unique per region
admin_members     = ["you@your-tenant.onmicrosoft.com"]   # added to the access group so you can log in
```

Leave `base_domain` unset to run on Front Door's `*.azurefd.net` hostname — no domain, no DNS
delegation, HTTP on the internal Front Door→origin leg (see
[`design-edge-origin-cert.md`](design-edge-origin-cert.md)). Set it (e.g. `base_domain = "acme.com"`)
to serve at `driftscope.acme.com` with a real cert end-to-end. All other settings have defaults
(see the example file).

---

## What `make up` does

Each step is also a standalone, re-runnable target (`make <step>`):

| Step | Target | What |
|---|---|---|
| 1 | `preflight` | tools + `az login` + tfvars present |
| 2 | `infra` | `terraform apply`: AKS (managed Istio, external gateway, Cilium), ACR, Key Vault, Entra app+group, DNS + Front Door + WAF. Generates `params.yaml` and the `edge/` manifests. |
| 3 | `image` | builds the app image **in ACR** (`az acr build`, no local Docker) and points the overlay at your registry |
| 4 | `cluster` | kubeconfig; registers the ext_authz provider (+ istiod restart); labels the namespace for sidecar injection; attaches the BYO public IP; installs cert-manager (custom-domain only) + **External Secrets Operator** |
| 5 | `deploy` | applies the app + the ESO `SecretStore`/`ExternalSecret` (which syncs the `oauth2-proxy` Secret from Key Vault — no human touches it), mesh hardening (STRICT mTLS + default-deny), the SSO gate, and the edge (origin cert + `X-Azure-FDID` lock); prints the URL |

The oauth2-proxy credentials (client id/secret + a Terraform-generated cookie secret) live in Key
Vault and are pulled into the cluster by External Secrets via Workload Identity — there is **no**
manual `create secret` step. (`make secret` remains only as a fallback for clusters without ESO.)

Unattended infra: `make up AUTO=-auto-approve` (otherwise `terraform apply` prompts once).

---

## Custom domain: one extra step

If you set `base_domain`, delegate the zone **after `make infra`** and before the cert can issue:

```sh
make infra
terraform -chdir=terraform output dns_name_servers   # add these 4 NS records at your registrar
```

Front Door's managed cert (browser leg) and cert-manager's DNS-01 (origin leg) both need the
delegation live. Give it a few minutes to propagate, then run the rest:

```sh
make image cluster secret deploy
```

`deploy` waits on the origin certificate (5-min timeout) and warns — rather than failing — if
DNS hasn't propagated yet; re-run `make deploy` once it has.

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
make down            # terraform destroy -- removes AKS, ACR, KV, DNS, Front Door, Entra app+group
```

---

## Notes on state & secrets

- Terraform **state is written locally** (`terraform/terraform.tfstate`) and is **gitignored** —
  it contains the Entra client secret value and must never be committed. So is `terraform.tfvars`.
  For teams/CD, move state into Azure Storage (shared, locked, versioned) — opt-in, see
  [`remote-state.md`](remote-state.md). (Secrets → External Secrets is the next 0.6 step.)
- The `oauth2-proxy` Secret lives only in the cluster, synced from Key Vault by External Secrets
  (Workload Identity, read-only). It is never written to disk or git, and no human handles the
  values.
- `make image` edits the overlay's `kustomization.yaml` to your registry; that's a local change,
  not something to commit (`git checkout deploy/overlays/cloud/kustomization.yaml` to reset).

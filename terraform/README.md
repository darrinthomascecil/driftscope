# Terraform — the Azure footprint (Phase 0.2)

Provisions the cloud substrate DriftScope runs on, as code, so it's reproducible: resource
group, **AKS** (Overlay CNI with **Cilium network policy** — the fix for the inert egress
policy, managed Istio **external** ingress, Workload Identity + OIDC), **ACR**, **Key Vault**
(RBAC + soft-delete + purge-protection), and the AcrPull role. It also **generates**
`deploy/overlays/cloud/params.yaml` and the `edge/` manifests from your values so the app
manifests need no hand-editing.

Scope: the Azure layer (`main.tf`), **Entra** (`entra.tf`, Phase 0.3), and the **edge** —
**Azure DNS** (`dns.tf`) + **Azure Front Door Standard** (`frontdoor.tf`, Phase 0.4): managed
TLS (browser-facing, no cert-manager) + WAF + DDoS + CDN, in front of the Istio gateway. This
replaces the original Cloudflare tunnel — the deployment is now all-Azure. The gateway-side
wiring (external gateway on the BYO IP; **cert-manager DNS-01** origin cert for `origin.<domain>`
via `certmanager.tf` + generated `edge/`; `X-Azure-FDID` origin lock; remove cloudflared) is
Phase 0.4b in the kustomize overlays — see `docs/design-edge-origin-cert.md`. In-cluster mesh/SSO
objects are the existing overlays + `mesh/`.

### Domain: bring your own, or don't
- **No custom domain (default):** leave `base_domain` unset. The app runs on Front Door's
  `*.azurefd.net` hostname — no domain ownership, no Azure DNS zone, no registrar delegation.
  The URL is the `app_url` output. Ideal for demos / clone-and-run.
- **Custom domain:** set `base_domain` (e.g. `acme.com`) → app at `driftscope.acme.com` with a
  managed cert + an Azure DNS zone. After apply, delegate by adding the four `dns_name_servers`
  (output) at your registrar.

The chosen host flows automatically into the Entra redirect URI, `params.yaml`, and oauth2-proxy.

## Structure (Phase 0.6d: module + per-env roots + shared)

```
terraform/
  shared/                    # applied ONCE (make shared-up): the ACR + DNS zone every env shares
  modules/driftscope/        # the per-env footprint, parameterized by var.environment; the shared
                             #   ACR + zone are DATA SOURCES here (data.tf), never recreated
  envs/{dev,staging,prod}/   # each is a Terraform ROOT: provider + backend(key=driftscope-<env>.tfstate)
                             #   + a module call (environment = "<env>") + <env>.tfvars + outputs
```

Why module + env-roots (not workspaces): separate state, backend, and tfvars per env, and an explicit
`terraform -chdir=terraform/envs/prod apply` — no hidden `workspace select` that silently retargets
prod. See `docs/design-multi-env-0.6d.md`.

## Use

Prefer the Makefile (it wires state, the shared layer, and the per-env flow):

```sh
make shared-up            # ONCE: shared ACR + DNS zone + remote state backend
make up ENV=dev           # a whole environment end-to-end (default ENV=dev; also staging, prod)
```

Raw Terraform, per root:

```sh
# shared (once):
terraform -chdir=terraform/shared init -backend-config=backend.hcl
terraform -chdir=terraform/shared apply

# an environment:
terraform -chdir=terraform/envs/dev init -backend-config=backend.hcl
terraform -chdir=terraform/envs/dev apply -var-file=dev.tfvars   # ~10 min for AKS
```

## What it deliberately changes vs. the hand-built cluster
- **`network_policy = cilium`** (was `none`) — so NetworkPolicies actually enforce (closes
  stress-test finding T2, and enables Hubble later in Phase 1).
- **Key Vault purge-protection on** (was off). Note the teardown/rebuild gotcha: purge protection
  reserves a destroyed vault name for 7 days — `make up` runs `scripts/bootstrap/kv-recover.sh` first.
- Node count/size and k8s version are variables — `node_count >= 2` for HA (stress-test finding).

## Notes / follow-ups
- **State** is remote + per-env: one shared Azure Storage account, isolated keys, keyless (AAD). See
  `docs/remote-state.md`. `make state` provisions it and writes each root's `backend.hcl` (gitignored).
- **Migration** from the pre-0.6d single state: `terraform state rm` the ACR + DNS zone (+ records)
  from the old `driftscope.tfstate`, then `terraform import` them into `terraform/shared`, so a later
  env destroy can't delete the shared registry/zone. Verify the registrar NS is unchanged.
- **Acceptance test** = `make up ENV=dev` into a *fresh* subscription (costs money) proving a clone
  stands up from scratch; run deliberately, not in CI.

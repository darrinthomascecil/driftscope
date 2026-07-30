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

## Use

```sh
cd terraform
cp terraform.tfvars.example terraform.tfvars   # fill in: subscription, base_domain, acr/kv names, group id
terraform init
terraform plan        # review what will be created
terraform apply       # ~10 min for AKS

# then wire kubectl + deploy the app:
az aks get-credentials -g $(terraform output -raw resource_group) -n $(terraform output -raw cluster_name)
# set the ACR in kustomization.yaml to: terraform output -raw acr_login_server
# create the oauth2-proxy secret from Key Vault (see deploy/overlays/cloud/oauth2-proxy.yaml)
kubectl apply -k ../deploy/overlays/cloud
kubectl apply -f ../deploy/overlays/cloud/mesh/    # the cluster-scoped mesh objects
```

## What it deliberately changes vs. the hand-built cluster
- **`network_policy = cilium`** (was `none`) — so NetworkPolicies actually enforce (closes
  stress-test finding T2, and enables Hubble later in Phase 1).
- **Key Vault purge-protection on** (was off).
- Node count/size and k8s version are variables — `node_count >= 2` for HA (stress-test finding).

## Notes / follow-ups
- **State** is local for now. For a team, move to a remote `azurerm` backend (see `versions.tf`).
- **Acceptance test** = `terraform apply` into a *fresh* subscription/RG (costs money) proving a
  clone stands up from scratch; run deliberately, not in CI.
- Entra `allowed_group_id` is an input here; Phase 0.3 will create the group and feed its id in.
- The generated `params.yaml` overwrites the committed one identically when tfvars match this
  deployment; new deployers get their own.

# DriftScope business-readiness roadmap

A multi-step project to take DriftScope from an impressive-but-hand-built artifact to an
operable, sellable, trustworthy product. **Safety, security, and best practices are the
governing constraints, not afterthoughts.**

## North star: reproducibility

> **Anyone should be able to clone this repo and stand up their own DriftScope, in their own
> domain, with the least friction possible.**

Target experience (the goal we build toward):
```sh
git clone https://github.com/<you>/driftscope && cd driftscope
cp config.example.tfvars config.tfvars     # set: your domain, Azure subscription, Cloudflare token, admin group
make up                                     # provision + deploy + wire DNS/SSO
# -> https://driftscope.<your-domain> is live, gated to YOUR Entra group
```
Nothing about the running system should be special to *this* deployment. Today it is riddled
with deployment-specific values — those are the first thing to fix.

### What blocks reproducibility today (grounded)
- **Everything is hand-built.** AKS, ACR, Key Vault, the Entra app *and* group, Cloudflare
  tunnel/DNS were created by `az`/`gh`/portal. No Infrastructure-as-Code. If the cluster dies,
  there is no button to rebuild it.
- **Hardcoded deployment values** in `deploy/` (`justmakeitgo.net`, `dscopeacr18860`,
  `dscopeclddev01`, tenant `f493fd7e…`, app `2fe61316…`, group `82cb7474…`). A second deployer
  can't reuse these; they must become parameters.
- **Imperative deploys.** Changes applied by hand (`kubectl apply`), not reconciled from git —
  not reproducible, not auditable, error-prone (stray-namespace mistakes happened twice).
- **Manual secrets.** Key Vault → `kubectl create secret` by hand; no automation, no rotation.

---

## Phase 0 — Reproducibility (current focus)

Goal: **clone → your own DriftScope in your own domain**, fully from code. This phase is itself
multi-step:

### 0.1 Parameterize (de-hardcode)
Extract every deployment-specific value into **one config surface** (a `tfvars` + a generated
kustomize/env layer). Parameters: base domain, Azure subscription/region/resource-group names,
ACR name, Key Vault name, Entra tenant id + app + group, Cloudflare account/zone/token.
Files to de-hardcode: `deploy/overlays/cloud/{oauth2-proxy.yaml,istio-routing.yaml,kustomization.yaml}`,
`deploy/overlays/kind/config.env`.

### 0.2 Terraform the cloud footprint (`azurerm`)
AKS (Overlay CNI, **network-policy engine on** — fixes the inert NetworkPolicy, managed Istio,
Workload Identity, OIDC issuer), ACR, Key Vault (soft-delete/purge protection), managed
identities + federated credentials, RBAC/role assignments. Everything we built by hand, as code.

### 0.3 Automate Entra (`azuread`)
App registration (redirect URIs derived from the domain), the access **group**, `groupMembershipClaims`,
the client secret into Key Vault, federated credentials. So a new tenant gets its own app + group
without portal clicks.

### 0.4 Terraform Cloudflare (`cloudflare`)
Tunnel, DNS (wildcard/subdomain from the domain param), and — decided — the edge config, so the
public hostname is derived from the deployer's domain, not hardcoded.

### 0.5 Bootstrap: clone → running
A `make up` / bootstrap script + a crisp README that runs 0.2–0.4, deploys the app, injects
secrets, and prints the live URL. This is the deliverable that makes the north star real.

### 0.6 GitOps/CD + secrets automation
ArgoCD or Flux reconciling the overlays (retire imperative `kubectl`); **External Secrets
Operator** / CSI Secrets Store pulling from Key Vault (retire manual `create secret`); real
dev/staging/prod separation. This is what makes *ongoing* change reproducible and safe, not just
the first install.

**Phase 0 done when:** a fresh clone + a filled-in config produces a working, SSO-gated DriftScope
on a *different* domain/tenant with no manual portal/CLI steps, and day-2 changes flow git → CD.

---

## Later phases (summary; each becomes its own tracked effort)

**Phase 1 — Security hardening.** Enable Cilium (egress policy actually enforces; default-deny L3;
Hubble). Admission control (Kyverno/Gatekeeper + Pod Security `restricted`). Supply chain: image
scanning (Defender/Trivy), pin-by-digest, cosign signing + Ratify admission, SBOMs. RBAC
least-privilege + PIM/just-in-time + access reviews.

**Phase 2 — Product & data.** Database + org/multi-tenancy model + scan history + **audit log**
(who scanned what). Data-handling/privacy policy + retention (critical for a tool reading private
repos). Async job model for fleet scans.

**Phase 3 — Operability.** Durable observability: ship logs (Loki/Azure Monitor) + traces
(Tempo/App Insights via the `traceparent` already flowing) + exemplars; define SLOs; real alert
delivery + on-call. Backup/DR (Velero + *tested* restore, RTO/RPO). Governance: Standard-tier
control plane, ResourceQuota/LimitRange, budgets, tagging. (Also: multi-node/HA — tracked
separately per the stress-test findings.)

**Phase 4 — Assurance/compliance.** Security in CI (SAST, dependency/secret/container scanning),
pen test, DAST against staging. SOC2 trajectory: change management, access reviews, incident
runbooks, vendor list.

## Backlog (parked, each its own feature)
- Denied-login spike alert (page on a burst of `403 UAEX`).
- Network prevention plan from the stress tests: multi-node HA (PDBs + anti-affinity), NAT
  Gateway for SNAT, Cilium/Hubble.

## Principle
Reproducibility and auditable change come **first** because every other guarantee — security,
DR, compliance, safe iteration — depends on being able to rebuild the system from code and know
exactly what is running. Build the foundation, then compound on it.

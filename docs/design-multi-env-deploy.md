# Design: multi-environment deployment (kind locally, AKS in higher envs)

Status: **proposed** — reviewed, not yet implemented.
Related: `docs/design-github-app-auth.md`, and the live workload-identity setup
(`~/driftscope-wi/setup-oidc-kind.sh`, kept as a reference environment).

## Goal

One manifest set that deploys driftscope to **kind** for a fast local loop and to
**AKS** for higher environments (dev/prod), with the per-environment differences
isolated and the workload-identity story intact everywhere.

## The framing that makes this simple

Two things — and only two — vary across environments:

1. **Packaging / per-env config** (image source, identity client-id, Key Vault + App
   references, replicas, exposure). This is a Kustomize concern.
2. **One-time cluster setup** (standing up the OIDC issuer + workload-identity
   webhook, the UAMI, Key Vault, role grants, federated credential). This is `az`/IaC,
   done once per environment, not per deploy.

Everything else is identical. The **application needs no code changes** — it already
reads its configuration from the environment (`GITHUB_APP_*`, `GITHUB_APP_KEY_VAULT_URI`,
allow-hosts) and authenticates with the projected federated token. And the
**workload-identity contract is the same on kind and AKS**: a ServiceAccount labelled
`azure.workload.identity/use: "true"`, annotated with a UAMI client-id, whose subject
`system:serviceaccount:<ns>:<sa>` a federated credential trusts.

## Tooling: Kustomize base + overlays

driftscope is a single Deployment + Service + ServiceAccount with small per-env diffs,
so **Kustomize** fits better than Helm: it is built into `kubectl`, has no templating
language, and expresses "same base, small patches" directly. (Helm remains the
alternative if we later want packaging/versioning or a chart to publish; GitOps —
Argo CD/Flux watching the overlays — is the eventual hands-off endgame, not step one.)

```
deploy/
  base/                     # env-agnostic: the app + the WI contract
    deployment.yaml         #   image placeholder; SA; azure.workload.identity/use label;
                            #   env via envFrom -> the overlay's configMap; probes
    serviceaccount.yaml     #   azure.workload.identity/client-id annotation (patched)
    service.yaml            #   ClusterIP; exposure is added per overlay
    kustomization.yaml
  overlays/
    kind/
      kustomization.yaml    #   image driftscope:dev; replicas 1; namespace driftscope
      config.env            #   local UAMI/KV/App values
      patch-sa.yaml         #   SA client-id = local UAMI
    dev-aks/
      kustomization.yaml    #   image <acr>.azurecr.io/driftscope:<tag>; 2 replicas; ingress
      config.env
      patch-sa.yaml
      ingress.yaml
    prod-aks/
      kustomization.yaml    #   3+ replicas; HPA; tighter probes; raised grace period
      config.env
      patch-sa.yaml
      ingress.yaml
```

Per-env env vars come from a `configMapGenerator` (from `config.env`) consumed by the
base Deployment via `envFrom`; the SA client-id annotation and image/replicas come from
overlay patches. Deploy is then:

```bash
kubectl config use-context kind-driftscope-wi
kubectl apply -k deploy/overlays/kind          # fast local loop

kubectl config use-context <aks-context>
kubectl apply -k deploy/overlays/prod-aks      # higher env
```

A thin `deploy/deploy.sh <env>` can select the context + overlay so the operator names
an environment, not a kubeconfig.

## What each overlay changes

| Dimension | kind | AKS (dev / prod) |
|---|---|---|
| Image | `driftscope:dev` via `kind load docker-image` (no registry) | `<acr>.azurecr.io/driftscope:<tag>`, pulled from ACR |
| SA `client-id` annotation | local UAMI | that env's UAMI |
| Env (`GITHUB_APP_*`, `GITHUB_APP_KEY_VAULT_URI`, allow-hosts) | local KV/App | per-env KV/App |
| Replicas / resources / HPA | 1, minimal | 2–3+, HPA, requests+limits |
| Exposure | `kubectl port-forward` | Service + **Ingress + TLS** |
| Graceful drain / probes | defaults | raised `terminationGracePeriodSeconds`, tuned probes |

## The one-time cluster setup — where kind and AKS truly differ

The app-facing contract is identical, but standing up workload identity is **far
simpler on AKS**, because it is managed:

| | kind (already built) | AKS |
|---|---|---|
| OIDC issuer | self-managed: host JWKS in a storage account, patch api-server flags | **managed**: `az aks update --enable-oidc-issuer --enable-workload-identity` publishes the issuer |
| WI webhook | manual `helm install workload-identity-webhook` | **managed addon**, installed by the flag above |
| Federated-credential issuer | the storage-account URL | `az aks show --query oidcIssuerProfile.issuerUrl -o tsv` |
| Image pull | `kind load docker-image` | `az aks update --attach-acr <acr>` (AcrPull on the kubelet identity) |

So the AKS setup is ~5 `az` commands and **drops the JWKS-hosting and api-server-patching
entirely** — kind was the hard case. The proposed `deploy/aks/setup-aks-wi.sh` is the
AKS analog of the kind script, parameterized per environment:

1. `az aks update --enable-oidc-issuer --enable-workload-identity` (or at create time)
2. read the managed issuer URL
3. `az identity create` the env's UAMI; `az identity federated-credential create`
   (issuer = the AKS issuer, subject = `system:serviceaccount:driftscope:driftscope`,
   audience = `api://AzureADTokenExchange`)
4. grant the UAMI its roles: `Key Vault Crypto User` on the env's vault, plus ADO org
   membership / ACR pull as needed
5. `az aks update --attach-acr <acr>`

## Identity isolation (the design call)

Give **each environment its own UAMI + federated credential + Key Vault** — blast-radius
isolation, so a dev credential can never read prod's secrets or repos. The federated
*subject* stays `system:serviceaccount:driftscope:driftscope` everywhere; because each
cluster has a different issuer, **each env needs its own federated credential** on its
own UAMI. The GitHub App can be shared for dev but should be **per-env for prod** (a
separate App + Key Vault key), so prod's installation scope and key rotate independently.

## Secrets and config surface, per env

Nothing secret lives in the manifests. Per env, an overlay's `config.env` carries only
*references*: `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_KEY_VAULT_URI`,
`GITHUB_APP_KEY_NAME`, and allow-hosts. The actual secret (the App private key) stays in
that env's Key Vault, reached via that env's workload identity — exactly the model
`design-github-app-auth.md` describes. The SA client-id annotation is the only
identity-specific value, set by a per-overlay patch.

## Infra vs app — keep them separate

- **Infra (one-time per env; `az`/bicep/Terraform):** the AKS cluster + OIDC/WI enable,
  UAMI, Key Vault, role grants, federated credential, ACR attach.
- **App (repeatable; Kustomize):** `kubectl apply -k overlays/<env>`.

Keeping these apart means a routine deploy never touches identity/infra, and standing up
a new environment is "run the infra script once, then `apply -k`."

## Image flow

- **kind:** `docker build -t driftscope:dev .` → `kind load docker-image driftscope:dev`
  → overlay pins `driftscope:dev`. No registry.
- **AKS:** `az acr build -t driftscope:<tag>` (or build + push) → overlay pins
  `<acr>.azurecr.io/driftscope:<tag>`. **Promote the same digest** dev→prod rather than
  rebuilding, so prod runs exactly what dev validated.

## Ingress on AKS: Application Gateway for Containers (AGC)

For the AKS environments, expose driftscope through **Application Gateway for Containers
(AGC)** — Azure's managed L7 load balancer for Kubernetes, the modern successor to the
classic Application Gateway Ingress Controller (AGIC). It fits driftscope for three
reasons:

- **Managed and scalable**, Azure-native, and it implements the **Kubernetes Gateway
  API** (`Gateway` + `HTTPRoute`) via the in-cluster **ALB Controller** — not the legacy
  Ingress-only AGIC.
- **Workload-identity native:** the ALB Controller authenticates to Azure with a managed
  identity + federated credential — the *same* pattern we already stood up for
  driftscope, so the whole cluster's Azure access stays consistent and secretless.
- **Per-route configurable timeouts**, which is the decisive advantage over the classic
  App Gateway / Container Apps *fixed* caps for driftscope's long scans and SSE.

### Gateway API resources (these replace `Ingress` in the AKS overlays)
AGC speaks the Gateway API, so the AKS overlays expose driftscope with:
- a **`Gateway`** referencing the AGC, with listeners (HTTP→HTTPS redirect, HTTPS with
  TLS — the cert sourced from **Key Vault**, tying into the vault we already use),
- an **`HTTPRoute`** routing `/`, `/scan`, `/ui`, `/healthz` to the driftscope Service,
- a **health-check policy** and a **timeout policy** on the `/scan` route sized for scans.

So `overlays/*-aks/` carry `gateway.yaml` + `httproute.yaml` instead of `ingress.yaml`;
the `kind` overlay keeps `port-forward` and needs none of this.

### Timeouts — the driftscope-specific bit
AGC lets you set the **request timeout per route** (unlike the classic App Gateway's
rigid cap), and its Envoy data plane streams SSE. So:
- size the `/scan` route timeout generously for a single-repo scan, and keep the SSE
  **heartbeats** (15s) driftscope already emits so an idle window never cuts a live scan;
- for the **fleet / 100-repo** case that can exceed any sane request timeout, this still
  points at the **async job model** (POST → `202` + job id, short poll/stream requests):
  AGC gives more headroom than Container Apps but is not a substitute for making long
  work asynchronous.

(Confirm the exact configurable maximum against current AGC docs at implementation time —
the design point is that it is *tunable per route*, not fixed.)

### One-time setup (per AKS env), on top of the workload-identity setup
1. Install the **ALB Controller** (helm) with its **own managed identity + federated
   credential** (federate the controller's ServiceAccount) — the same WI pattern as
   driftscope, so nothing new conceptually.
2. Provision the AGC — **managed** (the ALB Controller creates it from an
   `ApplicationLoadBalancer` resource) or **bring-your-own** (create it in Azure, then
   reference it from the `Gateway`).
3. Apply the `Gateway` (+ TLS from Key Vault), the `HTTPRoute`, and the timeout/health
   policies. Optionally attach a **WAF policy** for prod.

### Why AGC over the alternatives
| | nginx ingress | classic App Gateway + AGIC | **AGC** |
|---|---|---|---|
| Model | in-cluster, self-managed | managed, Ingress API | **managed, Gateway API** |
| Timeouts | idle-based (SSE-friendly) | rigid, short-ish cap | **configurable per route** |
| Azure identity | — | AGIC managed identity | **workload-identity native** |
| Status | ubiquitous, cheap | legacy, being superseded | **current Azure direction** |

Use **AGC** for the AKS envs; **nginx** stays a fine lighter-weight option for a dev
cluster if you want less setup.

## Two identities per cluster: app vs platform (least-privilege)

A cluster running driftscope behind AGC has **two** workload identities, each scoped to
its own job. Same mechanism (a UAMI + federated credential + projected token, no stored
secret); different subject and, crucially, different Azure roles. They are never shared —
the whole point is that neither can do the other's job.

| | driftscope (app) | ALB Controller (platform) |
|---|---|---|
| Workload | the driftscope pod | the ingress controller (`azure-alb-system` ns) |
| Federated subject | `system:serviceaccount:driftscope:driftscope` | `system:serviceaccount:azure-alb-system:alb-controller-sa` |
| Job in Azure | reads repos, **signs the GitHub App JWT in Key Vault**, pulls from ACR, talks to ADO | **programs the AGC load balancer** — frontends, routes, backends, subnet association |
| Azure roles | `Key Vault Crypto User` (the App key), `AcrPull`, ADO org membership | `AppGw for Containers Configuration Manager` (the AGC RG/resource), `Network Contributor` or `…/subnets/join/action` (the AGC subnet), `Reader` (the RG) |
| Blast radius if compromised | read repos + sign with one KV key | reconfigure one load balancer + join one subnet |

The boundaries do not overlap, deliberately: the **app** identity has **no** networking
rights (it cannot touch the load balancer), and the **platform** identity has **no**
access to Key Vault, ACR, repos, or any secret (it cannot read app data). A compromise
of either is contained to its own narrow job.

Both are **per environment** (see *Identity isolation*): dev and prod each get their own
app UAMI *and* their own ALB-Controller UAMI, federated to their own cluster's issuer.
The ALB-Controller identity is a **one-time platform bootstrap** — part of installing AGC
on the cluster — that a driftscope deploy never touches.

(Exact AGC role names/scopes are still settling — confirm at implementation time; the
design invariant is fixed: **one identity per component, each scoped to only its own
resources.**)

## Implementation phases

1. **Refactor `deploy/k8s/` → `deploy/base/` + `deploy/overlays/{kind,dev-aks,prod-aks}`**,
   migrating the live kind config into the `kind` overlay so nothing regresses.
2. **`deploy/aks/setup-aks-wi.sh`** — the AKS one-time WI setup (managed OIDC), plus ALB
   Controller install + AGC provisioning (its own managed identity + federated credential).
3. **AGC exposure in the AKS overlays** — `gateway.yaml` + `httproute.yaml` (+ TLS from
   Key Vault, `/scan` route timeout, health-check policy) in place of `ingress.yaml`.
4. **`deploy/README.md`** — the `apply -k` workflow and the per-env infra checklist.
5. Optional later: HPA on the AKS overlays; a `deploy.sh <env>` wrapper; a WAF policy for
   prod; GitOps (Argo CD/Flux) once the overlays are stable.

## Open questions

- **Per-env identities** (recommended) vs a shared UAMI across envs?
- **Per-env GitHub Apps** for prod, or one App installed per-env-scoped?
- ~~Ingress controller on AKS~~ — **resolved: Application Gateway for Containers (AGC)**;
  see the ingress section. (nginx remains a lighter-weight dev option.)
- Namespace: keep `driftscope` in every cluster (so the federated subject is identical),
  or per-env namespaces (which would change the subject and each federated credential)?

## Non-goals

- The async job model / ingress-timeout handling (its own follow-up; noted where the
  App Gateway cap bites).
- Provisioning the AKS cluster itself beyond enabling OIDC/WI (assumed to exist or be
  created by the infra script).

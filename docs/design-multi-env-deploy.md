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

## Implementation phases

1. **Refactor `deploy/k8s/` → `deploy/base/` + `deploy/overlays/{kind,dev-aks,prod-aks}`**,
   migrating the live kind config into the `kind` overlay so nothing regresses.
2. **`deploy/aks/setup-aks-wi.sh`** — the AKS one-time WI setup (managed OIDC).
3. **`deploy/README.md`** — the `apply -k` workflow and the per-env infra checklist.
4. Optional later: an HPA + Ingress/TLS example for the AKS overlays; a `deploy.sh <env>`
   wrapper; GitOps (Argo CD/Flux) once the overlays are stable.

## Open questions

- **Per-env identities** (recommended) vs a shared UAMI across envs?
- **Per-env GitHub Apps** for prod, or one App installed per-env-scoped?
- Ingress controller on AKS: **nginx** vs **Application Gateway** (App Gateway's ~240s
  request cap interacts with long scans — see the async-job-model follow-up).
- Namespace: keep `driftscope` in every cluster (so the federated subject is identical),
  or per-env namespaces (which would change the subject and each federated credential)?

## Non-goals

- The async job model / ingress-timeout handling (its own follow-up; noted where the
  App Gateway cap bites).
- Provisioning the AKS cluster itself beyond enabling OIDC/WI (assumed to exist or be
  created by the infra script).

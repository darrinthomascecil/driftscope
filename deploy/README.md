# Deploying the driftscope scan service

One Kustomize base with per-environment overlays: **kind** for a fast local loop, **AKS**
for higher environments. Design and rationale: [`docs/design-multi-env-deploy.md`](../docs/design-multi-env-deploy.md).

```
deploy/
  base/                     # env-agnostic: Deployment (hardened, workload-identity),
                            # ServiceAccount, Service, Namespace
  overlays/
    kind/                   # local: image driftscope:dev, the local UAMI/KV, 1 replica
    dev-aks/               # AKS: ACR image, per-env UAMI/KV, AGC ingress, 2 replicas
    prod-aks/              # AKS: promoted image, prod UAMI/KV, AGC ingress, 3 replicas
  aks/setup-aks-wi.sh       # one-time AKS workload-identity + Key Vault setup (per env)
```

Every environment shares the same identity contract: namespace + ServiceAccount are
`driftscope`, so the federated subject `system:serviceaccount:driftscope:driftscope` is
identical across clusters. Only the Azure resources differ per env (a `driftscope-<env>`
UAMI, a per-env Key Vault). The GitHub App is shared; its key is imported into each env's
own vault. Two identities per cluster — the driftscope **app** identity and the ALB
Controller **platform** identity — each least-privileged to its own job.

## Local (kind)

```bash
# 1. build + load the image (kind can't see your local Docker daemon)
docker build -t driftscope:dev .
kind load docker-image driftscope:dev --name <cluster>

# 2. deploy
kubectl apply -k deploy/overlays/kind

# 3. reach it
kubectl -n driftscope rollout status deploy/driftscope
kubectl -n driftscope port-forward deploy/driftscope 8081:8080 &
curl -s localhost:8081/healthz
```

The `kind` overlay assumes workload identity is already set up on the cluster (managed
identity + federated credential + Key Vault, per `docs/design-multi-env-deploy.md`). On a
plain cluster with no WI, the pod still runs — GitHub/ADO just degrade to `unknown`
(or supply a `driftscope-tokens` Secret with `GITHUB_TOKEN`/`AZURE_DEVOPS_TOKEN`).

### Updating after a code change (the tag gotcha)
Reusing `:dev` means k8s serves the **cached** image — a rebuild alone does nothing:
```bash
docker build -t driftscope:dev .
kind load docker-image driftscope:dev --name <cluster>
kubectl -n driftscope rollout restart deploy/driftscope
```

## Higher env (AKS)

```bash
# 1. one-time per env: workload identity + Key Vault + ACR (managed OIDC, ~a few az cmds)
ENV_NAME=dev AKS_CLUSTER=<cluster> ACR_NAME=<acr> KEYVAULT_NAME=<vault> \
  AZURE_TENANT_ID=<tenant> APPLY=true bash deploy/aks/setup-aks-wi.sh
#   (plus the ALB Controller + AGC bootstrap — its own identity; see the script's footer)

# 2. fill the overlay placeholders:
#    deploy/overlays/dev-aks/{kustomization.yaml (ACR image), config.env (tenant, vault),
#                             patch-sa.yaml (UAMI client-id), gateway.yaml (AGC id, TLS)}

# 3. build + push, then deploy
az acr build -r <acr> -t driftscope:<tag> .
az aks get-credentials -g <rg> -n <cluster>
kubectl apply -k deploy/overlays/dev-aks
```

Promote the **same image** dev→prod (`kubectl apply -k deploy/overlays/prod-aks` with the
tag/digest validated in dev) rather than rebuilding.

## Validate an overlay without applying
```bash
kubectl kustomize deploy/overlays/kind            # render the YAML
kubectl apply -k deploy/overlays/kind --dry-run=server   # validate against a cluster
```

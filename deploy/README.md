# Deploying the driftscope scan service

One Kustomize base (`overlays/cloud`, a REPLACE-templated template) with **thin per-environment
overlays** — `dev`, `staging`, `prod` (Phase 0.6d). Design: [`docs/design-multi-env-0.6d.md`](../docs/design-multi-env-0.6d.md);
promotion: [`docs/runbook-promotion.md`](../docs/runbook-promotion.md).

```
deploy/
  base/                     # env-agnostic: Deployment (hardened, workload-identity), SA, Service, Namespace
  overlays/
    cloud/                  # the REAL config as a REPLACE-* template: params, oauth2-proxy, ESO,
                            #   istio-routing, networkpolicy, forge-auth, mesh/, edge/
    dev/  staging/  prod/   # THIN overlays over ../cloud: image newName+newTag, replicas, an env label
  gitops/                   # Argo CD Application + the render CMP (render-overlay.sh) + argocd values
```

Every environment shares the same identity contract: the namespace + ServiceAccount are `driftscope`,
so the federated subject `system:serviceaccount:driftscope:driftscope` is identical across all three
clusters. Only the **Azure** resources differ per env (a `driftscope-<env>` UAMI, a per-env Key Vault,
a per-env Front Door + Entra app). The GitHub App is shared; its key is imported into each env's own
vault. (The edge is managed **Istio + Azure Front Door**, not AGC — the old
`docs/design-multi-env-deploy.md` AGC/Gateway-API sections are superseded.)

## How a deploy happens

You don't `kubectl apply` overlays by hand — **Argo CD** in each cluster reconciles its own env
(pull-based GitOps, ADR-0002). Each cluster's CMP runs `deploy/gitops/render-overlay.sh`, which binds
the `REPLACE-*` placeholders from that cluster's `driftscope-deploy-config` ConfigMap and builds
`overlays/$DRIFTSCOPE_ENV`. The env is selected **entirely** by that ConfigMap's `DRIFTSCOPE_ENV` key
— the Argo `Application`, CMP plugin, and values are byte-identical in every cluster.

Standing an environment up (bootstrap, one command):

```bash
make shared-up            # ONCE: shared ACR + DNS zone + state backend
make up ENV=dev           # dev end-to-end (default ENV=dev; also staging, prod)
```

## The image transformer lives in the env overlay

`overlays/cloud` carries **no** `images:` entry. Each env overlay sets the WHOLE transformer —
`newName: REPLACE-IMAGE-REPO` (the shared ACR, bound by `render-overlay.sh`) **and** `newTag` (the
promotion pointer). Splitting `newName`/`newTag` across base + overlay silently drops the tag: once
the base rewrites `newName`, the overlay's `name: driftscope` no longer matches. The CD pipeline
(`.github/workflows/deploy.yml`) moves each env's `newTag`:

- **push to main** → `overlays/dev/newTag = vX.Y.Z`
- **git tag `vX.Y.Z`** → `overlays/staging/newTag = vX.Y.Z@<digest>` (same image, no rebuild)
- **GitHub release** → `overlays/prod/newTag = vX.Y.Z@<digest>`

## Validate an overlay without applying

```bash
# Render exactly as Argo/the bootstrap do (all inputs are required; fail-closed on any REPLACE-* left):
HOST=dev.driftscope.example.com TENANT_ID=… ALLOWED_GROUP_ID=… APP_CLIENT_ID=… ESO_CLIENT_ID=… \
KEYVAULT=driftscope-kv-dev-… IMAGE_REPO=<acr>.azurecr.io/driftscope DRIFTSCOPE_ENV=dev \
  bash deploy/gitops/render-overlay.sh | less
```

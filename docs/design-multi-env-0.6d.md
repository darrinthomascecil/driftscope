# Design: Phase 0.6d — dev / staging / prod environment separation

> Status: **implemented**.
> Supersedes the AGC/Gateway-API sections of `docs/design-multi-env-deploy.md`.

## 1. Why

Before 0.6d, driftscope had **exactly one of everything**: one `overlays/cloud`, one Argo
`Application`, one flat Terraform state (`driftscope.tfstate`), one ACR/KV/AKS/Front Door, one
`gh-deploy` identity federated `environment:production`. Every merge to `main` bumped the single
`newTag` and deployed **straight to prod**. There was no pre-prod validation, no blast-radius
isolation (one vault signs the GitHub App JWT for private-repo scans), and "ship a tested version"
was the same action as "deploy latest main".

0.6d makes day-2 change flow through **gated, isolated environments**: **cluster-per-env** (3 isolated
AKS clusters), **build-once / promote-the-digest** up a dev → staging → prod ladder. It parameterizes
the existing render/deploy machinery rather than replacing it.

## 2. Topology

```
        git (main + tags + releases)                terraform/shared  (applied ONCE)
                 │                                   DNS zone · ACR · (+ state SA, imperative)
   merge→main    │  tag vX.Y.Z    release              │ AcrPull grant lives in each ENV module
   (dev newTag)  │  (staging)     (prod)               │ (env → data.shared, never shared → env)
        ▼        ▼                ▼                     ▼
   ┌── dev cluster ──┐   ┌─ staging cluster ─┐   ┌─── prod cluster ───┐
   │ AKS+Istio+FD    │   │  AKS+Istio+FD     │   │  AKS+Istio+FD      │
   │ KV·UAMIs·Entra  │   │  KV·UAMIs·Entra   │   │  KV·UAMIs·Entra    │
   │ Argo(own CMP)   │   │  Argo(own CMP)    │   │  Argo(own CMP)     │
   │ ConfigMap:      │   │  ConfigMap:       │   │  ConfigMap:        │
   │  DRIFTSCOPE_ENV │   │   DRIFTSCOPE_ENV  │   │   DRIFTSCOPE_ENV   │
   │  =dev → renders │   │   =staging        │   │   =prod            │
   │  overlays/dev   │   │   overlays/staging│   │   overlays/prod    │
   │ ns/SA: driftscope (invariant federated subject in all three)    │
   │ dev.driftscope. │   │ staging.drift…    │   │ driftscope.<domain>│
   └─────────────────┘   └───────────────────┘   └────────────────────┘
```

Env selection is carried **entirely by the `DRIFTSCOPE_ENV`** key in each cluster's
`driftscope-deploy-config` ConfigMap, which the CMP sidecar already reads via `envFrom`
(`deploy/gitops/argocd-values.yaml`). So **`application.yaml`, `argocd-cmp-plugin.configmap.yaml`, and
`argocd-values.yaml` are byte-identical in every cluster** — they diverge only by that one value.

## 3. Decisions

| Decision | Choice | Why |
|---|---|---|
| Topology | **Cluster-per-env** (3 AKS, each own FD/KV/identities) | Real blast-radius isolation; prod-like. The env lives in **Azure resource names**; ns/SA stay `driftscope`. |
| Promotion | **release/tag, digest-pinned, build once** | merge→dev, `git tag`→staging, GitHub release→prod. Prod runs exactly the digest dev+staging validated. |
| Terraform | **module + per-env root configs** (not workspaces) | Separate state/backend/tfvars + an explicit `-chdir=terraform/envs/prod apply` — no hidden `workspace select` that silently retargets prod. |
| Argo | **per-cluster Argo** (not a central ApplicationSet hub) | Reuses the existing bootstrap; keeps live values out of git (ADR-0001); no hub credential that reaches all clusters. |

**Isolation invariants:** each env has its **own UAMI + federated credential + Key Vault**; the k8s
namespace + ServiceAccount are `driftscope` everywhere, so the WI federated subject
`system:serviceaccount:driftscope:driftscope` is invariant — only Azure names carry the env. The
**GitHub App is shared** (one id/installation); its key is imported into each env's own vault, so only
`GITHUB_APP_KEY_VAULT_URI` differs and per-vault isolation still holds.

**Per-cluster Argo vs ADR-0002 phase 5.** ADR-0002's ApplicationSet endgame is per-**tenant** fan-out
*within* a cluster (BYO-forge identity). That is orthogonal to cross-**env** separation and remains
valid; per-cluster Argo does not contradict it.

## 4. Shared vs per-env

| Resource | Scope | Notes |
|---|---|---|
| DNS zone `driftscope.<base_domain>` | **Shared** (`terraform/shared`) | Delegated ONCE (one `driftscope` NS record). Per-env **records** live in each env's state. |
| ACR | **Shared** | Build once, promote the digest — every env's kubelet has `AcrPull` (grant in the env module, scoped to the shared ACR). |
| tfstate storage account | **Shared, per-env keys** | `driftscope-<env>.tfstate` + `shared.tfstate`. Created imperatively by `state-backend.sh` (a backend can't manage its own storage). |
| RG, AKS, KV, all UAMIs+fedcreds, Front Door, Entra app+group | **Per-env** | Full isolation; invariant subject, per-env names (`driftscope-<env>-*`). |

## 5. Terraform structure

```
terraform/
  shared/                    # applied ONCE (make shared-up): DNS zone + ACR (+ state SA is imperative)
  modules/driftscope/        # the per-env footprint, parameterized by var.environment; ACR + zone are
                             #   DATA SOURCES (data.tf) into the shared RG, never recreated
  envs/{dev,staging,prod}/   # root: providers + backend(key=driftscope-<env>.tfstate) + module call
                             #   (environment = "<env>") + <env>.tfvars + pass-through outputs
```

Every `${name_prefix}-*` name gains the env token (`driftscope-<env>-aks`, `-<env>-app/-eso/-gh-deploy`,
`-<env>-fd/-ep/-gw-pip`, `driftscope-kv-<env>-…`). `local.host` is per-env: prod at the zone **apex**
(`driftscope.<domain>`), dev/staging at `<env>.driftscope.<domain>`. The `gh-deploy` federated subject
is `environment:<github_environment>` (`prod`→`production`). The AcrPull grant lives in the env module
(scope = shared ACR data source, principal = this env's kubelet) so the dependency runs env→shared
only.

**Migration ordering (critical):** the current `driftscope.tfstate` owns the live ACR + DNS zone,
and both currently sit in the `driftscope-cloud` RG — but `terraform/shared` expects them in a **new
`driftscope-shared` RG**. So a bare `terraform import` would plan a `resource_group_name` change →
**replacement** (new zone = new name servers = broken Cloudflare delegation; recreated ACR = lost
images). The correct order is: `terraform state rm` the ACR + zone (+ their records) from the old
state, **`az resource move` them into `driftscope-shared`** (same subscription ⇒ name servers +
images preserved), *then* `terraform import` into `terraform/shared`, verify NS unchanged, and only
after new prod is cut over, decommission old prod with `az group delete` (not a Terraform destroy —
the flat config is gone). Because prod is at the zone **apex**, the prod DNS cutover has an inherent
few-minutes outage. **The full, exact, rollback-covered procedure is `docs/runbook-prod-migration.md`
— follow it, not this paragraph.**

## 6. Overlays, render, promotion pointer

- `overlays/cloud` stays the REPLACE-templated base (all real config). Thin
  `overlays/{dev,staging,prod}` `resources: [../cloud]` and set **only** the image `newTag`, replicas,
  and an env label.
- The **whole image transformer lives in the env overlay** (both `newName: REPLACE-IMAGE-REPO` and
  `newTag`). Splitting `newName`/`newTag` across base+overlay silently drops the tag — once the base
  rewrites `newName`, the overlay's `name: driftscope` no longer matches. `render-overlay.sh` seds
  `REPLACE-IMAGE-REPO` in the **env** overlay and `kustomize build`s `overlays/$DRIFTSCOPE_ENV`.
- **Digest-pinned promotion:** dev's `newTag` is the built `vX.Y.Z`; staging/prod get
  `vX.Y.Z@sha256:<digest>` (kustomize renders `repo:vX.Y.Z@sha256:…`, a valid pinned ref). Because the
  ACR is **shared**, the digest is already pullable everywhere — **no `az acr import`** on promotion.

## 7. CD — build once, promote thrice (`.github/workflows/deploy.yml`)

| Trigger | Mode | Action | GitHub Environment |
|---|---|---|---|
| push to main | build | build+sign; bump `overlays/dev/newTag = vX.Y.Z` | `dev` |
| push tag `vX.Y.Z` | promote | resolve digest from ACR; set `overlays/staging/newTag = vX.Y.Z@<digest>` | `staging` |
| release published | promote | set `overlays/prod/newTag = vX.Y.Z@<digest>` | `production` |
| workflow_dispatch | test | build a throwaway sha image; not promoted | — |

Each env is a **GitHub Environment** federated to its own `driftscope-<env>-gh-deploy` identity
(`vars.AZURE_CLIENT_ID` resolves environment-scoped), so one env's job can't authenticate to another's
Azure. A version can only be promoted after it was **built** (its digest must exist in the shared ACR).
A single `concurrency` group serializes the pointer-bump push to main across all triggers. Doc-only
main merges are skipped deterministically in-step (no `paths-ignore`, which can suppress tag pushes).

**Drift** (`terraform.yml`) runs one plan **per env** (matrix). It does **not** reference a GitHub
Environment — a scheduled job referencing `production` would pause on required-reviewers protection —
so it uses repo-level, env-suffixed inputs (`AZURE_CLIENT_ID_<ENV>`, `TFVARS_<ENV>`) and the per-env
state key. Wired by `make repo-config ENV=<env>`.

## 8. Bootstrap / Makefile

- `make shared-up` — ONCE: state backend + `terraform/shared` (ACR + zone).
- `make up ENV=<env>` (default dev) — preflight → state → **kv-recover** → infra (env root) →
  repo-config → image → cluster → deploy. `ENV` selects the env root (its state key) + `DRIFTSCOPE_ENV`.
- `make down ENV=<env>` tears down one env (its DNS records, not the shared zone). `make shared-down`
  is a guarded, rarely-wanted decommission.
- `lib.sh` keeps `OVERLAY=cloud` for the shared `mesh/` + `edge/` sources; only the **app render** is
  env-specific. `TF()` targets `terraform/envs/$ENV`.

## 9. Risks & gotchas

- **Key Vault purge protection** (7-day soft-delete) × 3 envs vs teardown/rebuild: a same-name rebuild
  fails until recovered → `scripts/bootstrap/kv-recover.sh` runs `az keyvault recover` before `infra`.
- **CD race across 3 triggers**: mitigated by the single `concurrency` group + `[skip ci]` +
  `GITHUB_TOKEN`-doesn't-retrigger.
- **Managed Istio per cluster**: keep staging == prod `istio_revision` before promoting mesh changes.
- **Front Door custom-domain validation** is per-env + async (three `_dnsauth*` TXT + managed certs).
- **cert-manager DNS Zone Contributor** is granted per-env on the **shared** zone (Azure DNS RBAC has
  no per-record-prefix grant) — bounded by the zone holding only driftscope records + per-env identity.
- **Cost**: ~3× AKS + 3× Front Door + 3× vault; shared ACR/DNS/tfstate blunts it. Lower envs run
  `node_count=1` + Free control plane; `az aks stop` staging+prod when idle keeps ~1 warm.

## 10. Verification

- **Per env**: `terraform -chdir=terraform/envs/<env> plan` clean; app reachable at its host, SSO to
  that env's Entra app, ESO syncs its vault, a private-repo scan KV-signs from that env's vault.
- **Promotion**: merge → dev rolls the digest; `git tag vX.Y.Z` → staging on the **same** `@sha256`;
  release → prod on that digest; ACR shows **one** build.
- **Isolation**: dev's `gh-deploy` OIDC can't auth to staging/prod; dev's app identity can't read
  staging's vault.
- **Reproducibility**: `make down ENV=dev` then `make up ENV=dev` reconstructs dev; the shared zone +
  NS delegation are unchanged.

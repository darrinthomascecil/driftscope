# ADR 0002 — GitOps deployment with Argo CD

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** platform engineering
- **Builds on:** ADR 0001 (deployment configuration & secret management)

## Context

ADR 0001 established the configuration model: the git repo is a **template** (placeholders only),
real per-deployment values are **injected at deploy time**, and secrets come from Key Vault via ESO +
Workload Identity. Deployment is currently **push-based**: a GitHub Actions pipeline builds the image,
renders the overlay, and runs `kubectl apply`.

That push model has three gaps for an enterprise-ready posture, all demonstrated during this project:

1. **No drift detection or auto-heal.** When the pipeline once applied `REPLACE-*` placeholders to the
   ServiceAccount and ESO SecretStore, Workload Identity and External Secrets broke **silently** — the
   pods kept running on cached tokens and nothing flagged the divergence. A reconciler would have caught
   it (drift) or refused it (invalid).
2. **Imperative `kubectl` from CI.** The pipeline holds cluster-admin credentials and mutates the cluster
   directly. There is no single, auditable record of "what is deployed" beyond the mutable cluster.
3. **Cluster state can drift from git** with no continuous correction — a manual `kubectl edit` persists.

## Decision

Adopt **Argo CD** as the deployment mechanism: **pull-based GitOps** with continuous reconciliation,
drift detection, and auto-heal.

The one design question GitOps forces — *Argo reconciles what is in git, but our git is a template; where
do the real values come from?* — is answered without abandoning ADR 0001:

> **Argo CD renders the overlay via a Config-Management Plugin (CMP) that resolves the `REPLACE-*`
> placeholders from Azure at sync time**, using Argo's own Workload Identity. The CMP runs the *same*
> render logic the deploy pipeline runs today — it is simply moved from CI (push) to Argo (sync).

Consequences of that choice:

- **The repo stays a pure template.** No live identifier is committed, and no second "rendered
  manifests" config repo is introduced.
- **Argo becomes both renderer and reconciler.** Every sync re-resolves the values and reconciles the
  cluster to match — so a drifted SA annotation (the exact past break) is detected and healed.
- **Secrets are untouched.** ESO + Workload Identity remain the secret path; Argo never handles secrets.

### Placement and lifecycle

Installing Argo is the **GitOps bootstrap problem**: Argo deploys everything via GitOps, but *something*
must install Argo *before* GitOps exists, and that something is inherently not GitOps. Make that step
minimal and imperative, then let Argo take over.

- **Argo CD is installed by the bootstrap** (`scripts/bootstrap/cluster-init.sh`, `helm upgrade --install`),
  right alongside cert-manager and ESO — the project's existing pattern for day-0 cluster controllers.
  It is **not** installed via Terraform: Terraform stays pure (Azure infrastructure + outputs, never the
  Kubernetes API, per ADR 0001), which also avoids the fragile Terraform Helm/Kubernetes providers
  (apply-time cluster dependency, CRD-ordering issues) for a controller that ships many CRDs.
- **Then Argo self-manages via app-of-apps.** After the minimal imperative install, Argo's own config
  (and progressively cert-manager/ESO, and Argo's own upgrades) becomes a git-tracked `Application`, so
  everything past the initial install is declarative GitOps.
- **Argo CD's own UI is behind the existing SSO edge** (Front Door → oauth2-proxy), same as the app and
  fleet dashboard.
- **The deploy pipeline's job shrinks** to: build + sign the image, and bump the tracked image tag. It
  no longer runs `kubectl apply`; Argo reconciles the change. CI stops holding cluster-admin.

## Alternatives considered

- **Separate private "config repo" (rendered manifests).** CI renders with real values and commits them
  to an access-controlled repo that Argo watches — the classic app-repo/config-repo split. **Rejected:**
  it puts live (if non-secret) identifiers in a git repo, softening ADR 0001's "nothing live in git," and
  adds a second repo to manage.
- **`argocd-vault-plugin` resolving from Key Vault.** Clean for secrets, but pushes non-secret config
  (host, client ids) into Key Vault as well. **Rejected:** Key Vault is for secrets; using it for
  non-secret config is a mismatch, and it requires additional plugin wiring for values that Azure already
  exposes directly.

The CMP approach reuses the mechanism ADR 0001 already defines (render from Azure), so it is the smallest
consistent step.

**Installing Argo via Terraform `helm_release`** was the initial proposal and was **rejected**: it
re-couples Terraform to the Kubernetes API (apply-time cluster dependency, fragile Helm/Kubernetes
providers for a CRD-heavy controller), contradicting ADR 0001's "Terraform never touches k8s," and it is
inconsistent with how the project already installs cluster controllers (cert-manager and ESO via the
bootstrap). The bootstrap install + Argo self-management (above) is cleaner and consistent.

## Consequences

- **Positive:** drift detection + auto-heal (the class of failure that bit this project becomes
  impossible to ship silently); git is the auditable source of intent; no human `kubectl`; CI no longer
  holds cluster-admin; a clean path to multi-tenant fan-out (Argo `ApplicationSet`, one Application per
  tenant) that aligns with the BYO-forge-identity work.
- **Cost:** a new control plane to run, secure, RBAC, and upgrade (Argo CD itself). Sync-time rendering
  requires Argo's Workload Identity to have read access to the Azure resources the CMP queries.
- **Neutral:** the Kustomize overlay and the ESO/WI secret path are unchanged; this changes *who applies*
  the manifests (Argo, not CI) and *when* values are resolved (sync, not push).

## Phased plan

1. **Install Argo CD** in the bootstrap (`cluster-init.sh`, `helm upgrade --install`, alongside
   cert-manager/ESO), UI behind the SSO edge, WI granted read on the Azure resources the render needs.
   No Applications yet — verify the control plane is healthy.
2. **Config-Management Plugin** that runs the render (resolve `REPLACE-*` from Azure) + `kustomize build`.
3. **One `Application`** for driftscope (app + fleet), synced from `deploy/overlays/cloud`. Run it in
   parallel with the pipeline (Argo in "detect but do not auto-sync" first) to confirm parity, then enable
   auto-sync + self-heal. **Done** — the Application (`deploy/gitops/application.yaml`) is applied by the
   bootstrap (`cluster-init.sh`) with the repo URL derived from the checkout's own `git remote origin`
   (a `REPLACE-REPO` placeholder, so a fork-and-rebuild registers against the forker's repo) and a
   read-only deploy key minted + registered for private repos. It runs **detect-only**; parity is
   confirmed (Argo renders the same image the pipeline deployed, and the `OutOfSync` is uniformly
   tracking-metadata adoption, not content drift).

### Cutover gate (Phase 4) — CLOSED

Auto-sync + `selfHeal` could not be enabled until the **image tag lived in git**: the CMP rendered the
tag from a mutable cluster ConfigMap while the deploy pipeline rendered `sha-<commit>`, so `selfHeal`
would have continuously reverted every pipeline image bump — the two fighting over the running image.
**Resolved:** the image tag is now `kustomization.yaml`'s `newTag` (the single source of truth) and
`render-overlay.sh` no longer takes a tag override, so the CMP and the pipeline render the *same* tag by
construction. The `driftscope-deploy-config` ConfigMap carries only `IMAGE_REPO` (the registry), not a tag.

4. **Cut the pipeline over.** **Done** — the deployed version is a **semantic version** in git
   (`kustomization.yaml` `newTag`). Cutting a release (a `vX.Y.Z` tag → GitHub Release) makes
   `deploy.yml` build + sign that version and **bump `newTag` via a commit to main** (`promote=true`);
   a plain `workflow_dispatch` builds a throwaway `sha-<commit>` test image and does **not** touch
   git, so `main` never accumulates sha tags. The pipeline no longer runs `kubectl apply` and holds no
   cluster credential (only `contents: write` to push the bump); `application.yaml` enables
   `automated: { prune, selfHeal }`, so Argo CD is the sole applier. The bootstrap's `image.sh` builds
   the committed `newTag`, so a fresh cluster's image matches what Argo renders and a rebuild is
   reproducible from the committed version.
5. **Multi-tenant (later):** `ApplicationSet` templating one Application per tenant, keyed to the
   per-tenant identity registry.

#!/usr/bin/env bash
# One-time cluster wiring that isn't the app itself: kubeconfig, the ext_authz provider,
# the injected namespace, the BYO gateway IP, and (custom-domain only) cert-manager.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

RG="$(tf_out resource_group)"
AKS="$(tf_out cluster_name)"
REV="$(tf_out istio_revision)"

log "fetching kubeconfig for $AKS"
az aks get-credentials -g "$RG" -n "$AKS" --overwrite-existing >/dev/null

log "namespace driftscope (+ istio.io/rev=$REV for sidecar injection)"
kubectl get ns driftscope >/dev/null 2>&1 || kubectl create ns driftscope
kubectl label ns driftscope "istio.io/rev=$REV" --overwrite

# Register the ext_authz provider in the shared mesh config, then bounce istiod so it loads.
log "registering ext_authz provider + restarting istiod-$REV"
kubectl apply -f "$OVERLAY/mesh/ext-authz-provider.configmap.yaml"
kubectl -n aks-istio-system rollout restart "deployment/istiod-$REV"
kubectl -n aks-istio-system rollout status  "deployment/istiod-$REV" --timeout=120s

# Attach the BYO static public IP to the managed external gateway (Terraform granted the
# cluster identity Network Contributor on it). NOTE: on AKS-managed Istio this annotation may
# be reconciled by the add-on -- if the IP doesn't take, see edge/README.md.
PIP="$(tf_out gateway_pip_name)"
log "annotating external gateway service -> public IP $PIP"
# Network-layer origin lock (defense-in-depth alongside the app-layer X-Azure-FDID check):
# restrict the LoadBalancer's NSG rule to the AzureFrontDoor.Backend service tag, so ONLY Front
# Door's backends can reach the gateway's public IP -- the internet is dropped at the network layer
# before it ever hits Envoy. (On AKS the LB NSG rules are add-on-managed; this annotation is the
# supported way to scope them -- a hand-authored azurerm NSG rule would be reconciled away.)
kubectl annotate svc aks-istio-ingressgateway-external -n aks-istio-ingress --overwrite \
  "service.beta.kubernetes.io/azure-pip-name=$PIP" \
  "service.beta.kubernetes.io/azure-load-balancer-resource-group=$RG" \
  "service.beta.kubernetes.io/azure-allowed-service-tags=AzureFrontDoor.Backend"

# cert-manager with Workload Identity, only when there's a custom domain (=> an origin cert).
if custom_domain; then
  CID="$(tf_out certmanager_client_id)"
  log "installing cert-manager (Workload Identity, client-id $CID)"
  helm repo add jetstack https://charts.jetstack.io >/dev/null
  helm repo update jetstack >/dev/null
  helm upgrade --install cert-manager jetstack/cert-manager \
    --namespace cert-manager --create-namespace \
    --version v1.16.2 \
    --set crds.enabled=true \
    --set "serviceAccount.annotations.azure\.workload\.identity/client-id=$CID" \
    --set-string "podLabels.azure\.workload\.identity/use=true" \
    --wait
else
  log "no custom domain -> skipping cert-manager (origin leg is HTTP)"
fi

# External Secrets Operator: syncs the oauth2-proxy creds from Key Vault (retires `make secret`).
# The driftscope-eso ServiceAccount (created by the app overlay, Terraform-annotated with the
# managed-identity client id) is what ESO federates as -- installed here so it's running before
# the ExternalSecret is applied in the deploy step.
log "installing External Secrets Operator"
helm repo add external-secrets https://charts.external-secrets.io >/dev/null
helm repo update external-secrets >/dev/null
helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace \
  --version 0.10.7 \
  --set installCRDs=true \
  --wait

# Argo CD -- the GitOps controller (docs/adr/0002-gitops-with-argocd.md). Installed imperatively
# here, in ONE place, alongside cert-manager + ESO: the minimal bootstrap before GitOps exists.
# Terraform stays pure (Azure only, ADR 0001); Argo then self-manages via app-of-apps.
GITOPS="$(dirname "${BASH_SOURCE[0]}")/../../deploy/gitops"
log "installing Argo CD (GitOps controller)"
kubectl get ns argocd >/dev/null 2>&1 || kubectl create ns argocd

# Render inputs for the driftscope Config-Management Plugin -- NON-SECRET ids only (ADR 0001).
# Secrets never land here; they stay in Key Vault (ESO + Workload Identity). Same values the
# deploy pipeline uses, sourced from Terraform outputs + the login tenant.
kubectl create configmap driftscope-deploy-config -n argocd \
  --from-literal=HOST="$(tf_out host)" \
  --from-literal=TENANT_ID="$(tf_out tenant_id)" \
  --from-literal=ALLOWED_GROUP_ID="$(tf_out allowed_group_id)" \
  --from-literal=APP_CLIENT_ID="$(tf_out app_client_id)" \
  --from-literal=ESO_CLIENT_ID="$(tf_out eso_client_id)" \
  --from-literal=KEYVAULT="$(tf_out key_vault_name)" \
  --from-literal=IMAGE_REPO="$(tf_out acr_login_server)/driftscope" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$GITOPS/argocd-cmp-plugin.configmap.yaml"

# The values file adds the render CMP as a repo-server sidecar + server.insecure (TLS at the gateway).
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null
helm repo update argo >/dev/null
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --version 7.7.5 \
  -f "$GITOPS/argocd-values.yaml" --wait

# ── Register the repo with Argo + create the Application (ADR 0002 phase 3) ──────────────────
# Argo pulls from git, so it needs the repo URL and -- for a PRIVATE repo -- a read-only deploy
# key. The repo URL is NOT infrastructure (it's wherever you cloned from), so we derive it from
# THIS checkout's own origin: a fork-and-rebuild then registers against the forker's repo with no
# edit to application.yaml (which ships a REPLACE-REPO placeholder, ADR 0001).
#
# This step is additive: if it can't run (no gh, or gh not authed for a private repo) it WARNS and
# skips -- Argo CD is still installed and the deploy pipeline still deploys. It never blocks bootstrap.
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  REPO_SLUG="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
  REPO_SSH="$(gh repo view  --json sshUrl       -q .sshUrl        2>/dev/null || true)"
  REPO_HTTPS="$(gh repo view --json url         -q .url           2>/dev/null || true)"
  REPO_VIS="$(gh repo view  --json visibility   -q .visibility    2>/dev/null || echo PRIVATE)"
else
  REPO_SLUG=""; REPO_SSH="$(git -C "$REPO" remote get-url origin 2>/dev/null || true)"
  REPO_HTTPS=""; REPO_VIS="UNKNOWN"
fi

if [ -z "$REPO_SSH" ]; then
  warn "could not determine the git repo URL -> skipping Argo Application registration."
  warn "  Argo CD is installed and the deploy pipeline still deploys; register manually per ADR 0002."
elif [ "$REPO_VIS" = "PUBLIC" ]; then
  # Public repo: Argo clones anonymously over HTTPS -- no deploy key / repo Secret needed.
  REPO_TARGET="${REPO_HTTPS:-$REPO_SSH}"
  log "applying Argo Application (public repo, anonymous HTTPS clone: $REPO_TARGET)"
  sed "s#REPLACE-REPO#${REPO_TARGET}#g" "$GITOPS/application.yaml" | kubectl apply -f -
else
  # Private repo: Argo needs an SSH read-only deploy key. Reuse an existing driftscope-repo Secret
  # (idempotent rerun -> no key sprawl); otherwise mint an ed25519 keypair, register the PUBLIC
  # half read-only on the repo (needs gh with admin, which repo owners have), and store the PRIVATE
  # half in the argocd repo Secret. github.com host key ships in the argo-cd chart's known-hosts CM.
  if kubectl -n argocd get secret driftscope-repo >/dev/null 2>&1; then
    log "Argo repo Secret driftscope-repo already present -> reusing deploy key"
  elif [ -n "$REPO_SLUG" ]; then
    log "minting read-only deploy key for $REPO_SLUG and registering it on the repo"
    KEYDIR="$(mktemp -d)"; trap 'rm -rf "$KEYDIR"' EXIT
    ssh-keygen -t ed25519 -N '' -C "argocd-driftscope@$AKS" -f "$KEYDIR/id" >/dev/null
    gh api -X POST "repos/$REPO_SLUG/keys" \
      -f title="argocd-driftscope ($AKS)" \
      -f key="$(cat "$KEYDIR/id.pub")" \
      -F read_only=true >/dev/null
    # stringData + indented key; lib.sh forbids `set -x`, so the private key never hits a trace.
    kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: driftscope-repo
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
stringData:
  type: git
  url: ${REPO_SSH}
  sshPrivateKey: |
$(sed 's/^/    /' "$KEYDIR/id")
EOF
    rm -rf "$KEYDIR"; trap - EXIT
  else
    warn "private repo but gh is unavailable to register a deploy key -> skipping Application."
    warn "  Argo CD is installed and the pipeline still deploys; add a read-only deploy key +"
    warn "  driftscope-repo Secret, then apply deploy/gitops/application.yaml, per ADR 0002."
  fi

  # Apply the Application once the repo credential exists (detect-only; the auto-sync cutover is
  # gated on the image-tag unification -- see application.yaml / ADR 0002).
  if kubectl -n argocd get secret driftscope-repo >/dev/null 2>&1; then
    log "applying Argo Application (detect-only; see ADR 0002 for the auto-sync cutover gate)"
    sed "s#REPLACE-REPO#${REPO_SSH}#g" "$GITOPS/application.yaml" | kubectl apply -f -
  fi
fi

log "cluster-init: OK"

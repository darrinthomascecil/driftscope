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
  --from-literal=TENANT_ID="$(az account show --query tenantId -o tsv)" \
  --from-literal=ALLOWED_GROUP_ID="$(tf_out allowed_group_id)" \
  --from-literal=APP_CLIENT_ID="$(tf_out app_client_id)" \
  --from-literal=ESO_CLIENT_ID="$(tf_out eso_client_id)" \
  --from-literal=KEYVAULT="$(tf_out key_vault_name)" \
  --from-literal=IMAGE="$(tf_out acr_login_server)/driftscope:v1" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$GITOPS/argocd-cmp-plugin.configmap.yaml"

# The values file adds the render CMP as a repo-server sidecar + server.insecure (TLS at the gateway).
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null
helm repo update argo >/dev/null
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --version 7.7.5 \
  -f "$GITOPS/argocd-values.yaml" --wait

# TODO(adr-0002 phase 3-4): register the repo (read-only deploy key) + apply the Argo Application
# (deploy/gitops/application.yaml), then flip the Application to automated sync + selfHeal. Left as
# a deliberate manual step until detect-only parity is signed off. See the ADR.

log "cluster-init: OK"

#!/usr/bin/env bash
#
# setup-aks-wi.sh — one-time workload-identity setup for driftscope on an AKS cluster.
#
# The AKS analog of the kind setup, and much simpler: AKS gives you a MANAGED OIDC
# issuer and the workload-identity webhook as an addon, so there is no JWKS to host and
# no api-server to patch. This script wires the driftscope *app* identity for one
# environment: enable the addons, create the env's UAMI + federated credential, grant it
# Key Vault + ACR access, and import the shared GitHub App key into this env's vault.
#
# The ALB Controller / Application Gateway for Containers is a SEPARATE platform bootstrap
# with its OWN identity (see "ALB Controller" at the bottom) — not done here.
#
# SAFETY: dry-run by default (prints commands). Set APPLY=true to execute. Creates real
# Azure resources in the target subscription. Run once per environment.
set -euo pipefail

########## 0. CONFIG — edit per environment, then re-read before APPLY ##########
ENV_NAME="${ENV_NAME:?set ENV_NAME=dev|prod}"
: "${AZURE_TENANT_ID:?export AZURE_TENANT_ID=<tenant>}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-$(az account show --query id -o tsv 2>/dev/null || echo CHANGE_ME)}"
LOCATION="${LOCATION:-eastus}"
RESOURCE_GROUP="${RESOURCE_GROUP:-driftscope-${ENV_NAME}}"
AKS_CLUSTER="${AKS_CLUSTER:?set AKS_CLUSTER=<cluster name>}"
ACR_NAME="${ACR_NAME:?set ACR_NAME=<acr name>}"

# The namespace/SA stay 'driftscope' in every env so the federated subject is constant;
# only these Azure resources carry the environment name.
UAMI_NAME="${UAMI_NAME:-driftscope-${ENV_NAME}}"
KEYVAULT_NAME="${KEYVAULT_NAME:?set KEYVAULT_NAME=<per-env vault, globally unique>}"
K8S_NAMESPACE="driftscope"
K8S_SERVICEACCOUNT="driftscope"
FEDERATED_CRED_NAME="${FEDERATED_CRED_NAME:-driftscope-${ENV_NAME}}"

# The SHARED GitHub App's private key (same App across all envs; the key is imported
# into each env's own vault). Point at the PEM you downloaded once from the App.
APP_PEM="${APP_PEM:-$HOME/Downloads/driftscope-app.private-key.pem}"
KV_KEY_NAME="${KV_KEY_NAME:-gh-app-key}"

APPLY="${APPLY:-false}"

c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
run() { printf '%s   $ %s%s\n' "$c_yel" "$*" "$c_off"; [[ "$APPLY" == true ]] && "$@" || true; }
runsh() { printf '%s   $ %s%s\n' "$c_yel" "$1" "$c_off"; [[ "$APPLY" == true ]] && bash -c "$1" || true; }
step() { printf '\n== %s ==\n' "$*"; }
note() { printf '%s   %s%s\n' "$c_dim" "$*" "$c_off"; }

echo "driftscope AKS workload-identity setup — env=${ENV_NAME}  cluster=${AKS_CLUSTER}"
echo "mode: $([[ "$APPLY" == true ]] && echo APPLY || echo 'dry-run (prints only)')"
run az account set --subscription "$SUBSCRIPTION_ID"

step "1. Enable the managed OIDC issuer + workload-identity webhook on the cluster"
run az aks update -g "$RESOURCE_GROUP" -n "$AKS_CLUSTER" \
  --enable-oidc-issuer --enable-workload-identity
note "Read the managed issuer URL (no JWKS hosting needed, unlike kind):"
runsh "ISSUER=\$(az aks show -g '$RESOURCE_GROUP' -n '$AKS_CLUSTER' --query oidcIssuerProfile.issuerUrl -o tsv); echo \"issuer=\$ISSUER\""

step "2. Create this env's UAMI (the driftscope app identity)"
run az identity create -g "$RESOURCE_GROUP" -n "$UAMI_NAME" -l "$LOCATION"
runsh "az identity show -g '$RESOURCE_GROUP' -n '$UAMI_NAME' --query '{clientId:clientId, principalId:principalId}' -o json"
note "Put clientId into deploy/overlays/${ENV_NAME}-aks/patch-sa.yaml"

step "3. Federate it to the cluster (same subject as every env)"
runsh "ISSUER=\$(az aks show -g '$RESOURCE_GROUP' -n '$AKS_CLUSTER' --query oidcIssuerProfile.issuerUrl -o tsv); \
  az identity federated-credential create --name '$FEDERATED_CRED_NAME' \
    --identity-name '$UAMI_NAME' -g '$RESOURCE_GROUP' \
    --issuer \"\$ISSUER\" \
    --subject 'system:serviceaccount:${K8S_NAMESPACE}:${K8S_SERVICEACCOUNT}' \
    --audience 'api://AzureADTokenExchange'"

step "4. Per-env Key Vault (RBAC), + let the UAMI sign, + let me import the App key"
run az keyvault create -g "$RESOURCE_GROUP" -n "$KEYVAULT_NAME" -l "$LOCATION" \
  --enable-rbac-authorization true
runsh "KVID=\$(az keyvault show -n '$KEYVAULT_NAME' --query id -o tsv); \
  PID=\$(az identity show -g '$RESOURCE_GROUP' -n '$UAMI_NAME' --query principalId -o tsv); \
  az role assignment create --assignee-object-id \"\$PID\" --assignee-principal-type ServicePrincipal \
    --role 'Key Vault Crypto User' --scope \"\$KVID\"; \
  ME=\$(az ad signed-in-user show --query id -o tsv); \
  az role assignment create --assignee-object-id \"\$ME\" --assignee-principal-type User \
    --role 'Key Vault Crypto Officer' --scope \"\$KVID\""

step "5. Import the SHARED GitHub App key into THIS env's vault"
note "Same App across envs; each env holds its own copy of the key in its own vault."
run az keyvault key import --vault-name "$KEYVAULT_NAME" -n "$KV_KEY_NAME" --pem-file "$APP_PEM"
note "Set GITHUB_APP_KEY_VAULT_URI=https://${KEYVAULT_NAME}.vault.azure.net in deploy/overlays/${ENV_NAME}-aks/config.env"

step "6. Let the cluster pull the image from ACR"
run az aks update -g "$RESOURCE_GROUP" -n "$AKS_CLUSTER" --attach-acr "$ACR_NAME"

step "7. Deploy driftscope"
note "az aks get-credentials -g $RESOURCE_GROUP -n $AKS_CLUSTER"
note "Fill deploy/overlays/${ENV_NAME}-aks/{config.env,patch-sa.yaml,kustomization.yaml}, then:"
note "kubectl apply -k deploy/overlays/${ENV_NAME}-aks"

cat <<'NOTE'

== ALB Controller / Application Gateway for Containers (separate platform bootstrap) ==
AGC ingress needs its OWN identity (it programs the load balancer, not the app):
  - install the ALB Controller (helm) with a managed identity + federated credential on
    system:serviceaccount:azure-alb-system:alb-controller-sa
  - grant that identity: "AppGw for Containers Configuration Manager" on the AGC RG, plus
    subnet join on the AGC-delegated subnet
  - provision the AGC (managed via ApplicationLoadBalancer, or bring-your-own)
Then the Gateway/HTTPRoute in the AKS overlay attach to it. Follow current AGC docs for
the exact role names and controller version.
NOTE

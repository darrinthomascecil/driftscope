#!/usr/bin/env bash
# Create (idempotently) the Azure Storage backend that holds Terraform state REMOTELY, with
# blob versioning + soft-delete + locking, so state lives off local disk and a team/CD can share
# it safely. This is the DEFAULT path (`make up` runs it); opt out with LOCAL_STATE=1. Keyless:
# uses your AAD identity, no account keys anywhere. See docs/remote-state.md.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

: "${STATE_RG:=driftscope-tfstate}"
: "${STATE_LOCATION:=eastus}"
: "${STATE_CONTAINER:=tfstate}"

# Idempotent storage-account name: reuse the one already recorded in backend.hcl (so re-running
# `make up` never creates a second account), else honour an explicit STATE_SA, else generate a
# globally-unique one. Storage account names are 3-24 lowercase alphanumeric.
BACKEND_HCL="$REPO/terraform/backend.hcl"
if [ -z "${STATE_SA:-}" ] && [ -f "$BACKEND_HCL" ]; then
  STATE_SA="$(sed -n 's/^storage_account_name *= *"\([^"]*\)".*/\1/p' "$BACKEND_HCL")"
  _rg="$(sed -n 's/^resource_group_name *= *"\([^"]*\)".*/\1/p' "$BACKEND_HCL")"
  [ -n "$_rg" ] && STATE_RG="$_rg"
fi
STATE_SA="${STATE_SA:-dstf$(openssl rand -hex 6)}"

SUB="$(az account show --query id -o tsv)"

log "resource group $STATE_RG ($STATE_LOCATION)"
az group create -n "$STATE_RG" -l "$STATE_LOCATION" -o none

log "storage account $STATE_SA (TLS1.2, no public blob, AAD-only)"
az storage account create -n "$STATE_SA" -g "$STATE_RG" -l "$STATE_LOCATION" \
  --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 \
  --allow-blob-public-access false --https-only true -o none

log "enabling blob versioning + 30-day soft-delete (state history / recovery)"
az storage account blob-service-properties update --account-name "$STATE_SA" \
  --enable-versioning true --enable-delete-retention true --delete-retention-days 30 -o none

# Keyless access: grant the signed-in user data-plane rights, then use --auth-mode login.
CALLER="$(az ad signed-in-user show --query id -o tsv)"
SCOPE="$(az storage account show -n "$STATE_SA" -g "$STATE_RG" --query id -o tsv)"
log "granting Storage Blob Data Contributor to the current user"
az role assignment create --assignee "$CALLER" --role "Storage Blob Data Contributor" \
  --scope "$SCOPE" -o none 2>/dev/null || true

log "container $STATE_CONTAINER (role assignment can take ~30s to propagate; will retry)"
az storage container create -n "$STATE_CONTAINER" --account-name "$STATE_SA" --auth-mode login -o none \
  || { sleep 30; az storage container create -n "$STATE_CONTAINER" --account-name "$STATE_SA" --auth-mode login -o none; }

cat > "$REPO/terraform/backend.hcl" <<EOF
resource_group_name  = "$STATE_RG"
storage_account_name = "$STATE_SA"
container_name       = "$STATE_CONTAINER"
key                  = "driftscope.tfstate"
use_azuread_auth     = true
subscription_id      = "$SUB"
EOF

log "wrote terraform/backend.hcl"

# Activate the azurerm backend so `terraform init` uses it. Generated (not committed) so a bare
# `terraform` invocation without this file still defaults to local -- the backend is opt-in at the
# file level, default at the `make up` level.
if [ ! -f "$REPO/terraform/backend.tf" ]; then
  cp "$REPO/terraform/backend.tf.example" "$REPO/terraform/backend.tf"
  log "activated terraform/backend.tf (azurerm backend)"
fi

# If a LOCAL state file with resources already exists, it must be migrated up (one-time) rather
# than silently starting a fresh empty remote state. A brand-new deploy has neither and just inits.
if [ -s "$REPO/terraform/terraform.tfstate" ] && grep -q '"resources"' "$REPO/terraform/terraform.tfstate" 2>/dev/null \
   && grep -q '"instances"' "$REPO/terraform/terraform.tfstate" 2>/dev/null; then
  warn "existing LOCAL state detected -- run \`make state-migrate\` once to move it into the backend"
fi

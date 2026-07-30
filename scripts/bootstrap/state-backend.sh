#!/usr/bin/env bash
# Create (idempotently) the Azure Storage backend that holds Terraform state REMOTELY, with
# blob versioning + soft-delete + locking, so state lives off local disk and a team/CD can share
# it safely. OPT-IN -- local state stays the default (see docs/remote-state.md). Keyless: uses
# your AAD identity, no account keys anywhere.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

: "${STATE_RG:=driftscope-tfstate}"
: "${STATE_LOCATION:=eastus}"
: "${STATE_CONTAINER:=tfstate}"
STATE_SA="${STATE_SA:-}"
[ -n "$STATE_SA" ] || die "set STATE_SA to a globally-unique storage account name \
(3-24 lowercase alphanumeric), e.g.  STATE_SA=driftscopetfstate12345 make state-backend"

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
log "next:  cp terraform/backend.tf.example terraform/backend.tf  &&  make state-migrate"

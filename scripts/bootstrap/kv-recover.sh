#!/usr/bin/env bash
# Key Vault purge-protection recovery (Phase 0.6d gotcha). The env vaults have purge protection ON +
# a 7-day soft-delete, so `make down ENV=<env>` RESERVES the vault name for 7 days -- a same-name
# rebuild then fails at `terraform apply` ("vault name already exists / is soft-deleted") until it is
# recovered. This runs before `make infra` so the teardown -> rebuild loop (how the IaC is proven)
# just works. No-op when there is no soft-deleted vault of that name. See docs/design-multi-env-0.6d.md.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TFVARS="$REPO/terraform/envs/$ENV/$ENV.tfvars"
[ -f "$TFVARS" ] || { warn "kv-recover: no $TFVARS -> skipping"; exit 0; }
KV="$(sed -n 's/^[[:space:]]*key_vault_name[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$TFVARS" | head -1)"
[ -n "$KV" ] || { warn "kv-recover: key_vault_name not set in $TFVARS -> skipping"; exit 0; }

if az keyvault list-deleted --query "[?name=='$KV'] | [0].name" -o tsv 2>/dev/null | grep -q .; then
  log "recovering soft-deleted Key Vault $KV (purge protection reserves the name for 7 days)"
  az keyvault recover --name "$KV" >/dev/null && log "kv-recover: recovered $KV"
else
  log "kv-recover: no soft-deleted vault named $KV -> nothing to recover"
fi

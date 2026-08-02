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
  # Non-fatal. `az keyvault recover` FAILS with ResourceGroupNotFound when the vault's original RG was
  # destroyed by a prior `make down` (the clone->rebuild path we are proving). Terraform's provider
  # (recover_soft_deleted_key_vaults=true) recovers the vault during `apply` anyway -- it recreates the
  # RG first, then the vault -- so this is a best-effort convenience, never a blocker.
  if az keyvault recover --name "$KV" >/dev/null 2>&1; then
    log "kv-recover: recovered $KV"
  else
    warn "kv-recover: could not recover $KV now (its RG may be gone) -> Terraform will auto-recover it on apply"
  fi
else
  log "kv-recover: no soft-deleted vault named $KV -> nothing to recover"
fi

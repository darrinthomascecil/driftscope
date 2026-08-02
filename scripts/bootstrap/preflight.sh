#!/usr/bin/env bash
# Fail fast BEFORE we touch Azure: required tools present, logged in, config in place.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

log "preflight: checking tools"
for c in az terraform kubectl helm jq openssl git; do require_cmd "$c"; done
# kustomize is the one tool a package manager often doesn't ship; point at the cross-platform installer.
command -v kustomize >/dev/null 2>&1 || die "kustomize not found -- install it with: make kustomize"

log "preflight: checking Azure login"
az account show >/dev/null 2>&1 || die "not logged in to Azure -- run: az login"
SUB="$(az account show --query id -o tsv)"
log "preflight: active subscription $SUB"

log "preflight: checking config (env=$ENV)"
[ -f "$REPO/terraform/envs/$ENV/$ENV.tfvars" ] || die \
  "terraform/envs/$ENV/$ENV.tfvars not found -- copy $ENV.tfvars.example to it and fill it in"
[ -f "$REPO/terraform/shared/terraform.tfvars" ] || die \
  "terraform/shared/terraform.tfvars not found -- copy shared/terraform.tfvars.example to it (run 'make shared-up' once before the first env)"

# GitHub OIDC immutable subject: many accounts now emit the Actions OIDC subject with immutable
# NUMERIC ids (repo:owner@<id>/repo@<id>:...) instead of names. If so, the gh-deploy federated
# credential MUST match that form or every CI Azure login fails (AADSTS700213). The module registers
# the numeric-id credential when `github_repo_ids` is set. Auto-derive + persist it (best-effort) so a
# from-scratch rebuild arms CI with no hand-editing; a name-based account yields no `@<id>` and is
# left empty (the name-based credential already matches). Idempotent: only writes when unset.
TFVARS="$REPO/terraform/envs/$ENV/$ENV.tfvars"
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 \
   && ! grep -qE '^[[:space:]]*github_repo_ids[[:space:]]*=' "$TFVARS"; then
  REPO_SLUG="$(sed -n 's/^[[:space:]]*github_repo[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$TFVARS" | head -1)"
  IDS="$(gh api "/repos/$REPO_SLUG/actions/oidc/customization/sub" -q .sub_claim_prefix 2>/dev/null)"
  IDS="${IDS#repo:}"   # module prepends `repo:`; it wants OWNER@<id>/REPO@<id>
  if printf '%s' "$IDS" | grep -q '@[0-9]'; then
    printf '\n# auto-derived by preflight: GitHub emits an immutable numeric-id OIDC subject for this\n# repo, so the gh-deploy federated credential must match it (else CI login = AADSTS700213).\ngithub_repo_ids = "%s"\n' "$IDS" >> "$TFVARS"
    log "preflight: armed github_repo_ids=$IDS (GitHub immutable OIDC subject)"
  fi
fi

log "preflight: OK"

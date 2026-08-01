#!/usr/bin/env bash
# repo-config.sh -- make the GitHub Actions pipelines reproducible. Sets the repo VARIABLES and the
# TFVARS SECRET the pipelines consume, sourced from Terraform outputs + the state-backend config, so
# a teardown + rebuild re-wires CI with no hand-editing in the GitHub UI. Run after `make infra`
# (needs outputs). Idempotent -- gh variable/secret set upserts.
#
# ADR 0001: Terraform stays pure-Azure (it created the deploy identity + trust); THIS gh step wires
# the GitHub-side config, rather than pulling in a Terraform GitHub provider + token.
#
# Requires gh, authenticated with admin on the repo. Fail-open: warns + skips if gh is unavailable,
# so it never blocks `make up` (the app still comes up; only CI wiring is deferred).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
  warn "gh not available/authenticated -> skipping repo-config (pipeline vars/secrets not set)."
  warn "  Wire them later: 'make repo-config' once gh is logged in with repo admin."
  exit 0
fi

REPO_SLUG="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
log "wiring GitHub Actions config for $REPO_SLUG (variables + TFVARS secret)"

set_var() { gh variable set "$1" --repo "$REPO_SLUG" --body "$2" >/dev/null && log "  var    $1"; }

# Non-secret pipeline inputs, from Terraform outputs (deterministic across rebuilds).
ACR_LOGIN="$(tf_out acr_login_server)"          # <name>.azurecr.io
set_var AZURE_CLIENT_ID       "$(tf_out gh_deploy_client_id)"
set_var AZURE_TENANT_ID       "$(tf_out tenant_id)"
set_var AZURE_SUBSCRIPTION_ID "$(tf_out subscription_id)"
set_var ACR_NAME              "${ACR_LOGIN%%.*}"

# Remote-state backend coordinates for terraform.yml's drift job (from the generated backend.hcl).
BH="$REPO/terraform/backend.hcl"
if [ -f "$BH" ]; then
  hcl() { grep -E "^[[:space:]]*$1[[:space:]]*=" "$BH" | sed -E 's/.*=[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/'; }
  set_var TFSTATE_RG        "$(hcl resource_group_name)"
  set_var TFSTATE_SA        "$(hcl storage_account_name)"
  set_var TFSTATE_CONTAINER "$(hcl container_name)"
else
  warn "  no terraform/backend.hcl (local state?) -> skipping TFSTATE_* (drift job needs remote state)"
fi

# TFVARS secret: the deployment's variables, so terraform.yml plans with the same inputs. It holds
# non-credential-but-private values (subscription id, acme email, admin members) -- set via stdin so
# it is never printed or traced.
TFVARS_FILE="$REPO/terraform/terraform.tfvars"
if [ -f "$TFVARS_FILE" ]; then
  gh secret set TFVARS --repo "$REPO_SLUG" < "$TFVARS_FILE" && log "  secret TFVARS"
else
  warn "  no terraform/terraform.tfvars -> skipping TFVARS secret"
fi

log "repo-config: OK (drift is opt-in: set repo variable TF_DRIFT_ENABLED=true to enable it)"

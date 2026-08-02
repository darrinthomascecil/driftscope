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

# The GitHub Environment for THIS env (deploy.yml gates the per-env job on it; the gh-deploy
# federated credential trusts environment:<this>). prod -> "production"; dev/staging keep their name.
GH_ENV="$ENV"; [ "$ENV" = "prod" ] && GH_ENV="production"
log "wiring GitHub Actions config for $REPO_SLUG (env=$ENV -> GitHub Environment '$GH_ENV')"

# Ensure the GitHub Environment exists before setting env-scoped vars/secrets on it (idempotent PUT).
gh api -X PUT "repos/$REPO_SLUG/environments/$GH_ENV" >/dev/null 2>&1 \
  && log "  env    $GH_ENV (ensured)" \
  || warn "  could not ensure environment $GH_ENV (need repo admin) -- create it in Settings > Environments"

set_var()     { gh variable set "$1" --repo "$REPO_SLUG" --body "$2" >/dev/null && log "  var    $1 (repo)"; }
set_env_var() { gh variable set "$1" --repo "$REPO_SLUG" --env "$GH_ENV" --body "$2" >/dev/null && log "  var    $1 (env:$GH_ENV)"; }

ENVUP="$(printf '%s' "$ENV" | tr '[:lower:]' '[:upper:]')"   # DEV | STAGING | PROD

# ENV-SCOPED (for deploy.yml): each env's deploy identity is distinct, so AZURE_CLIENT_ID is
# per-environment. This is the isolation guarantee -- one env's promotion job can only authenticate
# as its OWN gh-deploy UAMI, and production's environment protection (required reviewers) gates it.
set_env_var AZURE_CLIENT_ID "$(tf_out gh_deploy_client_id)"

# REPO-LEVEL (shared across envs): tenant, subscription, the ONE shared ACR, and the shared state
# account coordinates (the per-env KEY is built in terraform.yml's matrix, not stored here).
ACR_LOGIN="$(tf_out acr_login_server)"          # <name>.azurecr.io  (shared)
set_var AZURE_TENANT_ID       "$(tf_out tenant_id)"
set_var AZURE_SUBSCRIPTION_ID "$(tf_out subscription_id)"
set_var ACR_NAME              "${ACR_LOGIN%%.*}"

BH="$REPO/terraform/envs/$ENV/backend.hcl"
if [ -f "$BH" ]; then
  hcl() { grep -E "^[[:space:]]*$1[[:space:]]*=" "$BH" | sed -E 's/.*=[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/'; }
  set_var TFSTATE_RG        "$(hcl resource_group_name)"
  set_var TFSTATE_SA        "$(hcl storage_account_name)"
  set_var TFSTATE_CONTAINER "$(hcl container_name)"
else
  warn "  no $BH (local state?) -> skipping TFSTATE_* (drift job needs remote state)"
fi

# REPO-LEVEL, per-env-SUFFIXED (for terraform.yml's drift matrix): the drift job runs on a SCHEDULE
# and must NOT reference a GitHub Environment -- production's required-reviewers protection would
# pause the nightly run for approval. So the drift identity + inputs are repo-level, suffixed by env.
set_var "AZURE_CLIENT_ID_$ENVUP" "$(tf_out gh_deploy_client_id)"
TFVARS_FILE="$REPO/terraform/envs/$ENV/$ENV.tfvars"
if [ -f "$TFVARS_FILE" ]; then
  # non-credential-but-private (subscription id, admin members) -- set via stdin, never printed/traced.
  gh secret set "TFVARS_$ENVUP" --repo "$REPO_SLUG" < "$TFVARS_FILE" && log "  secret TFVARS_$ENVUP (repo)"
else
  warn "  no $TFVARS_FILE -> skipping TFVARS_$ENVUP secret"
fi

log "repo-config: OK for env=$ENV (drift is opt-in: set repo variable TF_DRIFT_ENABLED=true)"

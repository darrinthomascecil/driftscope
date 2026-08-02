#!/usr/bin/env bash
# Shared helpers for the bootstrap scripts. Sourced, not executed.
#
# NOTE: we deliberately never `set -x` in these scripts -- the secret step handles the Entra
# client secret and cookie secret, and tracing would print them. Keep it that way.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Phase 0.6d: which environment this bootstrap run targets (dev|staging|prod). `make up ENV=<env>`
# threads it in; default dev. It selects the Terraform env-root (terraform/envs/$ENV, its own state
# key) and DRIFTSCOPE_ENV for the ConfigMap + render (overlays/$ENV).
ENV="${ENV:-dev}"
case "$ENV" in
  dev|staging|prod) ;;
  *) printf '\033[1;31merror:\033[0m ENV must be one of dev|staging|prod (got "%s")\n' "$ENV" >&2; exit 1 ;;
esac

# The CLOUD overlay dir holds the SHARED, env-agnostic sources the bootstrap applies directly outside
# the app render: mesh/ (ext_authz provider, hardening, SSO gate) and edge/ (origin cert + FDID lock).
# These are the same in every env (rendered per-cluster from that env's TF outputs), so this stays
# pinned at cloud -- only the APP overlay render is env-specific (render-overlay.sh + DRIFTSCOPE_ENV).
OVERLAY="$REPO/deploy/overlays/cloud"

# terraform wrappers + raw-output readers. TF() targets THIS env's root (per-env state);
# TF_SHARED() targets the shared layer (ACR + DNS zone), applied once by `make shared-up`.
TF() { terraform -chdir="$REPO/terraform/envs/$ENV" "$@"; }
tf_out() { TF output -raw "$1"; }
TF_SHARED() { terraform -chdir="$REPO/terraform/shared" "$@"; }
tf_shared_out() { TF_SHARED output -raw "$1"; }

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

# True when Terraform reports a custom domain (=> cert-manager + origin cert are in play).
custom_domain() { [ "$(tf_out use_custom_domain 2>/dev/null || echo false)" = "true" ]; }

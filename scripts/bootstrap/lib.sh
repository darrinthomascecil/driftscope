#!/usr/bin/env bash
# Shared helpers for the bootstrap scripts. Sourced, not executed.
#
# NOTE: we deliberately never `set -x` in these scripts -- the secret step handles the Entra
# client secret and cookie secret, and tracing would print them. Keep it that way.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OVERLAY="$REPO/deploy/overlays/cloud"

# terraform wrapper pinned at the terraform/ dir, and a raw-output reader.
TF() { terraform -chdir="$REPO/terraform" "$@"; }
tf_out() { TF output -raw "$1"; }

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

# True when Terraform reports a custom domain (=> cert-manager + origin cert are in play).
custom_domain() { [ "$(tf_out use_custom_domain 2>/dev/null || echo false)" = "true" ]; }

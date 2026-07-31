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

log "preflight: checking config"
[ -f "$REPO/terraform/terraform.tfvars" ] || die \
  "terraform/terraform.tfvars not found -- copy terraform.tfvars.example to it and fill it in"

log "preflight: OK"

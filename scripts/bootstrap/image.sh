#!/usr/bin/env bash
# Build the driftscope image in ACR (no local docker needed) and point the overlay at it.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ACR="$(tf_out acr_login_server)"   # e.g. driftscopeacr12345.azurecr.io
ACR_NAME="${ACR%%.*}"              # -> driftscopeacr12345
TAG="v1"                           # matches the overlay's newTag

log "building $ACR/driftscope:$TAG in ACR (from $REPO)"
az acr build --registry "$ACR_NAME" --image "driftscope:$TAG" "$REPO"

# Point the cloud overlay at THIS registry. This edits the (tracked) kustomization.yaml in place
# -- it's your per-deployment registry, not something to commit. `git checkout` it to reset.
log "setting overlay image -> $ACR/driftscope:$TAG"
( cd "$OVERLAY" && kustomize edit set image "driftscope=$ACR/driftscope:$TAG" )

log "image: OK"

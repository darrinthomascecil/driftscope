#!/usr/bin/env bash
# Build the driftscope image in ACR (no local docker needed) and point the overlay at it.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ACR="$(tf_out acr_login_server)"   # e.g. driftscopeacr12345.azurecr.io
ACR_NAME="${ACR%%.*}"              # -> driftscopeacr12345
TAG="v1"                           # matches the overlay's newTag

log "building $ACR/driftscope:$TAG in ACR (from $REPO)"
# The Dockerfile is multi-arch (uses $TARGETARCH to pick the gh binary). `az acr build` uses the
# classic (non-BuildKit) builder, which doesn't auto-populate TARGETARCH, so pass it + --platform
# explicitly. AKS nodes are x64 -> amd64. (The Dockerfile avoids BuildKit-only features so this
# classic build works -- see the COPY-from-builder note in the Dockerfile.)
az acr build --registry "$ACR_NAME" --image "driftscope:$TAG" \
  --platform linux/amd64 --build-arg TARGETARCH=amd64 "$REPO"

# Point the cloud overlay at THIS registry. This edits the (tracked) kustomization.yaml in place
# -- it's your per-deployment registry, not something to commit. `git checkout` it to reset.
log "setting overlay image -> $ACR/driftscope:$TAG"
( cd "$OVERLAY" && kustomize edit set image "driftscope=$ACR/driftscope:$TAG" )

log "image: OK"

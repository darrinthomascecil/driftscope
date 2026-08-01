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

# The registry is NOT committed (ADR 0001): the overlay's newName is a REPLACE-IMAGE-REPO
# placeholder that render-overlay.sh binds from `terraform output acr_login_server` at deploy time
# (deploy.sh). The tag ($TAG) is the committed newTag in kustomization.yaml, which is what we just
# built -- so no in-tree `kustomize edit` is needed here anymore.
log "built $ACR/driftscope:$TAG (registry bound at deploy by render-overlay.sh)"

log "image: OK"

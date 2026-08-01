#!/usr/bin/env bash
# render-overlay.sh -- resolve the ADR-0001 REPLACE-* placeholders in the cloud overlay from the
# environment, then `kustomize build`, and print the final manifests to stdout.
#
# ONE render, used by two callers so their behaviour is identical:
#   * the deploy pipeline (CI)         -- values from Azure + repo vars; sets IMAGE to the built tag
#   * the Argo CD Config-Mgmt Plugin   -- values from a cluster ConfigMap; IMAGE from the committed tag
#
# The repo stays a pure template (no live value committed, ADR 0001); this binds the values at
# render time. Non-destructive: it renders a temp copy, never the working tree.
#
# Required env (the caller supplies them):
#   HOST TENANT_ID ALLOWED_GROUP_ID APP_CLIENT_ID ESO_CLIENT_ID KEYVAULT IMAGE_REPO
#     IMAGE_REPO -- the registry+repo WITHOUT a tag, e.g. myacr.azurecr.io/driftscope. The
#                   deployment's registry is a parameter (ADR 0001), resolved here, not committed.
#
# The image TAG is NOT an input: it lives in git (kustomization.yaml newTag) and is the single
# source of truth for the deployed version (ADR 0002 phase 4). CI and Argo therefore render the
# SAME tag by construction -- the pipeline bumps newTag via a commit; nobody overrides it here.
set -euo pipefail

for v in HOST TENANT_ID ALLOWED_GROUP_ID APP_CLIENT_ID ESO_CLIENT_ID KEYVAULT IMAGE_REPO; do
  [ -n "${!v:-}" ] || { echo "render-overlay: \$$v is required" >&2; exit 2; }
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # repo root, from deploy/gitops/
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp -r "$ROOT/deploy" "$TMP/deploy"
O="$TMP/deploy/overlays/cloud"

# sed -i.bak works on both GNU and BSD sed; the temp .bak files are unreferenced (ignored by build).
sed -i.bak \
  -e "s|REPLACE-HOST|${HOST}|g" \
  -e "s|REPLACE-TENANT-ID|${TENANT_ID}|g" \
  -e "s|REPLACE-ALLOWED-GROUP-ID|${ALLOWED_GROUP_ID}|g" \
  "$O/params.yaml"
sed -i.bak -e "s|REPLACE-APP-CLIENT-ID|${APP_CLIENT_ID}|g" "$O/patch-sa.yaml"
sed -i.bak \
  -e "s|REPLACE-ESO-CLIENT-ID|${ESO_CLIENT_ID}|g" \
  -e "s|REPLACE-TENANT-ID|${TENANT_ID}|g" \
  -e "s|REPLACE-KEYVAULT|${KEYVAULT}|g" \
  "$O/external-secrets.yaml"
# The image registry is a per-deployment parameter (ADR 0001): bind newName here. The tag stays as
# the committed kustomization.yaml newTag -- git is the single source of truth for the version.
sed -i.bak -e "s|REPLACE-IMAGE-REPO|${IMAGE_REPO}|g" "$O/kustomization.yaml"
find "$O" -name '*.bak' -delete

built="$(kustomize build "$O")"
# Fail closed: no placeholder may reach the output (the exact bug that broke WI + ESO).
if grep -q 'REPLACE-' <<<"$built"; then
  echo "render-overlay: unrendered REPLACE-* token in built manifests -- refusing" >&2
  exit 1
fi
printf '%s\n' "$built"

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
#   HOST TENANT_ID ALLOWED_GROUP_ID APP_CLIENT_ID ESO_CLIENT_ID KEYVAULT IMAGE_REPO DRIFTSCOPE_ENV
#     IMAGE_REPO     -- the registry+repo WITHOUT a tag, e.g. myacr.azurecr.io/driftscope. The
#                       deployment's registry is a parameter (ADR 0001), resolved here, not committed.
#     DRIFTSCOPE_ENV -- dev | staging | prod (Phase 0.6d). Selects which thin overlay to build:
#                       overlays/$DRIFTSCOPE_ENV, which layers the image TAG + replicas over the
#                       cloud base. Every cluster renders only its OWN env (from its own ConfigMap).
#
# The image TAG is NOT an input: it lives in git (overlays/<env>/kustomization.yaml newTag) and is the
# single source of truth for that env's deployed version (ADR 0002 phase 4). CI and Argo therefore
# render the SAME tag by construction -- the pipeline bumps newTag via a commit; nobody overrides it.
set -euo pipefail

# RENDER_TARGET selects what to render: the app overlay (default) or the monitoring stack. A separate
# Argo Application renders monitoring with RENDER_TARGET=monitoring (Argo exposes plugin.env as
# ARGOCD_ENV_*), so a monitoring manifest error can't break the app's render. ONE script, two targets.
TARGET="${RENDER_TARGET:-${ARGOCD_ENV_RENDER_TARGET:-app}}"
if [ "$TARGET" = "monitoring" ]; then
  for v in ESO_CLIENT_ID TENANT_ID KEYVAULT; do
    [ -n "${!v:-}" ] || { echo "render-overlay(monitoring): \$$v is required" >&2; exit 2; }
  done
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  cp -r "$ROOT/deploy" "$TMP/deploy"
  M="$TMP/deploy/monitoring"
  # Only external-secrets.yaml carries per-env values; the rest of the stack is env-agnostic.
  sed -i.bak \
    -e "s|REPLACE-ESO-CLIENT-ID|${ESO_CLIENT_ID}|g" \
    -e "s|REPLACE-TENANT-ID|${TENANT_ID}|g" \
    -e "s|REPLACE-KEYVAULT|${KEYVAULT}|g" \
    "$M/external-secrets.yaml"
  find "$M" -name '*.bak' -delete
  built="$(kustomize build "$M")"
  if grep -q 'REPLACE-' <<<"$built"; then
    echo "render-overlay(monitoring): unrendered REPLACE-* token in built manifests -- refusing" >&2
    exit 1
  fi
  printf '%s\n' "$built"
  exit 0
fi

for v in HOST TENANT_ID ALLOWED_GROUP_ID APP_CLIENT_ID ESO_CLIENT_ID KEYVAULT IMAGE_REPO DRIFTSCOPE_ENV; do
  [ -n "${!v:-}" ] || { echo "render-overlay: \$$v is required" >&2; exit 2; }
done
case "$DRIFTSCOPE_ENV" in
  dev|staging|prod) ;;
  *) echo "render-overlay: DRIFTSCOPE_ENV must be one of dev|staging|prod (got '$DRIFTSCOPE_ENV')" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # repo root, from deploy/gitops/
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp -r "$ROOT/deploy" "$TMP/deploy"
# The REPLACE-* placeholders live in the CLOUD base (all real config); sed binds them there. The
# per-env overlay ($ENV_OVERLAY) only layers the image tag + replicas and includes ../cloud.
O="$TMP/deploy/overlays/cloud"
ENV_OVERLAY="$TMP/deploy/overlays/$DRIFTSCOPE_ENV"
[ -d "$ENV_OVERLAY" ] || { echo "render-overlay: no overlay dir for env '$DRIFTSCOPE_ENV' ($ENV_OVERLAY)" >&2; exit 2; }

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
# The image registry is a per-deployment parameter (ADR 0001): bind newName in the ENV overlay,
# where the WHOLE image transformer lives (newName + newTag together, keyed on the base image name).
# The tag stays the committed per-env newTag -- git is the single source of truth for that env's
# version. (Binding newName in ../cloud instead would rename the image before this overlay's newTag
# could match it, silently dropping the tag -- see the cloud kustomization note.)
sed -i.bak -e "s|REPLACE-IMAGE-REPO|${IMAGE_REPO}|g" "$ENV_OVERLAY/kustomization.yaml"
find "$O" "$ENV_OVERLAY" -name '*.bak' -delete

built="$(kustomize build "$ENV_OVERLAY")"
# Fail closed: no placeholder may reach the output (the exact bug that broke WI + ESO).
if grep -q 'REPLACE-' <<<"$built"; then
  echo "render-overlay: unrendered REPLACE-* token in built manifests -- refusing" >&2
  exit 1
fi
printf '%s\n' "$built"

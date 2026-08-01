#!/usr/bin/env bash
# render-edge.sh -- resolve the REPLACE-* placeholders in ONE edge manifest and print it to stdout.
#
# The edge/ objects live in the gateway namespace (aks-istio-ingress) and are applied OUTSIDE the
# app kustomization, so render-overlay.sh (a `kustomize build` of the app overlay) can't render
# them. This is their counterpart -- same philosophy (ADR 0001): the repo holds committed templates,
# real per-deployment values are bound at deploy time from Terraform OUTPUTS, and Terraform itself
# emits outputs only (no local_file codegen into git). Bootstrap-only (deploy.sh) -- CI/Argo do not
# touch the gateway namespace.
#
# Usage: render-edge.sh <path-to-edge-yaml>
# Required env is per-file: each placeholder PRESENT in the file must have a non-empty value.
#   origin-lock.authorizationpolicy.yaml -> FRONT_DOOR_GUID
#   cluster-issuer.yaml                  -> DNS_ZONE RESOURCE_GROUP SUBSCRIPTION_ID
#                                           CERTMANAGER_CLIENT_ID  (ACME_EMAIL optional)
#   origin-certificate.yaml              -> ORIGIN_HOST
set -euo pipefail

f="${1:?render-edge: manifest path required}"
[ -f "$f" ] || { echo "render-edge: no such file: $f" >&2; exit 2; }
out="$(cat "$f")"

# Substitute a placeholder ONLY if the file contains it, and require its value to be non-empty --
# so a missing output fails loudly instead of rendering an empty field. bash 3.2 compatible
# (no associative arrays): indirect expansion + `case` glob + `${var//from/to}` global replace.
require_and_sub() {
  local ph="$1" var="$2" val="${!2:-}"
  case "$out" in
    *"$ph"*)
      [ -n "$val" ] || { echo "render-edge: $f needs \$$var (placeholder $ph)" >&2; exit 2; }
      out="${out//$ph/$val}" ;;
  esac
}

require_and_sub "REPLACE-FRONT-DOOR-GUID"       FRONT_DOOR_GUID
require_and_sub "REPLACE-DNS-ZONE"              DNS_ZONE
require_and_sub "REPLACE-RESOURCE-GROUP"        RESOURCE_GROUP
require_and_sub "REPLACE-SUBSCRIPTION-ID"       SUBSCRIPTION_ID
require_and_sub "REPLACE-CERTMANAGER-CLIENT-ID" CERTMANAGER_CLIENT_ID
require_and_sub "REPLACE-ORIGIN-HOST"           ORIGIN_HOST

# ACME email is optional (Let's Encrypt allows an anonymous account). Set -> substitute; unset ->
# drop the whole `email:` line so no empty field is emitted.
if [ -n "${ACME_EMAIL:-}" ]; then
  out="${out//REPLACE-ACME-EMAIL/$ACME_EMAIL}"
else
  out="$(printf '%s\n' "$out" | grep -v 'REPLACE-ACME-EMAIL')"
fi

# Fail closed: any REPLACE- left means a required value wasn't provided.
if printf '%s' "$out" | grep -q 'REPLACE-'; then
  echo "render-edge: unresolved REPLACE-* in $f -- a required value is unset:" >&2
  printf '%s\n' "$out" | grep 'REPLACE-' >&2
  exit 1
fi
printf '%s\n' "$out"

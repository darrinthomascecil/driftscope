#!/usr/bin/env bash
# Create the oauth2-proxy Secret from Key Vault. The Entra client secret + cookie secret are
# handled here and NEVER printed (no set -x; values go straight into kubectl via stdin).
# This is the one secret the app needs; Phase 0.6 replaces it with External Secrets.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

KV="$(tf_out key_vault_name)"
log "reading oauth2-proxy credentials from Key Vault $KV (values not printed)"

CID="$(az keyvault secret show --vault-name "$KV" -n oauth2-proxy-client-id     --query value -o tsv)"
CSECRET="$(az keyvault secret show --vault-name "$KV" -n oauth2-proxy-client-secret --query value -o tsv)"
COOKIE="$(openssl rand -hex 16)"   # 32 hex chars = 16 bytes, an AES key size oauth2-proxy accepts

[ -n "$CID" ]     || die "oauth2-proxy-client-id missing in Key Vault $KV"
[ -n "$CSECRET" ] || die "oauth2-proxy-client-secret missing in Key Vault $KV"
# Guard the classic AADSTS7000215 mistake: storing the credential keyId (a GUID) instead of the
# secret VALUE. The value is a ~40-char opaque string, never a bare GUID.
if printf '%s' "$CSECRET" | grep -Eq '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'; then
  die "oauth2-proxy-client-secret looks like a GUID (keyId), not the secret VALUE -- re-run the Entra secret reset; see docs"
fi

kubectl get ns driftscope >/dev/null 2>&1 || kubectl create ns driftscope
log "applying secret driftscope/oauth2-proxy"
kubectl -n driftscope create secret generic oauth2-proxy \
  --from-literal=client-id="$CID" \
  --from-literal=client-secret="$CSECRET" \
  --from-literal=cookie-secret="$COOKIE" \
  --dry-run=client -o yaml | kubectl apply -f -

unset CID CSECRET COOKIE
log "secret: OK"

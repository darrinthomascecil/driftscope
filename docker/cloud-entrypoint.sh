#!/bin/sh
# Make Azure auth "just work" for the two ways this image gets deployed, then hand
# off to driftscope. POSIX sh: the base is debian-slim, /bin/sh is dash.
#
#   mode 1  Microsoft-hosted infra with a managed identity (Container Apps job,
#           ACI, AKS): `az login --identity` mints tokens from IMDS, no secret.
#   mode 2  anywhere already logged into az (a mounted ~/.azure, a prior az login,
#           a workload-identity session): that existing credential is used as-is.
#
# GitHub is not Azure and stays on GITHUB_TOKEN (injected from Key Vault or -e),
# which `gh auth token` reads. This script never touches it.
#
# Azure DevOps' fixed resource id; an access token for it is what driftscope reads
# from AZURE_DEVOPS_TOKEN (source.ado_credential, Bearer inferred from the JWT).
set -eu

# Azure DevOps' fixed first-party application id -- the SAME GUID for every ADO
# org worldwide. It names the Azure DevOps service to AAD when requesting a token;
# it is NOT your org, tenant, or repo, and it never varies (barring an Azure
# sovereign cloud). A global constant, mirrored from source.ADO_RESOURCE.
ADO_RESOURCE="499b84ac-1321-427f-aa17-267ca6975798"
IMDS_URL="http://169.254.169.254/metadata/instance?api-version=2021-02-01"

# `az login --identity` retries the IMDS endpoint for over a minute before giving
# up, so off-Azure -- a local run, a CI box, anywhere without a managed identity --
# it hangs the whole scan on a login that was never going to work. A 2-second probe
# of IMDS decides in advance whether that attempt is even worth making.
imds_reachable() {
    curl -s --max-time 2 -H 'Metadata: true' "$IMDS_URL" >/dev/null 2>&1
}

# Prefer an existing login (mode 2). On Azure this also lets an explicitly mounted
# credential win over the platform identity, which is occasionally what you want.
# Record the outcome in a variable rather than re-running `az account show` later:
# the second check was redundant, and it coupled token-minting to state that only
# a real login persists -- fine on Azure, but a needless dependency.
have_azure=0
if az account show >/dev/null 2>&1; then
    echo "cloud-entrypoint: using the existing az login." >&2
    have_azure=1
elif imds_reachable && timeout 30 az login --identity ${AZURE_CLIENT_ID:+--username "$AZURE_CLIENT_ID"} >/dev/null 2>&1; then
    echo "cloud-entrypoint: signed in with the managed identity." >&2
    have_azure=1
else
    # Not fatal: driftscope degrades an unreachable forge/estate to `unknown`
    # findings, never to a false `current`. A GitHub-only scan needs no az at all.
    echo "cloud-entrypoint: no Azure credential (no managed identity, no existing az login);" >&2
    echo "  Azure DevOps / ARM / ACR / --live-azure results will be unknown, not failed." >&2
fi

# With any Azure session, publish the ADO token so even code paths that never call
# az still authenticate. `|| true` under `set -e`: a token failure must not abort
# the scan, only quiet the ADO half of it.
if [ "$have_azure" = 1 ]; then
    tok=$(az account get-access-token --resource "$ADO_RESOURCE" \
            --query accessToken -o tsv 2>/dev/null || true)
    if [ -n "${tok:-}" ]; then
        export AZURE_DEVOPS_TOKEN="$tok"
    fi
fi

exec driftscope "$@"

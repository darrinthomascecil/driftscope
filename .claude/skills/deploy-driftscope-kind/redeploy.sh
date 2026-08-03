#!/usr/bin/env bash
#
# Build the current driftscope working tree and roll it into a LOCAL kind cluster.
# Automates the loop the reused `:dev` tag requires (deploy/base/deployment.yaml says it):
#
#     build  ->  kind load  ->  rollout restart  ->  verify
#
# Because the image tag `driftscope:dev` is reused and imagePullPolicy is IfNotPresent,
# rebuilding alone changes nothing -- k8s keeps serving the cached image until it is
# reloaded into the node AND the pod is recreated. This does both.
#
# Deploys WHATEVER IS CHECKED OUT. `git checkout <branch>` first for the code you want live.
#
# Defaults target the driftscope-wi reference cluster; override via env:
#     CLUSTER=prod bash redeploy.sh                     # -> context kind-prod
#     NAMESPACE=... DEPLOYMENT=... IMAGE=... bash redeploy.sh
set -euo pipefail

CLUSTER="${CLUSTER:-driftscope-wi}"
CONTEXT="${CONTEXT:-kind-${CLUSTER}}"
NAMESPACE="${NAMESPACE:-driftscope}"
DEPLOYMENT="${DEPLOYMENT:-driftscope}"
IMAGE="${IMAGE:-driftscope:dev}"
ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel)}"
GITSHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BRANCH="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"

echo "==> deploy driftscope -> ${CONTEXT}"
echo "    ns=${NAMESPACE} deploy=${DEPLOYMENT} image=${IMAGE} branch=${BRANCH} git=${GITSHA}"

# --- guardrails: only ever touch a LOCAL kind cluster ------------------------------------
case "$CONTEXT" in
  kind-*) : ;;
  *) echo "REFUSING: '${CONTEXT}' is not a kind context. This skill only deploys to local kind." >&2
     exit 1 ;;
esac
kind get clusters | grep -qx "$CLUSTER" \
  || { echo "no kind cluster named '${CLUSTER}' (have: $(kind get clusters | paste -sd, -))" >&2; exit 1; }
kubectl --context "$CONTEXT" -n "$NAMESPACE" get deploy "$DEPLOYMENT" >/dev/null 2>&1 \
  || { echo "no deployment '${NAMESPACE}/${DEPLOYMENT}' in ${CONTEXT}" >&2; exit 1; }

# --- 1. build (buildx auto-sets TARGETARCH; --load puts the image where kind can read it) -
#        see the build-driftscope-image skill for the TARGETARCH rule.
echo "==> [1/4] build ${IMAGE}"
docker buildx build --load \
  --build-arg GIT_SHA="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)" \
  --build-arg IMAGE_VERSION="dev-${GITSHA}" \
  -t "$IMAGE" "$ROOT"

# --- 2. load into the kind node (overwrites the reused :dev content in containerd) --------
echo "==> [2/4] kind load into '${CLUSTER}'"
kind load docker-image "$IMAGE" --name "$CLUSTER"

# --- 3. roll a fresh pod so IfNotPresent picks up the just-reloaded image -----------------
echo "==> [3/4] rollout restart ${NAMESPACE}/${DEPLOYMENT}"
kubectl --context "$CONTEXT" -n "$NAMESPACE" rollout restart "deploy/${DEPLOYMENT}"
kubectl --context "$CONTEXT" -n "$NAMESPACE" rollout status  "deploy/${DEPLOYMENT}" --timeout=120s

# --- 4. verify ----------------------------------------------------------------------------
echo "==> [4/4] verify"
kubectl --context "$CONTEXT" -n "$NAMESPACE" get pods -o wide | grep -E "NAME|${DEPLOYMENT}" || true
if kubectl --context "$CONTEXT" -n "$NAMESPACE" exec "deploy/${DEPLOYMENT}" -- driftscope --version 2>/dev/null; then
  echo "==> LIVE: ${IMAGE} (branch ${BRANCH} @ ${GITSHA}) is running in ${CONTEXT}"
else
  echo "==> rolled out; skipped the in-pod --version check"
fi

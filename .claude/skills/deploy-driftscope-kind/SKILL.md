---
name: deploy-driftscope-kind
description: Build the current driftscope working tree and roll it into the LOCAL kind cluster — build → kind load → rollout restart → verify. Invoke whenever code changes need to actually run in kind ("deploy to kind", "get my changes in the cluster", "push the updated image to driftscope-wi"). Automates the reload+restart the reused :dev tag requires. Never touches AKS or remote clusters.
---

# Deploy the current driftscope code into a local kind cluster

driftscope in kind runs from a locally-built image `driftscope:dev` with
`imagePullPolicy: IfNotPresent` (`deploy/base/deployment.yaml`). Because that tag is
**reused**, editing code and rebuilding is not enough — Kubernetes keeps serving the
cached image until it is reloaded into the node's containerd **and** the pod is
recreated. Missing either half is the usual "I rebuilt but nothing changed" trap. This
skill does the whole loop in one shot.

## Run it

```bash
bash .claude/skills/deploy-driftscope-kind/redeploy.sh
```

Builds the working tree, `kind load`s it, `rollout restart`s the deployment, waits for
readiness, and verifies. Defaults target the **driftscope-wi** reference cluster.

> Deploys **whatever is checked out**. `git checkout <branch>` first for the code you
> want live — e.g. `git checkout feature/scan-fix` to get `scan --fix` into the cluster.

## Override the target

All optional env vars:

| var | default | meaning |
|---|---|---|
| `CLUSTER` | `driftscope-wi` | kind cluster name (context = `kind-<CLUSTER>`) |
| `NAMESPACE` | `driftscope` | deployment namespace |
| `DEPLOYMENT` | `driftscope` | deployment name |
| `IMAGE` | `driftscope:dev` | image tag to build + load |

```bash
CLUSTER=prod bash .claude/skills/deploy-driftscope-kind/redeploy.sh
```

## What it does, and why each step exists
1. **build** — `docker buildx build --load` (buildx auto-populates `TARGETARCH`; see the
   `build-driftscope-image` skill). Stamps `GIT_SHA` / `IMAGE_VERSION=dev-<sha>` so the
   running image traces back to a commit.
2. **kind load** — copies the image into the node's containerd, overwriting the reused
   `:dev` tag in place.
3. **rollout restart** — recreates the pod; with `IfNotPresent` + a just-reloaded tag the
   new pod runs the new content. (This reload+restart pair is exactly what the
   `deployment.yaml` comment warns is mandatory.)
4. **verify** — waits for `rollout status`, lists the fresh pod, runs `driftscope
   --version` inside it.

## Safety
- **Refuses any non-`kind-*` context** — it can never deploy to AKS or a remote cluster;
  it only build-loads into a local kind node.
- Fails fast if the kind cluster or the target deployment does not exist.
- Rolling a new image into `driftscope-wi` updates the running pod but does **not** tear
  the cluster down (that reference cluster is meant to be kept).

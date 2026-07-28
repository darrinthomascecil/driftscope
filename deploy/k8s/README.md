# Running the driftscope scan service on Kubernetes

Manifests to run `driftscope serve` (the HTTP scan service) as a hardened,
stateless Deployment behind a ClusterIP Service. Written for a **local cluster**
(kind / minikube / k3d / Docker Desktop), but the manifests themselves are
cluster-agnostic — only the image-loading step differs.

## What's here

| File | Purpose |
|------|---------|
| `deployment.yaml` | the service: non-root, read-only rootfs, dropped capabilities, `/healthz` probes, resource limits |
| `service.yaml` | ClusterIP on port 8080 (in-cluster only; port-forward to reach it) |

## Quick start (kind)

```bash
# 1. build the image
docker build -t driftscope:dev .

# 2. load it into the cluster's nodes (kind can't see your local Docker images)
kind load docker-image driftscope:dev --name <cluster-name>   # e.g. aks-local

# 3. apply
kubectl apply -f deploy/k8s/

# 4. wait for it, then reach it from your laptop
kubectl rollout status deploy/driftscope
kubectl port-forward svc/driftscope 8080:8080 &

curl -s http://127.0.0.1:8080/healthz
curl -s -X POST http://127.0.0.1:8080/scan \
  -H 'Content-Type: application/json' \
  -d '{"repo":"https://github.com/gorilla/mux"}'
```

Other clusters swap step 2: `minikube image load driftscope:dev`,
`k3d image import driftscope:dev`, or nothing at all for Docker Desktop (it shares
the daemon).

## Forge tokens (optional)

Without tokens a scan still runs — it just degrades the GitHub/ADO parts to
`unknown` instead of failing. To enable them, create the optional Secret the
Deployment already references:

```bash
kubectl create secret generic driftscope-tokens \
  --from-literal=GITHUB_TOKEN="$(gh auth token)" \
  --from-literal=AZURE_DEVOPS_TOKEN="$(az account get-access-token \
      --resource 499b84ac-1321-427f-aa17-267ca6975798 --query accessToken -o tsv)"
kubectl rollout restart deploy/driftscope
```

## Updating after a code change (the tag gotcha)

Reusing the `:dev` tag means k8s keeps serving the **cached** image — a rebuild
alone does nothing. Force it:

```bash
docker build -t driftscope:dev .
kind load docker-image driftscope:dev --name <cluster-name>
kubectl rollout restart deploy/driftscope
```

For an automated watch → rebuild → redeploy loop, a tool like Skaffold (`skaffold
dev`) or Tilt consumes these same manifests and does the three steps above on every
save. Not required — the manual loop is exactly the three commands.

## Security notes

- **ClusterIP, not LoadBalancer/NodePort.** `POST /scan` clones arbitrary URLs
  (an SSRF surface), so the service is in-cluster only; reach it via port-forward.
  The `--allow-host` args restrict which hosts it will clone from — keep them set.
- The pod runs unprivileged with a read-only root filesystem; only `/cache`,
  `/tmp` (clone workdir), and `/home/drift` are writable `emptyDir`s.
- A NetworkPolicy restricting egress to your forges (and blocking `169.254.169.254`)
  is a sensible addition in a shared cluster; not included here to stay minimal.

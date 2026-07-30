# Cluster-scoped mesh objects (applied outside the app kustomization)

These target AKS **managed-Istio** namespaces (`aks-istio-system`, `aks-istio-ingress`),
not the app namespace, so they are deliberately **not** in `../kustomization.yaml` — its
`namespace: driftscope` transformer would relocate them. Apply them directly.

Full rationale, per-decision, in [`docs/design-enterprise-istio-sso.md`](../../../../docs/design-enterprise-istio-sso.md).

## How these get applied

`make up` (the bootstrap, [`docs/quickstart-deploy.md`](../../../../docs/quickstart-deploy.md))
applies them for you: `cluster-init.sh` registers the ext_authz provider and restarts istiod,
`deploy.sh` applies `hardening.yaml`, `access-logs.yaml`, and `require-sso.authorizationpolicy.yaml`
after the app overlay (so the default-deny lands only once the ALLOW policies exist).

Managed Istio + the **external** ingress gateway are provisioned by Terraform
(`service_mesh_profile` in `terraform/main.tf`) — there is no `az aks mesh enable` step anymore.

The equivalent by hand, in order:

```sh
REV=asm-1-29   # terraform output istio_revision

# 1. Register the ext_authz provider, then bounce istiod so it loads
kubectl apply -f ext-authz-provider.configmap.yaml
kubectl -n aks-istio-system rollout restart deployment/istiod-$REV

# 2. Label the app namespace for sidecar injection (revision-pinned)
kubectl label namespace driftscope istio.io/rev=$REV --overwrite

# 3. oauth2-proxy Secret from Key Vault (see ../oauth2-proxy.yaml header, or `make secret`)

# 4. App overlay, THEN the mesh hardening + gateway-side SSO gate
kubectl apply -k ..
kubectl apply -f hardening.yaml -f access-logs.yaml -f require-sso.authorizationpolicy.yaml
```

The public entry point is Azure Front Door → the external gateway's public IP (the Cloudflare
tunnel is gone). Edge TLS + the origin lock live in [`../edge/`](../edge/README.md).

## Revision upgrades

`asm-1-29` is the mesh revision (`az aks show ... serviceMeshProfile.istio.revisions`).
On a managed-Istio upgrade, bump it in three places: this ConfigMap's name, the
`istiod-asm-1-XX` rollout target, and the namespace `istio.io/rev` label.

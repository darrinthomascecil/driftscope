# Cluster-scoped mesh objects (applied outside the app kustomization)

These target AKS **managed-Istio** namespaces (`aks-istio-system`, `aks-istio-ingress`),
not the app namespace, so they are deliberately **not** in `../kustomization.yaml` — its
`namespace: driftscope` transformer would relocate them. Apply them directly.

Full rationale, per-decision, in [`docs/design-enterprise-istio-sso.md`](../../../../docs/design-enterprise-istio-sso.md).

## One-time cluster setup

```sh
RG=driftscope-cloud AKS=driftscope-aks

# 1. Managed Istio + internal-only ingress gateway
az aks mesh enable            -g $RG -n $AKS
az aks mesh enable-ingress-gateway -g $RG -n $AKS --ingress-gateway-type Internal

# 2. Register the ext_authz provider, then bounce istiod so it loads
kubectl apply -f ext-authz-provider.configmap.yaml
kubectl -n aks-istio-system rollout restart deployment istiod-asm-1-29

# 3. Label the app namespace for sidecar injection (revision-pinned)
kubectl label namespace driftscope istio.io/rev=asm-1-29 --overwrite

# 4. oauth2-proxy Secret from Key Vault (see ../oauth2-proxy.yaml header for the exact cmd)

# 5. App-namespace resources via kustomize, then the gateway-side auth policy
kubectl apply -k ..
kubectl apply -f require-sso.authorizationpolicy.yaml
```

## Retarget the Cloudflare tunnel

Point the public hostname at the internal gateway (replaces the direct-to-app service):

```
service: http://aks-istio-ingressgateway-external.aks-istio-ingress.svc.cluster.local:80
```

## Revision upgrades

`asm-1-29` is the mesh revision (`az aks show ... serviceMeshProfile.istio.revisions`).
On a managed-Istio upgrade, bump it in three places: this ConfigMap's name, the
`istiod-asm-1-XX` rollout target, and the namespace `istio.io/rev` label.

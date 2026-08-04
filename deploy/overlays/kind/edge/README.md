# edge/ — local TLS via cert-manager (kind analogue of `cloud/edge/`)

The **local** counterpart of [`deploy/overlays/cloud/edge/`](../../cloud/edge/). Same cert-manager
object model as the cloud origin cert, but the trust anchor differs: the cloud proves control of a
public DNS zone via **Let's Encrypt DNS-01** (needs public reachability we don't have locally), so
here we stand up our **own self-signed root CA** and issue from it. Implements Option 2 of
[`docs/design-kind-local-tls.md`](../../../../docs/design-kind-local-tls.md).

The cert lands in secret `driftscope-tls` **in the gateway's namespace (`istio-system`)** — where
upstream Istio reads `credentialName` secrets from — and the Gateway `:443` server in
[`../mesh.yaml`](../mesh.yaml) serves it (mirrors the cloud `:443` SIMPLE listener).

| File | What |
|---|---|
| `cluster-issuer.yaml` | `SelfSigned` ClusterIssuer → root CA `Certificate` (isCA) → **CA `ClusterIssuer`** |
| `gateway-certificate.yaml` | leaf `Certificate` for `driftscope.local` → secret `driftscope-tls` in `istio-system` |

Chain: `selfsigned-root` (bootstrap) → `driftscope-local-ca` Certificate (secret in `cert-manager`,
the cluster-resource-namespace) → `driftscope-local-ca` CA ClusterIssuer → every leaf issues from it.

---

## Prerequisites

**Install cert-manager** (pinned to the cloud's version for parity):

```sh
helm repo add jetstack https://charts.jetstack.io && helm repo update
helm --kube-context kind-driftscope-wi install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --version v1.16.2 --set crds.enabled=true --wait
```

Unlike the cloud (Workload Identity for Azure DNS-01), the local self-signed issuer needs **no cloud
identity** — cert-manager signs everything in-cluster.

**Install MetalLB** (gives the gateway a real LB IP so no port-forward is needed):

```sh
kubectl --context kind-driftscope-wi apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/manifests/metallb-native.yaml
kubectl --context kind-driftscope-wi -n metallb-system rollout status deploy/controller --timeout=120s
kubectl --context kind-driftscope-wi apply -f ../metallb.yaml   # address pool (in the kind Docker subnet)
```

## Apply order

```sh
CTX=kind-driftscope-wi
# 1. issuer chain; wait for the root CA + CA ClusterIssuer to be Ready
kubectl --context $CTX apply -f cluster-issuer.yaml
kubectl --context $CTX wait --for=condition=Ready certificate/driftscope-local-ca -n cert-manager --timeout=60s
kubectl --context $CTX wait --for=condition=Ready clusterissuer/driftscope-local-ca --timeout=30s

# 2. leaf cert for the gateway host -> secret driftscope-tls in istio-system
kubectl --context $CTX apply -f gateway-certificate.yaml
kubectl --context $CTX wait --for=condition=Ready certificate/driftscope-tls -n istio-system --timeout=60s

# 3. the Gateway :443 server (../mesh.yaml) now finds secret driftscope-tls and serves it.
```

## Reach it — pick a local host that resolves to the gateway IP (host-side, NOT committed)

`../metallb.yaml` gives the ingressgateway a stable `EXTERNAL-IP` from the kind Docker subnet, so the
gateway is reachable directly — no port-forward. You then point a **local hostname of your choosing** at
that IP; that name is your `HOST` param (bound into `params.yaml` + the leaf cert at deploy). The repo
assumes NOTHING about the name or the mapping — it's all host-side and uncommitted. Discover the IP
(never hardcode it):

```sh
HOST=driftscope.local.test     # <- YOUR choice; whatever you'll type in the browser
LB=$(kubectl --context kind-driftscope-wi -n istio-system get svc istio-ingressgateway \
       -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
```

Map `$HOST -> $LB` however you like — none of it goes in git. One clean macOS option is **dnsmasq**
(so no `/etc/hosts` edits); a `.local.test` wildcard is handy but any suffix works:

```sh
# /opt/homebrew/etc/dnsmasq.conf  (host-local, not in git) -- the ONLY place the LB IP appears
address=/$HOST/$LB
# route that TLD's lookups to dnsmasq via /etc/resolver/<tld>  (nameserver 127.0.0.1)
```

(Verified on Docker Desktop here via host->kind-subnet routing: `curl -k https://$LB/healthz` returns
200. On a box without that routing, fall back to `port-forward svc/istio-ingressgateway 8443:443`.)

Trust the local CA once (unavoidable for any private CA), then browse:

```sh
kubectl --context $CTX get secret driftscope-local-ca -n cert-manager \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > /tmp/driftscope-local-ca.crt
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain /tmp/driftscope-local-ca.crt

# browser -> https://$HOST/ui   (green padlock; the leaf cert's dnsName == $HOST, rendered from REPLACE-HOST)
```

## Renewal

cert-manager renews the leaf 30 days before its 90-day expiry, unattended, off the local CA — no
DNS-01 propagation to wait on. The root CA lasts 5 years. `kubectl get certificate -A` shows status.

## Next step (not done here): flip SSO to HTTPS

Serving `:443` is independent of the SSO redirect. To move oauth2-proxy/Entra onto https, see the
"flip SSO to https" step in [`docs/design-kind-local-tls.md`](../../../../docs/design-kind-local-tls.md).

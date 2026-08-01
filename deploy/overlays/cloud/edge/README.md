# edge/ — origin TLS + origin lock (aks-istio-ingress)

These objects live in the managed-Istio ingress namespace (`aks-istio-ingress`), **not** the
app namespace, so — like `mesh/` — they are applied **directly**, outside the kustomization.
All three files are committed `REPLACE-*` **templates** (ADR 0001): the bootstrap renders them with
`deploy/gitops/render-edge.sh`, binding the DNS zone / origin host / Front Door GUID / ACME email
from `terraform output` at deploy time. Terraform emits those values as **outputs** and no longer
writes these files, so nothing per-deployment is committed to git.

The full design + the rejected alternatives (HTTP-01, Premium/Private Link) are in
[`docs/design-edge-origin-cert.md`](../../../../docs/design-edge-origin-cert.md).

| File | What | Mode |
|---|---|---|
| `cluster-issuer.yaml` | Let's Encrypt DNS-01 `ClusterIssuer` (Azure DNS, Workload Identity) | custom domain only |
| `origin-certificate.yaml` | `Certificate` for `origin.<domain>` → secret `driftscope-origin-tls` | custom domain only |
| `origin-lock.authorizationpolicy.yaml` | DENY requests missing this Front Door's `X-Azure-FDID` | **both modes** |

In demo mode (no `base_domain`) the first two render as no-op comments — don't apply them.

---

## Prerequisites

### 1. Point the external gateway at the BYO public IP

Terraform creates a static public IP (`<prefix>-gw-pip`) and grants the cluster identity
Network Contributor on it. Attach it to the managed external gateway service so the origin has
a stable IP the Front Door origin + `origin.<domain>` A record target:

```sh
RG=$(terraform -chdir=../../../../terraform output -raw resource_group)
PIP=$(terraform -chdir=../../../../terraform output -raw gateway_public_ip)   # informational
kubectl annotate svc aks-istio-ingressgateway-external -n aks-istio-ingress --overwrite \
  service.beta.kubernetes.io/azure-pip-name="$(terraform -chdir=../../../../terraform output -raw resource_group | sed 's/-cloud$//')-gw-pip" \
  service.beta.kubernetes.io/azure-load-balancer-resource-group="$RG"
```

(The PIP name is `<name_prefix>-gw-pip`; the annotation's resource-group is the cluster RG where
the PIP lives — not the AKS node resource group.)

### 2. Install cert-manager with Workload Identity (custom-domain mode only)

The DNS-01 issuer authenticates as the `certmanager` managed identity. cert-manager's pods must
run as the `cert-manager` service account **annotated with that identity's client id** and
**labeled for Workload Identity**. Install (pin the version) with those values:

```sh
CLIENT_ID=$(terraform -chdir=../../../../terraform output -raw certmanager_client_id)

helm repo add jetstack https://charts.jetstack.io && helm repo update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --version v1.16.2 \
  --set crds.enabled=true \
  --set "serviceAccount.annotations.azure\.workload\.identity/client-id=$CLIENT_ID" \
  --set "podLabels.azure\.workload\.identity/use=true"
```

The federated credential Terraform created trusts `system:serviceaccount:cert-manager:cert-manager`,
so the SA name/namespace above must match.

---

## Apply order

**Custom-domain mode:**

```sh
# 1. issuer + certificate (cert-manager must be installed first, per above)
kubectl apply -f cluster-issuer.yaml
kubectl apply -f origin-certificate.yaml

# 2. wait for issuance (DNS-01 propagation can take a few minutes)
kubectl wait --for=condition=Ready certificate/driftscope-origin -n aks-istio-ingress --timeout=5m

# 3. the Gateway :443 server (in the overlay) now finds secret driftscope-origin-tls and serves it.
# 4. lock the origin to Front Door
kubectl apply -f origin-lock.authorizationpolicy.yaml
```

**Demo mode (no custom domain):** only the origin lock applies —

```sh
kubectl apply -f origin-lock.authorizationpolicy.yaml
```

---

## Order note (origin lock + default-deny)

`mesh/hardening.yaml` sets a mesh-wide default-deny with `gateway-allow-ingress` (ALLOW all
inbound to the external gateway). This origin lock is a **DENY** on top of that — in Istio,
DENY is evaluated before ALLOW, so a request without the correct `X-Azure-FDID` is rejected even
though `gateway-allow-ingress` would otherwise permit it. Legitimate Front Door traffic carries
the header and passes both, then still faces the CUSTOM ext_authz (SSO) check. Apply the lock
any time after `gateway-allow-ingress` exists.

# Design: TLS on the local kind cluster

## Goal

Serve the driftscope app over **HTTPS** on the local kind cluster (`kind-driftscope-wi`) so the
browser reaches `https://…`, the SSO cookie can be `Secure`, and the local path matches the cloud
(which terminates TLS at the Istio gateway). Today everything is plain `http://localhost:8443` with
`--cookie-secure=false`.

## Current setup (what we're designing against)

| Piece | State |
|---|---|
| Cluster | `kind-driftscope-wi`, upstream **Istio 1.29.5** |
| Gateway | `deploy/overlays/kind/mesh.yaml` — **HTTP `:80` listener only**, `hosts: ["*"]` |
| Access | `kubectl port-forward svc/istio-ingressgateway 8443:80` |
| SSO | oauth2-proxy → Entra; redirect `http://localhost:8443/oauth2/callback`, `--cookie-secure=false` |
| Entra app | `driftscope-kind-sso`, redirect registered as the **http** URL above |
| Cloud reference | `deploy/overlays/cloud/istio-routing.yaml` — `:443` **SIMPLE** TLS listener, `credentialName: driftscope-origin-tls` (cert-manager DNS-01), Front Door in front |

## Constraints that shape the options

1. **Host → Docker-network routing — verified reachable here.** The classic kind-on-Mac worry is that
   the Docker bridge (and any `LoadBalancer` IP) lives inside the Docker Desktop VM, unreachable from
   the host. **That is not the case in this environment**: a direct `curl` from the macOS host to the
   kind node's Docker IP + NodePort (`http://172.18.0.4:31357/healthz`) returns **200**. So the Docker
   network **is** routable, which means **MetalLB `LoadBalancer` IPs are reachable from the browser** and
   a `port-forward` is *not* required. This is the better, more cloud-like path. (It depends on
   host→Docker routing being present — on a stock Docker Desktop without it, fall back to `port-forward`;
   re-run that NodePort `curl` to check on a new machine.)
2. **Keep the cluster.** `kind-driftscope-wi` is a kept reference cluster, so anything that requires
   **recreating** the cluster (e.g. `extraPortMappings` for host ports) is out.
3. **`port-forward` is a TCP passthrough.** It cannot *add* TLS — it can only forward bytes to a
   listener that already terminates TLS. So TLS must terminate either **at the gateway** (behind the
   port-forward, on a `:443` listener) or **in front of it** (a local proxy).
4. **Trust.** A self-signed cert gives a browser warning; a cert from a CA the OS trusts gives a
   green padlock. **`mkcert` is already installed** (CA root present), so a *trusted* local cert is
   cheap. cert-manager is **not** installed.
5. **Hostname.** A cert is for a name. `localhost` works directly; a custom name
   (`driftscope.local` via `/etc/hosts`) is more cloud-like (host-based routing) but adds a step.

## Changes common to *every* HTTPS option

Independent of where TLS terminates, going HTTPS means:

- **Gateway:** add a `:443` `SIMPLE` TLS server with a `credentialName` (mirrors the cloud Gateway).
- **Entra app:** update `driftscope-kind-sso`'s redirect URI to the **https** callback.
- **oauth2-proxy:** `OAUTH2_PROXY_REDIRECT_URL` → https, `--cookie-secure=true`, whitelist-domains
  updated to the https host.
- **Reach the `:443` listener:** with **MetalLB** the gateway's `LoadBalancer` service exposes `:443`
  on a real external IP directly (no tunnel); without it, `port-forward svc/istio-ingressgateway 8443:443`.

---

## Options

| # | Access | Cert source | Padlock | Cloud parity | Setup | Needs host→Docker routing |
|---|---|---|---|---|---|---|
| **5 ★** | **MetalLB** LB IP + hostname | mkcert / cert-manager | green | **highest** (real LB + `:443` + hostname) | medium | yes — **verified here** |
| 1 | port-forward `:443` | **mkcert** leaf | green (auto) | high | low | no (portable fallback) |
| 2 | either | cert-manager self-signed CA | green (trust CA once) | high + **declarative** | medium | either |
| 3 | local Caddy proxy | Caddy / mkcert | green | **low** (TLS outside the mesh) | low | no |
| 4 | gateway, self-signed | openssl | ⚠ warning | high | low | no (bad UX) |

### Option 1 — Gateway TLS with an mkcert cert, via `port-forward :443`  ★ recommended

Add a `:443` SIMPLE listener; put an `mkcert`-generated cert into the `credentialName` secret; the
gateway terminates TLS. Because `mkcert`'s CA is already in the OS trust store, the browser shows a
green padlock with no warnings.

**Pros**
- Closest to cloud *with the least work*: `SIMPLE` + `credentialName` is the **exact** cloud pattern
  (`deploy/overlays/cloud/istio-routing.yaml`), just a different cert issuer.
- **Zero new infra** — `mkcert` is already installed; no cert-manager, no proxy.
- Green padlock automatically (mkcert CA already trusted), so OIDC/`Secure` cookies "just work".
- TLS terminates **inside the mesh's ingress**, so you're exercising the real gateway TLS path.

**Cons**
- **Static cert** — regenerate by hand when it expires (mkcert leaves last ~2 years) and re-create
  the secret. No auto-renewal.
- The mkcert CA is **machine-local** — trusted only on this laptop (fine for local dev; a teammate
  would run their own `mkcert -install`).
- The cert lives in a hand-created secret, so it's **not something you'd commit** — the overlay would
  carry only the `:443` listener + a documented "make the secret with mkcert" step.

### Option 2 — Gateway TLS with a cert-manager self-signed CA

Install cert-manager; create a self-signed `Issuer` → a root **CA** `Certificate` → a CA
`ClusterIssuer` → a `Certificate` for the gateway host that lands in the `credentialName` secret.
Trust the generated CA once in the macOS keychain.

**Pros**
- **Most cloud-like** — the cloud uses cert-manager for the origin cert; this mirrors the automation,
  including **auto-renewal** and issuing for multiple hosts.
- **Fully declarative** — the Issuer + Certificate are YAML, so this *can* be committed into
  `deploy/overlays/kind/` (fits the IaC north-star) with no secret material in git.
- No dependency on a host-side tool; works headless / in CI.

**Cons**
- **More moving parts**: install cert-manager, author the issuer chain, wait for issuance.
- You must **manually trust** the generated root CA in the OS keychain (mkcert does this for you).
- Heavier than the need if this is only ever a single local box.

### Option 3 — Terminate TLS at a local Caddy (or nginx) in front of the port-forward

Run Caddy on the host: it serves HTTPS on `:8443` (Caddy's own local CA, or an mkcert cert) and
reverse-proxies to `http://localhost:8080`, where a second port-forward hits the gateway's `:80`.

**Pros**
- **Simplest to stand up** — Caddy auto-TLS is one line; no in-cluster changes at all.
- Nothing about the mesh/gateway/cert has to change.

**Cons**
- **Least parity** — TLS terminates *outside* the cluster; the gateway leg is still plain HTTP, so
  you're not exercising the real gateway TLS path the cloud uses. The "edge" becomes Caddy, not the
  gateway.
- **Another host process** to run and keep alive alongside the port-forward.
- Teaches you nothing transferable to the cloud topology.

### Option 4 — Gateway TLS with a plain openssl self-signed cert

Same as Option 1 but the cert is a bare `openssl` self-signed cert (no trusted CA).

**Pros**: zero extra tools.
**Cons**: **browser "Not secure" warning** on every context; OIDC redirects over an untrusted origin
are finicky; strictly worse than Option 1 given `mkcert` is already here. Only makes sense if `mkcert`
were unavailable.

### Option 5 — MetalLB LoadBalancer + hostname + gateway TLS  ★ recommended

Install MetalLB with an address pool inside the kind Docker subnet; `istio-ingressgateway` gets a real
`EXTERNAL-IP`. Map `driftscope.local` (`/etc/hosts`) to it, terminate TLS at the gateway (mkcert or
cert-manager), and drop the port-forward. Browser → `https://driftscope.local` (standard `:443`) → app.

**Pros**
- **Most cloud-like topology** — a real `LoadBalancer` with an `EXTERNAL-IP` (like AKS), host-based
  VirtualService routing off a hostname, standard `:443` URLs (no `:8443`). The closest the local box
  gets to "as it would be in the cloud".
- **No port-forward to babysit** — a stable IP that outlives a shell session; Grafana / Prometheus /
  the app can each get their own IP or hostname instead of juggling forwards.
- Composes with either cert source — Option 1's mkcert secret or Option 2's cert-manager `Certificate`.

**Cons**
- **Depends on host→Docker routing** — verified working here (the NodePort curl returned 200), but not
  guaranteed on a stock Docker Desktop elsewhere, so it's less portable than a port-forward.
- One `/etc/hosts` entry; the MetalLB pool must sit inside the kind subnet (`172.18.0.0/16`) and not
  collide with node/container IPs (e.g. `172.18.255.200-250`).
- A little more in-cluster infra (MetalLB controller + speaker) — trivial, but it exists.

---

## Recommendation

**Option 5 + a cert — MetalLB LoadBalancer + hostname + gateway TLS.** Because the host routes to the
Docker network here (verified), a real LB IP is reachable, so this is the most cloud-faithful setup and
it drops the port-forward: browser → `https://driftscope.local` (standard `:443`) → gateway → app. For
the cert, **start with mkcert** (Option 1's mechanics — green padlock, zero new infra) and **graduate to
cert-manager** (Option 2) when you want it declarative, auto-renewing, and committed into
`deploy/overlays/kind/` exactly as the cloud does.

If host→Docker routing is ever absent (a different machine / stock Docker Desktop), fall back to
**Option 1** — identical gateway TLS, just reached by `port-forward :443` instead of the LB IP. Only the
access method differs, so the fallback is cheap.

Skip 3 (TLS outside the mesh breaks the gateway-terminates parity) and 4 (worse than mkcert for free).

## Concrete recipe — gateway TLS + cert (shared by Options 1 and 5)

```sh
CTX=kind-driftscope-wi ; PORT=8443 ; HOST=localhost
mkcert -install                                   # ensure the mkcert CA is trusted (idempotent)
mkcert -cert-file /tmp/tls.crt -key-file /tmp/tls.key "$HOST" 127.0.0.1

# the gateway reads credentialName secrets from ITS namespace (istio-system)
kubectl --context $CTX -n istio-system create secret tls driftscope-tls \
  --cert=/tmp/tls.crt --key=/tmp/tls.key --dry-run=client -o yaml | kubectl --context $CTX apply -f -
```

Add the `:443` server to the Gateway in `deploy/overlays/kind/mesh.yaml`:

```yaml
    - port: { number: 443, name: https, protocol: HTTPS }
      hosts: [ "*" ]
      tls: { mode: SIMPLE, credentialName: driftscope-tls }
```

Then flip SSO to https and re-point Entra:

```sh
az ad app update --id <driftscope-kind-sso appId> \
  --web-redirect-uris "https://$HOST:$PORT/oauth2/callback"
# oauth2-proxy: OAUTH2_PROXY_REDIRECT_URL=https://localhost:8443/oauth2/callback, --cookie-secure=true
# access — pick ONE:
kubectl --context $CTX -n istio-system port-forward svc/istio-ingressgateway 8443:443   # Option 1 fallback
# browser -> https://localhost:8443/ui   (green padlock -> Entra login -> app)
```

## MetalLB layer (Option 5) — a real LB IP instead of the port-forward

```sh
# 1. install MetalLB (check for the current release tag)
kubectl --context $CTX apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/manifests/metallb-native.yaml
kubectl --context $CTX -n metallb-system rollout status deploy/controller --timeout=120s

# 2. address pool INSIDE the kind Docker subnet (172.18.0.0/16), clear of node/container IPs
cat <<'YAML' | kubectl --context $CTX apply -f -
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata: { name: kind, namespace: metallb-system }
spec: { addresses: [ "172.18.255.200-172.18.255.250" ] }
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata: { name: kind, namespace: metallb-system }
spec: { ipAddressPools: [ kind ] }
YAML

# 3. the istio-ingressgateway LoadBalancer now gets an EXTERNAL-IP from the pool
LB=$(kubectl --context $CTX -n istio-system get svc istio-ingressgateway \
       -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "$LB driftscope.local" | sudo tee -a /etc/hosts            # map the hostname to the LB IP

# 4. issue the cert for the hostname (not localhost), recreate driftscope-tls, and set the
#    Gateway/VirtualService hosts + Entra redirect + oauth2-proxy all to https://driftscope.local
mkcert -cert-file /tmp/tls.crt -key-file /tmp/tls.key driftscope.local
# browser -> https://driftscope.local/ui   (standard :443, no port-forward, green padlock)
```

### Note on hostname
`localhost` is simplest. For closer parity with the cloud's **host-based** routing (the VS keys off
the public host), issue the cert for a real name — `mkcert driftscope.local localhost` — add
`127.0.0.1 driftscope.local` to `/etc/hosts`, and set the Gateway/VirtualService `hosts` + the Entra
redirect + oauth2-proxy to `https://driftscope.local:8443`. One extra step, more realistic routing.

# Edge TLS & the origin-certificate decision (Phase 0.4b)

This is the "why" behind how DriftScope terminates TLS between Azure Front Door and the
AKS/Istio ingress gateway. If you only read one section, read
[The decision](#the-decision) and [Threat model, honestly](#threat-model-honestly).

---

## The shape of the edge

Every request crosses **two** TLS legs, and they are independent:

```
                leg 1                         leg 2
  browser ───────────────▶ Azure Front Door ───────────────▶ AKS Istio gateway ──▶ app
          HTTPS (FD cert)                     HTTPS? / HTTP?   (mTLS inside the mesh)
```

- **Leg 1 — browser → Front Door.** Always HTTPS, always a real CA cert. Front Door
  issues and auto-renews a **managed certificate** for the public hostname for free. This
  leg is never in question and needs nothing from us. (In custom-domain mode the cert is
  for `driftscope.<your-domain>`; in demo mode it's for the `*.azurefd.net` endpoint.)

- **Leg 2 — Front Door → origin.** This is the entire subject of this document. Front Door
  connects to a **public IP** on the AKS external ingress gateway. To make that leg HTTPS
  *and have Front Door validate it* (`certificate_name_check_enabled = true`), the gateway
  must present a **CA-issued certificate for the name Front Door dials** — and getting one
  is the hard part.

Why it's hard: a public CA (Let's Encrypt) only issues a cert after you **prove control of
the DNS name**. The proof (an ACME "challenge") is where the options diverge.

---

## The three ways to get a leg-2 certificate

### Option A — DNS-01 (the chosen path)

**Mechanism.** cert-manager proves control of the name by writing a
`_acme-challenge.<name>` **TXT record** into a DNS zone *we own*. Let's Encrypt reads the
TXT record, confirms it, and issues the cert. cert-manager stores it in a Kubernetes secret
that the Istio gateway serves.

- The name we certify is **`origin.<your-domain>`** — an A record pointing straight at the
  gateway's public IP. Front Door dials `origin.<your-domain>` (SNI), the gateway presents
  the matching cert, Front Door validates it. Encrypted and authenticated end-to-end.
- cert-manager authenticates to Azure DNS with **Workload Identity** (a managed identity
  federated to the cert-manager service account) holding **DNS Zone Contributor** on the
  zone. No secrets, no stored credentials.

**Requires a domain.** DNS-01 writes to a zone we control. In demo mode the origin name is
`…cloudapp.azure.com`, whose zone **Microsoft owns** — so DNS-01 is impossible there. Demo
mode therefore falls back to an HTTP origin leg (see [Demo mode](#demo-mode)).

**Pros**
- Rock-solid, boring, universally-supported renewal. Runs every ~60 days with zero moving
  parts on the data path. Nothing to route, nothing to expose.
- Works even though `origin.<domain>` and the public `driftscope.<domain>` are different
  names pointing at different places — DNS-01 doesn't care where a name *resolves*, only
  that we can write its TXT record.
- No inbound HTTP path has to be opened for the challenge.

**Cons**
- Only available when you bring a custom domain (the whole reason demo mode differs).
- One more managed identity + role assignment to provision (all in Terraform).

### Option B — HTTP-01

**Mechanism.** Let's Encrypt fetches `http://<name>/.well-known/acme-challenge/<token>`
over port 80. cert-manager must route that exact path to a solver pod it spins up.

**Why it's awkward here.** cert-manager's HTTP-01 solver targets an **Ingress controller**
or **Gateway API** object. DriftScope's cloud overlay uses a **classic Istio `Gateway`**,
which cert-manager does *not* natively drive. Making it work means hand-rolling a
`VirtualService` that routes the ACME path to cert-manager's (dynamically-named) solver
service — and re-proving it every renewal.

**Pros**
- Works on the `cloudapp.azure.com` name too, so it *could* give HTTPS on the origin leg in
  **both** modes (demo included) — the one thing DNS-01 can't do.

**Cons**
- The renewal mechanism is fragile and bespoke. If the hand-rolled ACME routing drifts, the
  cert **silently expires** and leg 2 breaks — **including in production**, because
  production would use the same HTTP-01 path. That makes the *paying-customer* path *less*
  reliable to buy a tidier demo.
- Requires port 80 to serve challenges and custom Envoy/VirtualService plumbing that is
  hard to test outside a live cluster.

### Option C — Front Door Premium + Private Link

**Mechanism.** Premium establishes a **private** connection from Front Door to the origin
over the Microsoft backbone. There is **no public origin and no origin certificate at all** —
the leg-2 problem simply ceases to exist.

**Pros**
- Deletes the entire problem. Also unlocks Microsoft-managed WAF rulesets (bot protection,
  OWASP CRS) that Standard lacks.

**Cons**
- ~10× the cost of Standard (roughly ~$330/mo base vs ~$35/mo). For DriftScope's stage the
  cost isn't justified by the leg-2 problem alone.
- **Ruled out for this project.** (If bot/WAF requirements later dominate the decision, Premium
  becomes attractive for *those* reasons — and it happens to also erase leg 2.)

---

## The decision

**Option A (DNS-01).** For a product, the deployments that matter run with a custom domain,
and there DNS-01 gives the most **air-tight** posture on the exact leg customers use:
encrypted, CA-validated, and — critically — renewed by the most reliable mechanism ACME
offers. We deliberately did **not** weaken that production path to make the demo prettier.

Counter-intuitive but important: the "HTTPS everywhere including the demo" option (B) is the
*less* air-tight choice for production, because it ships a fragile renewal mechanism onto the
customer path. Reliability of auto-renewal **is** a security property — a silently-expired
cert is an outage and a scramble.

### Demo mode

When no custom domain is set (`base_domain` unset), DriftScope runs on Azure-provided names
(`*.azurefd.net` public, `*.cloudapp.azure.com` origin) so it clones-and-runs with zero
domain ownership. There, leg 2 is **HTTP** over the Azure backbone:

```
  browser ──HTTPS (FD managed cert)──▶ Front Door ──HTTP──▶ AKS gateway   (DEMO MODE ONLY)
```

This is acceptable **only** because:
- Leg 1 (the browser leg) is still fully HTTPS — no user ever talks plaintext to the app.
- Leg 2 traverses the Azure backbone to a public IP, not the open internet.
- It is **explicitly demo-only** and documented as such. No real deployment runs this way.

A security reviewer will still flag "plaintext backhaul," and they're right to — which is
exactly why it lives behind the `base_domain` toggle and never on the production path. If the
demo's plaintext leg is unacceptable for your context, the clean fix is to **bring a domain**
(flip to Option A), not to bolt Option B onto production.

---

## How it's wired (custom-domain mode)

| Piece | What it does | Where |
|---|---|---|
| `origin.<domain>` A record | Resolves to the gateway public IP so Front Door can dial the origin | Azure DNS (`dns.tf`) |
| cert-manager managed identity | Federated to the cert-manager service account; holds **DNS Zone Contributor** | `certmanager.tf` |
| `ClusterIssuer` (Let's Encrypt, DNS-01) | Issues the origin cert via Azure DNS TXT records | `edge/cluster-issuer.yaml` (generated) |
| `Certificate` for `origin.<domain>` | Produces secret `driftscope-origin-tls` in `aks-istio-ingress` | `edge/origin-certificate.yaml` (generated) |
| Gateway `:443` server | Presents `driftscope-origin-tls`; `hosts: ["*"]` so any SNI terminates, HTTP routing keys off the Host header | `istio-routing.yaml` |
| Front Door origin | `host_name = origin.<domain>`, `forwarding_protocol = HttpsOnly`, `certificate_name_check_enabled = true` | `frontdoor.tf` |
| Origin lock (`X-Azure-FDID`) | Gateway **denies** any request not carrying *this* Front Door's GUID header — so the raw public IP can't be used to bypass Front Door | `edge/origin-lock.authorizationpolicy.yaml` (generated) |

Two names, on purpose:
- **`origin.<domain>`** is the TLS/SNI name Front Door dials (A → gateway IP). It's what the
  origin cert is for.
- **`driftscope.<domain>`** is the public name (CNAME → Front Door) and the HTTP `Host`
  header Front Door forwards, which Istio and oauth2-proxy route on. Keeping the Host as the
  public name is what makes oauth2-proxy's redirect/cookie logic correct.

The gateway's `:443` server uses `hosts: ["*"]` precisely to bridge these: it terminates the
`origin.<domain>` SNI with the origin cert, then hands off to the VirtualService, which binds
on the `driftscope.<domain>` Host.

---

## Threat model, honestly

| Concern | DNS-01 (chosen) | HTTP-01 | Premium/Private Link |
|---|---|---|---|
| Browser leg encrypted | ✅ always | ✅ always | ✅ always |
| Origin leg encrypted (production) | ✅ | ✅ | ✅ (private, no cert) |
| Origin leg encrypted (demo) | ❌ HTTP backhaul (documented, demo-only) | ✅ | n/a |
| Renewal reliability | ✅ standard, robust | ⚠️ bespoke, can silently expire | ✅ none needed |
| Raw-IP origin bypass | ✅ blocked by `X-Azure-FDID` lock + service tag | ✅ same lock | ✅ private, no public origin |
| Disabled-cert-validation finding (CWE-295) | ✅ none (real cert, validation on) | ✅ none | ✅ none |
| Cost | ~$35/mo (Standard) | ~$35/mo (Standard) | ~$330/mo |

The origin lock matters independently of the cert choice: even with a valid TLS leg, anyone
who learns the gateway's public IP could talk to it directly. The gateway therefore **rejects
any request that doesn't carry Front Door's `X-Azure-FDID` GUID**, and (belt-and-suspenders)
the public IP is restricted to the `AzureFrontDoor.Backend` service tag. The only path to the
app is *through your* Front Door.

---

## TL;DR

- Two TLS legs; the browser leg is always a free Front Door cert — solved.
- The origin leg needs a cert for the name Front Door dials; the challenge type is the fork.
- **DNS-01** (write a TXT record in a zone we own, authenticated by Workload Identity) is the
  most reliable production path and what we chose.
- It needs a custom domain; the domain-less demo mode uses an HTTP origin leg, documented and
  fenced off from production.
- **HTTP-01** could cover the demo too but ships fragile renewal onto production — rejected.
- **Premium/Private Link** erases the problem but at ~10× cost — ruled out for now.

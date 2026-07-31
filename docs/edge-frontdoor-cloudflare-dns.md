# Front Door on a Cloudflare-hosted domain (`dns_provider = "cloudflare"`)

Goal: **Azure Front Door serves the app at `driftscope.<your-domain>`** with a valid TLS cert and
an encrypted origin hop — while the **domain stays in Cloudflare** (registrar + DNS) and **nothing
in Cloudflare is managed by Terraform**. Terraform owns all of Azure; Cloudflare holds a handful of
**static records you add by hand, once**.

This is the alternative to the default `dns_provider = "azure"` mode (where Terraform creates the
Azure DNS zone and you delegate the whole domain to Azure). Pick this when the domain must stay in
Cloudflare.

## The architecture

```
client ──HTTPS (Front Door managed cert)──▶ Front Door (edge, WAF, TLS) ──HTTPS──▶ AKS gateway ──mTLS──▶ app
                                                                          ▲ origin cert: cert-manager / Let's Encrypt
DNS: Cloudflare (authoritative, DNS-only)      Edge cert: Front Door managed      X-Azure-FDID origin lock
```

- **Cloudflare is DNS-only (grey cloud).** It answers `driftscope → Front Door` and steps out of the
  data path. Front Door is the edge and terminates client TLS. If the record were *proxied* (orange),
  Cloudflare would answer the handshake and you'd have two stacked edges — so **grey cloud is
  mandatory** for the app record.
- **Edge cert** (public, `driftscope.<domain>`): **Front Door managed certificate** — Microsoft
  issues/renews/deploys it. Zero ops; it only needs the one-time `_dnsauth` TXT below.
- **Origin cert** (Front Door → AKS): **cert-manager → Let's Encrypt**, so the backend hop is
  encrypted *and* Front-Door-trusted. The DNS-01 challenge is **delegated to a small Azure
  `acme.<domain>` zone**, so cert-manager needs **no Cloudflare access** — it writes only to Azure
  (via Workload Identity). Renewal is unattended.

## The records you add in Cloudflare (by hand, once)

After `terraform apply`, run `terraform output cloudflare_records` and add each in Cloudflare. All are
**DNS-only (grey cloud)**:

| Name (in your zone) | Type | Value | Purpose |
|---|---|---|---|
| `driftscope` | CNAME | `<endpoint>.azurefd.net` | app → Front Door |
| `_dnsauth.driftscope` | TXT | `<validation token>` | Front Door managed-cert validation (one-time) |
| `origin` | A | `<gateway public IP>` | Front Door → origin |
| `acme` | NS | `<Azure name servers>` | delegate `acme.<domain>` to Azure for DNS-01 |
| `_acme-challenge.origin` | CNAME | `origin.acme.<domain>` | origin-cert challenge → delegated zone |

Terraform prints all five with their exact values. The token also comes from
`terraform output frontdoor_managed_cert_validation_token`; the Azure name servers from
`terraform output acme_delegation_name_servers`.

Also: **delete the old Cloudflare Tunnel + its `driftscope` record** if one still exists (the source
of the stale 1033/530 page) — it's not part of this architecture.

## Deploy flow (one manual DNS gate)

Because Front Door's managed cert won't issue until the CNAME + `_dnsauth` resolve, and cert-manager
needs the `acme` delegation live, `make up` is **not one-shot** in this mode:

1. `make infra` — Terraform builds Azure (Front Door, gateway IP, the `acme` delegation zone, the
   cert-manager identity).
2. **Add the five records above in Cloudflare** (from `terraform output cloudflare_records`). One-time.
3. Wait for Front Door to validate the domain and issue its managed cert (minutes), and for
   `acme.<domain>` delegation to propagate.
4. `make cluster && make deploy` — cert-manager issues the origin cert (DNS-01 via the delegated
   zone), and the app goes live at `https://driftscope.<domain>`.

Renewals need **no** further DNS changes.

## Verify

- `dig driftscope.<domain>` → a **CNAME to `azurefd.net`** (grey cloud), *not* Cloudflare proxy IPs.
- `dig NS acme.<domain>` → the Azure name servers (delegation is live).
- Front Door custom domain shows **Validated**; `curl -sI https://driftscope.<domain>` → `x-azure-ref`
  header + a Front-Door-issued cert for your domain.
- `kubectl -n aks-istio-ingress get certificate driftscope-origin` → **Ready** (origin hop encrypted).

## What this rides on (confirm before you apply)

Two facts this design depends on, called out honestly because they haven't been apply-tested here:

1. **Front Door managed-cert validation works against external (Cloudflare) DNS** via the `_dnsauth`
   TXT — standard, but confirm against current Azure Front Door docs.
2. **DNS-01 delegation** (`cnameStrategy: Follow` + the `acme` NS sub-delegation) resolves and lets
   cert-manager answer the challenge. This is the fragile part; watch the cert-manager
   `Challenge`/`Order` on first issuance if the origin cert doesn't go Ready.

If the origin-cert delegation proves troublesome, the fallback is HTTP-01 on `origin.<domain>` (needs
an origin-lock exemption for `/.well-known/acme-challenge/*` and port 80 reachable) — simpler DNS,
more origin surface.

## Why not…

- **cert-manager for the *edge* cert too?** More moving parts (KV push + BYOC + rotation) for the
  same result Front Door's managed cert gives for free. Managed is best for the public hostname.
- **Cloudflare terminating TLS (tunnel)?** Simpler, but then Cloudflare — not Azure — owns the edge;
  ruled out by "Azure manages everything."
- **A Cloudflare Terraform provider / API token?** Ruled out by "no Terraform (or automation) in
  Cloudflare." The delegation keeps every Cloudflare record static and hand-managed.

# Front Door on a delegated subdomain (`delegate_subdomain = true`)

Goal: **Azure Front Door serves the app at `driftscope.<base_domain>`, and Azure DNS manages every
record** — while the parent domain stays at another provider (e.g. Cloudflare, where you can't move
the apex name servers). You delegate **only the subdomain** to Azure by adding **one NS record** in
the parent zone. Nothing at the parent is Terraform-managed.

This is the same edge as the default `azure` mode; the only differences are *which* zone Azure owns
(the subdomain, not the apex) and how the app record is expressed (an alias at the zone apex).

## How it works

```
client ──HTTPS (Front Door managed cert)──▶ Front Door ──HTTPS──▶ AKS gateway ──mTLS──▶ app
                                                          ▲ origin cert: cert-manager / Let's Encrypt
Azure DNS is authoritative for driftscope.<base_domain>   X-Azure-FDID origin lock
Parent DNS (Cloudflare) holds ONE record: driftscope NS -> Azure name servers
```

- **One NS record in the parent** delegates `driftscope.<base_domain>` to Azure. After that, Azure
  DNS answers for the app name and everything under it, and **Terraform manages all of it**.
- **App record:** the app FQDN is the delegated zone's **apex**, where a CNAME is illegal. Terraform
  uses an **Azure DNS alias record** targeting the Front Door endpoint (Azure's "CNAME flattening" —
  the supported way to put Front Door at an apex). Front Door's IP is never pinned.
- **Edge cert:** Front Door **managed certificate** (Microsoft-signed). The `_dnsauth` validation TXT
  lives in the Azure zone (Terraform-managed).
- **Origin cert:** cert-manager / Let's Encrypt, **DNS-01 straight against the Azure zone** (Workload
  Identity, no stored secret) — encrypts Front Door → AKS. No delegation gymnastics.

## The one manual step (parent DNS)

After `make infra`, run `terraform output dns_name_servers` and add, in the parent zone (Cloudflare):

| Name | Type | Value | Cloud |
|---|---|---|---|
| `driftscope` | NS | the 4 Azure name servers | (n/a — NS) |

That's the **only** record you touch by hand. Until it's live, the Azure zone isn't reachable from
the internet, so Front Door's cert validation and cert-manager's DNS-01 will stay pending.

## Deploy flow (one gate)

1. `make infra` — Azure resources incl. the DNS zone (`_dnsauth`, app alias, origin A all created).
2. **Add the `driftscope` NS record** in Cloudflare (from `terraform output dns_name_servers`).
3. Wait for delegation to propagate + Front Door to validate + issue the managed cert (minutes).
4. `make image && make cluster && make deploy` — cert-manager issues the origin cert; app goes live
   at `https://driftscope.<base_domain>`.

## Known caveat — apex + managed cert rotation

Front Door **managed-cert auto-rotation on an apex domain requires re-validation** (Front Door checks
for a CNAME to its endpoint before auto-renewing, and an apex has none). About twice a year you'll
see *"Pending revalidation"* and must regenerate the `_dnsauth` TXT token and update it. Set a
reminder, or (upgrade path) move the edge cert to **Azure Key Vault Acmebot** for hands-off renewal.
This does **not** affect the origin cert (cert-manager renews that unattended).

## Verify

- `dig NS driftscope.<base_domain>` → the Azure name servers (delegation live).
- `dig driftscope.<base_domain>` → resolves via Azure to Front Door.
- Front Door custom domain → **Validated**; `curl -sI https://driftscope.<base_domain>` → `x-azure-ref`.
- `kubectl -n aks-istio-ingress get certificate driftscope-origin` → **Ready** (origin hop encrypted).

# Runbook: migrate the existing single-env prod into the 0.6d multi-env structure

**Status:** one-time procedure. Run once, when adopting Phase 0.6d on an environment that already
has the pre-0.6d single deployment live. New/greenfield installs skip this entirely (`make shared-up`
+ `make up ENV=dev`).

This is the **riskiest step in 0.6d** — it touches live production and the DNS delegation. Read it
whole before starting. The [design doc](design-multi-env-0.6d.md) §5 has the *why*; this is the *how*.

## The situation (verified)

Everything currently lives in **one** resource group, `driftscope-cloud` — including the two
resources that must **survive** the migration:

| Must survive | Gets decommissioned |
|---|---|
| ACR `driftscopeacrb491bf98` (images/digests) | AKS `driftscope-aks` |
| DNS zone `driftscope.justmakeitgo.net` (its **name servers** are delegated at Cloudflare and must not change) | Key Vault `driftscope-kv-…`, Front Door, WAF, gateway PIP, the 4 UAMIs |

The 0.6d `terraform/shared` config expects the ACR + zone in a **new** `driftscope-shared` RG. New
prod (`terraform/envs/prod`) creates **new-named** resources (`driftscope-prod-*`) — so it **cannot
clobber** the running prod — but it recreates the apex DNS records (`@ A`, `_dnsauth`, `origin`) that
old prod already owns, which is the cutover.

## Two traps this runbook exists to avoid

1. **Never `terraform import` the ACR/zone straight into `terraform/shared`.** Their real RG
   (`driftscope-cloud`) ≠ the config's RG (`driftscope-shared`), so Terraform would plan a
   `resource_group_name` change → **replacement**. For the DNS zone that means **new name servers →
   the Cloudflare delegation breaks**; for the ACR it means losing every image. Use an **Azure
   control-plane `az resource move`** first (same subscription ⇒ name servers and images preserved),
   *then* import.
2. **Never run Terraform against the old flat state with the refactored config.** The old
   `driftscope.tfstate` still tracks live prod; the flat `.tf` are gone. Decommission old prod with
   `az group delete`, not a Terraform destroy.

---

## Guardrails — do these first

```bash
SUB=b491bf98-6ea2-4a07-a641-200d61d7a735
OLD_RG=driftscope-cloud
ZONE=driftscope.justmakeitgo.net

# 1. Back up the old state (scratch dir wired to the OLD backend key=driftscope.tfstate)
mkdir -p /tmp/oldtf && cd /tmp/oldtf
cat > backend.tf <<'HCL'
terraform { backend "azurerm" {} }
HCL
terraform init -backend-config=<path-to-old>/backend.hcl   # key = "driftscope.tfstate"
terraform state pull > ~/driftscope.tfstate.bak            # KEEP THIS
terraform state list | tee ~/old-state-addresses.txt       # you'll need the exact addresses below

# 2. Snapshot the zone name servers — the invariant that proves the delegation is intact
az network dns zone show -g "$OLD_RG" -n "$ZONE" --query nameServers -o tsv | sort | tee ~/zone-ns.before
# 3. Confirm Cloudflare's `driftscope` NS record points at exactly those name servers (do this in the
#    Cloudflare dashboard). If it doesn't already match, STOP and fix that first.
```

---

## Phase A — Preserve the shared resources (no downtime; old prod keeps serving)

Move ACR + zone out of `driftscope-cloud` into `driftscope-shared` and adopt them into
`terraform/shared`, without recreating either.

```bash
# A1. Create the shared RG (terraform/shared will adopt it via import)
az group create -n driftscope-shared -l eastus

# A2. Stop the OLD state from managing the ACR + zone + their records, so a later teardown can't
#     delete them. Find the exact addresses from ~/old-state-addresses.txt, e.g.:
cd /tmp/oldtf
terraform state list | grep -iE 'container_registry|dns_zone|dns_(a|txt|cname)_record'
terraform state rm \
  azurerm_container_registry.<acr_addr> \
  azurerm_dns_zone.<zone_addr> \
  azurerm_dns_a_record.<apex_addr> azurerm_dns_a_record.<origin_addr> \
  azurerm_dns_txt_record.<dnsauth_addr>
#   (resources still EXIST in Azure — just untracked. Old prod is untouched and still serving.)

# A3. Azure control-plane MOVE ACR + zone -> driftscope-shared (NOT terraform).
ACR_ID=$(az acr show -n driftscopeacrb491bf98 -g "$OLD_RG" --query id -o tsv)
ZONE_ID=$(az network dns zone show -n "$ZONE" -g "$OLD_RG" --query id -o tsv)
az resource move --destination-group driftscope-shared --ids "$ACR_ID" "$ZONE_ID"

# A4. VERIFY name servers unchanged (same-subscription move preserves them). Must match ~/zone-ns.before:
az network dns zone show -g driftscope-shared -n "$ZONE" --query nameServers -o tsv | sort | diff - ~/zone-ns.before \
  && echo "NS UNCHANGED ✓" || { echo "NS CHANGED — STOP, roll back A3"; exit 1; }

# A5. Adopt into terraform/shared (its own state key = shared.tfstate)
make state-backend                       # ensures the shared backend.hcl (key=shared.tfstate)
cd terraform/shared && terraform init
terraform import azurerm_resource_group.shared \
  "/subscriptions/$SUB/resourceGroups/driftscope-shared"
terraform import azurerm_container_registry.this \
  "/subscriptions/$SUB/resourceGroups/driftscope-shared/providers/Microsoft.ContainerRegistry/registries/driftscopeacrb491bf98"
terraform import 'azurerm_dns_zone.this[0]' \
  "/subscriptions/$SUB/resourceGroups/driftscope-shared/providers/Microsoft.Network/dnszones/$ZONE"

# A6. GATE: plan MUST be clean (no create/replace). If it wants to recreate the zone or ACR, STOP.
terraform plan -detailed-exitcode    # exit 0 = no changes (what you want)
```

At the end of Phase A the old prod is **still fully live** (its Front Door endpoint and the
`@ A`/`origin`/`_dnsauth` records still resolve — they just moved with the zone and are untracked).

---

## Phase B — Stand up dev + staging (no prod impact)

```bash
make up ENV=dev        # app at dev.driftscope.<domain>
make up ENV=staging    # staging.driftscope.<domain>
```
Validate the full loop on non-prod (SSO redirect to that env's Entra app, ESO synced, origin cert
Ready, a private-repo scan works) and prove promotion (merge→dev rolls the image; `git tag vX.Y.Z`
lands the **same digest** on staging). dev/staging use `<env>.` subdomains, so **no apex conflict** —
old prod is untouched throughout.

---

## Phase C — Prod cutover (the only downtime window — schedule it)

New prod's `@ A` / `_dnsauth` / `origin` collide with old prod's. Bring new prod fully up *except* DNS,
then flip.

```bash
cd terraform/envs/prod && terraform init

# C1. Everything EXCEPT the three conflicting DNS records (RG, AKS, KV, Front Door, identities, PIP,
#     origin cert). Target the non-DNS resources so the apply doesn't fail on the existing records:
terraform apply \
  -target=azurerm_resource_group.this -target=azurerm_kubernetes_cluster.this \
  -target=azurerm_key_vault.this -target=module... (or apply-all and expect only the 3 record
  #   resources to error, then continue) — the point: new prod infra + its Front Door exist, apex not
  #   yet pointed at it.

# C2. THE CUTOVER (downtime starts). Delete old prod's now-orphaned apex records, then let new prod
#     create its own (pointing at NEW Front Door + NEW gateway PIP, with NEW _dnsauth token):
for rs in '@:A' '_dnsauth:TXT' 'origin:A'; do
  az network dns record-set "${rs#*:}" delete -g driftscope-shared -z "$ZONE" -n "${rs%%:*}" --yes 2>/dev/null || true
done
terraform apply    # creates @ A -> new FD, _dnsauth (new token), origin -> new PIP; FD re-validates the apex

# C3. Verify (allow a few minutes for FD apex re-validation + DNS TTL):
curl -sSI https://driftscope.justmakeitgo.net/healthz     # 200 via new Front Door
#   SSO redirects to prod Entra app; origin cert Ready; private-repo scan works.
```

**Honest downtime note:** prod is at the **zone apex**, and Front Door must re-validate the apex
custom domain against the new `_dnsauth` token (minutes) plus DNS TTL. So Phase C has an inherent
**few-minutes apex outage** — do it in a maintenance window. Zero-downtime apex cutover would need a
bigger change (temporary prod host or weighted Front Door) and is out of scope here.

---

## Phase D — Decommission the old prod (only after C is verified)

```bash
# D1. Delete the leftover OLD prod resources. ACR + zone already left this RG in Phase A, so this is
#     safe — it removes only AKS/KV/Front Door/WAF/PIP/identities.
az group delete -n driftscope-cloud --yes

# D2. Old Key Vault soft-deletes (purge protection, name reserved 7 days). New prod's KV is
#     driftscope-prod (different name) so there is NO clash. Optionally free the old name:
az keyvault purge --name driftscope-kv-b491bf98 || true     # optional

# D3. Retire the stale old state (keep the .bak): delete the driftscope.tfstate blob.
az storage blob delete --account-name dstf132b285fd0d7 --container-name tfstate \
  --name driftscope.tfstate --auth-mode login
rm -f terraform/backend.tf     # the old flat backend, now unused
```

---

## Rollback

- **Phase A**: old prod is untouched, so it's fully reversible. If A4 shows NS changed or A6's plan
  wants to recreate: `az resource move` the ACR/zone **back** to `driftscope-cloud`, `terraform state
  rm` them from `terraform/shared`, and re-add them to the old state (`terraform import` in `/tmp/oldtf`)
  if you want the old config to manage them again. Nothing served changed.
- **Phase C**: capture the old `@ A` / `origin` / `_dnsauth` values **before** deleting them (C2). If
  new prod fails to validate, recreate those three records pointing back at old prod's Front Door
  endpoint + gateway PIP → the apex resolves to old prod again. **Do not run Phase D until Phase C is
  verified** — old prod is your rollback target.

## Success criteria

- Zone name servers identical to `~/zone-ns.before` (Cloudflare delegation intact) — check again at the end.
- `terraform -chdir=terraform/shared plan` clean; ACR + zone owned by `terraform/shared`.
- `https://driftscope.<domain>` serves through the **new** Front Door; origin cert Ready; SSO to the
  prod Entra app; a private-repo scan succeeds (KV-sign from `driftscope-prod` vault).
- The **same image digest** dev built is what prod runs (`kubectl -n driftscope get deploy driftscope
  -o jsonpath='{..image}'` shows `…@sha256:…`, no rebuild).
- `driftscope-cloud` RG gone; old state blob removed; `.bak` retained.

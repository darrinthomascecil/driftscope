# Azure Front Door + custom-domain SSL — field notes from a live deploy

Context: bringing driftscope up behind **Azure Front Door (Standard)** at
`driftscope.justmakeitgo.net` — the **apex of a Cloudflare-delegated subdomain** — with a **Front
Door managed certificate** for the public edge and **cert-manager** for the origin. The deploy
surfaced a long run of real gotchas, most in Front Door's SSL/edge behavior. Every finding below
lists the **exact command(s) used to diagnose or fix it**. Front Door SSL/edge is the deep focus;
DNS and the general deploy bugs follow.

Values used in examples: RG `driftscope-cloud`, profile `driftscope-fd`, endpoint `driftscope-ep`,
custom-domain resource `driftscope`, host `driftscope.justmakeitgo.net`, gateway IP `4.227.248.0`,
FD GUID `9f1f9e32-…`, Azure zone NS `ns1-03.azure-dns.com`.

---

## Part 1 — Front Door SSL / certificates (the focus)

### 1. Managed-cert validation is TWO phases — `Approved` ≠ serving
- **Symptom:** browser `ERR_CERT_COMMON_NAME_INVALID` even though validation showed `Approved`.
- **Diagnose:**
  ```bash
  # validation flag (necessary but NOT sufficient)
  az afd custom-domain show -g driftscope-cloud --profile-name driftscope-fd \
    --custom-domain-name driftscope --query domainValidationState -o tsv     # -> Approved

  # what cert is ACTUALLY served right now
  echo | openssl s_client -connect driftscope.justmakeitgo.net:443 \
    -servername driftscope.justmakeitgo.net 2>/dev/null \
    | openssl x509 -noout -subject -issuer                                   # -> CN=*.azureedge.net  (!!)
  ```
- **Cause:** *domain validation* (proving control via the `_dnsauth` TXT) and *cert issuance +
  deployment to the global edge* are **separate steps**. `Approved` only means the first finished.
  Until the real cert deploys, Front Door serves a **`*.azureedge.net` fallback**, which fails CN
  matching.
- **Key point:** Trust the **served cert**, not `domainValidationState`. If the CN is
  `*.azureedge.net`, the managed cert hasn't deployed yet. **Watch the cert (openssl), not the flag.**

### 2. Managed-cert validation can be *very* slow, and polls on Front Door's schedule
- **Symptom:** validation stuck at `Pending` for ~1h05m before flipping to `Approved`.
- **Diagnose (poll):**
  ```bash
  while :; do
    az afd custom-domain show -g driftscope-cloud --profile-name driftscope-fd \
      --custom-domain-name driftscope --query domainValidationState -o tsv
    sleep 60
  done
  ```
- **Cause:** Front Door polls the `_dnsauth` TXT on its own cadence. Early attempts hit `NXDOMAIN`
  (DNS delegation wasn't fixed yet), so Front Door **backed off** and re-checked less often.
- **Key point:** Budget **up to a couple hours** (Microsoft's ceiling is "a few hours"). Get DNS
  **100% correct before the first validation attempt** so Front Door never sees `NXDOMAIN` and backs
  off — then wait.

### 2b. The slow validation was self-inflicted by ORDERING — and Front Door lacks a recovery affordance
- **Root cause (ordering):** `make infra` created the Front Door **custom domain** — which *starts*
  the validation polling — **before** the Cloudflare delegation was live. So every early poll hit
  `NXDOMAIN` (the tunnel-owned CNAME conflict, then the NS `Name`-field trap), and Front Door **backed
  off** to an ever-longer interval. By the time DNS was correct, it was deep in backoff and sat a long
  while before its next check. **That backoff *is* the ~1 hour — not the minutes spent editing DNS.**
- **Why it's arguably a *bug*, not just "slow":** the backoff itself is sane (don't hammer DNS/CA at
  scale). The defect is what's **missing around it**:
  - **No backoff reset when the state changes.** Editing the custom domain / DNS finally resolving
    should force an immediate re-check; Front Door keeps waiting out its timer as if nothing changed.
  - **No idempotent "recheck now."** The *only* trigger is `regenerate-validation-token`, which
    **mutates** the token (and rate-limits). Good reconcile loops (Kubernetes controllers, ACME
    clients) are **event-driven / reset-on-change** — they react to the edit instead of a timer.
  - Fair to file with Azure as: *"managed-cert validation doesn't re-check promptly after DNS is
    corrected, and there's no non-destructive revalidate — add a backoff reset on custom-domain edit
    plus an idempotent recheck API."* A concrete, fixable ask — not just "it's slow."
- **The fix (ordering):** **get the delegation live and *verified* BEFORE onboarding the domain to
  Front Door.** Confirm the referral first, *then* create the custom domain:
  ```bash
  dig NS driftscope.justmakeitgo.net @<parent-ns> +noall +authority   # must list azure-dns servers
  # ...THEN run the Terraform that creates azurerm_cdn_frontdoor_custom_domain
  ```
  First poll succeeds → no backoff → validation in **minutes, not an hour**. In this deploy the order
  was reversed (Terraform made the domain; delegation came ~an hour later), which is what triggered it.
- **Honesty caveat:** the "exponential backoff" mechanism is **inferred from behavior** (early
  `NXDOMAIN`s → slow recovery once DNS was correct), not confirmed from Microsoft docs/source. The
  *observable* defect — slow re-check after DNS is fixed, and only a token-churning nudge available —
  is real regardless of the exact internals.

### 3. Do NOT "nudge" validation by regenerating the token
- **What I did (the mistake):**
  ```bash
  az afd custom-domain regenerate-validation-token -g driftscope-cloud \
    --profile-name driftscope-fd --custom-domain-name driftscope
  # -> ERROR: (Conflict) Rate limit exceeded. Please try again in 2 minutes.
  ```
  On retry it *did* mint a new token, which then **mismatched the TXT**, and my re-sync briefly left
  the record **empty** — making it worse and resetting the clock.
- **Recovery (get the TXT back in sync with what FD expects):**
  ```bash
  FDTOK=$(az afd custom-domain show -g driftscope-cloud --profile-name driftscope-fd \
    --custom-domain-name driftscope --query validationProperties.validationToken -o tsv)
  az network dns record-set txt add-record -g driftscope-cloud \
    -z driftscope.justmakeitgo.net -n _dnsauth -v "$FDTOK"
  dig +short TXT _dnsauth.driftscope.justmakeitgo.net @ns1-03.azure-dns.com   # must equal $FDTOK
  ```
- **Key point:** If DNS is correct and the token matches, **wait — don't nudge.** Regeneration is
  rate-limited, mints a new token (the TXT must be re-synced), and in the normal flow the TXT is
  **Terraform-managed** (don't mutate it out-of-band). Patience beats fiddling.

### 4. The `_dnsauth` validation token is NOT a secret
- **Question raised:** should the token be vaulted?
- **Cause / reasoning:** it's a **public DNS TXT record** the CA/Front Door reads to prove domain
  control — *proof-of-control, not a credential*. Useless to anyone who doesn't already control your
  DNS.
- **Key point:** **Never vault DNS validation tokens.** In Terraform it's a *computed* attribute
  written straight to public DNS — never hand-handled. (Real secrets — oauth2 client/cookie secrets,
  signing keys — *do* go in Key Vault; a domain-control token does not.)

### 5. Apex domains need an ALIAS record, and managed-cert *renewal* has a footgun
- **Symptom:** can't `CNAME` at a zone apex; and the app host *is* the apex of the delegated zone.
- **Diagnose / confirm the alias resolves:**
  ```bash
  az network dns record-set a show -g driftscope-cloud -z driftscope.justmakeitgo.net -n '@' \
    --query '{targetResource:targetResource.id, aRecords:aRecords}' -o json
  # aRecords: null, targetResource: .../afdEndpoints/driftscope-ep   (alias, not a static A)

  dig A driftscope.justmakeitgo.net @ns1-03.azure-dns.com +noall +answer   # -> 150.171.110.146 (FD)
  dig +short driftscope-ep-….z01.azurefd.net                              # same IP => alias OK
  ```
- **Terraform:** `azurerm_dns_a_record` with `target_resource_id = azurerm_cdn_frontdoor_endpoint.this.id`
  (Azure "CNAME flattening"). Never pin Front Door's IP with a static A record — it changes.
- **Footgun:** Front Door managed-cert **auto-rotation fails on apex** (no CNAME to the endpoint to
  check) → needs a **manual `_dnsauth` revalidation ~2×/year** or the cert expires.
- **Key point:** Apex + Front Door = **alias record** (not CNAME, not static A). But the managed cert
  is *not* zero-ops on apex — either calendar the revalidation, or use a **customer cert
  (cert-manager / Key Vault Acmebot BYOC)** for hands-off renewal.

### 6. cert-manager (origin cert) was fast and reliable — the contrast
- **Observed:**
  ```bash
  kubectl get certificate -n aks-istio-ingress
  # NAME               READY  SECRET                 AGE
  # driftscope-origin  True   driftscope-origin-tls  104s   <-- ~100s, DNS-01 against the delegated zone
  ```
  …while the Front Door managed **edge** cert took **>1 hour**.
- **Key point:** cert-manager DNS-01 against a properly authoritative/delegated zone is **fast and
  unattended**. Front Door managed certs are convenient but slow, and on apex carry the renewal
  footgun. **BYOC via cert-manager is a legitimately better option** for reliability/control — the
  same tool already issuing your origin cert.

---

## Part 2 — Front Door routing / edge behavior

### 7. `link-to-default-domain`: the `*.azurefd.net` host 404s in custom-domain mode
- **Symptom:** `https://<endpoint>.azurefd.net/` → **404** while a custom domain is configured.
- **Diagnose / fix:**
  ```bash
  az afd route show -g driftscope-cloud --profile-name driftscope-fd \
    --endpoint-name driftscope-ep --route-name default \
    --query '{linked:linkToDefaultDomain, domains:customDomains[].id}' -o json
  # linked: Disabled  => default endpoint has no route

  az afd route update -g driftscope-cloud --profile-name driftscope-fd \
    --endpoint-name driftscope-ep --route-name default --link-to-default-domain Enabled
  ```
- **Why it's useful:** the `*.azurefd.net` cert is **always valid**, so this lets you smoke-test
  while the *custom* cert is still validating.
- **Key point:** In custom-domain mode the default host is dark; enable `link-to-default-domain` to
  test on the always-valid `*.azurefd.net` host (mind the oauth2 caveat, #11).

### 8. Control plane vs data plane: `Enabled` ≠ served
- **Symptom:** `az afd route show` said `linkToDefaultDomain=Enabled`, but the edge **404'd for
  ~35 min**.
- **Diagnose:**
  ```bash
  az afd endpoint show -g driftscope-cloud --profile-name driftscope-fd \
    --endpoint-name driftscope-ep --query '{enabled:enabledState, provisioning:provisioningState}' -o json
  # Enabled / Succeeded  (control plane happy)  -- yet curl still 404s (data plane behind)
  ```
- **Fix (force re-publish by toggling):**
  ```bash
  az afd route update … --link-to-default-domain Disabled; sleep 5
  az afd route update … --link-to-default-domain Enabled
  ```
- **Key point:** With Front Door, **"the API says Enabled" ≠ "the edge serves it."** Config changes
  take minutes to propagate globally; if stuck, a toggle forces a re-publish.

### 9. The `X-Azure-FDID` origin lock — what it is, and that it's *not* a secret
- **Get the GUID (four ways, all equivalent):**
  ```bash
  terraform -chdir=terraform output -raw frontdoor_id
  az afd profile show -g driftscope-cloud --profile-name driftscope-fd --query frontDoorId -o tsv
  kubectl get authorizationpolicy driftscope-origin-lock -n aks-istio-ingress -o yaml   # baked in
  # (or Portal -> Front Door profile -> Overview -> "Front Door ID")
  ```
- **What it is:** the FD profile's **resource GUID**, auto-stamped on every origin request; the mesh
  `AuthorizationPolicy` **DENIES** requests whose `x-azure-fdid` header ≠ that GUID.
- **Key point:** **Not a secret** (readable four ways) — **defense-in-depth, not a cryptographic
  gate.** Anyone with your **origin IP + the GUID** can bypass it (I did, to debug — see #12). Keep
  the origin IP unadvertised and rely on real auth (oauth2/SSO) as the actual control.

### 10. Health-probe path matters (and 404 vs 503 is diagnostic)
- **Diagnose:**
  ```bash
  az afd origin-group show -g driftscope-cloud --profile-name driftscope-fd \
    --origin-group-name gateway --query healthProbeSettings -o json
  # probePath: /healthz, probeProtocol: Https

  # what the app returns on each path (direct to origin, see #12)
  #   /healthz -> 200   (healthy)
  #   /        -> 302   (would be marked UNHEALTHY if it were the probe path)
  ```
- **Key point:** Point the probe at a path that returns **200** (a real health endpoint), never the
  app root that may redirect. And read the status code as a map: **404 = no route matched; 503 =
  origin unhealthy; TLS error = no valid cert for the host.**

---

## Part 3 — the interaction that bit us

### 11. The oauth2 callback pins you to the custom domain
- **Symptom:** `ERR_CERT_COMMON_NAME_INVALID` **on the oauth2 callback** when logging in via
  `azurefd.net`.
- **Diagnose:**
  ```bash
  kubectl get deploy oauth2-proxy -n driftscope -o yaml | grep -i REDIRECT_URL
  # OAUTH2_PROXY_REDIRECT_URL = https://$(HOST)/oauth2/callback   ($(HOST)=driftscope.justmakeitgo.net)
  ```
- **Cause:** the redirect URL is hard-wired to the **custom domain**. Entra always bounces the
  callback there, so it fails while that host's cert is still the `*.azureedge.net` fallback.
- **Key point:** **You cannot complete SSO via the `*.azurefd.net` host while the custom-domain cert
  is pending** — the callback always targets the custom domain. The default host only gets you the
  login *bounce*, not a completed session.

### 12. Adding a user to the access group needs a FRESH token (first login can 403)
- **Symptom:** SSO succeeds ("You signed in successfully"), but oauth2-proxy shows the custom
  **"Access denied — requires membership in the driftscope group"** page — even though the user *is*
  in the group.
- **Diagnose:**
  ```bash
  SSO=$(terraform -chdir=terraform output -raw sso_client_id)
  GRP=$(terraform -chdir=terraform output -raw allowed_group_id)
  az ad app show --id "$SSO" --query groupMembershipClaims -o tsv          # "SecurityGroup" => app emits the claim
  az ad group member check --group "$GRP" --member-id <oid> --query value  # true => membership is correct

  # what oauth2-proxy ACTUALLY received in the token (the truth):
  kubectl logs -n driftscope -l app=oauth2-proxy --tail=30 | grep AuthSuccess
  #   ... groups:[… 8ae4d6e7-…]   <- if the allowed group id is MISSING here, the token is stale
  ```
- **Cause:** group-membership changes **do not apply to already-issued tokens**. A cached Entra
  session, or a token minted before the new membership propagated, won't carry the group in its
  `groups` claim → `OAUTH2_PROXY_ALLOWED_GROUPS` fails → 403. Nothing is misconfigured.
- **Fix:** **re-authenticate** — sign out / "Sign in with a different account" / new session — to mint
  a **fresh token** that includes the group. (In the observed case, the second login went straight in.)
- **Key point:** After `az ad group member add`, the user needs a **new token**. The first login on a
  stale session 403s; a fresh sign-in fixes it — it is *not* a redirect bug or a config error.

---

## Part 4 — the debugging method that worked (transferable)

1. **Read the exact error/status** — 404 vs 503 vs `ERR_CERT_COMMON_NAME_INVALID` each localize a
   different layer (routing / origin health / cert).
2. **Bisect the request path** — the single highest-value move: test the origin **directly, bypassing
   Front Door**. A `302` back proved the *entire* backend (gateway, origin cert, origin-lock,
   oauth2-proxy) healthy, collapsing the problem to Front Door's edge:
   ```bash
   FDID=$(terraform -chdir=terraform output -raw frontdoor_id)
   curl -k --resolve driftscope.justmakeitgo.net:443:4.227.248.0 \
        -H "Host: driftscope.justmakeitgo.net" -H "X-Azure-FDID: $FDID" \
        https://driftscope.justmakeitgo.net/            # -> 302 (login redirect) = backend fine
   ```
   `--resolve` forces the connection to the gateway IP while keeping the right SNI/Host; the
   `X-Azure-FDID` header replays what Front Door sends so the origin-lock doesn't reject you.
3. **Confirm the gateway is bound to the public IP:**
   ```bash
   kubectl get svc -n aks-istio-ingress aks-istio-ingressgateway-external -o wide   # EXTERNAL-IP = 4.227.248.0
   ```
4. **Separate control plane from data plane** — "the config API says enabled" ≠ "the edge serves it."

---

## Part 5 — DNS delegation gotchas (Cloudflare → Azure)

- **Cloudflare Tunnel owns its DNS record.** A tunnel's proxied `CNAME` (`…cfargotunnel.com`) can't
  be deleted from the DNS editor — delete the **tunnel** (Zero Trust → Networks → Tunnels) or its
  public-hostname route. This orphaned tunnel caused the `1033` page *and* blocked the delegation.
  ```bash
  dig +short driftscope.justmakeitgo.net           # 104.21.x / 172.67.x = still Cloudflare-proxied
  curl -sS -D - -o /dev/null https://driftscope.justmakeitgo.net/   # HTTP 530 = Cloudflare 1033 tunnel error
  ```
- **NS-record `Name` field must be the subdomain (`driftscope`), not the nameserver value.** The
  fat-finger created records at `ns1-03.azure-dns.com.justmakeitgo.net` and left `driftscope` as
  `NXDOMAIN`.
- **Cloudflare won't save an `NS` record where a proxied `CNAME` already exists** for that name.
- **A delegation appears in dig's `AUTHORITY` section (a referral), not `ANSWER`** — `+short` hides it:
  ```bash
  # authoritative check, bypassing caches; look at AUTHORITY / status
  dig NS driftscope.justmakeitgo.net @decker.ns.cloudflare.com +noall +authority +comments
  # status: NXDOMAIN + flags: aa  => Cloudflare is authoritative, NO delegation exists
  # once fixed: AUTHORITY SECTION lists the 4 azure-dns nameservers (a referral)

  dig A driftscope.justmakeitgo.net @decker ANY        # Cloudflare returns HINFO "RFC8482" for ANY
  ```
- **Cloudflare Registrar forces Cloudflare nameservers on the apex** (`whois <domain> | grep Registrar`),
  so you delegate a **subdomain** to Azure via one `NS` record in the parent zone — not the whole domain.

---

## Part 6 — general deploy/bootstrap bugs (all knowledge)

### A. Image build fails inside the Docker `test` stage
- **Symptom:** `make image` (→ `az acr build`) fails: `FileNotFoundError:
  /src/deploy/overlays/cloud/kustomization.yaml`.
- **Diagnose:**
  ```bash
  grep -iE 'error|fail|non-zero' /tmp/dsimage.log | tail   # -> pytest FAILED in tests/test_deploy_wi.py
  ```
- **Cause:** the classic (`az acr build`) builder runs **every** stage including `test`;
  `test_deploy_wi.py` reads `deploy/overlays/…`, but the Dockerfile `test` stage only COPYed
  `tests/ tools/ fixtures/`, **not `deploy/`**.
- **Fix:** add `COPY deploy /src/deploy` to the `test` stage. (`.dockerignore` excludes `*.md` but not
  the `.yaml`, so the fix works.)
- **Key point:** `az acr build` runs *all* Dockerfile stages, not just the target — the `test` stage
  must contain everything the suite reads.

### B. cert-manager Workload-Identity pod label as boolean vs string
- **Symptom:** helm install fails: `.metadata.labels."azure.workload.identity/use": expected string,
  got true`.
- **Cause:** `--set podLabels.azure\.workload\.identity/use=true` makes Helm emit a **boolean**;
  Kubernetes label values must be **strings**.
- **Fix:** use `--set-string …=true`.

### C. cert-manager Helm upgrade conflicts with AKS `admissionsenforcer`
- **Symptom:** `UPGRADE FAILED: conflict … webhook.cert-manager.io namespaceSelector … "admissionsenforcer"`.
- **Cause:** AKS's admission enforcer mutates the webhook's `namespaceSelector`; a Helm *re-apply*
  (server-side) then conflicts on that field. (My first failed run left a partial release; the
  re-run was an upgrade → conflict.)
- **Fix — clean out and do a fresh install (create doesn't conflict, only re-apply does):**
  ```bash
  helm uninstall cert-manager -n cert-manager --wait
  kubectl delete validatingwebhookconfiguration cert-manager-webhook --ignore-not-found
  kubectl delete mutatingwebhookconfiguration  cert-manager-webhook --ignore-not-found
  make cluster    # fresh install now succeeds
  ```

### D. `make deploy` fails before ESO CRDs exist
- **Symptom:** `no matches for kind "ExternalSecret"/"SecretStore" in external-secrets.io/v1beta1`.
- **Cause:** the cluster step that installs the External Secrets Operator (after cert-manager) never
  ran because cert-manager failed first. Downstream `make deploy` applies ESO CRs that don't yet
  exist.
- **Fix:** finish `make cluster` (installs cert-manager **and** ESO), then re-run `make deploy`
  (idempotent).

### E. Deployed the wrong image (old ACR) — a self-inflicted overlay revert
- **Symptom:** app pod `ImagePullBackOff` pulling `dscopeacr18860.azurecr.io/driftscope:v1` (the
  *deleted* ACR).
- **Diagnose:**
  ```bash
  kubectl get deploy driftscope -n driftscope -o jsonpath='{.spec.template.spec.containers[*].image}'
  # -> the OLD registry
  KUBELET=$(terraform -chdir=terraform output -raw kubelet_identity_object_id)
  az role assignment list --assignee "$KUBELET" \
    --scope "$(az acr show -n driftscopeacrb491bf98 --query id -o tsv)" --query '[].roleDefinitionName' -o tsv
  # -> AcrPull  (perms fine; it was the image REFERENCE that was stale)
  ```
- **Cause:** I ran `git checkout -- kustomization.yaml` to keep the per-deploy registry edit out of a
  commit — which **reverted the image pointer** `make image` had set. The deploy then used the
  committed (old-ACR) reference.
- **Fix:**
  ```bash
  ( cd deploy/overlays/cloud && kustomize edit set image driftscope=driftscopeacrb491bf98.azurecr.io/driftscope:v1 )
  make deploy
  ```
- **Key point:** `make image`'s `kustomize edit set image` is a **per-deploy** change the deploy
  *needs*. Don't `git checkout` it back before deploying; only reset it *after* (or never commit it).

### F. Orphaned resource group blocking a clean deploy → delete + recreate for idempotency
- **Diagnose:**
  ```bash
  az group list --query "[?ends_with(name,'-cloud')].name" -o tsv
  az resource list -g driftscope-cloud --query '[].{n:name,t:type}' -o table   # orphaned ACR + KV from a prior deploy
  ```
- **Fix (clean slate; Terraform then owns everything → idempotent):**
  ```bash
  az group delete --name driftscope-cloud --yes
  rm -f terraform/terraform.tfstate*        # discard stale local state; init fresh against the remote backend
  make state && terraform -chdir=terraform init -backend-config=backend.hcl && make infra
  ```
- **Key point:** For idempotency, don't deploy *into* a half-existing RG — delete it and let
  Terraform recreate from empty state.

---

## Quick reference — the commands that mattered

```bash
# --- Front Door SSL ---
echo | openssl s_client -connect HOST:443 -servername HOST 2>/dev/null | openssl x509 -noout -subject -issuer   # served cert
az afd custom-domain show -g RG --profile-name PROF --custom-domain-name CD --query domainValidationState -o tsv  # flag (insufficient!)
az afd custom-domain show -g RG --profile-name PROF --custom-domain-name CD --query validationProperties.validationToken -o tsv
dig +short TXT _dnsauth.HOST @AZURE_NS                                # must equal the token above

# --- Front Door routing/edge ---
az afd route show   -g RG --profile-name PROF --endpoint-name EP --route-name default --query linkToDefaultDomain -o tsv
az afd route update -g RG --profile-name PROF --endpoint-name EP --route-name default --link-to-default-domain Enabled
az afd endpoint show -g RG --profile-name PROF --endpoint-name EP --query '{e:enabledState,p:provisioningState}' -o json
az afd origin-group show -g RG --profile-name PROF --origin-group-name gateway --query healthProbeSettings -o json
az afd profile show -g RG --profile-name PROF --query frontDoorId -o tsv

# --- test the origin directly, bypassing Front Door ---
curl -k --resolve HOST:443:GATEWAY_IP -H "Host: HOST" -H "X-Azure-FDID: FDID" https://HOST/

# --- DNS delegation ---
dig NS SUBDOMAIN.DOMAIN @PARENT_NS +noall +authority +comments        # AUTHORITY = referral; aa+NXDOMAIN = no delegation
dig A  HOST @AZURE_NS +noall +answer                                   # alias -> Front Door IP
az network dns record-set a   show -g RG -z ZONE -n '@'      --query targetResource.id -o tsv
az network dns record-set txt show -g RG -z ZONE -n _dnsauth --query 'txtRecords[].value' -o json

# --- cluster / app ---
kubectl get certificate -n aks-istio-ingress                          # origin cert Ready
kubectl get externalsecret,secret -n driftscope                       # ESO synced from Key Vault
kubectl get svc aks-istio-ingressgateway-external -n aks-istio-ingress -o wide   # EXTERNAL-IP bound
kubectl get deploy DEPLOY -n NS -o jsonpath='{.spec.template.spec.containers[*].image}'   # deployed image ref
```

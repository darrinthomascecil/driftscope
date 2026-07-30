# Network stress test — findings & prevention plan

Chaos/stress test of the live cluster's networking, 2026-07-30. Seven failure modes were
planned and actively triggered (not just reasoned about); state was restored after each.
All values below are measured, not estimated.

## The one finding that frames the rest
Killing individual **pods** (tunnel, DNS, SSO) recovered in **1–3 seconds** every time —
fast reschedule on the node with a cached image. That speed is **misleading**: it masks the
real exposure, which is at the **node** level. Every "2-replica" component
(istiod, ingress gateway, cloudflared, oauth2-proxy, CoreDNS) runs **both replicas on the
single node**, and **no deployment has pod anti-affinity or topology-spread**. So the HA is
cosmetic — pod chaos looks survivable while a node failure would take out *everything at
once* with nowhere to reschedule. **Node-level resilience is the headline gap.**

## Findings

| # | Test | Triggered? | Result | Severity |
|---|---|---|---|---|
| **T2** | Egress NetworkPolicy enforcement | ✅ | **IMDS reachable from any pod; a NetworkPolicy blocking `169.254.0.0/16` did NOT stop it** — no policy engine (`networkPolicy: none`). Egress control is inert. | **High** |
| **T6** | Single-node "false HA" | ✅ (observed) | All 2-replica components co-located on one node; **zero** anti-affinity / topology-spread. "2 replicas" ≠ HA. | **High** |
| **T3** | ext_authz fail-open | ✅ | **Fails closed** (403 with oauth2-proxy down — no SSO bypass ✅), *but* oauth2-proxy is a hard dependency for **all** traffic: when it's down the entire app is unreachable, even for authenticated users. | Medium |
| **T4** | Tunnel (cloudflared) outage | ✅ | Killing both replicas → external users got **`530`**; recovered in **~3s**. Fine on pod loss, fatal on node loss. | Medium |
| **T5** | CoreDNS blast radius | ✅ | Killing both → **cluster-wide DNS `connection refused`** (app→GitHub, mesh discovery all fail); recovered **~1–2s**. | Medium |
| **T1** | SNAT port exhaustion | ⚠️ partial | 2048 ports/node (auto, divided on scale-out). Couldn't exhaust from one pod — hit the **pod fd-limit (1024)** first. Real risk is multi-pod (scanner-at-scale), unproven here. | Medium |
| **T7** | Cloudflare ~100s proxy timeout | ✅ (config) | SSE path safe (15s heartbeat keeps it alive); **synchronous JSON `/scan` over 100s would `524`**. | Low |

## Prevention plan (prioritized by leverage)

### 1. Real HA — fixes the blast radius of T3/T4/T5/T6 at once
The single highest-value change. Nothing else matters if one node takes the whole system down.
- **≥3 nodes across availability zones** (or ≥2 minimum).
- **Pod anti-affinity + topology-spread** on every critical deployment so replicas *can't* co-locate:
  ```yaml
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: kubernetes.io/hostname
      whenUnsatisfiable: DoNotSchedule
      labelSelector: { matchLabels: { app.kubernetes.io/name: <name> } }
  ```
- **PodDisruptionBudgets** (`minAvailable: 1`) for cloudflared, oauth2-proxy, the gateway, CoreDNS — so node drains/upgrades can't evict all replicas at once (the exact worst case T4/T5 simulated).

### 2. Enable a network-policy engine — fixes T2
The committed egress NetworkPolicy is a no-op today. Enable enforcement:
```sh
az aks update -g driftscope-cloud -n driftscope-aks --network-policy cilium
```
Then re-run T2 (curl IMDS after applying the block) — it should now **fail**. Until this is on,
the IMDS/private-range egress controls provide zero protection.

### 3. NAT Gateway for outbound — fixes T1 at scale
Replaces "2048 ports/node, divided as you add nodes" with a large per-subnet pool that doesn't
shrink on scale-out. The correct outbound design for a fan-out scanner. Also add pod-level
`ulimit`/fd headroom if a single scanner pod opens many connections (T1 hit fd=1024).

### 4. Async job model + timeout handling — fixes T7
Move fleet scans off the synchronous request path (queue + worker + poll/stream result), so no
request depends on staying open past Cloudflare's ~100s. Interim: keep SSE for the UI (already
mitigated) and document the JSON `/scan` limit, or raise the Cloudflare origin timeout.

### 5. Make oauth2-proxy resilient — hardens T3
Fail-closed is correct, but it makes oauth2-proxy a whole-app dependency. Give it the anti-affinity
+ PDB from #1, and consider a readiness-gated rollout so an oauth2-proxy deploy never drops below 1.

## Re-test
Every trigger in this doc is a one-liner (kill pods / curl IMDS / flood connections) — re-run them
after the fixes to confirm: IMDS blocked (T2), replicas land on different nodes (T6), a single node
drain keeps the app up (T3/T4/T5), SNAT backed by a NAT Gateway (T1).

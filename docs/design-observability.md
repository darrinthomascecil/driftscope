# Observability design: spot a networking issue fast, pinpoint the hop

**Goal (verbatim intent):** *"quickly spot whenever there is a networking issue and
pinpoint it ASAP — I need visibility to do that."*

That's two metrics: **MTTD** (time to *notice*) and **MTTR-to-pinpoint** (time to know
*which hop*). This design optimizes both across the layered path
`Cloudflare edge → tunnel → Istio gateway → mesh → app`, plus DNS and outbound/SNAT.

Context: the [network stress test](network-stress-test.md) proved the controls *work* but
that we're **blind to attempts** — the signals exist in the mesh but nothing collects them,
Envoy access logs are off, and edge errors (530) live only at Cloudflare. This closes that gap.

---

## Principle: three pillars, one correlation ID

Logs alone don't give fast pinpointing. You want all three, joined by a shared request ID:

| Pillar | Job | Tool for it |
|---|---|---|
| **Metrics** (golden signals: latency, traffic, errors, saturation) | **SPOT** — something's wrong, roughly where | Prometheus + alerts |
| **Traces** (distributed) | **PINPOINT** — which hop, which request | the request's waterfall edge→app |
| **Logs** (structured) | **ROOT CAUSE** — why | per-request detail |

The glue is a **correlation ID flowing through every layer** — but pick a *vendor-neutral*
one as the backbone, or you couple your log schema to today's edge (the same trap the
[pathtrace](../examples/pathtrace/README.md) generalization fixed: key on the *role*, not the
vendor header).

- **Backbone (always): W3C `traceparent` + Envoy `x-request-id`.** These work end-to-end
  regardless of what's in front — Cloudflare, Front Door, a plain LB, or nothing. Envoy
  already stamps `x-request-id`, so this correlation exists today with zero edge dependency.
  Propagating `traceparent` (OpenTelemetry) is the durable way to make one trace span edge
  *and* cluster.
- **Optional supplement: an "edge request id"**, captured as a generically-named field that is
  `cf-ray` for this deployment but is a one-line swap per vendor (`x-azure-ref` for Front Door,
  `x-amzn-trace-id` for ALB). It only helps for requests that *reached* the cluster — a `530`
  means the request never arrived, so there's nothing in-cluster to join it to.

**Alert on symptoms** (error rate, latency), not causes — then use traces/logs to find the
cause. This avoids alert fatigue.

## The highest-value signal for networking: Envoy response flags

The HTTP code says *what*; the **response flag** says *why*. For pinpointing a network fault,
nothing beats it — Envoy names the exact failure. Mapped to the stress-test failure modes:

| Flag | Meaning | Failure it pinpoints |
|---|---|---|
| `UH` | no healthy upstream | backend down (oauth2-proxy/app crash) |
| `UF` | upstream connection failure | SNAT exhaustion, mTLS conflict, conn refused |
| `UAEX` | ext_authz denied | SSO / oauth2-proxy rejected |
| `NR` | no route configured | VirtualService/routing misconfig |
| `DC` | downstream disconnect | client/tunnel dropped |
| `UO` / `URX` | circuit-break / retry-limit | overload |
| `-` + `403` | RBAC AuthorizationPolicy deny | east-west lateral movement |

Istio's own recommended flow: `kubectl logs deploy/<svc> -c istio-proxy | grep -v '" 200 '`
→ read the flag → follow `x-request-id` through the chain. **Today access logs are OFF**, so
this is impossible — enabling them is the single biggest pinpoint win.

## Architecture (layer → signal → collector)

Recommended: **Azure-native, low-ops** (you don't want to run infra). The open-source
equivalent is the LGTM stack (Loki/Grafana/Tempo/Mimir) — same shape, more to operate.

| Layer | Signal | Collector |
|---|---|---|
| **Edge** (530/524) | edge logs — *the only place these exist* | edge log export (Cloudflare Logpush today) → log store; correlate via propagated `traceparent` |
| **Tunnel** | cloudflared reconnects | pod logs → log store |
| **Gateway + mesh** | RED metrics + **access logs w/ response flags** | **Azure Managed Prometheus** + access logs via **Telemetry API** → Loki / Azure Monitor |
| **Traces** | per-request waterfall | **OpenTelemetry** (propagate `traceparent`) → Tempo / App Insights |
| **DNS** | query / error | CoreDNS `log` plugin → log store |
| **L3 flows / policy drops / DNS** | per-flow visibility | **Cilium Hubble** (free once Cilium is enabled for the network-policy fix) |
| **Node / outbound** | saturation, LB SNAT ports | node-exporter + LB `SnatConnectionCount` / `UsedSnatPorts` |

**Exemplars** are the correlation accelerator: a spike on a metric graph carries a marker
that jumps straight to the responsible trace (Azure Managed Grafana + Prometheus support
them). That's the pivot that turns "error rate is up" into "*this* request, *this* hop."

## The ~60-second pinpoint workflow this enables

1. **Alert** on a golden signal (error-rate SLO breach) → *when* + roughly *which service*.
2. **Dashboard** (RED + response-flag breakdown per hop) → the hop + failure *type* (`UH` vs `UF` vs `UAEX`).
3. **Exemplar → trace** → the exact failing request across edge→gateway→app.
4. **Logs by that `x-request-id`** → the why.

## How the 7 stress-test failures light up

| Failure | Fires as | Pinpoint |
|---|---|---|
| 530 tunnel down | mesh traffic *drops* + Cloudflare Logpush 530 + cloudflared disconnect logs | edge/tunnel |
| 403 SSO (oauth2 down) | `UAEX` flag + `istio_requests_total{403}` + oauth2-proxy logs | auth |
| DNS refused | CoreDNS error logs + `UF` upstreams + Hubble DNS drops | DNS |
| SNAT exhaustion | LB SNAT-port metric climbs + `UF` + app "cannot assign address" | outbound/SNAT |
| Node loss | everything drops at once + K8s events + node metrics gone | infra |
| East-west 403 | `403` from a non-gateway `source_principal` (alert already built) | lateral movement |

## Prioritized rollout (builds on the Prometheus/Grafana already deployed)

### Step 1 — Enable Envoy access logs with response flags (do first; biggest pinpoint win)
Current best practice is the **Telemetry API** (target by workload label), not mesh-wide config.
```yaml
apiVersion: telemetry.istio.io/v1
kind: Telemetry
metadata:
  name: mesh-access-logs
  namespace: aks-istio-system      # root namespace => mesh-wide
spec:
  accessLogging:
    - providers:
        - name: envoy               # default provider -> stdout, includes %RESPONSE_FLAGS%
```
The default format already carries `x-request-id` — the vendor-neutral backbone — so
`kubectl logs <pod> -c istio-proxy | grep -v '" 200 '` gives per-request lines with the flag
that names the fault, no edge dependency. *Optionally*, a custom-format provider can append an
"edge request id" (`%REQ(CF-RAY)%` for Cloudflare today, swappable per vendor) for requests
that reached the cluster — but this is a supplement, not the correlation key.

### Step 2 — Broaden metrics + wire alert delivery
- Extend Prometheus scraping to **node-exporter + kube-state-metrics + LB SNAT** (today it only
  scrapes istio-proxy sidecars).
- Wire **Alertmanager** — rules exist (`MeshAuthzDenied`, etc.) but nothing delivers them yet.
- Build the **golden-signal dashboards** (RED per hop + response-flag breakdown).

### Step 3 — Traces + exemplars
OpenTelemetry, propagate `traceparent` end-to-end (already flowing), export to Tempo / App
Insights. Enable **exemplars** in Grafana for the one-click metric→trace pivot.

### Step 4 — Edge log export (Cloudflare Logpush today)
Ship the edge's logs to the same store — the *only* place `530`/`524` exist (the request
never reached the cluster). Correlation to in-cluster traces is via propagated `traceparent`,
not a vendor id; the edge request id (`cf-ray`) is a supplementary field. If the edge ever
changes (Front Door, direct), only this step's exporter and the edge-id field change — the
rest of the design is untouched.

### Step 5 — Cilium Hubble
Rides along with enabling Cilium (needed to make the egress NetworkPolicy actually enforce).
Gives per-flow L3/L4 + DNS visibility and a live flow map — excellent for pinpointing the
lower-layer network faults metrics/logs don't cover.

## Ship vs buy
- **Azure Managed Prometheus + Managed Grafana + Container Insights** removes the ops burden
  and is the AKS-recommended path; the self-hosted Prometheus we deployed is fine to grow into
  step 2 and swap later.
- Keep the **security alerting** from `deploy/monitoring/` — this design extends it from
  security signals to full networking observability.

## Sources
- Istio — [Envoy Access Logs](https://istio.io/latest/docs/tasks/observability/logs/access-log/),
  [Traffic Management Problems](https://istio.io/latest/docs/ops/common-problems/network-issues/)
- [Understanding Envoy Response Flags in Istio Access Logs](https://oneuptime.com/blog/post/2026-02-24-how-to-understand-envoy-response-flags-in-istio-access-logs/view)
- [AKS Observability with Azure Monitor, Managed Grafana & Prometheus (ClearPeaks)](https://www.clearpeaks.com/azure-kubernetes-service-observability-with-azure-monitor-managed-grafana-prometheus/)
- [Connect Metrics to Traces with Exemplars in Azure Monitor (Microsoft)](https://techcommunity.microsoft.com/blog/azureobservabilityblog/connect-metrics-to-traces-with-exemplars-in-azure-monitor/4525714)

# Mesh-security observability (Prometheus + Grafana)

Collects the telemetry the mesh already emits and turns the attack fingerprints into
queryable metrics + firing alerts. Both components run **out of the mesh** (the
`monitoring` namespace is not istio-injected) so the mesh-wide deny-all / STRICT don't
block scraping — Prometheus reaches each sidecar's Envoy metrics on port `15090`.

## What it watches — attack → signal → alert

| Attack (see docs/design-enterprise-istio-sso.md) | Metric signal | Alert |
|---|---|---|
| Lateral movement (in-mesh pod → app, bypassing SSO) | `istio_requests_total{response_code="403", source_principal!=gateway}` | **MeshAuthzDenied** (critical) |
| Plaintext reaching a workload | `istio_requests_total{connection_security_policy!="mutual_tls"}` | NonMutualTLSToWorkload (warning) |
| Auth probing / path fuzzing | spike in `istio_requests_total{response_code="302"}` | SSOUnauthenticatedSpike (warning) |

Verified live: an in-mesh `attacker` pod hitting the app produced
`istio_requests_total{source_workload="attacker", source_principal=".../sa/default", response_code="403"}`
and **MeshAuthzDenied fired**.

## Access
```sh
kubectl -n monitoring port-forward svc/grafana 3000:3000     # http://localhost:3000  (dashboard: "Mesh Security")
kubectl -n monitoring port-forward svc/prometheus 9090:9090  # http://localhost:9090/alerts
```
Grafana: anonymous **Viewer** is on for read-only; the admin password is synced by ESO from the Key
Vault secret `grafana-admin-password` (Terraform-generated) into the `grafana-admin` Secret. Both are
ClusterIP / internal-only — nothing public.

## Deploy

**GitOps** — a dedicated Argo CD Application (`deploy/gitops/monitoring-application.yaml`) reconciles
this stack on every cluster, rendered via `render-overlay.sh RENDER_TARGET=monitoring`. `make up`
(cluster-init) registers it; Argo self-heals it thereafter — no manual `kubectl apply`. Secrets
(Grafana admin password, Alertmanager webhook) are synced from Key Vault by ESO, so nothing secret is
committed (ADR 0001). It's a **separate** Application from the app so a monitoring change can't stall
the app's rollout.

## Known tuning / caveats
- **NonMutualTLSToWorkload** currently also flags traffic Istio reports as
  `connection_security_policy="unknown"` (health probes, denied requests). Tighten it
  with `reporter="destination"` and by excluding probe sources before treating it as
  actionable — otherwise it's noisy.
- **Alertmanager is deployed** (`alertmanager.yaml`) and wired to Prometheus (verified:
  `MeshAuthzDenied` routes to it). The webhook URL is synced by ESO from the Key Vault secret
  `alertmanager-webhook-url`, which Terraform seeds with a placeholder (`example.invalid`) — delivery
  fails harmlessly until it's real. To go live, set the real URL in Key Vault and restart:
  `az keyvault secret set --vault-name <kv> --name alertmanager-webhook-url --value 'https://...' && kubectl -n monitoring rollout restart deploy alertmanager` (ESO re-syncs within its refresh interval).
- Retention is 6h on an `emptyDir` — this is a security-signal store, not long-term
  metrics. For history, point it at a PVC or Azure Monitor managed Prometheus.
- Not exposed through the gateway/SSO. If you want Grafana behind Entra like the app,
  add it to the mesh + a VirtualService route (and an allow policy), same pattern as
  pathtrace.

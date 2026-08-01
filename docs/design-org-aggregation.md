# Design: org-wide aggregation — a dependency/API governance control plane

## Problem

A single driftscope run scans one repo. But the value a *company* wants is fleet-wide: "which of our
10,000 repos are on a deprecating API," "did someone just upgrade a package past what we've sanctioned,"
"what do we actually run in production, and where." It is not feasible to check out 10k large codebases
in one run. So instead:

> Each CI pipeline run (on **release**) produces a driftscope **artifact** for its repo. A central,
> per-tenant service **ingests** those artifacts, **evaluates** each against the company's **policy** and
> the rest of the fleet, and answers two questions — *did you drift from what's sanctioned?* and *is
> anything you depend on at risk?* — as a **verdict** the CD pipeline can gate a prod promotion on.

driftscope stops being "a scanner" and becomes a **governance control plane** that detects drift from the
org's declared baseline in **both directions**:

- **Too far behind** → deprecation / EOL / vuln risk (the classic direction).
- **Too far ahead** → an *unsanctioned upgrade* ("you moved off the pinned version") — the novel half.

## Decisions (settled in design interview)

| Axis | Decision |
|---|---|
| Consumers | all four lenses — security, deprecation campaigns, platform health, inventory/compliance |
| Scale | 10k+ repos **per tenant** (frontier-company size) |
| Cadence | pipelines run **on release** → the view reflects *shipped* state; ingest is event-driven |
| Tenancy | **multi-tenant product** primary; internal use = one tenant (degenerate case) |
| Isolation | **strict per-tenant** — data *and* upstream resolution run under the tenant's own identity |
| Sanctioned version | a **pluggable source**: curated spec ⊕ fleet-derived baseline ⊕ composite |
| Evaluation | **central, async, per-tenant**; a few minutes' latency is fine (env soak absorbs it) |
| Enforcement | at **prod promotion**, via a **verdict API**; can hard-gate because latency is hidden |
| UI | a report/dashboard **pod on AKS**, behind the existing SSO edge |

## Architecture

```
  (per repo, on release)                    (central, per tenant)
  ┌────────────────────┐   artifact   ┌──────────────────────────────────────┐
  │ CI: driftscope scan │ ───────────▶ │  Ingest API  →  Inventory store       │
  │  emits report.json  │  POST        │     (latest release per repo)         │
  └────────────────────┘              │            │                          │
                                      │            ▼                          │
  ┌────────────────────┐   GET verdict │   Evaluation engine                   │
  │ CD: prod promotion  │ ◀──────────── │   inventory × sanctioned-source ×    │
  │  gate               │  pass|block   │   deprecation signals → Verdict      │
  └────────────────────┘              │            │                          │
                                      │            ▼                          │
  ┌────────────────────┐   GET views   │   Query API  →  UI pod (AKS)          │
  │ humans (SSO)        │ ◀──────────── │   "who uses X", blast radius, trends │
  └────────────────────┘              └──────────────────────────────────────┘
```

Everything on the right reuses infrastructure driftscope **already runs**: the AKS cluster, the Istio
gateway + oauth2-proxy SSO (for the UI/humans), Workload Identity (services→Azure), the Front Door edge,
and the BYO-forge-identity backbone (per-tenant ingest auth + resolution under the tenant's own creds).
This is an *extension*, not a greenfield build.

## The artifact contract (already ~exists)

The per-run artifact is the existing `report.json` (`schema: "driftscope/1"`). The evaluator needs only a
projection of it — the **inventory record**:

- `source.remote` — the repo identity (latest-wins key)
- `generated_at` + `source.commit` — freshness / provenance
- per finding: `component.ecosystem`, `component.name`, `component.current` (version in use),
  `severity`, `resolution.deprecated`, `resolution.latest_stable`

**Gap to close (open item #4):** the artifact carries commit + timestamp but no explicit **release
version**. On release cadence we want `release: "v2.3.0"` in `source` so provenance is "what shipped,"
not just a commit. Additive to `driftscope/1` → bump to `driftscope/2` or add an optional field.

## Sanctioned-version source (pluggable)

An interface — `sanctioned_version(ecosystem, name) -> version | None` — with composable providers:

- **CuratedSpec** — a hand-authored allowlist, `{ "pypi:django": "4.2.11", ... }`. Explicit control for
  the critical few. A package absent from the spec is *ungoverned* (no verdict).
- **FleetBaseline** — the de-facto standard computed from *this tenant's* inventory: the modal version of
  a package across repos, gated by a min-support threshold (e.g. ≥ N repos and ≥ X% share). Zero-config —
  flip it on and outliers surface immediately; a direct payoff of the aggregation.
- **Composite** — curated wins where present, baseline fills the rest.

Baseline is always computed **within the tenant's isolation boundary** — never across tenants.

## Evaluation (the two signals)

For a repo's inventory record, per governed component:

1. **Policy conformance (drift from sanctioned).** Compare `component.current` to `sanctioned_version`
   using `version.compare`:
   - equal → **conform**
   - repo version **>** sanctioned → **unsanctioned-upgrade** ("you moved ahead of policy")
   - repo version **<** sanctioned → **behind** (below the sanctioned floor)
2. **Deprecation / risk.** Independent of policy, from the report itself: `resolution.deprecated`, a
   `severity` of critical/high (stale/EOL), or breaking-change notes in the delta. Needs no policy at all.

A **Verdict** for a repo release = `{ status: pass | block, violations: [...] }`. What counts as `block`
vs advisory is policy config (e.g. block on unsanctioned-upgrade of a curated package; advise on behind).

## Multi-tenancy & isolation

- Every artifact, store record, and query is keyed by **tenant**. One tenant's data and resolution never
  touch another's.
- Ingest is authenticated by the tenant's identity (BYO-forge backbone). MVP stub: an explicit `tenant`
  id; production: per-tenant token, same mechanism as private-repo clone/resolution.
- The production store is a **per-tenant indexed store** (a DB) so "who uses log4j < 2.17" answers in
  seconds over 10k repos. The MVP uses a file-backed store to prove the *logic*, not the *scale*.

## Open decisions (not blocking the MVP)

1. **Ownership routing** — repo → team map (CODEOWNERS / service catalog) so a violation reaches an owner.
2. **Policy home & authoring** — where the curated spec + baseline config live; who may edit; audit trail.
3. **Ingest identity** — the per-tenant token exchange (reuse BYO-forge-identity).
4. **Artifact contract** — add the explicit `release` field; freeze the schema version.

*(Parked: an opt-in AI model for richer semantic severity/sentiment — a `driftscope[ai]` extra that
enriches signals when configured, heuristics otherwise, keeping the core dependency-free.)*

## MVP slice

Prove the **detection core** end to end, stdlib-only, reusing the `Report` model + `version` comparison:

- `driftscope/fleet/store.py` — file-backed per-tenant inventory; ingest a report.json → latest-per-repo.
- `driftscope/fleet/policy.py` — load a curated spec + baseline config.
- `driftscope/fleet/baseline.py` — compute the fleet baseline (modal version, min-support).
- `driftscope/fleet/evaluate.py` — inventory × sanctioned-source × deprecation → `Verdict`.
- `driftscope/fleet/api.py` — `POST /fleet/reports` (ingest), `GET /fleet/verdict`, `GET /fleet/inventory`.
- tests + a demo that ingests a few reports + a policy and prints verdicts.

**Out of MVP scope (deferred, documented above):** the indexed DB store, real per-tenant auth, ownership
routing, the UI pod, predictive "deprecation *soon*" dating. The MVP validates the logic and the API
shape; scale and auth are known follow-ons that reuse existing infrastructure.

## Known limitations (honest review)

- **Version constraints vs pins.** `component.current` is usually a resolved pin, but some ecosystems
  carry a range (`~> 2.99`). Conformance is exact-version; ranged pins evaluate on the resolved version
  and are flagged as lower-confidence. Acceptable for MVP; note in output.
- **"Deprecation soon" is really "deprecated / EOL / high-risk now."** True forward-looking prediction
  needs per-ecosystem EOL dates we don't uniformly have. MVP reports known-deprecated + high-severity;
  predictive dating is a later enrichment.
- **Fleet baseline is popularity, not safety.** The de-facto standard can itself be a bad version
  ("everyone's on the vulnerable one"). Mitigated by design: the *risk* signal is independent of
  conformance, so a deprecated baseline still flags every repo on it. (Verified in testing.)
- **MVP store is not the production store.** File-backed, single-node — proves logic, not 10k-scale.

### Hardening found by adversarial testing (`test_fleet.py`)

- **Repo identity is required (fail-closed).** A report with no `source.remote` and no explicit `repo`
  is *rejected*, not keyed to a shared `"unknown"` where two identity-less reports would silently
  overwrite each other. A governance store must never conflate two repos.
- **Baseline ties produce no standard.** A 3-vs-3 split has no majority; blessing one side (by arbitrary
  hash order) would flag half the fleet. `min_share` defaults to 0.6 and a tie for the top spot yields
  no baseline.
- **Version schemes are robust across ecosystems** — go `v`-prefix, docker major-only, Azure date
  versions, prerelease ordering all compare correctly via driftscope's own parser.

### Scale note (production, not MVP)

`fleet_baseline` recomputes over *all* records on *every* ingest → **O(N²)** for a full fleet load
(measured: ~16 ms/ingest at 800 repos, rising). The production reducer maintains **incremental
per-component version counts** (update on ingest, no full recompute) in the indexed store, so a
baseline lookup and a blast-radius query are both indexed rather than O(N).

# How driftscope measures drift

This document states what driftscope claims, how each claim is computed, which
upstream it came from, and where the method breaks down. It exists because a
staleness report is an argument made to someone who owns the system being judged,
and an argument you cannot inspect is one they are entitled to dismiss.

Everything here is rule-based. There is no model, no scoring heuristic tuned on a
corpus, and no number that cannot be traced to a stated rule and a named source.

---

## 1. The governing principle: measured zero ≠ not measured

This is the invariant the whole codebase is built around, and it is the reason
several fields exist that would otherwise look redundant.

A report that says "0 known vulnerabilities" when the advisory lookup was rate
limited is worse than useless — it is a false all-clear that a human will act on.
So a failed measurement never collapses into a passing one:

| Sentinel | Means |
|---|---|
| `None` | the lookup was attempted and failed, or was never possible |
| `-1` (`NOT_SAMPLED`) | deliberately not sampled, to stay inside an API budget |
| `0` | measured, and the answer is genuinely zero |
| `counts_available = False` | the count beside it is not a count |
| `complete = False` | this window's data does not cover the whole window |
| `unmatched` / `indiscriminate` | the query ran but could not be narrowed to your artifact |

Every one of these has been a bug at least once: a zero that meant "unknown"
reached a report and read as good news. They are load-bearing, not defensive
programming.

---

## 2. Two independent measures of "behind"

driftscope computes distance twice, from different evidence, and does not
reconcile them:

**Version distance** — how many majors, minors, patches and releases separate the
pin from latest stable. Computed from version strings alone. Requires the two
versions be *comparable* (§4).

**Calendar age** — `days_behind` (how much newer latest is than the pin) and
`current_age_days` (how old the pin is as of now). Computed from registry
timestamps.

They are deliberately kept apart because either can be available without the
other. An AKS node image version string is opaque and can never be compared, but
its date is perfectly meaningful — "I cannot compare these strings, but yours is
400 days older" is a useful sentence, and `lag_between` emits it
([version.py:317-326](../driftscope/version.py#L317-L326)). Conversely, an Azure
API version *is* a date, so it needs no registry timestamp at all.

---

## 3. Domains, and where the truth comes from

Nineteen ecosystems, each mapping to exactly one resolver
([resolvers/__init__.py:84](../driftscope/resolvers/__init__.py#L84)). The report
groups them into six sections
([report/summarize.py](../driftscope/report/summarize.py)).

### Infrastructure as code

| Ecosystem | Read from | Truth source | Measures |
|---|---|---|---|
| `terraform-provider` | `*.tf` | `registry.terraform.io/v1/providers/...` | version distance + dates |
| `terraform-module` | `*.tf` | `registry.terraform.io/v1/modules/...` | version distance + dates |
| `azure-api-version` | `*.bicep`, ARM JSON | `az provider show` (local CLI) | calver distance; preview flag |

Azure API versions come from the signed-in `az` CLI rather than a public
endpoint, because no public catalogue of per-resource-type API versions exists.
No `az`, or not logged in, means no resolution — reported as such, not as
current.

### Containers and orchestration

| Ecosystem | Read from | Truth source | Measures |
|---|---|---|---|
| `docker-image` (public) | `Dockerfile`, compose files, k8s manifests | Docker Hub `/v2/repositories/{}/tags` | version distance + **push** dates |
| `docker-image` (ACR) | same | ACR token + `/v2/{repo}/tags/list`, config blob | version distance + **build** dates + changelog |
| `docker-image` (other registries) | same | falls back to the project's GitHub releases | version distance + release dates |
| `helm-chart` | `Chart.yaml` | `artifacthub.io/api/v1/packages/...` | version distance + dates |
| `k8s-api` | k8s manifests | Kubernetes deprecation guide (baked in) | binary: removed / deprecated / fine |
| `k8s-cluster` | live Azure | `kubernetes.io/releases` | version distance |

### Runtimes and toolchains

| Ecosystem | Read from | Truth source | Measures |
|---|---|---|---|
| `dotnet-sdk` | `global.json` | `dotnet.microsoft.com` support policy (baked in) | **support status** |
| `dotnet-tfm` | `*.csproj`, `*.fsproj`, `*.vbproj` | same | **support status** |
| `jvm-toolchain` | `pom.xml`, `build.gradle` | Oracle SE support roadmap (baked in) | **support status** |
| `ruby-runtime` | `Gemfile`, `.gemspec`, `Gemfile.lock` | ruby-lang.org branch table (baked in) | **support status** |

### Application dependencies

| Ecosystem | Read from | Truth source |
|---|---|---|
| `npm` | `package.json`, `package-lock.json` | `registry.npmjs.org` |
| `pypi` | `pyproject.toml`, `requirements*.txt`, `pyvenv.cfg` | `pypi.org/pypi/{}/json` |
| `go-module` | `go.mod` | `proxy.golang.org` |
| `nuget` | `*.csproj`, `Directory.Packages.props`, `packages.lock.json` | `api.nuget.org` flat container + registration |
| `maven` | `pom.xml`, `build.gradle` | `search.maven.org/solrsearch` |
| `rubygems` | `Gemfile`, `Gemfile.lock`, `*.gemspec` | `rubygems.org/api/v1` |

### CI and build

| Ecosystem | Read from | Truth source |
|---|---|---|
| `github-action` | `.github/workflows/*.yml` | GitHub releases/tags API |

### Cloud resources (live scan only)

| Ecosystem | Read from | Truth source |
|---|---|---|
| `azure-sku` | live subscription | the live scan itself, which supplies the candidate list |
| `azure-node-image` | live AKS | `az aks nodepool get-upgrades` + Azure/AgentBaker releases |

### Cross-cutting: vulnerabilities

Every component with a current version is queried against **OSV**
(`api.osv.dev/v1/querybatch`) at *both* its current and its target version, in one
batched call. What an upgrade would fix is then a set difference over advisory
data — not an inference from version numbers.

---

## 4. Comparability: the refusal to guess

Two versions are compared only when they are the same *kind*, and for container
tags only within the same *variant*
([version.py:209-216](../driftscope/version.py#L209-L216)).

Four kinds are recognised:

- **semver** — `1.2.3`, `v1.2.3`, `3.0.0-rc.1`, up to four numeric segments
- **calver** — `2025-06-01`, `2025-06-01-preview` (Azure API versions, some charts)
- **k8sapi** — `v1`, `v1beta1`, `v2beta2` (ordered by group rules, not numbers)
- **opaque** — `alpine`, `latest`, AKS node image names

Mismatched kinds return `comparable = False` with a reason, which surfaces as
`unknown` severity rather than a fabricated distance.

**Variant scoping is the subtle one.** `python:3.12-slim` and `python:3.13-alpine`
are not two versions of one thing; they are different base operating systems that
happen to share a repository. Comparing them is meaningless, so driftscope
refuses. Further, the variant is the *whole* trailing token chain, not the last
token: `18-slim` and `18-bullseye-slim` are **different** variants. Collapsing
both to `slim` once caused the resolver to offer `26.5-bullseye-slim` as an
upgrade for `26.5-slim` — recommending an *older* Debian base as an improvement.

Pre-release ranking is `dev < alpha < preview < beta < rc < final`. An
alphabetic token that isn't in that table (`slim`, `final`) is **not** treated as
a pre-release marker — otherwise every variant tag would rank below its own
release.

---

## 5. Severity

Assigned by [`advisor.score`](../driftscope/advisor.py#L69). The rules are ordered,
and the ordering matters — several branches exist specifically to run *before* a
guard that would otherwise swallow the finding.

**Short-circuits, in order:**

1. Resolver failed with no version → `unknown`
2. Upstream marks it deprecated → `critical`
3. Floating or untagged (`latest`, no tag) → `medium`, regardless of numbers
4. AKS node image → scored on **age alone** (below)
5. Runtime/toolchain → scored on **support status** (below)
6. No latest / no current / not comparable → `unknown`
7. Kubernetes API → binary: in the deprecation table, or not

On (7): the code reads as a three-way split — `removed in` → `critical`,
`deprecated` → `high`, else `current`
([advisor.py:141-147](../driftscope/advisor.py#L141-L147)) — but **the `high` case
is unreachable**. `resolve_k8s_api` sets `deprecated = True` for every entry it
matches, and rule (2) above returns `critical` before the Kubernetes branch is
ever evaluated. All 22 entries in the migration table also carry a removal
version, so there is currently no such thing as a deprecated-but-not-removed
Kubernetes API in driftscope's data. **In practice: a matched Kubernetes API is
`critical`, an unmatched one is `current`.** Verified, not inferred.

**The general case** — version distance, then age escalation:

| Condition | Severity |
|---|---|
| ≥ 2 majors behind | `critical` |
| 1 major behind | `high` |
| ≥ 5 minors behind | `medium` |
| ≥ 1 minor behind | `low` |
| nothing behind | `current` |

then:

- `days_behind ≥ 730` escalates `low`/`medium` → `high`
- `days_behind ≥ 365` escalates `low` → `medium`
- a **preview** Azure API version in production IaC escalates to `high`

**AKS node images** get their own much tighter scale — 30 / 60 / 120 / 240 days
for low / medium / high / critical. They ship roughly weekly and carry OS security
patches, so the generic year-scale thresholds would rate a dangerously unpatched
node pool `low`.

**Runtimes are judged on support, not distance.** Java 17 is eight majors behind
25 and is a fully supported LTS. Rating that `critical` would train people to
ignore the report. So:

| Support state | Severity |
|---|---|
| past end of support | `critical` |
| ≤ 180 days left | `high` |
| ≤ 365 days left | `medium` |
| supported, non-LTS | `high` (six-month window, no time to plan) |
| supported LTS, no end date | `low` if a major behind, else `current` |
| no support data at all | never above `medium` |

**Vulnerabilities override distance.** After scoring, a component with a known
CVE at its *current* version is escalated to `critical` (if the worst advisory is
CRITICAL/HIGH) or `high`
([engine.py:254-261](../driftscope/engine.py#L254-L261)) — but only when the
advisory set was actually narrowed to that artifact (`indiscriminate` is false;
see §7).

---

## 6. Upstream health: delta and velocity

Two optional, API-expensive signals that answer "is upgrading safe" and "is this
project still alive".

**Delta** — for the window between your pin and latest: which bug-labelled issues
were opened, which release notes mention breaking changes. Runs only for findings
at or above `--delta-threshold` (default `high`), capped at `--max-delta` (25).
Queries every bug-label convention, not just the literal label `bug`.

**Velocity** — four 90-day windows over the upstream's issue feed, yielding three
trends and a majority-vote verdict:

- **backlog** — `opened − closed` per window. Reported as an *absolute*
  issues-per-window delta, not a percentage: percent change of a signed value
  straddling zero is meaningless (+2 → −4 is "−300%").
- **sentiment** — negative share of reactions, thresholded at **5 percentage
  points**, not 5%. A +35% relative move was only +4pp absolute, and showing the
  relative figure beside a "steady" verdict read as a contradiction.
- **shipping** — releases per window.

Windows the issue walk did not reach back far enough to cover are marked
`complete = False` and **excluded from the trend baseline**. An undercounted old
window makes the latest one look like it is accelerating.

Velocity is deduplicated by `(forge, repo)` — every Jackson artifact resolves to
one FasterXML repo, and a measurement costs ~13 API calls. The same `owner/name`
on two different forges is two different projects and is never merged.

---

## 7. Edge cases and known limits

These are the places the method is weakest. They are stated here rather than
smoothed over, because a reader who discovers one unaided stops trusting the rest.

### Docker Hub dates are push times, not release dates

Hub's `last_updated` is when the tag was last **pushed**. Base images are rebuilt
for OS patches, so an old tag can carry a recent timestamp. Day counts for Hub
images are therefore weaker evidence than version distance, and the report says so
in prose.

**Known gap:** the resolver also sets a machine-readable
`extra["dates_are_push_times"]` flag — and *nothing reads it*. The caveat reaches
the user only through the note text. A consumer of the JSON output cannot
distinguish a Hub push date from a real release date programmatically.

### Truncated tag listings

Hub is paged by **recency, not version**, and driftscope stops at 200 tags. So the
tags it did *not* fetch are arbitrary — not "the older ones". This blocks the
age-lower-bound heuristic below, because "oldest listed" in a recency-ordered
truncated list is simply the least-recently-pushed of the newest few hundred.
Without that block, `node:14` was reported as "13 days behind".

### The age lower bound

When a pin predates the registry's version window, its date is unknown. Rather
than report nothing for the *stalest* pins — exactly the ones that matter most —
driftscope uses the oldest listed release's date as an upper bound on the pin's
date, making the age a lower bound, flagged `age_lower_bound = True` and rendered
as "at least N days". Only applied when the release list is version-complete.

### Container vulnerability matching is approximate

An image tag is not a distro package version. driftscope maps a tag variant to an
OSV distro ecosystem (`-alpine` → Alpine, and so on) and queries with the image's
own version — which is a guess. Three outcomes are distinguished:

- **matched** — advisories differ between current and target; the fixed/remaining
  split is real
- **`unmatched`** — no advisories at either version. For an image this usually
  means the distro/package/version guess did not line up, **not that the image is
  clean**. Reported with that sentence.
- **`indiscriminate`** — the same advisory set matches both versions, so it could
  not be narrowed to your image. Treated as an upper bound, excluded from severity
  escalation, and the report tells you to run a real image scanner.

Verified case: `Debian:12` `redis` returns 72 advisories at both `5` and `8.8.1`,
while its real package version returns 25.

**Known gap:** bare variants (`slim`, or no variant at all) default to Debian:12,
which is now behind — bare `debian` tags are trixie.

### Constraint expressions

`>=3.0,<4.0` has no single version. driftscope extracts a floor, preferring the
token attached to `==`, `~>`, `^` or `>=`. An **upper bound alone** (`<2`,
`<=1.9`) returns `None`, not `2` — reporting `2` would claim the project is *on*
2.x when it is in fact capped *below* it, the opposite of the truth.

### Charts and forges

- A Helm chart defined in the scanned repo has no upstream; it is reported as
  "yours to bump", not as drift.
- Artifact Hub's repository URL is usually a Helm repo (`charts.bitnami.com`), not
  source. The source link lives in `links[]`; without checking there first, no
  chart ever gets an issue delta.
- Azure DevOps has no "releases" concept — the analogue is annotated git tags.
  **Lightweight tags carry no date or message at all** and are reported as such
  rather than dated from their commit, which would silently make "days behind"
  mean something different per tag.
- ADO work-item reactions live on *comments*, one API call per item, so sentiment
  is **not sampled by default** on ADO. It reports `unknown` with the reason —
  never `0%`.

### Budget truncation

`--max-delta` (25) and `--max-velocity` (6) cap the expensive lookups. Candidates
are ranked by severity first, so the budget is spent on the worst findings. When
velocity truncates, the number skipped is recorded in `report.errors` — a silent
cap would read as "we checked everything".

### What is not measured at all

- Transitive dependencies beyond what the lockfile names
- Whether an available upgrade actually builds or passes your tests
- Anything about a private registry driftscope has no credential for — reported as
  an error, never as current
- License, provenance, or supply-chain attestation

---

## 8. Reproducing any number in a report

Every finding carries its inputs. Given a report:

```
driftscope scan <path> --json out.json
```

each finding's `component` (what was found, in which file, at which line),
`resolution` (what upstream said, including the full release list and the
resolver's note), and `lag` (the computed distance, with `comparable` and
`reason`) are all present. The severity is a pure function of those three
([advisor.py:69](../driftscope/advisor.py#L69)) — no hidden state, no network call
at scoring time.

If a number looks wrong, the resolver's `note` field is the first place to look:
every caveat in §7 that can be detected at resolve time is written there.

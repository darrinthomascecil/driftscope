# driftscope

Measures how far behind latest stable a codebase's infrastructure, IaC and
dependencies are — and tells you how safely each gap can be closed.

Point it at a repo. It finds everything that has a version, asks upstream what the
current version is, computes the distance in majors/minors/releases/days, then
mines the upstream GitHub history for breaking changes and bug signals introduced
between where you are and where you'd be going.

```
driftscope scan ./customer-repo --markdown drift.md --json drift.json
driftscope scan https://github.com/org/repo          # clones, scans, cleans up
driftscope scan org/repo --ref release-2.1           # shorthand + a branch/tag
```

```
47 components in 15 files -> CRITICAL:15  HIGH:9  MEDIUM:4  LOW:2  current:2  unknown:15

[CRITICAL] pypi               Django              2.2.28   -> 6.0.7   (4 major, 168 releases, 1548 days)  app/requirements.txt:3
[CRITICAL] k8s-api            extensions/Ingress  v1beta1  -> v1                                          k8s/workloads.yaml:1
[HIGH    ] terraform-provider hashicorp/azurerm   3.75.0   -> 4.81.0  (1 major, 135 releases)             infra/.terraform.lock.hcl:1
[MEDIUM  ] docker-image       python              3.8-slim -> 3.14.6-slim (6 minor, 12 releases)          docker/Dockerfile:1
```

## Why not just `npm outdated` / `terraform init -upgrade` / Dependabot?

Those answer one ecosystem each, and only for code they can resolve. This answers
one question across the whole estate — *how current is this customer* — including
the surfaces no package manager covers: Bicep and ARM API versions, Kubernetes API
deprecations, container base image tags, CI runner images, and the versions actually
deployed in the cloud as opposed to the ones committed to the repo.

It also answers the follow-up question that decides whether anyone acts:
**how risky is the upgrade**, with the upstream evidence attached.

## No dependencies

Standard library only, Python 3.11+. Nothing to `pip install` into a customer's CI.

```bash
git clone <this repo> && cd driftscope
python3 -m driftscope scan .
# or install it
pip install -e .
driftscope scan .
```

GitHub lookups go through the `gh` CLI if it is installed and authenticated, so
there is no token to manage. Without `gh`, everything still works minus the issue
delta. Azure API version lookups use the `az` CLI; without it those come back
unresolved rather than guessed.

## What it covers

| Surface | Found in | Latest from |
|---|---|---|
| Terraform providers, modules, core | `*.tf`, `.terraform.lock.hcl` | Terraform Registry + GitHub releases |
| Azure API versions | `*.bicep`, ARM JSON | live ARM provider metadata (`az`) |
| Container images | Dockerfile, compose, K8s manifests | Docker Hub, GitHub releases |
| Helm charts | `Chart.yaml` + dependencies | Artifact Hub |
| Kubernetes APIs | any manifest | curated deprecation/removal table |
| Python packages | `requirements*.txt`, `pyproject.toml` | PyPI |
| npm packages, Node runtime | `package.json`, `package-lock.json` | npm registry, nodejs.org (LTS-aware) |
| Go modules, toolchain | `go.mod` | Go module proxy |
| GitHub Actions, runner images | `.github/workflows/*.yml` | GitHub releases |
| .NET packages, SDK pin, target frameworks | `*.csproj`, `Directory.Packages.props`, `global.json`, `packages.config` | NuGet + support-lifecycle dates |
| Maven / Gradle artifacts, Java + Gradle versions | `pom.xml`, `build.gradle`, `gradle-wrapper.properties` | Maven Central, LTS support dates |
| **Live**: model deployments, SKUs, AKS versions, node images | Azure subscription | `az` (read-only) |
| **Advisories** for every ecosystem above | — | OSV (GitHub Advisories, PyPA, distro trackers) |

`driftscope ecosystems` lists the filter names.

## Severity is rule-based, not vibes

A staleness report shown to a customer has to be arguable, so every score traces
to a stated rule:

| Severity | Rule |
|---|---|
| CRITICAL | deprecated/removed upstream, or ≥2 majors behind |
| HIGH | 1 major behind, or ≥730 days stale |
| MEDIUM | ≥5 minors behind, ≥365 days stale, or unpinned/floating |
| LOW | minor or patch behind |
| — | a CRITICAL/HIGH advisory escalates the finding regardless of distance |

Two surfaces are deliberately scored on something other than version distance:
**runtimes** (.NET target frameworks, Java, the .NET SDK) on support-expiry dates,
because Java 17 is eight "majors" behind 25 and is a perfectly supported LTS; and
**AKS node images** on age, because they ship weekly and carry OS security patches.
| unknown | not comparable — and it says why |

Two deliberate refusals, because a wrong number is worse than no number:

- **Different version schemes are never compared.** `3.12-slim` vs `3.13-alpine`
  is reported as "different image variants", not as "1 minor behind".
- **Expressions are never guessed.** `version = var.provider_version` is
  unresolved, not silently treated as the registry's latest.

## Upgrade safety

Each finding carries a strategy, ordered steps, and cautions specific to the
ecosystem — `terraform init -upgrade` then read the plan for `must be replaced`;
`az deployment group what-if` before an API version bump; `helm diff upgrade`
because chart majors rename values keys. Multi-major gaps get a **staged path**
through the highest release on each intervening major, so breakage is isolated and
revertible:

```
Staged path: 1.9.0 -> 2.4.0 -> 3.7.0 -> 4.1.0
```

## Known vulnerabilities, and what an upgrade would fix

Version distance says "you are behind". Advisory data says "you are exposed",
which is the sentence that gets an upgrade scheduled. Every component is queried
against [OSV](https://osv.dev) at **both** its current and its target version in a
single batched call, so the difference between those sets answers the question
directly:

```
Newtonsoft.Json 12.0.3 -> 13.0.4    1 exposed,  1 fixed by upgrading (CVE-2024-21907, HIGH)
express         4.16.0 -> 5.2.1     2 exposed,  2 fixed
jackson-databind 2.15.2 -> 2.19.0   5 exposed,  0 fixed -- all five affect the target too
node:18-slim -> 26.5-slim          50 exposed, 38 fixed, 12 remain
```

The jackson row is the reason this exists: version distance alone calls that a
routine minor bump. A CRITICAL or HIGH advisory also escalates the finding's
severity, so a known-vulnerable pin cannot rank below a stale CI action.

Counts come from advisory ids and are always exact; only the human-readable
descriptions are capped (6 per component), so a truncated report never shows a
truncated number.

**Container images are approximate and say so.** Without pulling an image there is
no package inventory, so the image's primary package is queried against the distro
implied by its tag variant. Where a distro version scheme does not line up with an
image tag — Debian's `redis` returns the same 72 advisories at both `5` and `8.8.1`
— the report refuses to split fixed from remaining and labels the count an upper
bound rather than inventing a verdict.

## Upstream project health: velocity and sentiment

`--velocity` measures which way each dependency's project is *moving*, over
consecutive windows, with each metric compared against the mean of the preceding
ones so the output is an acceleration rather than a level:

```
Upstream velocity  deteriorating   FasterXML/jackson-databind · 4 × 90-day windows

Issue backlog        ▁▅█▁    -3    +86    steady
Negative reactions   ▁▅▁█     8%    +6pp  deteriorating
Releases shipped     ▁▁▁▁     0     —     steady
```

Sentiment comes from GitHub *reactions* (👍❤️🎉🚀😄 positive, 👎😕 negative) — structured
data, not inferred text. It is a proxy for how users feel, not a measurement, and
the report says so. One paginated walk of the issues list covers every window; that
endpoint is on the core rate limit (5000/hour) rather than the search limit
(~30/minute), so a quiet project costs two calls and a busy one about fifteen.

Windows the walk could not reach back to are marked incomplete and **excluded from
the trend** — an undercounted old window would make the latest one look like it was
accelerating.

## Are we catching up? Estate history

`scan --history DIR` saves each run and reports movement since the last one;
`trend DIR` compares any two. Findings match on `(ecosystem, name)`, so a bumped
version reads as the same component improving rather than one finding vanishing and
another appearing:

```
$ driftscope trend .driftscope/
20260727T012711Z.json -> 20260727T012745Z.json
actionable findings: 11 -> 7 (-4, improving)

improved (4):
  actions/checkout    critical -> current   4      -> 7
  Newtonsoft.Json     high     -> low       12.0.3 -> 13.0.4
```

## The upstream delta, and its honest limits

For findings at or above `--delta-threshold`, driftscope resolves both versions to
release tags and dates, then:

1. **Quotes breaking-change notes** from the release bodies in that window
   (`BREAKING`, `removed`, `deprecated`, `migration guide`, …). This is the
   high-confidence signal — maintainers announce breakage.
2. **Counts and links bug-labelled issues filed in the window**, open ones first.

Step 2 is an approximation and the report says so on every page. An issue filed
during the window may predate the release; a regression introduced by it may be
filed months later. It is offered as *signals with links*, never as a verdict, and
never as a count you should trust without clicking through.

There is no GitHub API for "bugs introduced by version X". This is the closest
defensible thing, labelled as such.

## Usage

```bash
driftscope scan .                                   # everything, terminal summary
driftscope scan https://github.com/org/repo          # remote: shallow clone, scan, delete
driftscope scan org/repo --ref v2.1 --keep-clone     # pin a ref, keep the clone
driftscope scan ./infra --ecosystem terraform,azure # scope it
driftscope scan . --markdown drift.md               # customer-facing report
driftscope scan . --json drift.json                 # machine-readable
driftscope scan . --fail-on high                    # CI gate: exit 1 past threshold
driftscope scan . --live-azure --resource-group rg   # add deployed-resource drift
driftscope scan . --offline                          # cache only, no sockets
driftscope scan . --no-github --no-az                # fully air-gapped
driftscope scan . --html                             # auto-named: drift-<repo>.html
driftscope scan . --html --open                      # ...and open it
driftscope scan . --velocity                         # add upstream health
driftscope scan . --no-vulns                         # skip the OSV lookup
driftscope scan . --history .driftscope/             # save the run, report movement
driftscope trend .driftscope/                        # compare stored runs
```

Output flags take an optional value: omit it (or pass a directory) and the file is
named after what was scanned, so reports from different repositories never collide.

Reports come in four shapes and all four carry the same content and the same
caveats: a terminal summary, Markdown, JSON, and a self-contained HTML page where
each finding expands in place to show its guidance, advisories and upstream health.

Every HTTP GET is cached on disk (6h TTL, `~/.cache/driftscope`), so re-runs and
`--offline` runs are fast and reproducible. `--delta-threshold` and `--max-delta`
bound the GitHub API spend; the default gives a delta to at most 25 findings at
HIGH or above.

## Remote targets

A git URL (or `owner/repo` shorthand) is shallow-cloned to a temp directory,
scanned, and deleted. The report is attributed to the repo and commit, not the
temp path, so results are reproducible:

```
**Scanned:** `https://github.com/grafana/loki`
**Commit:** `195b91ab...` on `main`
```

Clones are `--depth 1 --single-branch --no-tags` with hooks disabled and terminal
prompts off, so a hostile repo cannot execute anything and a private repo fails
fast instead of hanging on a credential prompt. A local directory always wins over
the shorthand form, so a directory named `foo/bar` is never mistaken for a repo.

## Live vs static

Static scanning tells you what the IaC *claims*. `--live-azure` tells you what is
actually deployed, which differs the moment someone changes something in the portal
or the IaC was never applied. Live findings carry `source="azure://<resource-id>"`
so the report distinguishes "your repo is stale" from "your subscription is stale".

Every live command is a `show`/`list`/`get-*`. Nothing in this tool mutates cloud
state.

## Development

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'
./.venv/bin/python -m pytest -q
```

### Fixtures

`fixtures/stale-estate/` is a deliberately outdated tree covering every supported
surface. `fixtures/dotnet-jvm/` covers NuGet/Maven plus edge cases a compiling app
does not exercise (Central Package Management, multi-targeting, `packages.config`,
Gradle, unresolved `${property}` indirection).

**`github.com/darrinthomascecil/driftscope-testapp`** is the live end-to-end
fixture: minimal .NET and Java apps that genuinely compile (verified by their own
CI) while pinning popular packages deliberately behind current. Kept public on
purpose — the remote-clone path runs without credentials, so a private repo would
not exercise it. Its packages are intentionally stale, including one with a known
CVE, so dependency-scanning alerts on that repo are expected rather than a problem
to fix.

Scanner and advisor tests run offline against the local fixtures; engine and report
tests stub resolution for determinism. Tests marked `network` hit real registries
and guard against upstream API shape changes:

```bash
./.venv/bin/python -m pytest -q -m "not network"   # offline, fast
./.venv/bin/python -m pytest -q -m network          # real registries
```

## Known limitations

- **HCL is parsed with regex**, not a real parser. Version metadata blocks are
  highly conventional so this is reliable in practice, but interpolation and
  variables are not evaluated (they are reported unresolved).
- **YAML uses a small subset parser** — flat keys and simple list-of-mappings.
  Anchors and flow mappings are skipped.
- **Only Docker Hub enumerates tags.** ghcr.io/mcr images fall back to GitHub
  releases when the repo is known, otherwise they are unresolved.
- **Transitive dependencies are not scanned** — direct declarations only.
- **ARM resource providers are not open source**, so Azure API version findings
  never carry an issue delta.
- The Kubernetes deprecation table is curated and needs updating as releases land.

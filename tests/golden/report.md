# Version drift report

**Scanned:** `/tmp/estate`  
**Commit:** `0123456789ab` on `main`  
**Generated:** <SCRUBBED>
**Coverage:** 10 versioned components across 41 files - 8 resolved, 1 unresolved (12.34s, 18 GitHub queries)

## Summary

| Severity | Count | Meaning |
|---|---:|---|
| **CRITICAL** | 2 | Deprecated/removed upstream, or 2+ majors behind - will break |
| **HIGH** | 2 | One major behind, or 2+ years stale |
| **MEDIUM** | 2 | Several minors behind, over a year old, or unpinned |
| **LOW** | 1 | Minor or patch behind |
| **current** | 1 | At the latest stable version |
| **unknown** | 2 | Could not be assessed - see notes |

## Since the last run

**improving** &middot; -1 actionable findings (`2026-01-02T00-00-00` -> `2026-07-27T00-00-00`)

| Change | Count |
|---|---:|
| worsened | 13 |
| new | 1 |
| improved | 1 |
| resolved | 1 |
| unchanged | 7 |

| Change | Component | Severity | Version |
|---|---|---|---|
| worsened | `org.example:lib-0` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-1` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-2` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-3` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-4` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-5` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-6` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-7` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-8` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-9` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-10` | medium -> high | `1.0.0` |
| worsened | `org.example:lib-11` | medium -> high | `1.0.0` |
| worsened | _+1 more_ | | |
| new | `redis` | - -> high | `5` |
| improved | `org.apache.commons:commons-lang3` | high -> low | `3.9` -> `3.19.0` |
| resolved | `hashicorp/random` | high -> gone | `3.1.0` |

_matched 24 of 26 components across the two runs_

_Findings are matched by ecosystem and name, so a bumped version reads as the same component moving rather than one disappearing and another appearing. "Actionable" counts MEDIUM and worse._

## By ecosystem

| Ecosystem | Components | Worst |
|---|---:|---|
| Terraform provider | 2 | CRITICAL |
| Maven artifact | 1 | CRITICAL |
| Container image | 3 | HIGH |
| Azure API version | 1 | MEDIUM |
| Go module | 1 | MEDIUM |
| JVM toolchain | 1 | LOW |
| Kubernetes API | 1 | current |

## Drift table

| Sev | Component | Current | Latest stable | Behind | Where |
|---|---|---|---|---|---|
| CRITICAL | `hashicorp/azurerm` <br/><sub>Terraform provider</sub> | `2.99.0` | `4.81.0` | 2 major, 220 releases, 1586 days | `.terraform.lock.hcl`:12 |
| CRITICAL | `com.fasterxml.jackson.core:jackson-databind` <br/><sub>Maven artifact</sub> | `2.15.2` | `2.20.0` | 5 minor, 38 releases, 900 days | `pom.xml`:34 |
| HIGH | `redis` <br/><sub>Container image</sub> | `5` | `8.8.1` | 3 major, 2100 days | `docker-compose.yml`:7 |
| HIGH | `postgres` <br/><sub>Container image</sub> | `15` | `18.2` | 3 major, 1100 days | `docker-compose.yml`:15 |
| MEDIUM | `Microsoft.CognitiveServices/accounts` <br/><sub>Azure API version</sub> | `2023-05-01` | `2026-05-01` | 3 years, 4 api-versions, 1096 days | `main.bicep`:21 |
| MEDIUM | `github.com/spf13/cobra` <br/><sub>Go module</sub> | `v1.4.0` | `v1.8.1` | 4 minor, 13 releases, 760 days | `svc/go.mod`:6 |

## Upgrade guidance

### CRITICAL - `hashicorp/azurerm` (Terraform provider)

**Should you upgrade? — PLAN A STAGED UPGRADE**

- _Why upgrade:_ 2 major versions behind; 4 years old
- _Why wait:_ crosses 2 major versions -- stage the upgrade

- **Current:** `2.99.0` (constraint `~> 2.99`)
- **Latest stable:** `4.81.0`
- **Behind by:** 2 major, 220 releases, 1586 days
- **Age of your version:** 4.4 years (1598 days since publish)
- **Declared at:** `.terraform.lock.hcl`:12
- **Source:** https://github.com/hashicorp/terraform-provider-azurerm
- **Checked against:** [Terraform Registry](https://registry.terraform.io)

**Strategy.** Staged upgrade. Do not jump 2.99.0 -> 4.81.0 in one change.

**Staged path:** `2.99.0` -> `3.117.1` -> `4.81.0`

**Steps**

1. Set version = "~> 4.81.0" in required_providers

**Cautions**

- Provider major bumps can force resource replacement

### CRITICAL - `com.fasterxml.jackson.core:jackson-databind` (Maven artifact)

**Should you upgrade? — PLAN A STAGED UPGRADE**

- _Why upgrade:_ 9 known vulnerabilities cleared by upgrading (3 critical/high); 3 years old
- _Why wait:_ 3 breaking-change notes quoted from the release notes; the target introduces 1 new vulnerability

- **Current:** `2.15.2`
- **Latest stable:** `2.20.0`
- **Behind by:** 5 minor, 38 releases, 900 days
- **Age of your version:** 3.3 years (1200 days since publish)
- **Declared at:** `pom.xml`:34
- **Also at:** `service-a/pom.xml`, `service-b/pom.xml`
- **Source:** https://github.com/FasterXML/jackson-databind
- **Docs:** https://github.com/FasterXML/jackson-databind
- **Checked against:** [Maven Central](https://search.maven.org)

**Strategy.** Staged upgrade. Step through 2.17.x so each minor's serialisation changes are isolated and revertible.

**Staged path:** `2.15.2` -> `2.17.2` -> `2.20.0`

**Steps**

1. Bump the jackson-bom entry, not the individual artifacts
2. Run the contract tests before the unit tests
3. Change one service per commit so a regression bisects cleanly

**Cautions**

- jackson ships many artifacts on one version line; a partial upgrade silently mixes versions
- 3 breaking-change note(s) found in release notes

<details>
<summary>Upstream delta 2.15.2 -> 2.20.0 (5 releases)</summary>

**Breaking-change notes from release bodies**

- 2.16.0: `ObjectMapper` - the `USE_BIG_DECIMAL_FOR_FLOATS` default has changed
- 2.17.0: removed support for the legacy `JsonSerializable` interface
- 2.18.0: minimum required Java version is now 17

**Bug activity in the version window** (14 open of 61 filed) — a rough signal, not scoped to a specific version

- [#4100](https://github.com/FasterXML/jackson-databind/issues/4100) Deserialisation regression #0
- [#4101](https://github.com/FasterXML/jackson-databind/issues/4101) Deserialisation regression #1
- [#4102](https://github.com/FasterXML/jackson-databind/issues/4102) Deserialisation regression #2

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

**Known vulnerabilities** &middot; 11 affecting `2.15.2` &middot; `com.fasterxml.jackson.core:jackson-databind` via OSV

- **Fixed by upgrading to 2.20.0: 9**
  - ` 98/100` [CVE-2024-1001](https://osv.dev/vulnerability/GHSA-xxxx-0001) `CRITICAL` Advisory number 1 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - ` 88/100` [CVE-2024-1002](https://osv.dev/vulnerability/GHSA-xxxx-0002) `HIGH` Advisory number 2 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - ` 75/100` [CVE-2024-1003](https://osv.dev/vulnerability/GHSA-xxxx-0003) `MODERATE` Advisory number 3 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - ` 61/100` [CVE-2024-1004](https://osv.dev/vulnerability/GHSA-xxxx-0004) `LOW` Advisory number 4 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - ` --/100` [CVE-2024-1005](https://osv.dev/vulnerability/GHSA-xxxx-0005) `unrated` Advisory number 5 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - `100/100` [CVE-2024-1006](https://osv.dev/vulnerability/GHSA-xxxx-0006) `HIGH` Advisory number 6 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - _+3 more not described_
- **Still present after upgrade: 2**
  - ` 88/100` [CVE-2024-1020](https://osv.dev/vulnerability/GHSA-xxxx-0020) `HIGH` Advisory number 20 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - ` 75/100` [CVE-2024-1021](https://osv.dev/vulnerability/GHSA-xxxx-0021) `MODERATE` Advisory number 21 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
- **Introduced by the upgrade: 1**
  - `100/100` [CVE-2024-1030](https://osv.dev/vulnerability/GHSA-xxxx-0030) `LOW` Advisory number 30 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.

**Upstream velocity** &middot; deteriorating &middot; `FasterXML/jackson-databind` (4 x 90-day windows)

| Metric | Trend | Latest | Change | Verdict |
|---|---|---:|---:|---|
| Issue backlog | `▂▂▅█` | +60 | +42 | deteriorating |
| Negative reactions | `▃▅█▁` | — | +11pp | deteriorating |
| Releases shipped | `█▅▄▂` | 1 | -77% | deteriorating |

_Latest window 2025-10-01..2026-01-01: 90 opened, 30 closed, 0 reactions across 35 sampled issues, 1 release(s)._

_oldest window excluded from the baseline: the issue walk stopped at page 20 before covering it_

### HIGH - `redis` (Container image)

**Should you upgrade? — PLAN A STAGED UPGRADE**

- _Why upgrade:_ 3 major versions behind; 6 years old
- _Why wait:_ crosses 3 major versions -- stage the upgrade

- **Current:** `5`
- **Latest stable:** `8.8.1`
- **Behind by:** 3 major, 2100 days
- **Age of your version:** at least 6.6 years — this version predates the registry's version window, so its exact publish date is unavailable
- **Declared at:** `docker-compose.yml`:7
- **Source:** https://github.com/redis/redis
- **Checked against:** [GitHub releases (redis/redis)](https://github.com/redis/redis/releases)

**Strategy.** Rebuild against the 8.x tag.

**Steps**

1. Pin a full version, not a bare major

**Known vulnerabilities** &middot; 72 on record &middot; `redis` via OSV (approximate)

- **Advisories on record: 72**
  - ` 61/100` [CVE-2024-1040](https://osv.dev/vulnerability/GHSA-xxxx-0040) `HIGH` Advisory number 40 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - ` --/100` [CVE-2024-1041](https://osv.dev/vulnerability/GHSA-xxxx-0041) `HIGH` Advisory number 41 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - `100/100` [CVE-2024-1042](https://osv.dev/vulnerability/GHSA-xxxx-0042) `HIGH` Advisory number 42 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - ` 98/100` [CVE-2024-1043](https://osv.dev/vulnerability/GHSA-xxxx-0043) `HIGH` Advisory number 43 with a summary long enough to exercise the 110- and 120-character truncation in both reporters.
  - _+68 more not described_

_both versions matched the same 72 advisories, so this query did not discriminate between them; distro package versions do not follow image tags. The fixed/remaining split is withheld._

### HIGH - `postgres` (Container image)

**Should you upgrade? — PLAN A STAGED UPGRADE**

- _Why upgrade:_ 3 major versions behind; 3 years old
- _Why wait:_ crosses 3 major versions -- stage the upgrade

- **Current:** `15`
- **Latest stable:** `18.2`
- **Behind by:** 3 major, 1100 days
- **Age of your version:** 3.1 years (1150 days since publish)
- **Declared at:** `docker-compose.yml`:15
- **Source:** https://github.com/postgres/postgres
- **Checked against:** [GitHub releases (postgres/postgres)](https://github.com/postgres/postgres/releases)

**Strategy.** Plan a major-version data migration.

_Vulnerability lookup: the guessed distro/package/version triple returned no advisories, which means the mapping found nothing -- not that the image is clean_

### MEDIUM - `Microsoft.CognitiveServices/accounts` (Azure API version)

**Should you upgrade? — PLAN A STAGED UPGRADE**

- _Why upgrade:_ 3 major versions behind
- _Why wait:_ crosses 3 major versions -- stage the upgrade

- **Current:** `2023-05-01`
- **Latest stable:** `2026-05-01`
- **Behind by:** 3 years, 4 api-versions, 1096 days
- **Declared at:** `main.bicep`:21
- **Checked against:** [Azure Resource Manager provider metadata](https://learn.microsoft.com/rest/api/resources/providers/list)

**Strategy.** Bump the apiVersion and re-deploy.

### MEDIUM - `github.com/spf13/cobra` (Go module)

**Should you upgrade? — UPGRADE NOW**

- _Why upgrade:_ 2 years old

- **Current:** `v1.4.0`
- **Latest stable:** `v1.8.1`
- **Behind by:** 4 minor, 13 releases, 760 days
- **Age of your version:** 2.7 years (980 days since publish)
- **Declared at:** `svc/go.mod`:6
- **Source:** https://github.com/spf13/cobra
- **Docs:** https://pkg.go.dev/github.com/spf13/cobra
- **Checked against:** [Go module proxy](https://proxy.golang.org)

**Strategy.** Bump the require line and reconcile the module graph.

**Steps**

1. Update the github.com/spf13/cobra version in go.mod
2. Run `go mod tidy` to settle indirect dependencies

## Not assessed

| Component | Reason |
|---|---|
| `hashicorp/kubernetes` <sub>Terraform provider</sub> | unparseable version ('' vs '3.2.1') |
| `internal/tool` <sub>Container image</sub> | registry returned HTTP 404 |

## Scan errors

- velocity: measured 6 of 11 eligible repos (--max-velocity); the rest were skipped

---

**How to read this.** Severity is rule-based and inspectable: deprecated or 2+ majors behind is CRITICAL, one major behind or 2+ years stale is HIGH. Runtimes and AKS node images are scored on support expiry and age instead of version distance.

Issue counts in an upstream delta are *issues filed during the version window* -- a signal, not proof of a regression; the breaking-change notes quoted from release bodies are the higher-confidence input. Velocity percentages compare the latest window with the mean of the preceding ones, so they read as acceleration rather than level, and reaction mix is a proxy for sentiment rather than a measurement of it.

Vulnerability data comes from OSV. "Fixed by upgrading" is the set of advisories affecting the current version but not the target, computed from advisory ranges. For container images the query is approximate and says so: without an image scan there is no package inventory, and where a distro version scheme does not line up with an image tag the report declines to split fixed from remaining rather than guess.

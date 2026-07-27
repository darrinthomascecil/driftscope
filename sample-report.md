# Version drift report

**Scanned:** `/Users/darrincecil/projects/driftscope/fixtures/stale-estate`  
**Generated:** 2026-07-26 17:35 PDT  
**Coverage:** 47 versioned components across 15 files - 41 resolved, 0 unresolved (15.13s, 125 GitHub queries)

## Summary

| Severity | Count | Meaning |
|---|---:|---|
| **CRITICAL** | 22 | Deprecated/removed upstream, or 2+ majors behind - will break |
| **HIGH** | 12 | One major behind, or 2+ years stale |
| **MEDIUM** | 3 | Several minors behind, over a year old, or unpinned |
| **current** | 2 | At the latest stable version |
| **unknown** | 8 | Could not be assessed - see notes |

## By ecosystem

| Ecosystem | Components | Worst |
|---|---:|---|
| Container image | 6 | CRITICAL |
| Helm chart | 3 | CRITICAL |
| npm package | 5 | CRITICAL |
| Azure API version | 5 | CRITICAL |
| GitHub Action | 6 | CRITICAL |
| Python package | 7 | CRITICAL |
| Kubernetes API | 4 | CRITICAL |
| Terraform provider | 5 | HIGH |
| Go module | 4 | HIGH |
| Terraform module | 2 | HIGH |

## Drift table

| Sev | Component | Current | Latest stable | Behind | Where |
|---|---|---|---|---|---|
| CRITICAL | `node` <br/><sub>Container image</sub> | `14-alpine` | `26.5-alpine` | 12 major, 6 releases | `docker/web.Dockerfile`:1 |
| CRITICAL | `redis` <br/><sub>Helm chart</sub> | `16.8.0` | `27.0.17` | 11 major, 316 releases, 1511 days | `helm/Chart.yaml` |
| CRITICAL | `node` <br/><sub>npm package</sub> | `16` | `24.18.0` | 8 major, 250 releases | `web/package.json` |
| CRITICAL | `postgresql` <br/><sub>Helm chart</sub> | `11.6.12` | `18.8.0` | 7 major, 395 releases, 1476 days | `helm/Chart.yaml` |
| CRITICAL | `postgres` <br/><sub>Container image</sub> | `11` | `18.4` | 7 major, 42 releases | `docker-compose.yml`:5 |
| CRITICAL | `Microsoft.Storage/storageAccounts` <br/><sub>Azure API version</sub> | `2021-04-01` | `2026-04-01` | 5 years, 14 api-versions, 1826 days | `azure/template.json` |
| CRITICAL | `Microsoft.KeyVault/vaults` <br/><sub>Azure API version</sub> | `2021-10-01` | `2026-02-01` | 5 years, 7 api-versions, 1584 days | `azure/main.bicep`:10 |
| CRITICAL | `actions/checkout` <br/><sub>GitHub Action</sub> | `2` | `7` | 5 major, 38 releases | `.github/workflows/ci.yml`:7 |
| CRITICAL | `jest` <br/><sub>npm package</sub> | `26.0.0` | `30.4.2` | 4 major, 101 releases, 2195 days | `web/package.json` |
| CRITICAL | `Django` <br/><sub>Python package</sub> | `2.2.28` | `6.0.7` | 4 major, 168 releases, 1548 days | `app/requirements.txt`:3 |
| CRITICAL | `docker/build-push-action` <br/><sub>GitHub Action</sub> | `3` | `7` | 4 major, 40 releases, 1247 days | `.github/workflows/ci.yml`:11 |
| CRITICAL | `react` <br/><sub>npm package</sub> | `16.8.6` | `19.2.8` | 3 major, 45 releases, 2672 days | `web/package.json` |
| CRITICAL | `pytest` <br/><sub>Python package</sub> | `6.2.5` | `9.1.1` | 3 major, 41 releases, 1753 days | `app/pyproject.toml` |
| CRITICAL | `Microsoft.CognitiveServices/accounts` <br/><sub>Azure API version</sub> | `2023-05-01` | `2026-05-01` | 3 years, 6 api-versions, 1096 days | `azure/main.bicep`:3 |
| CRITICAL | `Microsoft.App/containerApps` <br/><sub>Azure API version</sub> | `2023-04-01-preview` | `2026-01-01` | 3 years, 5 api-versions, 1006 days | `azure/main.bicep`:19 |
| CRITICAL | `actions/setup-python` <br/><sub>GitHub Action</sub> | `4` | `7` | 3 major, 13 releases, 458 days | `.github/workflows/ci.yml`:8 |
| CRITICAL | `redis` <br/><sub>Container image</sub> | `5` | `8.8.1` | 3 major, 58 releases | `docker-compose.yml`:3 |
| CRITICAL | `flask` <br/><sub>Python package</sub> | `1.0.2` | `3.1.3` | 2 major, 33 releases, 2849 days | `app/requirements.txt`:4 |
| CRITICAL | `runner/ubuntu-20.04` <br/><sub>GitHub Action</sub> | `ubuntu-20.04` | `ubuntu-24.04` | not comparable | `.github/workflows/ci.yml`:5 |
| CRITICAL | `autoscaling/HorizontalPodAutoscaler` <br/><sub>Kubernetes API</sub> | `v2beta2` | `v2` | 1 release | `k8s/workloads.yaml`:1 |
| CRITICAL | `extensions/Ingress` <br/><sub>Kubernetes API</sub> | `v1beta1` | `v1` | 1 release | `k8s/workloads.yaml`:1 |
| CRITICAL | `policy/PodDisruptionBudget` <br/><sub>Kubernetes API</sub> | `v1beta1` | `v1` | 1 release | `k8s/workloads.yaml`:1 |
| HIGH | `express` <br/><sub>npm package</sub> | `4.16.0` | `5.2.1` | 1 major, 27 releases, 2986 days | `web/package.json` |
| HIGH | `pydantic` <br/><sub>Python package</sub> | `1.10.2` | `2.13.4` | 1 major, 85 releases, 1338 days | `app/pyproject.toml` |
| HIGH | `hashicorp/azurerm` <br/><sub>Terraform provider</sub> | `3.75.0` | `4.81.0` | 1 major, 135 releases, 1019 days | `infra/.terraform.lock.hcl`:1 |
| HIGH | `hashicorp/azurerm` <br/><sub>Terraform provider</sub> | `3.0` | `4.81.0` | 1 major, 219 releases, 773 days | `infra/main.tf`:1 |
| HIGH | `github.com/inconshreveable/mousetrap` <br/><sub>Go module</sub> | `v1.0.0` | `v1.1.0` | 1 minor, 2 releases, 2963 days | `svc/go.mod` |
| HIGH | `requests` <br/><sub>Python package</sub> | `2.20.0` | `2.34.2` | 14 minor, 27 releases, 2765 days | `app/requirements.txt`:2 |
| HIGH | `lodash` <br/><sub>npm package</sub> | `4.17.11` | `4.18.1` | 1 minor, 13 releases, 2758 days | `web/package.json` |
| HIGH | `azure-identity` <br/><sub>Python package</sub> | `1.5.0` | `1.25.3` | 20 minor, 29 releases, 1947 days | `app/requirements.txt`:5 |
| HIGH | `github.com/stretchr/testify` <br/><sub>Go module</sub> | `v1.7.0` | `v1.11.1` | 4 minor, 14 releases, 1687 days | `svc/go.mod` |
| HIGH | `github.com/spf13/cobra` <br/><sub>Go module</sub> | `v1.4.0` | `v1.10.2` | 6 minor, 11 releases, 1364 days | `svc/go.mod` |
| HIGH | `hashicorp/random` <br/><sub>Terraform provider</sub> | `3.4.3` | `3.9.0` | 5 minor, 11 releases, 1342 days | `infra/.terraform.lock.hcl`:7 |
| HIGH | `Azure/naming/azurerm` <br/><sub>Terraform module</sub> | `0.3.0` | `0.4.3` | 1 minor, 4 releases, 947 days | `infra/modules.tf`:1 |
| MEDIUM | `hashicorp/terraform` <br/><sub>Terraform provider</sub> | `1.3` | `1.15.8` | 12 minor, 39 releases, 602 days | `infra/main.tf`:2 |
| MEDIUM | `nginx` <br/><sub>Container image</sub> | `latest` | `?` | not comparable | `docker-compose.yml`:7 |
| MEDIUM | `python` <br/><sub>Container image</sub> | `3.8-slim` | `3.14.6-slim` | 6 minor, 12 releases | `docker/Dockerfile`:1 |

## Upgrade guidance

### CRITICAL - `node` (Container image)

- **Current:** `14-alpine` (constraint `node:14-alpine`)
- **Latest stable:** `26.5-alpine`
- **Behind by:** 12 major, 6 releases
- **Declared at:** `docker/web.Dockerfile`:1
- **Source:** https://github.com/nodejs/docker-node
- **Docs:** https://hub.docker.com/_/node

**Strategy.** Staged upgrade. Do not jump 14-alpine -> 26.5-alpine in one change; step through 24.18.0-alpine so each major's breaking changes are isolated and revertible.

**Staged path:** `14-alpine` -> `24.18.0-alpine` -> `26.5-alpine`

**Steps**

1. Update the FROM tag to 26.5-alpine
2. Rebuild and run the image's own test suite; base images change libc, OpenSSL and CA bundles between majors
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- compared within variant 'alpine'; dates are Docker Hub last-push times, not original release dates; tag listing truncated at 200 tags (Hub pages by recency, not version), so an old pin may be absent and undatable; tag '14-alpine' was not in the fetched listing; age unavailable

### CRITICAL - `redis` (Helm chart)

- **Current:** `16.8.0`
- **Latest stable:** `27.0.17`
- **Behind by:** 11 major, 316 releases, 1511 days
- **Age of your version:** at least 4.1 years — this version predates the registry's version window, so its exact publish date is unavailable
- **Declared at:** `helm/Chart.yaml`
- **Source:** https://github.com/bitnami/charts
- **Docs:** https://bitnami.com

**Strategy.** Staged upgrade. Do not jump 16.8.0 -> 27.0.17 in one change; step through 17.17.1, 18.19.4, 19.6.4, 20.13.4, 21.2.14, 22.0.7, 23.2.12, 24.1.8, 25.5.3 so each major's breaking changes are isolated and revertible.

**Staged path:** `16.8.0` -> `17.17.1` -> `18.19.4` -> `19.6.4` -> `20.13.4` -> `21.2.14` -> `22.0.7` -> `23.2.12` -> `24.1.8` -> `25.5.3` -> `27.0.17`

**Steps**

1. Bump the dependency to 27.0.17 and run `helm dependency update`
2. Run `helm diff upgrade` (helm-diff plugin) before applying
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- Chart majors frequently rename values keys; your values.yaml may silently stop applying

### CRITICAL - `node` (npm package)

- **Current:** `16` (constraint `>=16`)
- **Latest stable:** `24.18.0`
- **Behind by:** 8 major, 250 releases
- **Declared at:** `web/package.json`
- **Source:** https://github.com/nodejs/node
- **Docs:** https://nodejs.org

**Strategy.** Staged upgrade. Do not jump 16 -> 24.18.0 in one change; step through 17.9.1, 18.20.8, 19.9.0, 20.20.2, 21.7.3, 22.23.1, 23.11.1 so each major's breaking changes are isolated and revertible.

**Staged path:** `16` -> `17.9.1` -> `18.20.8` -> `19.9.0` -> `20.20.2` -> `21.7.3` -> `22.23.1` -> `23.11.1` -> `24.18.0`

**Steps**

1. Update the constraint to 24.18.0 and refresh the lock file (npm)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- Runtime bumps affect every dependency's native modules; rebuild from clean
- latest_stable is the newest LTS line

### CRITICAL - `postgresql` (Helm chart)

- **Current:** `11.6.12`
- **Latest stable:** `18.8.0`
- **Behind by:** 7 major, 395 releases, 1476 days
- **Age of your version:** 4.1 years (1487 days since publish)
- **Declared at:** `helm/Chart.yaml`
- **Source:** https://github.com/bitnami/charts
- **Docs:** https://bitnami.com

**Strategy.** Staged upgrade. Do not jump 11.6.12 -> 18.8.0 in one change; step through 12.12.10, 13.4.4, 14.3.3, 15.5.38, 16.7.27 so each major's breaking changes are isolated and revertible.

**Staged path:** `11.6.12` -> `12.12.10` -> `13.4.4` -> `14.3.3` -> `15.5.38` -> `16.7.27` -> `18.8.0`

**Steps**

1. Bump the dependency to 18.8.0 and run `helm dependency update`
2. Run `helm diff upgrade` (helm-diff plugin) before applying
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- Chart majors frequently rename values keys; your values.yaml may silently stop applying

### CRITICAL - `postgres` (Container image)

- **Current:** `11` (constraint `postgres:11`)
- **Latest stable:** `18.4`
- **Behind by:** 7 major, 42 releases
- **Declared at:** `docker-compose.yml`:5
- **Source:** https://github.com/docker-library/postgres
- **Docs:** https://hub.docker.com/_/postgres

**Strategy.** Staged upgrade. Do not jump 11 -> 18.4 in one change; step through 13.23-trixie, 14.23-trixie, 15.18-trixie, 16.14-trixie, 17.10-trixie so each major's breaking changes are isolated and revertible.

**Staged path:** `11` -> `13.23-trixie` -> `14.23-trixie` -> `15.18-trixie` -> `16.14-trixie` -> `17.10-trixie` -> `18.4`

**Steps**

1. Update the FROM tag to 18.4
2. Rebuild and run the image's own test suite; base images change libc, OpenSSL and CA bundles between majors
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- dates are Docker Hub last-push times, not original release dates; tag listing truncated at 200 tags (Hub pages by recency, not version), so an old pin may be absent and undatable; tag '11' was not in the fetched listing; age unavailable

### CRITICAL - `Microsoft.Storage/storageAccounts` (Azure API version)

- **Current:** `2021-04-01`
- **Latest stable:** `2026-04-01`
- **Behind by:** 5 years, 14 api-versions, 1826 days
- **Age of your version:** 5.3 years (1943 days since publish)
- **Declared at:** `azure/template.json`
- **Docs:** https://learn.microsoft.com/azure/templates/microsoft.storage/storageaccounts

**Strategy.** Staged upgrade. Do not jump 2021-04-01 -> 2026-04-01 in one change; step through 2022-09-01, 2023-05-01, 2024-01-01, 2025-08-01 so each api-version's breaking changes are isolated and revertible.

**Staged path:** `2021-04-01` -> `2022-09-01` -> `2023-05-01` -> `2024-01-01` -> `2025-08-01` -> `2026-04-01`

**Steps**

1. Change the api-version on `Microsoft.Storage/storageAccounts` to 2026-04-01
2. Run `az deployment group what-if` (or `--confirm-with-what-if`) against a non-production resource group and diff the result
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- Newer api-versions can add required properties or change defaults; what-if is the only reliable way to see it before it applies

### CRITICAL - `Microsoft.KeyVault/vaults` (Azure API version)

- **Current:** `2021-10-01`
- **Latest stable:** `2026-02-01`
- **Behind by:** 5 years, 7 api-versions, 1584 days
- **Age of your version:** 4.8 years (1760 days since publish)
- **Declared at:** `azure/main.bicep`:10
- **Docs:** https://learn.microsoft.com/azure/templates/microsoft.keyvault/vaults

**Strategy.** Staged upgrade. Do not jump 2021-10-01 -> 2026-02-01 in one change; step through 2022-11-01, 2023-07-01, 2024-11-01, 2025-05-01 so each api-version's breaking changes are isolated and revertible.

**Staged path:** `2021-10-01` -> `2022-11-01` -> `2023-07-01` -> `2024-11-01` -> `2025-05-01` -> `2026-02-01`

**Steps**

1. Change the api-version on `Microsoft.KeyVault/vaults` to 2026-02-01
2. Run `az deployment group what-if` (or `--confirm-with-what-if`) against a non-production resource group and diff the result
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- Newer api-versions can add required properties or change defaults; what-if is the only reliable way to see it before it applies

### CRITICAL - `actions/checkout` (GitHub Action)

- **Current:** `2` (constraint `actions/checkout@v2`)
- **Latest stable:** `7`
- **Behind by:** 5 major, 38 releases
- **Age of your version:** 6 days (6 days since publish)
- **Declared at:** `.github/workflows/ci.yml`:7
- **Source:** https://github.com/actions/checkout
- **Docs:** https://github.com/actions/checkout

**Strategy.** Staged upgrade. Do not jump 2 -> 7 in one change; step through 3, 4, 5, 6 so each major's breaking changes are isolated and revertible.

**Staged path:** `2` -> `3` -> `4` -> `5` -> `6` -> `7`

**Steps**

1. Update `uses: actions/checkout@v7`
2. Consider pinning to a commit SHA instead of a tag for supply-chain safety
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- comparing major tags; newest full release is 7.0.1

### CRITICAL - `jest` (npm package)

- **Current:** `26.0.0` (constraint `^26.0.0`)
- **Latest stable:** `30.4.2`
- **Behind by:** 4 major, 101 releases, 2195 days
- **Age of your version:** 6.2 years (2274 days since publish)
- **Declared at:** `web/package.json`
- **Source:** https://github.com/jestjs/jest
- **Docs:** https://jestjs.io/

**Strategy.** Staged upgrade. Do not jump 26.0.0 -> 30.4.2 in one change; step through 27.5.1, 28.1.3, 29.7.0 so each major's breaking changes are isolated and revertible.

**Staged path:** `26.0.0` -> `27.5.1` -> `28.1.3` -> `29.7.0` -> `30.4.2`

**Steps**

1. Update the constraint to 30.4.2 and refresh the lock file (npm)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- 25 breaking-change note(s) found in release notes between these versions - read them before upgrading

<details>
<summary>Upstream delta 26.0.0 -> 30.4.2 (60 releases)</summary>

**Breaking-change notes from release bodies**

- v27.0.0: `[jest-circus]` [**BREAKING**] Fail tests when multiple `done()` calls are made ([#10624](https://github.com/facebook/jest/pull/10624))
- v27.0.0: `[jest-circus, jest-jasmine2]` [**BREAKING**] Fail the test instead of just warning when describe returns a value ([#10947](https://github.com/facebook/jest/pull/10947))
- v27.0.0: `[jest-config]` [**BREAKING**] Default to Node testing environment instead of browser (JSDOM) ([#9874](https://github.com/facebook/jest/pull/9874))
- v27.0.0: `[jest-config]` [**BREAKING**] Use `jest-circus` as default test runner ([#10686](https://github.com/facebook/jest/pull/10686))
- v27.0.0: `[jest-config, jest-runtime]` [**BREAKING**] Use "modern" implementation as default for fake timers ([#10874](https://github.com/facebook/jest/pull/10874) & [#11197](https://github.com/facebook/jest/pull/11197))
- v27.0.0: `[jest-config, jest-haste-map, jest-resolve, jest-runner, jest-runtime, jest-test-sequencer, jest-transform, jest-types]` [**BREAKING**] Add custom HasteMap class implementation config option ([#11107](https://github.com/facebook/jest/pull/11107))
- v27.0.0: `[jest-repl, jest-runner]` [**BREAKING**] Run transforms over environment ([#8751](https://github.com/facebook/jest/pull/8751))
- v27.0.0: `[jest-runner]` [**BREAKING**] set exit code to 1 if test logs after teardown ([#10728](https://github.com/facebook/jest/pull/10728))
- v27.0.0: `[jest-runner]` [**BREAKING**] Run transforms over `runnner` ([#8823](https://github.com/facebook/jest/pull/8823))
- v27.0.0: `[jest-runner]` [**BREAKING**] Run transforms over `testRunnner` ([#8823](https://github.com/facebook/jest/pull/8823))
- v27.0.0: `[jest-snapshot]` [**BREAKING**] Make prettier optional for inline snapshots - fall back to string replacement ([#7792](https://github.com/facebook/jest/pull/7792) & [#11192](https://github.com/facebook/jest/pull/11192))
- v27.0.0: `[jest-snapshot]` [**BREAKING**] Run transforms over `snapshotResolver` ([#8751](https://github.com/facebook/jest/pull/8829))

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

### CRITICAL - `Django` (Python package)

- **Current:** `2.2.28` (constraint `==2.2.28`)
- **Latest stable:** `6.0.7`
- **Behind by:** 4 major, 168 releases, 1548 days
- **Age of your version:** 4.3 years (1567 days since publish)
- **Declared at:** `app/requirements.txt`:3
- **Source:** https://github.com/django/django
- **Docs:** https://pypi.org/project/Django/

**Strategy.** Staged upgrade. Do not jump 2.2.28 -> 6.0.7 in one change; step through 3.2.25, 4.2.30, 5.2.16 so each major's breaking changes are isolated and revertible.

**Staged path:** `2.2.28` -> `3.2.25` -> `4.2.30` -> `5.2.16` -> `6.0.7`

**Steps**

1. Update the constraint to 6.0.7 and refresh the lock file (pip)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

### CRITICAL - `docker/build-push-action` (GitHub Action)

- **Current:** `3` (constraint `docker/build-push-action@v3`)
- **Latest stable:** `7`
- **Behind by:** 4 major, 40 releases, 1247 days
- **Age of your version:** 3.5 years (1273 days since publish)
- **Declared at:** `.github/workflows/ci.yml`:11
- **Source:** https://github.com/docker/build-push-action
- **Docs:** https://github.com/docker/build-push-action

**Strategy.** Staged upgrade. Do not jump 3 -> 7 in one change; step through 4, 5, 6 so each major's breaking changes are isolated and revertible.

**Staged path:** `3` -> `4` -> `5` -> `6` -> `7`

**Steps**

1. Update `uses: docker/build-push-action@v7`
2. Consider pinning to a commit SHA instead of a tag for supply-chain safety
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- 2 breaking-change note(s) found in release notes between these versions - read them before upgrading
- comparing major tags; newest full release is 7.3.0

<details>
<summary>Upstream delta 3 -> 7 (40 releases)</summary>

**Breaking-change notes from release bodies**

- v6.3.0: `DOCKER_BUILD_NO_SUMMARY` has been deprecated. Set `DOCKER_BUILD_SUMMARY` to `false` instead by @crazy-max in https://github.com/docker/build-push-action/pull/1170 https://github.com/docker/build-push-action/pull/1173
- v7.0.0: Remove deprecated `DOCKER_BUILD_NO_SUMMARY` and `DOCKER_BUILD_EXPORT_RETENTION_DAYS` envs by @crazy-max in https://github.com/docker/build-push-action/pull/1473

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

### CRITICAL - `react` (npm package)

- **Current:** `16.8.6`
- **Latest stable:** `19.2.8`
- **Behind by:** 3 major, 45 releases, 2672 days
- **Age of your version:** 7.3 years (2677 days since publish)
- **Declared at:** `web/package.json`
- **Source:** https://github.com/react/react
- **Docs:** https://react.dev/

**Strategy.** Staged upgrade. Do not jump 16.8.6 -> 19.2.8 in one change; step through 17.0.2, 18.3.1 so each major's breaking changes are isolated and revertible.

**Staged path:** `16.8.6` -> `17.0.2` -> `18.3.1` -> `19.2.8`

**Steps**

1. Update the constraint to 19.2.8 and refresh the lock file (npm)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- 25 breaking-change note(s) found in release notes between these versions - read them before upgrading

<details>
<summary>Upstream delta 16.8.6 -> 19.2.8 (50 releases)</summary>

**Breaking-change notes from release bodies**

- v16.9.0: Deprecate old names for the `UNSAFE_*` lifecycle methods. ([@bvaughn](https://github.com/bvaughn) in [#15186](https://github.com/facebook/react/pull/15186) and [@threepointone](https://github.com/threepointone) in [#16103](https://github.com/facebook/react/pull/16103))
- v16.9.0: Deprecate `javascript:` URLs as a common attack surface. ([@sebmarkbage](https://github.com/sebmarkbage) in [#15047](https://github.com/facebook/react/pull/15047))
- v16.9.0: Deprecate uncommon "module pattern" (factory) components. ([@sebmarkbage](https://github.com/sebmarkbage) in [#15145](https://github.com/facebook/react/pull/15145))
- v16.13.0: Deprecate `React.createFactory()` ([@trueadm](https://github.com/trueadm) in [#17878](https://github.com/facebook/react/pull/17878))
- v16.13.0: Deprecate `unstable_createPortal` ([@trueadm](https://github.com/trueadm) in [#17880](https://github.com/facebook/react/pull/17880))
- v17.0.0: Deprecate the undocumented and misleading `ReactTestUtils.SimulateNative` API. ([@gaearon](https://github.com/gaearon) in [#13407](https://github.com/facebook/react/pull/13407))
- v18.0.0: Below is a list of all new features, APIs, deprecations, and breaking changes.
- v18.0.0: Read [React 18 release post](https://reactjs.org/blog/2022/03/29/react-v18.html) and [React 18 upgrade guide](https://reactjs.org/blog/2022/03/08/react-18-upgrade-guide.html) for more information.
- v18.0.0: Deprecations
- v18.0.0: `react-dom`: `ReactDOM.render` has been deprecated. Using it will warn and run your app in React 17 mode.
- v18.0.0: `react-dom`: `ReactDOM.hydrate` has been deprecated. Using it will warn and run your app in React 17 mode.
- v18.0.0: `react-dom`: `ReactDOM.unmountComponentAtNode` has been deprecated.

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

### CRITICAL - `pytest` (Python package)

- **Current:** `6.2.5` (constraint `==6.2.5`)
- **Latest stable:** `9.1.1`
- **Behind by:** 3 major, 41 releases, 1753 days
- **Age of your version:** 4.9 years (1791 days since publish)
- **Declared at:** `app/pyproject.toml`
- **Source:** https://github.com/pytest-dev/pytest
- **Docs:** https://pypi.org/project/pytest/

**Strategy.** Staged upgrade. Do not jump 6.2.5 -> 9.1.1 in one change; step through 7.4.4, 8.4.2 so each major's breaking changes are isolated and revertible.

**Staged path:** `6.2.5` -> `7.4.4` -> `8.4.2` -> `9.1.1`

**Steps**

1. Update the constraint to 9.1.1 and refresh the lock file (pip)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- 25 breaking-change note(s) found in release notes between these versions - read them before upgrading

<details>
<summary>Upstream delta 6.2.5 -> 9.1.1 (44 releases)</summary>

**Breaking-change notes from release bodies**

- 7.0.0: Deprecations
- 7.0.0: `uncooperative-constructors-deprecated`{.interpreted-text role="ref"} for details.
- 7.0.0: arguments pytest expected to pass. This deprecation was already part of pytest
- 7.0.0: [\#9404](https://github.com/pytest-dev/pytest/issues/9404): Added extra documentation on alternatives to common misuses of [pytest.warns(None)]{.title-ref} ahead of its deprecation.
- 7.0.0: Breaking Changes
- 7.0.0: Note: pytest was not able to provide a deprecation period for this change.
- 7.0.0: [\#9277](https://github.com/pytest-dev/pytest/issues/9277): The `pytest.Instance` collector type has been removed.
- 7.0.0: Importing `pytest.Instance` or `_pytest.python.Instance` returns a dummy type and emits a deprecation warning.
- 7.0.0: See `instance-collector-deprecation`{.interpreted-text role="ref"} for details.
- 7.0.0: [\#9308](https://github.com/pytest-dev/pytest/issues/9308): **PytestRemovedIn7Warning deprecation warnings are now errors by default.**
- 7.0.0: Following our plan to remove deprecated features with as little disruption as
- 7.0.0: The affected features will be effectively removed in pytest 7.1**, so please consult the

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

### CRITICAL - `Microsoft.CognitiveServices/accounts` (Azure API version)

- **Current:** `2023-05-01`
- **Latest stable:** `2026-05-01`
- **Behind by:** 3 years, 6 api-versions, 1096 days
- **Age of your version:** 3.2 years (1183 days since publish)
- **Declared at:** `azure/main.bicep`:3
- **Docs:** https://learn.microsoft.com/azure/templates/microsoft.cognitiveservices/accounts

**Strategy.** Staged upgrade. Do not jump 2023-05-01 -> 2026-05-01 in one change; step through 2024-10-01, 2025-12-01 so each api-version's breaking changes are isolated and revertible.

**Staged path:** `2023-05-01` -> `2024-10-01` -> `2025-12-01` -> `2026-05-01`

**Steps**

1. Change the api-version on `Microsoft.CognitiveServices/accounts` to 2026-05-01
2. Run `az deployment group what-if` (or `--confirm-with-what-if`) against a non-production resource group and diff the result
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- Newer api-versions can add required properties or change defaults; what-if is the only reliable way to see it before it applies

### CRITICAL - `Microsoft.App/containerApps` (Azure API version)

- **Current:** `2023-04-01-preview`
- **Latest stable:** `2026-01-01`
- **Behind by:** 3 years, 5 api-versions, 1006 days
- **Age of your version:** 3.3 years (1213 days since publish)
- **Declared at:** `azure/main.bicep`:19
- **Docs:** https://learn.microsoft.com/azure/templates/microsoft.app/containerapps

**Strategy.** Staged upgrade. Do not jump 2023-04-01-preview -> 2026-01-01 in one change; step through 2024-03-01, 2025-07-01 so each api-version's breaking changes are isolated and revertible.

**Staged path:** `2023-04-01-preview` -> `2024-03-01` -> `2025-07-01` -> `2026-01-01`

**Steps**

1. Change the api-version on `Microsoft.App/containerApps` to 2026-01-01
2. Run `az deployment group what-if` (or `--confirm-with-what-if`) against a non-production resource group and diff the result
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- Newer api-versions can add required properties or change defaults; what-if is the only reliable way to see it before it applies
- Currently on a preview api-version: preview APIs can be withdrawn without notice and are not covered by the Azure SLA
- currently pinned to a PREVIEW api-version

### CRITICAL - `actions/setup-python` (GitHub Action)

- **Current:** `4` (constraint `actions/setup-python@v4`)
- **Latest stable:** `7`
- **Behind by:** 3 major, 13 releases, 458 days
- **Age of your version:** 1.3 years (465 days since publish)
- **Declared at:** `.github/workflows/ci.yml`:8
- **Source:** https://github.com/actions/setup-python
- **Docs:** https://github.com/actions/setup-python

**Strategy.** Staged upgrade. Do not jump 4 -> 7 in one change; step through 5, 6 so each major's breaking changes are isolated and revertible.

**Staged path:** `4` -> `5` -> `6` -> `7`

**Steps**

1. Update `uses: actions/setup-python@v7`
2. Consider pinning to a commit SHA instead of a tag for supply-chain safety
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- 2 breaking-change note(s) found in release notes between these versions - read them before upgrading
- 1 open bug-labelled issue(s) filed in the window
- comparing major tags; newest full release is 7.0.0

<details>
<summary>Upstream delta 4 -> 7 (6 releases)</summary>

**Breaking-change notes from release bodies**

- v6.0.0: Breaking Changes
- v6.1.0: Upgrade urllib3 from 1.26.19 to 2.5.0 and document breaking changes in v6 by @dependabot in https://github.com/actions/setup-python/pull/1139

**Open bug-labelled issues filed in this window** (1 open of 58 total)

- [#1306](https://github.com/actions/setup-python/issues/1306) MacOS self hosted runner using sudo to install Python?

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

### CRITICAL - `redis` (Container image)

- **Current:** `5` (constraint `redis:5`)
- **Latest stable:** `8.8.1`
- **Behind by:** 3 major, 58 releases
- **Declared at:** `docker-compose.yml`:3
- **Source:** https://github.com/docker-library/redis
- **Docs:** https://hub.docker.com/_/redis

**Strategy.** Staged upgrade. Do not jump 5 -> 8.8.1 in one change; step through 6.2.23, 7.4.10 so each major's breaking changes are isolated and revertible.

**Staged path:** `5` -> `6.2.23` -> `7.4.10` -> `8.8.1`

**Steps**

1. Update the FROM tag to 8.8.1
2. Rebuild and run the image's own test suite; base images change libc, OpenSSL and CA bundles between majors
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- dates are Docker Hub last-push times, not original release dates; tag listing truncated at 200 tags (Hub pages by recency, not version), so an old pin may be absent and undatable; tag '5' was not in the fetched listing; age unavailable

### CRITICAL - `flask` (Python package)

- **Current:** `1.0.2` (constraint `==1.0.2`)
- **Latest stable:** `3.1.3`
- **Behind by:** 2 major, 33 releases, 2849 days
- **Age of your version:** 8.2 years (3007 days since publish)
- **Declared at:** `app/requirements.txt`:4
- **Source:** https://github.com/pallets/flask
- **Docs:** https://pypi.org/project/Flask/

**Strategy.** Staged upgrade. Do not jump 1.0.2 -> 3.1.3 in one change; step through 2.3.3 so each major's breaking changes are isolated and revertible.

**Staged path:** `1.0.2` -> `2.3.3` -> `3.1.3`

**Steps**

1. Update the constraint to 3.1.3 and refresh the lock file (pip)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- 5 breaking-change note(s) found in release notes between these versions - read them before upgrading

<details>
<summary>Upstream delta 1.0.2 -> 3.1.3 (33 releases)</summary>

**Breaking-change notes from release bodies**

- 1.1.2: 1.1.x is the last version to support Python 2.7 and Python 3.5. It also contains deprecation warnings for code that will be removed in 2.0. Please pay attention to deprecation warnings in your project!
- 3.0.2: This is a fix release for the 3.0.x feature release branch. It fixes bugs but does not otherwise change behavior and should not result in breaking changes.
- 3.1.0: Drop support for Python 3.8. #5623
- 3.1.1: This is the Flask 3.1.1 fix release, which fixes bugs but does not otherwise change behavior and should not result in breaking changes compared to the latest feature release.
- 3.1.2: This is the Flask 3.1.2 fix release, which fixes bugs but does not otherwise change behavior and should not result in breaking changes compared to the latest feature release.

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

### CRITICAL - `runner/ubuntu-20.04` (GitHub Action)

- **Current:** `ubuntu-20.04`
- **Latest stable:** `ubuntu-24.04`
- **Behind by:** not comparable
- **Declared at:** `.github/workflows/ci.yml`:5
- **Source:** https://github.com/actions/runner-images
- **Docs:** https://github.com/actions/runner-images

**Strategy.** Patch upgrade ubuntu-20.04 -> ubuntu-24.04. Low risk; batch with other patches.

**Steps**

1. Change `runs-on` to ubuntu-24.04
2. Expect preinstalled tool versions to differ; pin tool versions explicitly
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- Retired runner labels fail the whole workflow, not a single step
- runner image ubuntu-20.04 is deprecated or retired; move to ubuntu-24.04

### CRITICAL - `autoscaling/HorizontalPodAutoscaler` (Kubernetes API)

- **Current:** `v2beta2` (constraint `autoscaling/v2beta2`)
- **Latest stable:** `v2`
- **Behind by:** 1 release
- **Declared at:** `k8s/workloads.yaml`:1
- **Source:** https://github.com/kubernetes/kubernetes
- **Docs:** https://kubernetes.io/docs/reference/using-api/deprecation-guide/

**Strategy.** Patch upgrade v2beta2 -> v2. Low risk; batch with other patches.

**Steps**

1. Rewrite the manifest to `autoscaling/v2`
2. Run `kubectl apply --dry-run=server` to confirm the new version is served
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- If the cluster has already removed this version, `kubectl get` will not return existing objects; migrate before upgrading the cluster
- autoscaling/v2beta2 deprecated in k8s 1.23, REMOVED in 1.26; migrate to autoscaling/v2

### CRITICAL - `extensions/Ingress` (Kubernetes API)

- **Current:** `v1beta1` (constraint `extensions/v1beta1`)
- **Latest stable:** `v1`
- **Behind by:** 1 release
- **Declared at:** `k8s/workloads.yaml`:1
- **Source:** https://github.com/kubernetes/kubernetes
- **Docs:** https://kubernetes.io/docs/reference/using-api/deprecation-guide/

**Strategy.** Patch upgrade v1beta1 -> v1. Low risk; batch with other patches.

**Steps**

1. Rewrite the manifest to `networking.k8s.io/v1`
2. Run `kubectl apply --dry-run=server` to confirm the new version is served
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- If the cluster has already removed this version, `kubectl get` will not return existing objects; migrate before upgrading the cluster
- extensions/v1beta1 deprecated in k8s 1.14, REMOVED in 1.22; migrate to networking.k8s.io/v1

### CRITICAL - `policy/PodDisruptionBudget` (Kubernetes API)

- **Current:** `v1beta1` (constraint `policy/v1beta1`)
- **Latest stable:** `v1`
- **Behind by:** 1 release
- **Declared at:** `k8s/workloads.yaml`:1
- **Source:** https://github.com/kubernetes/kubernetes
- **Docs:** https://kubernetes.io/docs/reference/using-api/deprecation-guide/

**Strategy.** Patch upgrade v1beta1 -> v1. Low risk; batch with other patches.

**Steps**

1. Rewrite the manifest to `policy/v1`
2. Run `kubectl apply --dry-run=server` to confirm the new version is served
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- If the cluster has already removed this version, `kubectl get` will not return existing objects; migrate before upgrading the cluster
- policy/v1beta1 deprecated in k8s 1.21, REMOVED in 1.25; migrate to policy/v1

### HIGH - `express` (npm package)

- **Current:** `4.16.0` (constraint `^4.16.0`)
- **Latest stable:** `5.2.1`
- **Behind by:** 1 major, 27 releases, 2986 days
- **Age of your version:** 8.8 years (3223 days since publish)
- **Declared at:** `web/package.json`
- **Source:** https://github.com/expressjs/express
- **Docs:** https://expressjs.com/

**Strategy.** Single major upgrade 4.16.0 -> 5.2.1. Expect breaking changes; read the migration guide before editing anything.

**Steps**

1. Update the constraint to 5.2.1 and refresh the lock file (npm)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- 19 breaking-change note(s) found in release notes between these versions - read them before upgrading
- 3 open bug-labelled issue(s) filed in the window

<details>
<summary>Upstream delta 4.16.0 -> 5.2.1 (30 releases)</summary>

**Breaking-change notes from release bodies**

- 4.16.4: Fix deprecation warnings on Node.js 10+
- 4.17.2: Fix `res.jsonp(obj, status)` deprecation message
- 4.17.2: Use `req.socket` over deprecated `req.connection`
- v5.0.0-beta.1: Special `*` path segment behavior removed
- 4.18.0: Deprecate string and non-integer arguments to `res.status`
- 4.18.0: Limit the headers removed for 304 response
- 4.18.3: Use https: protocol instead of deprecated git: protocol by @vcsjones in https://github.com/expressjs/express/pull/5032
- 5.0.0-beta.2: Replace deprecated String.prototype.substr() by @CommanderRoot in https://github.com/expressjs/express/pull/4860
- 5.0.0-beta.2: fix: remove deprecated html attribute by @Hashen110 in https://github.com/expressjs/express/pull/4866
- 5.0.0-beta.2: Deprecate non integer status codes in v4 by @jonchurch in https://github.com/expressjs/express/pull/4223
- 5.0.0-beta.2: Use https: protocol instead of deprecated git: protocol by @vcsjones in https://github.com/expressjs/express/pull/5032
- 4.20.0: [v4] Deprecate `res.clearCookie` accepting `options.maxAge` and `options.expires` by @jonchurch in https://github.com/expressjs/express/pull/5672

**Open bug-labelled issues filed in this window** (3 open of 63 total)

- [#5878](https://github.com/expressjs/express/issues/5878) Query Param Silently Remove param query value if it is over 1000
- [#4944](https://github.com/expressjs/express/issues/4944) response.download does not return /proc/* files
- [#4557](https://github.com/expressjs/express/issues/4557) `App.use(path, ...)` Does Not Mount at `'//'`

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

### HIGH - `pydantic` (Python package)

- **Current:** `1.10.2` (constraint `==1.10.2`)
- **Latest stable:** `2.13.4`
- **Behind by:** 1 major, 85 releases, 1338 days
- **Age of your version:** 3.9 years (1420 days since publish)
- **Declared at:** `app/pyproject.toml`
- **Source:** https://github.com/sponsors/samuelcolvin
- **Docs:** https://pypi.org/project/pydantic/

**Strategy.** Single major upgrade 1.10.2 -> 2.13.4. Expect breaking changes; read the migration guide before editing anything.

**Steps**

1. Update the constraint to 2.13.4 and refresh the lock file (pip)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

### HIGH - `hashicorp/azurerm` (Terraform provider)

- **Current:** `3.75.0`
- **Latest stable:** `4.81.0`
- **Behind by:** 1 major, 135 releases, 1019 days
- **Age of your version:** 2.8 years (1031 days since publish)
- **Declared at:** `infra/.terraform.lock.hcl`:1
- **Source:** https://github.com/hashicorp/terraform-provider-azurerm
- **Docs:** https://registry.terraform.io/providers/hashicorp/azurerm

**Strategy.** Single major upgrade 3.75.0 -> 4.81.0. Expect breaking changes; read the migration guide before editing anything.

**Steps**

1. Set version = "~> 4.81.0" in required_providers
2. Run `terraform init -upgrade` to refresh .terraform.lock.hcl
3. Run `terraform plan` and read every diff: provider majors change resource schemas, so a plan may show replacements you did not intend
4. Change one component per commit so a regression bisects cleanly

**Cautions**

- Provider major bumps can force resource replacement. Check the plan for `must be replaced` before applying to production
- Commit the updated lock file so CI resolves identically
- 25 breaking-change note(s) found in release notes between these versions - read them before upgrading
- 165 open bug-labelled issues were filed in this version window; consider targeting one release below the newest

<details>
<summary>Upstream delta 3.75.0 -> 4.81.0 (60 releases)</summary>

**Breaking-change notes from release bodies**

- v3.107.0: DEPRECATIONS:
- v3.107.0: `azurerm_nginx_deployment` - the block `configuration` has been deprecated and superseded by the resource `azurerm_nginx_configuration` ([#25773](https://github.com/hashicorp/terraform-provider-azurerm/issues/25773))
- v3.108.0: DEPRECATIONS:
- v3.108.0: `azurerm_api_management` - the `policy` block has been deprecated is superseded by the `azurerm_api_management_policy` resource ([#26305](https://github.com/hashicorp/terraform-provider-azurerm/issues/26305))
- v3.108.0: `azurerm_kubernetes_cluster` - the `ebpf_data_plane` property has been deprecated and superseded by the `network_data_plane` property ([#26251](https://github.com/hashicorp/terraform-provider-azurerm/issues/26251))
- v3.108.0: `azurerm_network_interface` - the `enable_ip_forwarding` and `enable_accelerated_networking` properties have been deprecated and superseded by the `ip_forwarding_enabled` and `accelerated_networking_enabled` properties ([#26293](https://github.com/hashicorp/terraform-provider-azurerm/issues/26293))
- v3.108.0: `azurerm_synapse_workspace` - the `aad_admin` and `sql_aad_admin` blocks have been deprecated and superseded by the `azurerm_synapse_workspace_aad_admin` and `azurerm_synapse_workspace_sql_aad_admin` resources ([#26317](https://github.com/hashicorp/terraform-provider-azurerm/issues/26317))
- v3.109.0: DEPRECATIONS:
- v3.109.0: Data Source: `azurerm_mariadb_server` - deprecated since the service is retiring. Please use `azurerm_mysql_flexible_server` instead ([#26354](https://github.com/hashicorp/terraform-provider-azurerm/issues/26354))
- v3.109.0: `azurerm_mariadb_configuration` - deprecated since the service is retiring. Please use `azurerm_mysql_flexible_server_configuration` instead ([#26354](https://github.com/hashicorp/terraform-provider-azurerm/issues/26354))
- v3.109.0: `azurerm_mariadb_database` - deprecated since the service is retiring. Please use `azurerm_mysql_flexible_database` instead ([#26354](https://github.com/hashicorp/terraform-provider-azurerm/issues/26354))
- v3.109.0: `azurerm_mariadb_firewall_rule` - deprecated since the service is retiring. Please use `azurerm_mysql_flexible_server_firewall_rule` instead ([#26354](https://github.com/hashicorp/terraform-provider-azurerm/issues/26354))

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

### HIGH - `hashicorp/azurerm` (Terraform provider)

- **Current:** `3.0` (constraint `~> 3.0`)
- **Latest stable:** `4.81.0`
- **Behind by:** 1 major, 219 releases, 773 days
- **Age of your version:** at least 2.2 years — this version predates the registry's version window, so its exact publish date is unavailable
- **Declared at:** `infra/main.tf`:1
- **Source:** https://github.com/hashicorp/terraform-provider-azurerm
- **Docs:** https://registry.terraform.io/providers/hashicorp/azurerm

**Strategy.** Single major upgrade 3.0 -> 4.81.0. Expect breaking changes; read the migration guide before editing anything.

**Steps**

1. Set version = "~> 4.81.0" in required_providers
2. Run `terraform init -upgrade` to refresh .terraform.lock.hcl
3. Run `terraform plan` and read every diff: provider majors change resource schemas, so a plan may show replacements you did not intend
4. Change one component per commit so a regression bisects cleanly

**Cautions**

- Provider major bumps can force resource replacement. Check the plan for `must be replaced` before applying to production
- Commit the updated lock file so CI resolves identically

### HIGH - `github.com/inconshreveable/mousetrap` (Go module)

- **Current:** `v1.0.0`
- **Latest stable:** `v1.1.0`
- **Behind by:** 1 minor, 2 releases, 2963 days
- **Age of your version:** 11.8 years (4300 days since publish)
- **Declared at:** `svc/go.mod`
- **Source:** https://github.com/inconshreveable/mousetrap
- **Docs:** https://pkg.go.dev/github.com/inconshreveable/mousetrap

**Strategy.** Minor upgrade v1.0.0 -> v1.1.0. Additive by convention; verify with tests rather than a full migration review.

**Steps**

1. Update the constraint to v1.1.0 and refresh the lock file (go)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- Go proxy exposes dates only for the latest version

### HIGH - `requests` (Python package)

- **Current:** `2.20.0` (constraint `==2.20.0`)
- **Latest stable:** `2.34.2`
- **Behind by:** 14 minor, 27 releases, 2765 days
- **Age of your version:** 7.8 years (2838 days since publish)
- **Declared at:** `app/requirements.txt`:2
- **Source:** https://github.com/psf/requests
- **Docs:** https://pypi.org/project/requests/

**Strategy.** Minor upgrade 2.20.0 -> 2.34.2. Additive by convention; verify with tests rather than a full migration review.

**Steps**

1. Update the constraint to 2.34.2 and refresh the lock file (pip)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

### HIGH - `lodash` (npm package)

- **Current:** `4.17.11`
- **Latest stable:** `4.18.1`
- **Behind by:** 1 minor, 13 releases, 2758 days
- **Age of your version:** 7.9 years (2874 days since publish)
- **Declared at:** `web/package.json`
- **Source:** https://github.com/lodash/lodash
- **Docs:** https://lodash.com/

**Strategy.** Minor upgrade 4.17.11 -> 4.18.1. Additive by convention; verify with tests rather than a full migration review.

**Steps**

1. Update the constraint to 4.18.1 and refresh the lock file (npm)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

<details>
<summary>Upstream delta 4.17.11 -> 4.18.1 (1 releases)</summary>

_no labelled bugs or breaking-change notes found in the window_

</details>

### HIGH - `azure-identity` (Python package)

- **Current:** `1.5.0` (constraint `>=1.5.0`)
- **Latest stable:** `1.25.3`
- **Behind by:** 20 minor, 29 releases, 1947 days
- **Age of your version:** 5.7 years (2083 days since publish)
- **Declared at:** `app/requirements.txt`:5
- **Source:** https://github.com/Azure/azure-sdk-for-python
- **Docs:** https://pypi.org/project/azure-identity/

**Strategy.** Minor upgrade 1.5.0 -> 1.25.3. Additive by convention; verify with tests rather than a full migration review.

**Steps**

1. Update the constraint to 1.25.3 and refresh the lock file (pip)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

### HIGH - `github.com/stretchr/testify` (Go module)

- **Current:** `v1.7.0`
- **Latest stable:** `v1.11.1`
- **Behind by:** 4 minor, 14 releases, 1687 days
- **Age of your version:** 5.5 years (2020 days since publish)
- **Declared at:** `svc/go.mod`
- **Source:** https://github.com/stretchr/testify
- **Docs:** https://pkg.go.dev/github.com/stretchr/testify

**Strategy.** Minor upgrade v1.7.0 -> v1.11.1. Additive by convention; verify with tests rather than a full migration review.

**Steps**

1. Update the constraint to v1.11.1 and refresh the lock file (go)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- 7 breaking-change note(s) found in release notes between these versions - read them before upgrading
- Go proxy exposes dates only for the latest version

<details>
<summary>Upstream delta v1.7.0 -> v1.11.1 (14 releases)</summary>

**Breaking-change notes from release bodies**

- v1.9.0: mock: deprecate type AnythingOfTypeArgument (#1434) by @dolmen in https://github.com/stretchr/testify/pull/1441
- v1.9.0: assert: Deprecate EqualExportedValues by @HaraldNordgren in https://github.com/stretchr/testify/pull/1488
- v1.9.0: docs: Fix deprecation comments for http package by @alexandear in https://github.com/stretchr/testify/pull/1335
- v1.10.0: assert: deprecate CompareType by @dolmen in https://github.com/stretchr/testify/pull/1566
- v1.10.0: mock: document more alternatives to deprecated AnythingOfTypeArgument by @dolmen in https://github.com/stretchr/testify/pull/1569
- v1.11.0: Replace deprecated io/ioutil with io and os by @alexandear in https://github.com/stretchr/testify/pull/1684
- v1.11.0: assert: remove deprecated build constraints by @alexandear in https://github.com/stretchr/testify/pull/1671

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

### HIGH - `github.com/spf13/cobra` (Go module)

- **Current:** `v1.4.0`
- **Latest stable:** `v1.10.2`
- **Behind by:** 6 minor, 11 releases, 1364 days
- **Age of your version:** 4.4 years (1599 days since publish)
- **Declared at:** `svc/go.mod`
- **Source:** https://github.com/spf13/cobra
- **Docs:** https://pkg.go.dev/github.com/spf13/cobra

**Strategy.** Minor upgrade v1.4.0 -> v1.10.2. Additive by convention; verify with tests rather than a full migration review.

**Steps**

1. Update the constraint to v1.10.2 and refresh the lock file (go)
2. Run the test suite; for a major, read the changelog for removed APIs first
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- 14 breaking-change note(s) found in release notes between these versions - read them before upgrading
- Go proxy exposes dates only for the latest version

<details>
<summary>Upstream delta v1.4.0 -> v1.10.2 (11 releases)</summary>

**Breaking-change notes from release bodies**

- v1.5.0: Removed `CHANGELOG.md` as it isn't updated - @johnSchnake https://github.com/spf13/cobra/pull/1634
- v1.6.0: Deprecation 👎🏼
- v1.6.0: Renamed powershell completion tests: @marckhouzam #1803
- v1.7.0: Deprecate Go 1.15 in CI: @umarcor #1866
- v1.7.0: Deprecate Go 1.16 in CI: @umarcor #1926
- v1.7.0: Replace deprecated ExactValidArgs with MatchAll in doc: @doniacld #1836
- v1.8.0: golangci: enable 'unused' and disable deprecated replaced by it by @umarcor in https://github.com/spf13/cobra/pull/1983
- v1.8.0: doc: fix typo, Deperecated -> Deprecated by @callthingsoff in https://github.com/spf13/cobra/pull/2000
- v1.8.1: Address golangci-lint deprecation warnings, enable some more linters by @scop in https://github.com/spf13/cobra/pull/2152
- v1.9.0: Fix deprecation comment for Command.SetOutput by @thaJeztah in https://github.com/spf13/cobra/pull/2172
- v1.9.0: Replace deprecated ioutil usage by @nirs in https://github.com/spf13/cobra/pull/2181
- v1.10.0: This version of `pflag` carried a breaking change: it renamed `ParseErrorsWhitelist` to `ParseErrorsAllowlist` which can break builds if both `pflag` and `cobra` are dependencies in your project.

_issues are filed-in-window signals, not proven regressions; breaking-change notes are quoted from release bodies_

</details>

### HIGH - `hashicorp/random` (Terraform provider)

- **Current:** `3.4.3`
- **Latest stable:** `3.9.0`
- **Behind by:** 5 minor, 11 releases, 1342 days
- **Age of your version:** 3.9 years (1417 days since publish)
- **Declared at:** `infra/.terraform.lock.hcl`:7
- **Also at:** `infra/main.tf:6`
- **Source:** https://github.com/hashicorp/terraform-provider-random
- **Docs:** https://registry.terraform.io/providers/hashicorp/random

**Strategy.** Minor upgrade 3.4.3 -> 3.9.0. Additive by convention; verify with tests rather than a full migration review.

**Steps**

1. Set version = "~> 3.9.0" in required_providers
2. Run `terraform init -upgrade` to refresh .terraform.lock.hcl
3. Run `terraform plan` and read every diff: provider majors change resource schemas, so a plan may show replacements you did not intend
4. Change one component per commit so a regression bisects cleanly

**Cautions**

- Commit the updated lock file so CI resolves identically

### HIGH - `Azure/naming/azurerm` (Terraform module)

- **Current:** `0.3.0`
- **Latest stable:** `0.4.3`
- **Behind by:** 1 minor, 4 releases, 947 days
- **Age of your version:** 3.2 years (1156 days since publish)
- **Declared at:** `infra/modules.tf`:1
- **Source:** https://github.com/Azure/terraform-azurerm-naming
- **Docs:** https://registry.terraform.io/modules/Azure/naming/azurerm

**Strategy.** Minor upgrade 0.3.0 -> 0.4.3. Additive by convention; verify with tests rather than a full migration review.

**Steps**

1. Bump the module version to 0.4.3
2. Run `terraform init -upgrade` then `plan`; module majors often rename inputs
3. Change one component per commit so a regression bisects cleanly

### MEDIUM - `hashicorp/terraform` (Terraform provider)

- **Current:** `1.3` (constraint `>= 1.3`)
- **Latest stable:** `1.15.8`
- **Behind by:** 12 minor, 39 releases, 602 days
- **Age of your version:** at least 1.7 years — this version predates the registry's version window, so its exact publish date is unavailable
- **Declared at:** `infra/main.tf`:2
- **Source:** https://github.com/hashicorp/terraform
- **Docs:** https://github.com/hashicorp/terraform

**Strategy.** Minor upgrade 1.3 -> 1.15.8. Additive by convention; verify with tests rather than a full migration review.

**Steps**

1. Bump required_version, then run `terraform version` on the new binary
2. Run `terraform plan` on every workspace: state upgrades are one-way
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- State written by a newer Terraform cannot be read by an older one

### MEDIUM - `nginx` (Container image)

- **Current:** `latest` (constraint `nginx:latest`)
- **Latest stable:** `unknown`
- **Behind by:** not comparable
- **Declared at:** `docker-compose.yml`:7
- **Source:** https://github.com/nginxinc/docker-nginx

**Strategy.** Pin this image. A floating tag means the running artifact changes without a commit, so builds are not reproducible and a base-image regression is untraceable.

**Steps**

1. Replace `nginx:latest` with an explicit version tag
2. Better: pin by digest (`image@sha256:...`) and update it deliberately
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- floating tag; the running image is unpinned and unreproducible

### MEDIUM - `python` (Container image)

- **Current:** `3.8-slim` (constraint `python:3.8-slim`)
- **Latest stable:** `3.14.6-slim`
- **Behind by:** 6 minor, 12 releases
- **Declared at:** `docker/Dockerfile`:1
- **Also at:** `docker/Dockerfile:3`
- **Source:** https://github.com/docker-library/python
- **Docs:** https://hub.docker.com/_/python

**Strategy.** Minor upgrade 3.8-slim -> 3.14.6-slim. Additive by convention; verify with tests rather than a full migration review.

**Steps**

1. Update the FROM tag to 3.14.6-slim
2. Rebuild and run the image's own test suite; base images change libc, OpenSSL and CA bundles between majors
3. Change one component per commit so a regression bisects cleanly

**Cautions**

- compared within variant 'slim'; dates are Docker Hub last-push times, not original release dates; tag listing truncated at 200 tags (Hub pages by recency, not version), so an old pin may be absent and undatable; tag '3.8-slim' was not in the fetched listing; age unavailable

## Not assessed

| Component | Reason |
|---|---|
| `golang.org/toolchain` <sub>Go module</sub> | unparseable version ('1.19' vs '') |
| `myregistry.example.com/api` <sub>Container image</sub> | unparseable version ('1.4.0' vs '') |
| `hashicorp/kubernetes` <sub>Terraform provider</sub> | unparseable version ('' vs '3.2.1') |
| `../modules/local` <sub>Terraform module</sub> | unparseable version ('' vs '') |
| `my-platform` <sub>Helm chart</sub> | unparseable version ('0.1.0' vs '') |
| `urllib3` <sub>Python package</sub> | unparseable version ('' vs '2.7.0') |
| `actions/checkout` <sub>GitHub Action</sub> | unparseable version ('' vs '') |
| `runner/ubuntu-latest` <sub>GitHub Action</sub> | unparseable version ('ubuntu-latest' vs 'ubuntu-latest') |

---

**How to read this.** Severity is rule-based and inspectable: deprecated or 2+ majors behind is CRITICAL, one major behind or 2+ years stale is HIGH. Issue counts in the upstream delta are *issues filed during the version window*, which is a signal and not proof of a regression; the breaking-change notes quoted from release bodies are the higher-confidence input.

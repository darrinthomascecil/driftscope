# Design: image tagging & provenance (proper build-once / promote-the-digest)

Status: **proposed**. Follow-on to Phase 0.6d (`docs/design-multi-env-0.6d.md`). Closes the **R3**
red-team gap (the version→digest binding is re-derived, not recorded) and hardens the tagging,
versioning, and provenance story so lower environments provably run the same artifact as prod.

## The problem — what's weak today

0.6d says "build once, promote the digest," but the implementation doesn't fully deliver it:

1. **dev runs a bare, mutable tag.** All three overlays commit `newTag: vX.Y.Z` (a plain tag, no
   digest). Digest-pinning is applied only *at promotion time* by CD, so dev — and every fresh
   stand-up — references a **mutable** tag that can be repointed and would drift on a pod restart.
2. **The version is a meaningless auto-counter.** Every merge to main does `patch+1`; no major/minor,
   no tie to what changed or to a release.
3. **The version→digest binding is *re-derived*, not *recorded*.** On `git tag`, CD "resolves that
   version's digest from ACR" — trusting the tag still points to what dev validated. If it were ever
   rebuilt/repointed, promotion ships a *different* artifact than dev ran. **(R3.)**
4. **No source traceability.** The semver doesn't encode the git SHA; tracing a running image to its
   commit means digging through bot pointer-bump commits. No `sha-<gitsha>` tag, no OCI revision label.
5. **Tags aren't immutable.** Nothing stops `vX.Y.Z` being overwritten in the ACR (Basic SKU, no lock).
   The "same digest" guarantee rests on discipline, not enforcement.

## Principle

**Build once → content-address everywhere → record the binding at build time → promote by copying the
digest, never by re-resolving a tag.** The **digest is the identity**; the semver and git-SHA are
human-readable/traceable labels attached to it.

## Design

### 1. Two-tier reference: readable tag + immutable digest, in every env
Each build produces one image, addressed three ways:
- `vX.Y.Z` — human-readable **version tag** (release identity),
- `sha-<gitsha>` — **traceability tag** (exact source commit),
- `@sha256:<digest>` — the **immutable identity** actually deployed.

Every overlay pointer (dev **and** staging **and** prod) is `newTag: vX.Y.Z@sha256:<digest>`. kustomize
renders `<acr>/driftscope:vX.Y.Z@sha256:…` — a valid pinned reference. No environment can drift on a
repointed tag, and dev is pinned exactly like prod.

### 2. Record the binding at build, promote by copy
- **Build (on merge to main → dev):** `az acr build` → read the **digest `D`**; tag the image
  `vX.Y.Z` **and** `sha-<gitsha>`; set `overlays/dev/newTag = vX.Y.Z@sha256:D`.
- **Promote → staging:** read the **digest already pinned in `overlays/dev`** and copy it verbatim
  into `overlays/staging/newTag`. **No `az acr` lookup, no re-resolution.**
- **Promote → prod:** copy `overlays/staging`'s digest (== dev's) into `overlays/prod/newTag`.
- Because the digest flows by copy from the dev overlay, all three overlays provably reference the
  **same `@sha256`** — the binding is fixed the moment dev builds it. **This closes R3.**

### 3. Meaningful versioning
Drive the semver from **git tags / GitHub releases** (or conventional-commits automation like
release-please/semantic-release) so the number reflects intent (major/minor/patch), and a release is
the promotion trigger. Keep the `sha-<gitsha>` tag on every build for traceability regardless.
*(Interim: the current auto-patch-bump can stay for dev CI builds; only make the promoted version
release-driven.)*

### 4. Immutable tags (defense-in-depth)
Enable ACR **tag immutability / a repository lock policy** so a version tag can never be repointed.
Content-addressing (digest) is the real guarantee; immutable tags stop an accidental overwrite from
ever mattering. *(Basic SKU has limited lock support — evaluate Standard, or enforce via CI that never
re-pushes an existing version.)*

### 5. Provenance & supply chain
- Build with **OCI labels**: `org.opencontainers.image.revision=<gitsha>`, `.version=vX.Y.Z`,
  `.source=<repo>`, `.created=<ts>` — so a pulled image self-describes its origin.
- Keep **cosign** signing (already in CD); add an **SBOM** (syft) and a **SLSA provenance attestation**
  attached to the digest.
- **Admission**: verify signature + attestation before a pod runs (Ratify or Kyverno) — so only
  signed, provenanced digests deploy. *(This is the Phase-1 supply-chain item; noted here for the
  full picture.)*

## Changes to the code

| File | Change |
|---|---|
| `.github/workflows/deploy.yml` (build job) | capture the built **digest**; tag `vX.Y.Z` + `sha-<gitsha>`; pass `--label org.opencontainers.image.revision=…/.version=…`; bump `overlays/dev/newTag = vX.Y.Z@sha256:<digest>` (was a bare tag) |
| `.github/workflows/deploy.yml` (promote jobs) | read the **pinned digest from the previous env's overlay** and copy it into this env's `newTag` — remove the `az acr repository show --query digest` re-resolution |
| `deploy/overlays/{dev,staging,prod}/kustomization.yaml` | `newTag` is always the `vX.Y.Z@sha256:…` form (committed default can be a placeholder digest until the first build) |
| ACR (Terraform, later) | tag-immutability / lock policy on the shared registry |

## Rollout (incremental)

1. **PR-1 (this design + the minimal fix):** digest-pin dev at build, promote-by-copy, add the
   `sha-<gitsha>` tag + OCI revision label. Closes weaknesses **#1, #3, #4** (dev drift, the R3
   re-derivation, traceability) with small, contained changes.
2. **PR-2:** immutable/locked tags on the ACR.
3. **PR-3:** SBOM + SLSA attestation + admission verification (folds into the Phase-1 supply-chain
   work).
4. **PR-4 (optional):** release-driven semantic versioning.

## Verification

- After a promotion, `grep newTag deploy/overlays/{dev,staging,prod}/kustomization.yaml` shows the
  **identical `@sha256`** in all three.
- `kubectl get deploy driftscope -o jsonpath='{..image}'` in each env shows the same `@sha256`.
- A running image's `org.opencontainers.image.revision` label (or its `sha-<gitsha>` tag) resolves to
  the exact source commit.
- Re-running a build for an existing version is either idempotent (same digest) or blocked by tag
  immutability — never silently changes what a version means.

## Relationship to the red team

This closes **R3** ("promote the same digest is underspecified — it can promote the wrong artifact"):
the version→digest binding is **recorded at dev-build time and promoted by copying the pinned digest
from the overlay**, never re-derived from a mutable tag. Combined with digest-pinning dev, it makes
"lower envs are byte-identical to prod" a structural guarantee rather than a convention.

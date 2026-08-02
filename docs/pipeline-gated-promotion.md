# Design: gated promotion pipeline (human-in-the-loop before each environment)

Status: **proposed**. Builds on Phase 0.6d (`docs/design-multi-env-0.6d.md`): 3 isolated environments,
**build once / promote the same digest**, GitHub Actions bumps a git pointer, Argo CD pulls.

## Why

0.6d can already put the *same image* in dev, staging, and prod. What it lacks is a **gate**: today a
version can move up the ladder with no verification and no human sign-off. This design adds, before
**every** environment promotion, a two-part gate:

1. an **automated check** that the previous environment is actually healthy on the new build, and
2. a **human-in-the-loop (HITL) approval** — a person must click *Approve* before the deploy proceeds.

Nothing reaches staging unless dev proved out *and* a human approved; nothing reaches prod unless
staging proved out *and* a human approved. Lower environments run the **byte-identical digest** prod
will run.

## The pipeline

```
  PR ──► main                 (CI: tests + lint; build+sign ONCE ► shared ACR; record vX.Y.Z@sha256)
   │
   ▼  bump overlays/dev/newTag
  DEV  ──(Argo pulls)──►  deployed
   │
   ├─ 🤖 verify-dev        automated: dev serving the new digest + healthy + smoke + soak
   │        │ pass
   │        ▼
   ├─ 🧑 APPROVE staging   HUMAN-IN-THE-LOOP  (GitHub Environment "staging", required reviewers)
   │        │ approved
   ▼        ▼  bump overlays/staging/newTag = vX.Y.Z@sha256  (SAME digest, no rebuild)
  STAGING ──(Argo pulls)──►  deployed
   │
   ├─ 🤖 verify-staging    automated: staging healthy on the new digest + smoke + soak
   │        │ pass
   │        ▼
   ├─ 🧑 APPROVE prod      HUMAN-IN-THE-LOOP  (GitHub Environment "production", required reviewers)
   │        │ approved
   ▼        ▼  bump overlays/prod/newTag = vX.Y.Z@sha256  (SAME digest)
  PROD ──(Argo pulls)──►  deployed
```

**dev is the only automatic deploy.** Every hop after it is gated by *both* an automated check and a
human approval.

## The gate (repeated before staging, and before prod)

### Part 1 — Automated check: `verify-<prev-env>`

Because CD is **decoupled from the cluster** (ADR-0002: the pipeline only bumps the committed tag;
Argo applies it — CI holds no cluster credential), the check verifies the environment **from the
outside**:

- **Right build is live** — poll the environment's URL until `/healthz` = 200 **and** a version/digest
  marker matches the just-promoted `@sha256`, so it's confirming the *new* build, not the old one.
- **Functional smoke** — one real end-to-end request (e.g. a canary scan of a small known repo)
  returns the expected result.
- **Soak** — stays healthy for a short window (~2–5 min): no error spike, no crashloop.
- Any failure → the job fails → **promotion is blocked** and the human sees red at the gate.

> **Upgrade path:** wire **Argo CD Notifications** to emit a `Healthy on revision <sha>` webhook, and
> have the pipeline wait on that instead of polling — real in-cluster health truth without giving CI a
> cluster credential.

### Part 2 — Human-in-the-loop approval (the HITL gate)

The promote job targets a **GitHub Environment** (`staging`, then `production`) protected by a
**required-reviewers** rule. When the pipeline reaches it, the job **pauses** and GitHub requests a
review. The approver:

- sees the `verify-<prev-env>` result (green/red) and its logs,
- can open the Argo UI / the previous env directly and the promotion diff,
- clicks **Approve** (promotion proceeds) or **Reject** (it stops).

Optional per-environment protections: a **wait timer** (forced soak, e.g. 10 min), **allowed
branches/tags**, and a **distinct reviewer set** for prod (e.g. two approvers, or a different team).

**This HITL step exists before *each* environment** — `dev → staging` and `staging → prod` — so a
human is always the last word before a new environment receives the build.

## How it maps to the existing `deploy.yml`

0.6d already does the hard parts: build+sign once, per-env GitHub Environments, digest promotion,
Argo pull. The additions are small:

| Add | What it is |
|---|---|
| `verify-dev` job | the automated smoke/soak check against dev; `promote-staging` `needs: [verify-dev]` |
| `promote-staging` job | `environment: staging`; on approval, bump `overlays/staging/newTag = vX.Y.Z@sha256` |
| `verify-staging` job | same automated check against staging; `promote-prod` `needs: [verify-staging]` |
| `promote-prod` job | `environment: production`; on approval, bump `overlays/prod/newTag = vX.Y.Z@sha256` |
| **repo setting** | **required reviewers** on the `staging` and `production` Environments (the HITL gate — a setting, not code) |

The digest is resolved from the **shared ACR** (reachable by every env's kubelet), so promotion is a
pointer change, never a rebuild or a cross-registry copy.

## Trigger model — pick one

- **A) Continuous:** `merge → dev → verify-dev → [HITL approve] → staging → verify-staging → [HITL
  approve] → prod`, one workflow run that pauses at each gate. Most automated; the human's only action
  is Approve/Reject.
- **B) Tag-driven** (the current 0.6d shape): `merge → dev` auto; cutting `git tag vX.Y.Z` starts the
  staging promotion (runs `verify-dev` + HITL approval); publishing a **release** starts prod (runs
  `verify-staging` + HITL approval). The tag/release is an explicit "promote *this* version" act; the
  approval is the second gate.

**Recommended:** **A** for a smooth single-flow gated pipeline; choose **B** if you want an explicit
human "pick the version to promote" step in addition to the approval. Both reuse the same verify +
HITL mechanics.

## Setup checklist

1. **GitHub Environments** `dev`, `staging`, `production` (0.6d already federates each to its own
   `driftscope-<env>-gh-deploy` identity).
2. On **`staging`** and **`production`**: enable **Required reviewers** (add the approver(s));
   optionally a wait timer + allowed branches/tags. *(Leave `dev` unprotected — it's the auto env.)*
3. Add the `verify-*` jobs (health/version poll + smoke + soak) and the `promote-*` jobs (`needs:` the
   matching verify, `environment:` the target) to `deploy.yml`.
4. (Optional) install **Argo CD Notifications** and switch `verify-*` from polling to the
   `Healthy on revision` webhook.

## Guarantee

A build reaches an environment **only if**: the previous environment is serving that exact digest and
passed its automated checks, **and** a human explicitly approved the promotion. Every environment runs
the **same digest** — so staging and dev are provably the same artifact as prod, and a person is in
the loop before each promotion.

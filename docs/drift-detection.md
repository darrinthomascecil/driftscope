# Terraform drift detection (CI)

`.github/workflows/terraform.yml` gives the infrastructure two guardrails:

- **`validate`** — `terraform fmt -check` + `terraform validate` on every PR/push that touches
  `terraform/`. No cloud credentials; always on.
- **`drift`** — a scheduled `terraform plan` against the **live remote state**, so an interrupted
  apply/destroy or an out-of-band `az`/portal change can't silently diverge unnoticed. This is the
  automated backstop for the failure we hit once: a phantom Front Door lingering in state while
  real resources moved on. Read-only (`-lock=false`), so it never blocks a real apply.

The drift job is **opt-in and self-skipping** — inert until you set the repo variable
`TF_DRIFT_ENABLED=true` and the config below. On drift it fails the run (a failed scheduled
workflow notifies repo admins) and opens/updates a `terraform-drift` issue with the plan.

## Enable it

Almost everything is wired by the bootstrap — you only flip the switch.

1. **Wire CI config** (idempotent; `make up` runs it automatically): `make repo-config`
   (`scripts/bootstrap/repo-config.sh`) sets the repo variables `AZURE_CLIENT_ID`,
   `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `TFSTATE_RG/SA/CONTAINER` and the `TFVARS` secret —
   all from Terraform outputs + `terraform/backend.hcl`. Nothing is typed into the GitHub UI.
2. **OIDC trust is already in place**: `terraform/gh-deploy-identity.tf` registers the federated
   credential `repo:<owner>/<repo>:ref:refs/heads/main` (name-based + immutable-id form) on the
   deploy identity, created by `make infra`. `schedule`/`workflow_dispatch` runs use that subject.
3. **Turn it on**: set the repo variable `TF_DRIFT_ENABLED=true` — the one deliberate switch (drift
   stays off until you opt in). `gh variable set TF_DRIFT_ENABLED --body true`.

### Identity + permissions

The drift job reuses the **`driftscope-gh-deploy`** identity — **Contributor** on the deployment RG
(`terraform/gh-deploy-identity.tf`). A `plan` only reads, but the identity is Contributor because it
is *shared* with the deploy pipeline. Contributor also covers the Graph/Entra reads the plan needs
(`azuread_application`, `azuread_group`, `data.azuread_user.admins`) and state access. A stricter
posture would give drift a **separate Reader-only** identity (Reader on the RG + Storage Blob Data
Contributor on the state SA + read-only Graph); the reused-identity default trades that for one
fewer identity to manage.

## What you'll see

- **No drift** → job passes, summary says "state matches Azure".
- **Drift** → job fails (notifies), the plan is in the run summary, and a `terraform-drift` issue is
  created or updated with the diff. Investigate: an interrupted operation, an out-of-band change, or
  a config change on `main` not yet applied. Reconcile with `terraform apply` or by importing/removing
  the affected resources (see the drift section of `docs/remote-state.md`).

## Local equivalent

```sh
make state           # ensure the backend is configured
terraform -chdir=terraform plan -lock=false -detailed-exitcode    # exit 2 == drift
```

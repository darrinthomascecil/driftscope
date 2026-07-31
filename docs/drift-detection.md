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

### 1. Repo **variables** (Settings → Secrets and variables → Actions → Variables)

| Variable | Value |
|---|---|
| `TF_DRIFT_ENABLED` | `true` |
| `AZURE_CLIENT_ID` | client id of the identity CI authenticates as (below) |
| `AZURE_TENANT_ID` | your tenant id |
| `AZURE_SUBSCRIPTION_ID` | the subscription the deployment lives in |
| `TFSTATE_RG` / `TFSTATE_SA` / `TFSTATE_CONTAINER` | the remote-state backend (from `terraform/backend.hcl`) |

### 2. Repo **secret**

| Secret | Value |
|---|---|
| `TFVARS` | the full contents of your `terraform/terraform.tfvars` (it carries deployment config; `terraform.tfvars` is git-ignored, so CI reconstructs it here) |

### 3. Azure identity + **keyless** OIDC trust

CI authenticates with **native Terraform OIDC** — no stored client secret. Create (or reuse) an
Entra app / user-assigned identity and add a **federated credential** trusting this repo's Actions
subject:

```sh
az ad app federated-credential create --id <AZURE_CLIENT_ID> --parameters '{
  "name": "github-actions-drift",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<owner>/<repo>:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

> `workflow_dispatch` and `schedule` runs use the default branch's subject
> (`ref:refs/heads/main`). If you run drift from other refs, add matching federated subjects.

### 4. Permissions the identity needs (a `plan` refreshes the whole config)

- **Reader** on the subscription/resource groups — to refresh the Azure resources.
- **Storage Blob Data Contributor** on the state storage account — to read state (keyless).
- **Microsoft Graph directory read** (e.g. `User.Read.All`, `Group.Read.All`,
  `Application.Read.All`) — because the config manages/reads Entra objects
  (`azuread_application`, `azuread_group`, `data.azuread_user.admins`).

Least privilege: Reader (not Contributor) on Azure, read-only Graph — a plan never writes.

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

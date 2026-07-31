# Remote Terraform state (default)

`make up` provisions **remote** Terraform state by default: an Azure Storage backend that is
shared, locked, versioned, and off anyone's laptop. A lone local state file is exactly how a
deployment silently drifts — an interrupted apply/destroy leaves state and Azure disagreeing with
no lock, no history, and no shared source of truth to reconcile against. Remote state closes that.

Opt out for a throwaway demo with `LOCAL_STATE=1 make up` (local `terraform/terraform.tfstate`,
git-ignored). A bare `terraform` invocation with no generated `backend.tf` also stays local.

## Why bother

- **It holds secrets.** State stores the Entra **client secret value** (and more). One laptop is
  the wrong place for it; a locked-down storage account with AAD-only access is the right one.
- **Locking.** The `azurerm` backend takes a blob lease on every operation, so two people (or a
  human + CD) can't `apply` at once and corrupt state.
- **Recovery.** Blob **versioning + 30-day soft-delete** (set by the script) means a botched
  apply or deleted state is recoverable.
- **Sharing.** CD and teammates converge on one source of truth instead of drifting local copies.

Keyless throughout: `use_azuread_auth = true` — no storage account keys in config, state, or CI.

## How it works (automatic)

`make up` runs `make state` before `infra`. That step (`state-backend.sh`) is idempotent and:

1. Creates the backend storage (resource group + StorageV2 account + container) if absent, and
   **reuses** the storage account already recorded in `backend.hcl` on re-runs — it never spawns a
   second account. The account name is auto-generated (`dstf…`) unless you set `STATE_SA`.
2. Writes `terraform/backend.hcl` (the per-deployment config) and generates `terraform/backend.tf`
   (activates the `azurerm` backend). Both are git-ignored; the `.example` files are the committed
   templates. `make infra` then inits with `-backend-config=backend.hcl` automatically.

```sh
make up                       # default: provisions remote state, then infra -> ... -> app
LOCAL_STATE=1 make up         # opt out: local state (throwaway demo only)
STATE_SA=mystatestore123 make up   # pin the storage account name instead of auto-generating
```

### Migrating an existing local state up

If you already applied with local state and are switching a deployment to remote, move the
existing state into the backend once (rather than starting a fresh empty remote state):

```sh
make state          # provision the backend + generate backend.tf/backend.hcl
make state-migrate  # one-time: terraform init -migrate-state -backend-config=backend.hcl
```

## What the backend storage account looks like

`make state-backend` creates a resource group (`driftscope-tfstate` by default) + a StorageV2
account that is: **AAD-only** (`--allow-blob-public-access false`), **TLS 1.2 min**, HTTPS-only,
blob **versioning** + **30-day soft-delete** on. It grants the signed-in user
**Storage Blob Data Contributor** (data-plane, keyless). Override defaults with env vars:
`STATE_RG`, `STATE_LOCATION`, `STATE_CONTAINER`.

## CI note

Give the CD identity (a managed identity or the pipeline's service principal) its **own**
Storage Blob Data Contributor grant on the state account — don't share a human's. With
`use_azuread_auth = true`, that identity authenticates to the backend with no keys.

## Reverting to local state

```sh
rm terraform/backend.tf
terraform -chdir=terraform init -migrate-state    # pulls state back down to local
```

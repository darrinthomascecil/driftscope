# Remote Terraform state (opt-in)

By default `make up` keeps Terraform state **local** (`terraform/terraform.tfstate`, git-ignored).
That's fine for a solo demo. For a **team or CD**, move state into Azure Storage — shared, locked,
versioned, and off anyone's laptop. This page is that switch.

## Why bother

- **It holds secrets.** State stores the Entra **client secret value** (and more). One laptop is
  the wrong place for it; a locked-down storage account with AAD-only access is the right one.
- **Locking.** The `azurerm` backend takes a blob lease on every operation, so two people (or a
  human + CD) can't `apply` at once and corrupt state.
- **Recovery.** Blob **versioning + 30-day soft-delete** (set by the script) means a botched
  apply or deleted state is recoverable.
- **Sharing.** CD and teammates converge on one source of truth instead of drifting local copies.

Keyless throughout: `use_azuread_auth = true` — no storage account keys in config, state, or CI.

## Enable it

```sh
# 1. Create the backend (idempotent). Pick a globally-unique storage account name.
STATE_SA=driftscopetfstate12345 make state-backend      # writes terraform/backend.hcl

# 2. Turn on the azurerm backend and migrate your existing local state up.
cp terraform/backend.tf.example terraform/backend.tf
make state-migrate                                       # one-time: terraform init -migrate-state

# 3. From now on, everything is normal — init picks up backend.hcl automatically.
make infra      # or the full  make up
```

`backend.tf` and `backend.hcl` are git-ignored (per-deployment); the `.example` files are the
committed templates. The Makefile keys off `backend.hcl`: present → remote, absent → local.

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

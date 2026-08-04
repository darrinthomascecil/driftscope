# DriftScope cloud bootstrap -- clone -> running on Azure (Phase 0.6d: dev/staging/prod).
#
#   cp terraform/shared/terraform.tfvars.example terraform/shared/terraform.tfvars  # ACR + DNS zone
#   cp terraform/envs/dev/dev.tfvars.example     terraform/envs/dev/dev.tfvars      # fill it in
#   az login
#   make shared-up            # ONCE: the shared ACR + DNS zone + state backend
#   make up ENV=dev           # then each environment (default ENV=dev)
#
# `make up ENV=<env>` runs the whole per-env pipeline; each step is also a re-runnable target.
# ENV selects the Terraform env-root (terraform/envs/$ENV, its own state key) + DRIFTSCOPE_ENV for
# the ConfigMap + render. For unattended infra: make up ENV=dev AUTO=-auto-approve.
# Details + prerequisites: docs/quickstart-deploy.md, docs/design-multi-env-0.6d.md.

ENV      ?= dev
export ENV                                     # the env-aware bootstrap scripts read $ENV
TF       := terraform -chdir=terraform/envs/$(ENV)
TFSHARED := terraform -chdir=terraform/shared
B        := scripts/bootstrap
AUTO ?=
# Remote state is REQUIRED for multi-env (per-env keys on one shared account, ADR 0.6d). `make state`
# provisions the Azure Storage backend and writes a backend.hcl into shared + every env root, so each
# `terraform init -backend-config=backend.hcl` targets isolated, locked, versioned state.

.DEFAULT_GOAL := help

.PHONY: help up shared-up down shared-down preflight state shared kv-recover infra repo-config image cluster secret deploy url state-backend verify-image kind-up

# Keyless cosign verification of a published image. No secrets, no key files: it checks the
# signature chains to a Fulcio cert whose identity is THIS repo's release workflow, issued by
# GitHub's OIDC. Override IMAGE (defaults to :latest); relax IDENTITY only if verifying a PR build.
IMAGE    ?= ghcr.io/darrinthomascecil/driftscope:latest
IDENTITY ?= ^https://github.com/darrinthomascecil/driftscope/\.github/workflows/release\.yml@refs/tags/v
ISSUER   := https://token.actions.githubusercontent.com

help: ## Show this help
	@grep -hE '^[a-z][a-zA-Z0-9_-]*:.*## ' $(MAKEFILE_LIST) \
	  | sort | awk 'BEGIN{FS=":.*## "}{printf "  \033[1;36m%-10s\033[0m %s\n", $$1, $$2}'

up: preflight state kv-recover infra repo-config image cluster deploy ## Bootstrap ONE env: make up ENV=dev (state -> KV recover -> infra -> CI wiring -> image -> cluster -> app)

shared-up: preflight state shared ## ONCE: the shared ACR + DNS zone + state backend (run before the first env)

preflight: ## Check tools, Azure login, and this env's tfvars
	@$(B)/preflight.sh

state: ## Provision the remote state backend + write per-env/shared backend.hcl (idempotent)
	@$(B)/state-backend.sh

shared: ## terraform init + apply the SHARED layer (ACR + DNS zone)
	@$(TFSHARED) init -input=false -backend-config=backend.hcl
	@$(TFSHARED) apply -input=false $(AUTO)

kv-recover: ## Recover this env's soft-deleted Key Vault if a prior teardown reserved the name
	@$(B)/kv-recover.sh

infra: ## terraform init + apply this env (AKS, KV, Entra, Front Door, DNS records; emits outputs)
	@$(TF) init -input=false -backend-config=backend.hcl
	@$(TF) apply -input=false -var-file=$(ENV).tfvars $(AUTO)

repo-config: ## Wire GitHub Actions for this env (env-scoped AZURE_CLIENT_ID/TFVARS + shared repo vars)
	@$(B)/repo-config.sh

image: ## Build the app image (this env's newTag) into the SHARED ACR
	@$(B)/image.sh

cluster: ## Kubeconfig, ext_authz provider, namespace, BYO gateway IP, cert-manager, Argo (this env)
	@$(B)/cluster-init.sh

secret: ## FALLBACK only: create the oauth2-proxy Secret by hand (default flow uses ESO)
	@$(B)/secret.sh

deploy: ## Render + apply this env's app + mesh hardening + edge TLS/origin-lock, then print the URL
	@$(B)/deploy.sh

url: ## Print this env's app URL (ENV=dev|staging|prod)
	@$(TF) output -raw app_url; echo

kustomize: ## Install the standalone kustomize binary for this platform (Linux/macOS; not brew)
	@$(B)/install-kustomize.sh

state-backend: ## Create the Azure Storage remote-state backend directly (also run by `make state`)
	@$(B)/state-backend.sh

verify-image: ## Verify a published image's cosign signature + SBOM (IMAGE=... ; keyless, no secrets)
	@command -v cosign >/dev/null || { echo "cosign not found -- https://docs.sigstore.dev/cosign/installation"; exit 1; }
	@echo "verifying signature: $(IMAGE)"
	@cosign verify --certificate-identity-regexp '$(IDENTITY)' --certificate-oidc-issuer '$(ISSUER)' '$(IMAGE)' >/dev/null
	@echo "verifying SBOM attestation: $(IMAGE)"
	@cosign verify-attestation --type spdxjson --certificate-identity-regexp '$(IDENTITY)' --certificate-oidc-issuer '$(ISSUER)' '$(IMAGE)' >/dev/null
	@echo "OK: signature + SBOM verified, signed by $(IMAGE) release workflow"

kind-up: ## LOCAL: bootstrap an existing kind cluster -> running mesh (Istio via Helm + cert-manager TLS + MetalLB + Entra SSO). Config: deploy/overlays/kind/kind.env (see .example). Not Azure.
	$(B)/kind-up.sh

down: ## Tear down ONE env: make down ENV=dev (destroys that env's AKS/KV/FD/Entra + its DNS records; NOT the shared ACR/zone)
	@echo "This destroys the '$(ENV)' environment (AKS, KV, Front Door, Entra app+group, this env's DNS records)."
	@echo "The SHARED ACR + DNS zone (and its registrar NS delegation) are untouched -- use 'make shared-down' for those."
	@$(TF) destroy -input=false -var-file=$(ENV).tfvars $(AUTO)

shared-down: ## DANGER: destroy the SHARED ACR + DNS zone (breaks NS delegation + deletes all images). Rarely wanted.
	@echo "WARNING: this deletes the shared ACR (ALL images) and the DNS zone (the registrar NS delegation breaks)."
	@echo "Only do this to fully decommission. Every env must be torn down first. Ctrl-C now to abort."
	@$(TFSHARED) destroy -input=false $(AUTO)

# DriftScope cloud bootstrap -- clone -> running on Azure (Phase 0.5).
#
#   cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # fill it in
#   az login
#   make up
#
# `make up` runs the whole pipeline; each step is also a target you can run alone (re-runnable).
# For unattended infra: make up AUTO=-auto-approve   (otherwise terraform apply prompts once).
# Details + prerequisites: docs/quickstart-deploy.md.

TF   := terraform -chdir=terraform
B    := scripts/bootstrap
AUTO ?=
# Remote state is the DEFAULT: `make up` provisions an Azure Storage backend (make state) and writes
# terraform/backend.hcl, so `infra` inits against locked, versioned, shared state. Opt out with
# LOCAL_STATE=1 (local state -- fine for a throwaway demo, but a lone unlocked state file is how
# deployments silently drift; see docs/remote-state.md). `infra` detects backend.hcl at RUN time
# (not parse time), because within one `make up` the `state` step creates it just before `infra`.
LOCAL_STATE ?=

.DEFAULT_GOAL := help

.PHONY: help up down preflight state infra image cluster secret deploy url state-backend state-migrate verify-image

# Keyless cosign verification of a published image. No secrets, no key files: it checks the
# signature chains to a Fulcio cert whose identity is THIS repo's release workflow, issued by
# GitHub's OIDC. Override IMAGE (defaults to :latest); relax IDENTITY only if verifying a PR build.
IMAGE    ?= ghcr.io/darrinthomascecil/driftscope:latest
IDENTITY ?= ^https://github.com/darrinthomascecil/driftscope/\.github/workflows/release\.yml@refs/tags/v
ISSUER   := https://token.actions.githubusercontent.com

help: ## Show this help
	@grep -hE '^[a-z][a-zA-Z0-9_-]*:.*## ' $(MAKEFILE_LIST) \
	  | sort | awk 'BEGIN{FS=":.*## "}{printf "  \033[1;36m%-10s\033[0m %s\n", $$1, $$2}'

up: preflight state infra image cluster deploy ## Full bootstrap: remote state -> infra -> image -> cluster -> app (prints URL)

preflight: ## Check tools, Azure login, and terraform.tfvars
	@$(B)/preflight.sh

state: ## Provision the remote Terraform state backend (default). Skip with LOCAL_STATE=1.
	@if [ -n "$(LOCAL_STATE)" ]; then \
	  echo "LOCAL_STATE set -> using LOCAL Terraform state (not recommended: unlocked, unshared, drift-prone)"; \
	else \
	  $(B)/state-backend.sh; \
	fi

infra: ## terraform init + apply (AKS, ACR, KV, Entra, DNS, Front Door; generates params + edge/)
	@if [ -f terraform/backend.hcl ]; then BA="-backend-config=backend.hcl"; else BA=""; fi; \
	 $(TF) init -input=false $$BA
	@$(TF) apply -input=false $(AUTO)

image: ## Build the app image in ACR and point the overlay at it
	@$(B)/image.sh

cluster: ## Kubeconfig, ext_authz provider, namespace, BYO gateway IP, cert-manager (custom domain)
	@$(B)/cluster-init.sh

secret: ## FALLBACK only: create the oauth2-proxy Secret by hand (default flow uses ESO)
	@$(B)/secret.sh

deploy: ## Apply the app + mesh hardening + edge TLS/origin-lock, then print the URL
	@$(B)/deploy.sh

url: ## Print the app URL
	@$(TF) output -raw app_url; echo

kustomize: ## Install the standalone kustomize binary for this platform (Linux/macOS; not brew)
	@$(B)/install-kustomize.sh

state-backend: ## Create the Azure Storage remote-state backend directly (also run by `make state`)
	@$(B)/state-backend.sh

state-migrate: ## One-time: migrate an existing LOCAL state into the remote backend
	@$(TF) init -input=false -migrate-state -backend-config=backend.hcl

verify-image: ## Verify a published image's cosign signature + SBOM (IMAGE=... ; keyless, no secrets)
	@command -v cosign >/dev/null || { echo "cosign not found -- https://docs.sigstore.dev/cosign/installation"; exit 1; }
	@echo "verifying signature: $(IMAGE)"
	@cosign verify --certificate-identity-regexp '$(IDENTITY)' --certificate-oidc-issuer '$(ISSUER)' '$(IMAGE)' >/dev/null
	@echo "verifying SBOM attestation: $(IMAGE)"
	@cosign verify-attestation --type spdxjson --certificate-identity-regexp '$(IDENTITY)' --certificate-oidc-issuer '$(ISSUER)' '$(IMAGE)' >/dev/null
	@echo "OK: signature + SBOM verified, signed by $(IMAGE) release workflow"

down: ## Tear down ALL Azure resources (terraform destroy)
	@echo "This destroys the entire deployment (AKS, ACR, KV, DNS, Front Door, Entra app+group)."
	@$(TF) destroy -input=false $(AUTO)

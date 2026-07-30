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
# Remote state is opt-in: if terraform/backend.hcl exists, init points at it (else local state).
BACKEND := $(wildcard terraform/backend.hcl)
BACKEND_ARG := $(if $(BACKEND),-backend-config=backend.hcl,)

.DEFAULT_GOAL := help

.PHONY: help up down preflight infra image cluster secret deploy url state-backend state-migrate

help: ## Show this help
	@grep -hE '^[a-z][a-zA-Z0-9_-]*:.*## ' $(MAKEFILE_LIST) \
	  | sort | awk 'BEGIN{FS=":.*## "}{printf "  \033[1;36m%-10s\033[0m %s\n", $$1, $$2}'

up: preflight infra image cluster secret deploy ## Full bootstrap: infra -> image -> cluster -> secret -> app (prints URL)

preflight: ## Check tools, Azure login, and terraform.tfvars
	@$(B)/preflight.sh

infra: ## terraform init + apply (AKS, ACR, KV, Entra, DNS, Front Door; generates params + edge/)
	@$(TF) init -input=false $(BACKEND_ARG)
	@$(TF) apply -input=false $(AUTO)

image: ## Build the app image in ACR and point the overlay at it
	@$(B)/image.sh

cluster: ## Kubeconfig, ext_authz provider, namespace, BYO gateway IP, cert-manager (custom domain)
	@$(B)/cluster-init.sh

secret: ## Create the oauth2-proxy Secret from Key Vault (values never printed)
	@$(B)/secret.sh

deploy: ## Apply the app + mesh hardening + edge TLS/origin-lock, then print the URL
	@$(B)/deploy.sh

url: ## Print the app URL
	@$(TF) output -raw app_url; echo

state-backend: ## (opt-in) Create the Azure Storage remote-state backend; writes terraform/backend.hcl
	@$(B)/state-backend.sh

state-migrate: ## (opt-in) One-time: migrate existing local state into the remote backend
	@$(TF) init -input=false -migrate-state -backend-config=backend.hcl

down: ## Tear down ALL Azure resources (terraform destroy)
	@echo "This destroys the entire deployment (AKS, ACR, KV, DNS, Front Door, Entra app+group)."
	@$(TF) destroy -input=false $(AUTO)

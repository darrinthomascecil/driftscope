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

.DEFAULT_GOAL := help

.PHONY: help up down preflight infra image cluster secret deploy url

help: ## Show this help
	@grep -hE '^[a-z][a-zA-Z0-9_-]*:.*## ' $(MAKEFILE_LIST) \
	  | sort | awk 'BEGIN{FS=":.*## "}{printf "  \033[1;36m%-10s\033[0m %s\n", $$1, $$2}'

up: preflight infra image cluster secret deploy ## Full bootstrap: infra -> image -> cluster -> secret -> app (prints URL)

preflight: ## Check tools, Azure login, and terraform.tfvars
	@$(B)/preflight.sh

infra: ## terraform init + apply (AKS, ACR, KV, Entra, DNS, Front Door; generates params + edge/)
	@$(TF) init -input=false
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

down: ## Tear down ALL Azure resources (terraform destroy)
	@echo "This destroys the entire deployment (AKS, ACR, KV, DNS, Front Door, Entra app+group)."
	@$(TF) destroy -input=false $(AUTO)

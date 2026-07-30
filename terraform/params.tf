# Close the loop: generate the kustomize params (deploy/overlays/cloud/params.yaml) from
# Terraform's values, so a fresh deploy needs no hand-editing of manifests. `terraform apply`
# regenerates it; with matching tfvars it reproduces the committed file identically.
resource "local_file" "params" {
  filename        = "${path.module}/../deploy/overlays/cloud/params.yaml"
  file_permission = "0644"
  content = templatefile("${path.module}/templates/params.yaml.tftpl", {
    host               = local.host
    tenant_id          = local.tenant_id
    allowed_group_id   = var.allowed_group_id
    allowed_group_name = var.allowed_group_name
  })
}

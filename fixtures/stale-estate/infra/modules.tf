module "naming" {
  source  = "Azure/naming/azurerm"
  version = "0.3.0"
  suffix  = ["demo"]
}

module "local_thing" {
  source = "../modules/local"
}

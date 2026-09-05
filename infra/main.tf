# MISO Copilot - reference cloud deployment (NOT deployed; nothing here has run).
# Resource group + shared data sources. The rest of the platform is split by
# concern: network, edge, cluster, registry, storage, secrets, observability.

data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "main" {
  name     = "${var.prefix}-rg"
  location = var.location
}

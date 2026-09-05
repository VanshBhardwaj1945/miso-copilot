# Logs land in Log Analytics; the workspace is onboarded so everything
# forwards to SIEM.

resource "azurerm_log_analytics_workspace" "main" {
  name                = "${var.prefix}-logs"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  retention_in_days   = 30
}

resource "azurerm_sentinel_log_analytics_workspace_onboarding" "siem" {
  workspace_id = azurerm_log_analytics_workspace.main.id
}

# Shared state: Redis (answer cache + per-IP rate-limit counters across
# replicas) and Azure Files (snapshots, request log, Chroma volume).

resource "azurerm_redis_cache" "main" {
  name                = "${var.prefix}-redis"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  capacity            = 0
  family              = "C"
  sku_name            = "Standard"
}

resource "azurerm_storage_account" "data" {
  name                     = "${var.prefix}data"
  location                 = azurerm_resource_group.main.location
  resource_group_name      = azurerm_resource_group.main.name
  account_tier             = "Standard"
  account_replication_type = "LRS"
}

resource "azurerm_storage_share" "data" {
  name               = "copilot-data"
  storage_account_id = azurerm_storage_account.data.id
  quota              = 16 # GB - snapshots, request log, Chroma volume
}

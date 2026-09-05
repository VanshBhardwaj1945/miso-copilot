# The React build, served as static files.

resource "azurerm_static_web_app" "frontend" {
  name                = "${var.prefix}-web"
  location            = "centralus"
  resource_group_name = azurerm_resource_group.main.name
  sku_tier            = "Free"
  sku_size            = "Free"
}

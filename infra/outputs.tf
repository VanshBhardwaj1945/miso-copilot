output "public_entrypoint" {
  value = azurerm_public_ip.appgw.ip_address
}

output "frontend_hostname" {
  value = azurerm_static_web_app.frontend.default_host_name
}

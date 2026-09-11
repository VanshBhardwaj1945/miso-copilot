# AKS: 2-5 autoscaling nodes running five workloads (k8s manifests, not
# Terraform): api (xN, HPA - orchestrates answers), embedder (MiniLM in its
# own deployment so the api tier carries no ML model; a stateless reader, so
# it scales like api), poller (EXACTLY 1 - the rate-guard rule), chroma
# (client/server, persistent volume), mcp (own deployment).

resource "azurerm_kubernetes_cluster" "main" {
  name                = "${var.prefix}-aks"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  dns_prefix          = var.prefix

  default_node_pool {
    name                 = "default"
    vm_size              = "Standard_D2s_v5"
    auto_scaling_enabled = true
    min_count            = 2
    max_count            = 5
    vnet_subnet_id       = azurerm_subnet.aks.id
  }

  identity {
    type = "SystemAssigned"
  }

  node_provisioning_profile {
    mode = "Manual"
  }

  network_profile {
    network_plugin = "azure"
  }

  ingress_application_gateway {
    gateway_id = azurerm_application_gateway.main.id
  }

  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  oms_agent {
    log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  }
}

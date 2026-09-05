# MISO Copilot - reference cloud deployment (NOT deployed; nothing here has run).
# Production-shaped: AKS for scale, WAF in front, Redis for shared state,
# logs forwarded to SIEM. App manifests (Deployments/Ingress) would live in
# k8s/, not here - this file is the platform underneath them.

terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.4"
    }
  }
}

provider "azurerm" {
  features {}
}

variable "prefix" {
  description = "Name prefix for every resource"
  type        = string
  default     = "misocopilot"
}

variable "location" {
  description = "Azure region"
  type        = string
  default     = "centralus"
}

variable "claude_api_key" {
  description = "Anthropic API key - pass via TF_VAR_claude_api_key, never commit"
  type        = string
  sensitive   = true
}

data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "main" {
  name     = "${var.prefix}-rg"
  location = var.location
}

# --- logging -> SIEM -------------------------------------------------------

resource "azurerm_log_analytics_workspace" "main" {
  name                = "${var.prefix}-logs"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  retention_in_days   = 30
}

# forwarded to SIEM: the workspace is onboarded so security gets every
# request log (ip, question, outcome) and WAF event in one place
resource "azurerm_sentinel_log_analytics_workspace_onboarding" "siem" {
  workspace_id = azurerm_log_analytics_workspace.main.id
}

# --- network + WAF edge ----------------------------------------------------

resource "azurerm_virtual_network" "main" {
  name                = "${var.prefix}-vnet"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = ["10.10.0.0/16"]
}

resource "azurerm_subnet" "appgw" {
  name                 = "appgw"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.10.1.0/24"]
}

resource "azurerm_subnet" "aks" {
  name                 = "aks"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.10.2.0/23"]
}

resource "azurerm_public_ip" "appgw" {
  name                = "${var.prefix}-appgw-ip"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
}

# the firewall + edge rate limiter. Our in-app per-IP limiter stays as
# defense in depth; this blocks floods before they reach a pod.
resource "azurerm_web_application_firewall_policy" "main" {
  name                = "${var.prefix}-waf"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  policy_settings {
    enabled = true
    mode    = "Prevention"
  }

  custom_rules {
    name                 = "PerIPRateLimit"
    priority             = 10
    rule_type            = "RateLimitRule"
    action               = "Block"
    rate_limit_duration  = "OneMin"
    rate_limit_threshold = 60
    group_rate_limit_by  = "ClientAddr"

    match_conditions {
      match_variables {
        variable_name = "RemoteAddr"
      }
      operator           = "IPMatch"
      negation_condition = false
      match_values       = ["0.0.0.0/0"]
    }
  }

  managed_rules {
    managed_rule_set {
      type    = "OWASP"
      version = "3.2"
    }
  }
}

# load balancer: WAF_v2 Application Gateway in front of the cluster.
# The AGIC addon on AKS keeps its backend pools pointed at the pods, so the
# listener/pool blocks below are just the required skeleton.
# TLS cert intentionally omitted from the sketch.
resource "azurerm_application_gateway" "main" {
  name                = "${var.prefix}-appgw"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  firewall_policy_id  = azurerm_web_application_firewall_policy.main.id

  sku {
    name     = "WAF_v2"
    tier     = "WAF_v2"
    capacity = 1
  }

  gateway_ip_configuration {
    name      = "gateway-ip"
    subnet_id = azurerm_subnet.appgw.id
  }

  frontend_port {
    name = "http"
    port = 80
  }

  frontend_ip_configuration {
    name                 = "public"
    public_ip_address_id = azurerm_public_ip.appgw.id
  }

  backend_address_pool {
    name = "default"
  }

  backend_http_settings {
    name                  = "default"
    cookie_based_affinity = "Disabled"
    port                  = 8000
    protocol              = "Http"
    request_timeout       = 60
  }

  http_listener {
    name                           = "default"
    frontend_ip_configuration_name = "public"
    frontend_port_name             = "http"
    protocol                       = "Http"
  }

  request_routing_rule {
    name                       = "default"
    priority                   = 100
    rule_type                  = "Basic"
    http_listener_name         = "default"
    backend_address_pool_name  = "default"
    backend_http_settings_name = "default"
  }

  lifecycle {
    # AGIC rewrites pools/listeners at runtime; Terraform must not fight it
    ignore_changes = [backend_address_pool, backend_http_settings,
    http_listener, request_routing_rule, probe]
  }
}

# --- the cluster -----------------------------------------------------------

# AKS over raw VMs: autoscaling, rolling deploys, and one place to run the
# four workloads (see the comment below) instead of hand-managed machines.
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

# Workloads this cluster runs (k8s manifests, not Terraform):
#   api     - FastAPI /ask, N replicas behind the gateway, HPA on CPU
#   poller  - EXACTLY 1 replica, same rate-guard rule as AGENTS.md;
#             splitting it out is what lets the api scale freely
#   mcp     - the MCP server, its own deployment + service
#   chroma  - Chroma in client/server mode, 1 replica on a persistent
#             volume; api/mcp pods query it over HTTP instead of
#             embedding it (embedded mode can't share across pods)

# --- images ----------------------------------------------------------------

resource "azurerm_container_registry" "main" {
  name                = "${var.prefix}acr"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Basic"
}

resource "azurerm_role_assignment" "aks_pulls_images" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
}

# --- shared state ----------------------------------------------------------

# answer cache + per-IP rate-limit counters. In-memory state stops working
# the moment the api has 2 replicas; Redis is where both move.
resource "azurerm_redis_cache" "main" {
  name                = "${var.prefix}-redis"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  capacity            = 0
  family              = "C"
  sku_name            = "Standard"
}

# poller snapshots + request log; mounted into the poller and api pods
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

# --- secrets ---------------------------------------------------------------

resource "azurerm_key_vault" "main" {
  name                       = "${var.prefix}-kv"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
}

resource "azurerm_key_vault_secret" "claude_key" {
  name         = "claude-api-key"
  value        = var.claude_api_key
  key_vault_id = azurerm_key_vault.main.id
}

# pods read secrets through workload identity; the federated credential
# binding to a service account happens alongside the k8s manifests
resource "azurerm_user_assigned_identity" "app" {
  name                = "${var.prefix}-identity"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_role_assignment" "app_reads_secrets" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- frontend --------------------------------------------------------------

resource "azurerm_static_web_app" "frontend" {
  name                = "${var.prefix}-web"
  location            = "centralus"
  resource_group_name = azurerm_resource_group.main.name
  sku_tier            = "Free"
  sku_size            = "Free"
}

# --- outputs ---------------------------------------------------------------

output "public_entrypoint" {
  value = azurerm_public_ip.appgw.ip_address
}

output "frontend_hostname" {
  value = azurerm_static_web_app.frontend.default_host_name
}

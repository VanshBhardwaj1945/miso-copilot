# MISO Copilot - reference cloud deployment (NOT deployed; nothing here has run).
# A sketch of what production could look like, kept honest and small.

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

# logs for the backend + poller (replaces tailing /tmp/miso-backend.log)
resource "azurerm_log_analytics_workspace" "main" {
  name                = "${var.prefix}-logs"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  retention_in_days   = 30
}

# persistent disk for data/: Chroma store, raw snapshots, request log
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
  quota              = 5 # GB - Chroma + snapshots are tiny
}

# the API key lives in Key Vault; the app reads it with a managed identity
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

resource "azurerm_container_app_environment" "main" {
  name                       = "${var.prefix}-env"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
}

resource "azurerm_container_app_environment_storage" "data" {
  name                         = "copilot-data"
  container_app_environment_id = azurerm_container_app_environment.main.id
  account_name                 = azurerm_storage_account.data.name
  share_name                   = azurerm_storage_share.data.name
  access_key                   = azurerm_storage_account.data.primary_access_key
  access_mode                  = "ReadWrite"
}

# the FastAPI backend + in-process poller.
# EXACTLY ONE REPLICA, on purpose: the poller's rate guard is a local file
# and the scheduler must not run twice (same rule as AGENTS.md's "one
# worker, one machine"). Scaling reads means splitting the poller out first.
resource "azurerm_container_app" "backend" {
  name                         = "${var.prefix}-api"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  secret {
    name                = "claude-api-key"
    key_vault_secret_id = azurerm_key_vault_secret.claude_key.id
    identity            = azurerm_user_assigned_identity.app.id
  }

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "api"
      image  = "ghcr.io/vanshbhardwaj1945/miso-copilot:latest" # built by CI (not set up yet)
      cpu    = 1.0
      memory = "2Gi" # embedding model needs the headroom

      env {
        name        = "CLAUDE_API_KEY"
        secret_name = "claude-api-key"
      }

      volume_mounts {
        name = "data"
        path = "/app/data"
      }
    }

    volume {
      name         = "data"
      storage_name = azurerm_container_app_environment_storage.data.name
      storage_type = "AzureFile"
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}

# the React frontend, built and served as static files
resource "azurerm_static_web_app" "frontend" {
  name                = "${var.prefix}-web"
  location            = "centralus"
  resource_group_name = azurerm_resource_group.main.name
  sku_tier            = "Free"
  sku_size            = "Free"
}

output "backend_url" {
  value = "https://${azurerm_container_app.backend.ingress[0].fqdn}"
}

output "frontend_hostname" {
  value = azurerm_static_web_app.frontend.default_host_name
}

# MISO Copilot - reference cloud deployment (NOT deployed; nothing here has run).
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

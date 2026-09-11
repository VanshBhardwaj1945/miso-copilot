# MISO Ramen - reference cloud deployment (NOT deployed; nothing here has run).
variable "prefix" {
  description = "Name prefix for every resource"
  type        = string
  default     = "misoramen"
}

variable "location" {
  description = "Azure region"
  type        = string
  default     = "centralus"
}

variable "model_api_key" {
  description = "Credential for the answer model - Claude today, but MISO could self-host the model or use any provider. Pass via TF_VAR_model_api_key, never commit"
  type        = string
  sensitive   = true
}

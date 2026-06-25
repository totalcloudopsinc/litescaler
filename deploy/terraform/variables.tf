variable "folder_id" {
  type        = string
  description = "Yandex Cloud folder that holds the cluster and where the scaler service account is created."
}

variable "cluster_id" {
  type        = string
  description = "Pre-existing Managed Kubernetes cluster id the scaler will manage. Terraform references it; it does not create it."
}

variable "env" {
  type        = string
  description = "Environment name (dev|prod). Used to name the service account and select the matching Kustomize overlay."
}

variable "namespace" {
  type        = string
  default     = "kube-system"
  description = "Namespace the scaler runs in. The SA-key Secret is created here and must match the overlay's namespace."
}

variable "secret_name" {
  type        = string
  default     = "lite-scaller-sa"
  description = "Name of the Kubernetes Secret holding sa-key.json. Must match secretName in the Deployment."
}

variable "yc_token" {
  type        = string
  default     = null
  sensitive   = true
  description = "Operator IAM token (OAuth/IAM). Null -> provider falls back to YC_TOKEN env var."
}

variable "yc_service_account_key_file" {
  type        = string
  default     = null
  description = "Path to an operator SA key file, as an alternative to yc_token. Null -> env-var fallback."
}
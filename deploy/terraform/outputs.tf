output "service_account_id" {
  value       = yandex_iam_service_account.scaler.id
  description = "Id of the scaler service account created for this environment."
}

output "secret_name" {
  value       = kubernetes_secret_v1.scaler_sa.metadata[0].name
  description = "Name of the Kubernetes Secret holding sa-key.json. Must match the Deployment's secretName."
}

output "namespace" {
  value       = var.namespace
  description = "Namespace the Secret was created in; the overlay must deploy into the same one."
}

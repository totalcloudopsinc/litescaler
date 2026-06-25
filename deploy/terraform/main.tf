data "yandex_client_config" "this" {}

data "yandex_kubernetes_cluster" "this" {
  cluster_id = var.cluster_id
}

resource "yandex_iam_service_account" "scaler" {
  name        = "lite-scaler-${var.env}"
  folder_id   = var.folder_id
  description = "lite-scaler autoscaler (${var.env})"
}

resource "yandex_resourcemanager_folder_iam_member" "editor" {
  folder_id = var.folder_id
  role      = "k8s.editor"
  member    = "serviceAccount:${yandex_iam_service_account.scaler.id}"
}

resource "yandex_resourcemanager_folder_iam_member" "cluster_admin" {
  folder_id = var.folder_id
  role      = "k8s.cluster-api.cluster-admin"
  member    = "serviceAccount:${yandex_iam_service_account.scaler.id}"
}

resource "yandex_iam_service_account_key" "scaler" {
  service_account_id = yandex_iam_service_account.scaler.id
  description        = "lite-scaler key (${var.env})"
}

resource "kubernetes_secret_v1" "scaler_sa" {
  metadata {
    name      = var.secret_name
    namespace = var.namespace
  }

  data = {
    "sa-key.json" = jsonencode({
      id                 = yandex_iam_service_account_key.scaler.id
      service_account_id = yandex_iam_service_account.scaler.id
      created_at         = yandex_iam_service_account_key.scaler.created_at
      key_algorithm      = yandex_iam_service_account_key.scaler.key_algorithm
      public_key         = yandex_iam_service_account_key.scaler.public_key
      private_key        = yandex_iam_service_account_key.scaler.private_key
    })
  }

  type = "Opaque"
}
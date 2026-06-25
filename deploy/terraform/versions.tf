terraform {
  required_version = ">= 1.3"

  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = ">= 0.100"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.20"
    }
  }
}

provider "yandex" {
  folder_id                = var.folder_id
  token                    = var.yc_token
  service_account_key_file = var.yc_service_account_key_file
}

provider "kubernetes" {
  host                   = data.yandex_kubernetes_cluster.this.master[0].external_v4_endpoint
  cluster_ca_certificate = data.yandex_kubernetes_cluster.this.master[0].cluster_ca_certificate
  token                  = data.yandex_client_config.this.iam_token
}
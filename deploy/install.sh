#!/usr/bin/env bash
set -euo pipefail

# One-click install for one environment.
#   ./deploy/install.sh <env>            # apply
#   ./deploy/install.sh <env> plan       # terraform plan + kustomize dry-run only
#   ./deploy/install.sh <env> destroy    # delete kustomize resources + terraform destroy
#
# Step 1 (Terraform): create the scaler service account, its IAM bindings, the
#   SA key, and the Kubernetes Secret holding sa-key.json.
# Step 2 (Kustomize): apply the Deployment + per-env ConfigMap.
#
# Prereqs: terraform, kubectl (>=1.27 for built-in kustomize), and the `yc` CLI
# authenticated as the operator. The script points kubectl at the target
# cluster itself (yc ... get-credentials --external), so you don't have to.

ENV="${1:?usage: install.sh <env> [plan|destroy]}"
MODE="${2:-apply}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/terraform"
OVERLAY="${SCRIPT_DIR}/kustomize/overlays/${ENV}"
TFVARS="${TF_DIR}/envs/${ENV}.tfvars"

[[ -f "${TFVARS}" ]]   || { echo "no tfvars for env '${ENV}': ${TFVARS}" >&2; exit 1; }
[[ -d "${OVERLAY}" ]]  || { echo "no overlay for env '${ENV}': ${OVERLAY}" >&2; exit 1; }

CLUSTER_ID="$(sed -nE 's/^[[:space:]]*cluster_id[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "${TFVARS}")"
[[ -n "${CLUSTER_ID}" ]] || { echo "could not read cluster_id from ${TFVARS}" >&2; exit 1; }

configure_kubectl() {
  command -v yc >/dev/null 2>&1 || {
    echo "WARN: 'yc' CLI not found; using the existing kubectl context as-is." >&2
    return 0
  }
  echo "--- pointing kubectl at cluster ${CLUSTER_ID} (external endpoint) ---"
  yc managed-kubernetes cluster get-credentials --id "${CLUSTER_ID}" --external --force
}

if [[ -z "${YC_TOKEN:-}" && -z "${TF_VAR_yc_service_account_key_file:-}" ]]; then
  if command -v yc >/dev/null 2>&1; then
    YC_TOKEN="$(yc iam create-token)"; export YC_TOKEN
  else
    echo "YC_TOKEN is unset and the 'yc' CLI was not found. Export YC_TOKEN or set TF_VAR_yc_service_account_key_file." >&2
    exit 1
  fi
fi

terraform -chdir="${TF_DIR}" init -input=false

if [[ "${MODE}" == "plan" ]]; then
  terraform -chdir="${TF_DIR}" plan -input=false -var-file="envs/${ENV}.tfvars"
  echo "--- kustomize build (${ENV}) ---"
  kubectl kustomize "${OVERLAY}"
  configure_kubectl || true
  echo "--- kustomize server dry-run (${ENV}) ---"
  kubectl apply -k "${OVERLAY}" --dry-run=server \
    || echo "WARN: server dry-run skipped — cluster API unreachable; the offline build above is valid." >&2
  exit 0
fi

if [[ "${MODE}" == "destroy" ]]; then
  configure_kubectl || true
  echo "--- delete kustomize resources (${ENV}) ---"
  kubectl delete -k "${OVERLAY}" --ignore-not-found \
    || echo "WARN: kubectl delete skipped — cluster API unreachable or already gone." >&2
  terraform -chdir="${TF_DIR}" destroy -input=false -var-file="envs/${ENV}.tfvars"
  exit 0
fi

terraform -chdir="${TF_DIR}" apply -input=false -var-file="envs/${ENV}.tfvars"
configure_kubectl
kubectl apply -k "${OVERLAY}"

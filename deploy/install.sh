#!/usr/bin/env bash
set -euo pipefail

# One-click install for one environment (manual / bootstrap path).
# The primary, day-to-day deploy is GitHub Actions (.github/workflows/
# deploy-k8s*.yaml); this script covers the one-time Terraform bootstrap
# (service account + IAM + SA-key Secret) and manual applies.
#
#   ./deploy/install.sh <env> [apply|plan|destroy] [--dry-run|--no-dry-run]
#     apply    (default) terraform apply + kubectl apply -k
#     plan     terraform plan + offline kustomize build + best-effort server dry-run
#     destroy  delete kustomize resources + terraform destroy
#
#   --dry-run / --no-dry-run  override the overlay's scaling.dry_run for this
#                             apply/plan (omit to keep the overlay's value).
#
# Step 1 (Terraform): create the scaler service account, its IAM bindings, the
#   SA key, and the Kubernetes Secret holding sa-key.json.
# Step 2 (Kustomize): apply the Deployment + per-env ConfigMap.
#
# Prereqs: terraform, kubectl (>=1.27 for built-in kustomize), and the `yc` CLI
# authenticated as the operator. The script points kubectl at the target
# cluster itself (yc ... get-credentials --external), so you don't have to.

ENV="${1:?usage: install.sh <env> [apply|plan|destroy] [--dry-run|--no-dry-run]}"
shift || true

MODE="apply"
DRY_RUN_OVERRIDE=""
for arg in "$@"; do
  case "${arg}" in
    apply|plan|destroy) MODE="${arg}" ;;
    --dry-run)          DRY_RUN_OVERRIDE="true" ;;
    --no-dry-run)       DRY_RUN_OVERRIDE="false" ;;
    *) echo "unknown argument: ${arg}" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/terraform"
OVERLAY="${SCRIPT_DIR}/kustomize/overlays/${ENV}"
TFVARS="${TF_DIR}/envs/${ENV}.tfvars"

[[ -f "${TFVARS}" ]]   || { echo "no tfvars for env '${ENV}': ${TFVARS}" >&2; exit 1; }
[[ -d "${OVERLAY}" ]]  || { echo "no overlay for env '${ENV}': ${OVERLAY}" >&2; exit 1; }

CLUSTER_ID="$(sed -nE 's/^[[:space:]]*cluster_id[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "${TFVARS}")"
[[ -n "${CLUSTER_ID}" ]] || { echo "could not read cluster_id from ${TFVARS}" >&2; exit 1; }

# Render the overlay, applying the optional dry_run override. The embedded
# config.yaml is a YAML string that can't be patched field-by-field, so we
# rewrite the single dry_run line in the rendered output (same approach the
# GitHub Actions deploy uses).
render() {
  if [[ -n "${DRY_RUN_OVERRIDE}" ]]; then
    kubectl kustomize "${OVERLAY}" \
      | sed -E "s/^([[:space:]]*)dry_run:[[:space:]]*(true|false).*/\1dry_run: ${DRY_RUN_OVERRIDE}/"
  else
    kubectl kustomize "${OVERLAY}"
  fi
}

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

[[ -n "${DRY_RUN_OVERRIDE}" ]] && echo "--- scaling.dry_run override for this run: ${DRY_RUN_OVERRIDE} ---"

terraform -chdir="${TF_DIR}" init -input=false

if [[ "${MODE}" == "plan" ]]; then
  terraform -chdir="${TF_DIR}" plan -input=false -var-file="envs/${ENV}.tfvars"
  echo "--- kustomize build (${ENV}) ---"
  render
  configure_kubectl || true
  echo "--- kustomize server dry-run (${ENV}) ---"
  render | kubectl apply -f - --dry-run=server \
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
render | kubectl apply -f -

# The ConfigMap is a plain resource, not a configMapGenerator, so its name carries
# no content hash: changing config.yaml (a dry_run override, say) leaves the pod
# spec identical and triggers no rollout, so the pod would keep running the old
# config indefinitely. Unlike CI, this path usually does not change the image tag
# either, so without an explicit restart nothing would pick the change up at all.
NAMESPACE="$(sed -nE 's/^namespace:[[:space:]]*([^[:space:]]+).*/\1/p' "${OVERLAY}/kustomization.yaml")"
NAMESPACE="${NAMESPACE:-kube-system}"
kubectl -n "${NAMESPACE}" rollout restart deploy/lite-scaler

# Not fatal: this script is also the FIRST thing run on a new environment, before
# CI has ever pushed an image. The overlay points at :latest, so until then the
# pod cannot pull and will never become ready — while the Terraform and manifest
# work above did succeed. Report it and let the operator judge.
if ! kubectl -n "${NAMESPACE}" rollout status deploy/lite-scaler --timeout=120s; then
  echo >&2
  echo "WARN: lite-scaler did not become ready within 120s." >&2
  echo "      On a first install this is expected: the registry is empty until a" >&2
  echo "      CI run builds and pushes an image. Everything else was applied." >&2
  kubectl -n "${NAMESPACE}" get pods -l app=lite-scaler >&2 || true
fi

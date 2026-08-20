# Deploy lite-scaler (Terraform + Kustomize)

One-click, per-environment install. **Terraform** provisions the cloud bits that
need the Yandex Cloud API and must not live in Git (service account, IAM
bindings, SA key, and the Kubernetes Secret that carries it). **Kustomize** owns
the manifests: a `base/` plus one overlay per environment that sets the per-env
`config.yaml` values and image tag.

The cluster and node group are **pre-existing** — Terraform references them.

```
deploy/
  install.sh                 # wrapper: terraform apply + kubectl apply -k
  terraform/                 # SA, IAM bindings, key, kubernetes_secret
    envs/{dev,prod}.tfvars
  kustomize/
    base/                    # deployment + base config.yaml
    overlays/{dev,prod}/   # per-env config-patch.yaml + image tag
```

## What you edit per environment

1. `terraform/envs/<env>.tfvars` — `folder_id`, `cluster_id`, `namespace`.
2. `kustomize/overlays/<env>/config-patch.yaml` — `node_group_id`, `cluster_id`,
   `namespace`, `label_selectors`, scaling sizes (`max_size`, `min_size`,
   `dry_run`, ...), and the `metrics` block (`enabled`, `port`).
3. `kustomize/overlays/<env>/kustomization.yaml` — `namespace` (must match the
   tfvars namespace) and the image `newTag`.
4. `kustomize/overlays/<env>/deployment-patch.yaml` — the `nodeSelector` pinning
   the scaler to a node. Use a label on a **stable** node the scaler never
   resizes; do **not** target the managed node group, or a scale-down could
   drain the scaler's own node. Until replaced, the pod stays `Pending`.

Search for `REPLACE_` placeholders before applying.

## Install

The Terraform `yandex` provider needs an **operator** credential (yours, not the
scaler's SA). `install.sh` mints one from the active `yc` CLI profile when
`YC_TOKEN` is unset; export it yourself if you don't have the `yc` CLI:

```bash
export YC_TOKEN="$(yc iam create-token)"          # or set TF_VAR_yc_service_account_key_file=/path/to/key.json
```

```bash
# install.sh points kubectl at the cluster itself (yc ... get-credentials
# --external), reading cluster_id from the env's tfvars — no manual context.
./deploy/install.sh prod plan     # terraform plan + offline kustomize build + best-effort dry-run
./deploy/install.sh prod          # terraform apply + kubectl apply -k
./deploy/install.sh prod destroy  # kubectl delete -k + terraform destroy (reverse order)
```

## Why the split

- The SA key is a secret; Kustomize stores manifests in Git, so Terraform creates
  the Secret instead and Kustomize only references it by name (`lite-scaller-sa`).
- The two IAM grants (`k8s.editor` + `k8s.cluster-api.cluster-admin`) need the YC
  API, so they live in Terraform — see the main README for why both are required.
- Everything per-env and non-secret (the `config.yaml` values, image tag) lives in
  the overlay, which is exactly what Kustomize overlays are for.

## Notes

- The base Deployment exposes port `9090` as `metrics` and sets the
  `prometheus.io/{scrape,port,path}` pod annotations. Keep the annotation port
  in sync with `metrics.port` in the overlay's `config-patch.yaml`.

- The embedded `config.yaml` string is overridden **whole** per overlay (an
  embedded YAML string can't be patched field-by-field), so each overlay carries
  a complete `config.yaml`. Keep them in sync with `base/configmap.yaml` defaults.
- `install.sh ... plan` first renders the overlay offline (`kubectl kustomize`,
  no cluster needed), then attempts a server-side `--dry-run=server` as a
  best-effort check — an unreachable cluster API warns instead of failing. To
  inspect the rendered config against the app's Pydantic model, run
  `kubectl kustomize overlays/<env>` and read the ConfigMap's `config.yaml`.

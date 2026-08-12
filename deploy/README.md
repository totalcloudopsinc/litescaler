# Deploy lite-scaler (Terraform + Kustomize)

One-click, per-environment install. **Terraform** provisions the cloud bits that
need the Yandex Cloud API and must not live in Git (service account, IAM
bindings, SA key, and the Kubernetes Secret that carries it). **Kustomize** owns
the manifests: a `base/` plus one overlay per environment that sets the per-env
`config.yaml` values and image tag.

The cluster and node group are **pre-existing** — Terraform references them.

## Deploy via GitHub Actions (intended, NOT yet usable)

> **There is no runner for this repo yet.** The workflows below are written and
> committed, but nothing picks up their jobs — `runs-on: [bot]` is BotsService's
> self-hosted runner label. Until a runner is attached, build and deploy by hand:
> see [Manual build and push](#manual-build-and-push) below, then `./install.sh <env>`.
> A push to a branch will not start anything by accident: the dev job additionally
> requires the `dev` branch or `deploy to dev` in the commit message.

Day-to-day deploys are meant to go through CI, like BotsService. Two workflows
build+push the image and apply the matching Kustomize overlay on the self-hosted
`[bot]` runner (which is expected to already have `kubectl` pointed at the cluster):

| Workflow | Env / namespace | Triggers |
|:---------|:----------------|:---------|
| `.github/workflows/deploy-k8s.yaml` | dev / `kube-system` | manual `workflow_dispatch`; push to `dev`; or a push whose commit message contains `deploy to dev` (not a `Merge`) |
| `.github/workflows/deploy-k8s-prod.yaml` | prod / `kube-system` | manual `workflow_dispatch`; a PR merged into `master`; or a push whose message contains `deploy to prod` (not a `Merge`) |

Image tag = commit SHA (dev) / `prod-<sha>` (prod), pinned into the rendered
manifest at deploy time.

**dry_run per deploy.** Each workflow has a `workflow_dispatch` input `dry_run`
with `overlay-default | true | false`. `overlay-default` keeps the overlay's
baked value (dev = `true`, prod = `false`); `true`/`false` override it for that
run (the scaler logs decisions but performs no resize when `dry_run: true`).
Auto-triggered (push/PR) deploys always use the overlay default.

Terraform below is a **one-time bootstrap** per environment (service account +
IAM + SA-key Secret); CI does not run it.

## Manual build and push

While there is no runner, the image is built and pushed from a workstation:

```bash
./build.sh            # builds linux/amd64, pushes :latest and :<short-sha>
```

The platform is pinned because the cluster nodes are amd64 while this is usually
built on an arm64 Mac — a native build pushes fine and then crash-loops in the
cluster with `exec format error`.

**Credential gotcha.** `docker-credential-yc` resolves `cr.yandex` against the
profile pinned in `~/.config/yandex-cloud/credhelper-config.yaml`, *not* the
active `yc` profile, and that mapping is one profile per registry **host** — so
several clouds sharing `cr.yandex` cannot all work through the helper. If the
bound profile is not the one that owns this registry, the push fails with an
opaque `denied: Permission denied` after the whole build has completed.
`build.sh` warns when it detects the mismatch. To push anyway, authenticate
explicitly — the helper takes precedence, so its entry has to leave
`credHelpers` in `~/.docker/config.json` first, and go back afterwards:

```bash
# remove the "cr.yandex" line from credHelpers in ~/.docker/config.json, then:
docker login cr.yandex -u iam -p "$(yc iam create-token)"
docker push cr.yandex/<registry-id>/lite-scaler:latest
# restore the credHelpers entry
```

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
   `namespace`, `label_selectors`, and scaling sizes (`max_size`, `min_size`,
   `dry_run`, ...).
3. `kustomize/overlays/<env>/kustomization.yaml` — `namespace` (must match the
   tfvars namespace) and the image `newTag`.
4. `kustomize/overlays/<env>/deployment-patch.yaml` — the `nodeSelector` pinning
   the scaler to a node, **plus a matching toleration if that node is tainted**.
   Use a label on a **stable** node the scaler never resizes; do **not** target
   the managed node group, or a scale-down could drain the scaler's own node.
   A `nodeSelector` onto a tainted node without the toleration leaves the pod
   `Pending` forever.

Both overlays are filled in for the MyMeet cluster; only `base/` still carries
`REPLACE_PER_OVERLAY`, and that is fine — `base/` is never applied on its own,
each overlay replaces `config.yaml` wholesale.

## Current state per environment

| | dev | prod |
|:--|:--|:--|
| Node group | `worker-dev` `cat6ukhj05u7rvd2qqi3` | `worker-prod` `cat8gsfad91brpidohkl` |
| Watches | ns `bot-dev`, `app=bot-dev-worker` | ns `bot-prod`, `app=bot-prod-worker` |
| Scaler runs on | `workload=infra` (tolerates `dedicated=infra`) | same |
| `dry_run` | `true` — first rollout observes only | `true` — **blocked**, see below |
| Ready to scale for real | after the dry-run logs check out | no |

**prod is not ready.** `worker-prod` is still an `auto_scale` group owned by the
Yandex cluster-autoscaler. This scaler only understands `fixed_scale`
(`app/yc.py` reads `scale_policy.fixed_scale.size`), so on an `auto_scale` group
it reads size `0`, its stability gate blocks every decision, and it runs as a
silent no-op. Converting `worker-prod` recreates the group — a production
outage — so it is a separate, planned change. `dev` was converted this way
already (`k8s-terraform`, `yandex_kubernetes_node_group.external_scaled`).

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

# Override scaling.dry_run for a manual apply/plan (omit to keep the overlay value):
./deploy/install.sh dev  --dry-run     # force dry_run: true
./deploy/install.sh prod --no-dry-run  # force dry_run: false
```

## Why the split

- The SA key is a secret; Kustomize stores manifests in Git, so Terraform creates
  the Secret instead and Kustomize only references it by name (`lite-scaler-sa`).
- The two IAM grants (`k8s.editor` + `k8s.cluster-api.cluster-admin`) need the YC
  API, so they live in Terraform — see the main README for why both are required.
- Everything per-env and non-secret (the `config.yaml` values, image tag) lives in
  the overlay, which is exactly what Kustomize overlays are for.

## Notes

- The embedded `config.yaml` string is overridden **whole** per overlay (an
  embedded YAML string can't be patched field-by-field), so each overlay carries
  a complete `config.yaml`. Keep them in sync with `base/configmap.yaml` defaults.
- `install.sh ... plan` first renders the overlay offline (`kubectl kustomize`,
  no cluster needed), then attempts a server-side `--dry-run=server` as a
  best-effort check — an unreachable cluster API warns instead of failing. To
  inspect the rendered config against the app's Pydantic model, run
  `kubectl kustomize overlays/<env>` and read the ConfigMap's `config.yaml`.

# Deploy lite-scaler (Terraform + Kustomize)

One-click, per-environment install. **Terraform** provisions the cloud bits that
need the Yandex Cloud API and must not live in Git (service account, IAM
bindings, SA key, and the Kubernetes Secret that carries it). **Kustomize** owns
the manifests: a `base/` plus one overlay per environment that sets the per-env
`config.yaml` values and image tag.

The cluster and node group are **pre-existing** — Terraform references them.

## Deploy via GitHub Actions (blocked on runner access)

> **The `deploy_*` jobs still have no runner.** `runs-on: [bot]` is BotsService's
> self-hosted runner label; whether it also serves this repo depends on the
> runner being registered at org level with this repo in its runner group. Until
> that is arranged, build and deploy by hand: see
> [Manual build and push](#manual-build-and-push) below, then `./install.sh <env>`.
> A push to a branch will not start a deploy by accident: the dev job additionally
> requires the `dev` branch or `deploy to dev` in the commit message.
>
> The `test` job is **not** blocked — it runs on `ubuntu-latest` and gates both
> deploy jobs via `needs: test`.

### Checklist to make CI usable

**1. Repository access to the runner — the only certain blocker.**
Two labels are in use across the org: `[bot]` (BotsService) and `[org]`
(CalendarService), plus `review` for the PR-review workflows. Two different
repos sharing labels means the runners are registered at organisation level, so
this is a runner-*group* membership question, not a new-runner question:

> org settings → Actions → Runner groups → the group owning the runner →
> Repository access → add `MyMeetAI/litescaler`.

Prefer `[org]`: `[bot]` belongs to the BotsService builder (a ~1.3 GB image with
Chrome and a warm BuildKit cache for it), while CalendarService's use of `[org]`
shows it is the general-purpose one, which is the closer fit for a small Python
image. The label only decides which runner picks the job — access is the group.

**2. Registry push rights — nothing to do.**
Checked on 2026-08-16: our `lite-scaler` registry (`crpcvsde88m85as3hdu0`) and
the `mymeet-k8s` one the other repos push to (`crp2u89d0h0f91016d6q`) live in the
**same folder** `b1gs6pmsqltug8ii0j5d`, and *neither* has registry-scoped access
bindings — all grants are folder-level, so they apply to both registries
identically:

| Service account | Folder-level role |
|:--|:--|
| `cicd-runner` | `container-registry.images.pusher` |
| `k8s-cicd-runner` | `container-registry.editor` |

Whichever of these the runner uses can already push to ours. The
`YC_CR_SA_KEY` secret exists only as an override if that ever stops being true;
leaving it unset is expected and only logs a warning.

**3. Cluster write access in `kube-system` — probably fine, verified on first run.**
`cicd-runner` holds `k8s.cluster-api.editor` at folder level, which Yandex maps
to the built-in `edit` ClusterRole across all namespaces, `kube-system`
included. Note this is different from `k8s.editor`, which only grants management
of clusters through the cloud API and nothing inside them.

**4. `kubectl` on the runner.** BotsService and CalendarService both deploy with
`helm` and never call `kubectl`, so the binary may be absent even though a
working kubeconfig is present. We need >= 1.27 for the built-in
`kubectl kustomize`.

Items 1, 3 and 4 each fail loudly and specifically in the `Preflight` step, in
seconds rather than after the image build, so one run of the workflow is a
reasonable way to find out which are actually missing.

Day-to-day deploys are meant to go through CI, like BotsService. Two workflows
build+push the image and apply the matching Kustomize overlay on the self-hosted
`[bot]` runner (which is expected to already have `kubectl` pointed at the cluster):

| Workflow | Env / namespace | Triggers |
|:---------|:----------------|:---------|
| `.github/workflows/deploy-k8s.yaml` | dev / `kube-system` | manual `workflow_dispatch`; push to `dev`; or a push whose commit message contains `deploy to dev` (not a `Merge`) |
| `.github/workflows/deploy-k8s-prod.yaml` | prod / `kube-system` | manual `workflow_dispatch`; a PR merged into `master`; or a push whose message contains `deploy to prod` (not a `Merge`) |

Image tag = commit SHA (dev) / `prod-<sha>` (prod), pinned into the rendered
manifest at deploy time.

**Both deploys end with a verification step**, because `rollout status` returns
as soon as the pod is Ready, which for this scaler only means its HTTP port is
up. The step tails the log until a real decision appears. The prod variant also
fails explicitly on `desired size 0` — the signature of pointing the scaler at
an `auto_scale` group, where it runs as a silent no-op (see
[prod is not ready](#current-state-per-environment)).

**dry_run per deploy.** Each workflow has a `workflow_dispatch` input `dry_run`
with `overlay-default | true | false`. `overlay-default` keeps the overlay's
baked value (dev = `false`, prod = `true`); `true`/`false` override it for that
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
   `namespace`, `label_selectors`, scaling sizes (`max_size`, `min_size`,
   `dry_run`, ...), and the `metrics` block (`enabled`, `port`).
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

## Live scale test on dev (2026-08-13)

Forced load by patching the constant in the `bot-dev-general` ScaledJob's
Prometheus query (`+ 1` → `+ 15`), which makes KEDA target 15 jobs against an
empty queue. **Always save the original query first and restore it after** — left
in place, dev holds N pods and burns nodes indefinitely.

| T+ | Event |
|:--|:--|
| 0s | query patched to `+ 15` |
| 17s | 8 pods `Pending`, 8 `Running` |
| 56s | scaler decided `3 -> 5` and issued the resize |
| 3m46s | 5 nodes `Ready` |
| 7m03s | `squat.ai/video` registered on the new nodes |
| — | query restored to `+ 1` |
| +2m06s | scaler decided `5 -> 3`, floored by `min_size` |
| +3m20s | group back to 3 nodes, dev services untouched (0 restarts) |

Sizing arithmetic matched the prediction exactly:

```
unmet demand after free capacity: 12280m cpu -> 1.55 nodes by cpu
  max * (1 + 0.10 headroom) = 1.71 -> ceil = 2 nodes needed
3 + 2 = 5 wanted, capped by max_size 6 -> target 5 (+2)
```

Two pods never scheduled, which exposed the capacity-accounting flaw below.

### Free capacity was summed across nodes, but scheduling is per-node — FIXED

> Fixed upstream in `totalcloudopsinc/litescaler@e8496ae`, merged into this fork
> in `dad332b`. Kept here because both symptoms below were found on this cluster
> and the numbers are the ones the fix is now regression-tested against.

`decide` subtracted the group's **total** free CPU from pending demand. Scheduling
is bin-packing, so a remainder too small for one pod is unusable — yet was still
counted. `group_free_capacity()` summed the per-node remainders into one number,
so the distribution never reached `decide` at all.

Straight from the scaler's log at the moment it sized the resize:

```
node worker-dev-1: allocatable 7910m, requested 6470m, free 1440m
node worker-dev-2: allocatable 7910m, requested 6470m, free 1440m
node worker-dev-3: allocatable 7910m, requested 7070m, free  840m
Free capacity across 3 Ready node(s): 3720m cpu / 16.17Gi memory
decide: pending=8, requests 16000m cpu, free in group 3720m cpu, node size 7910m
decide: unmet demand after free capacity: 12280m cpu -> 1.55 nodes by cpu;
        max * (1 + 0.10 headroom) = 1.71 -> ceil = 2 nodes needed
decide: 3 + 2 = 5 wanted, capped by max_size 6 -> target 5 (+2)
```

The largest hole was 1440m against a 2000m request, so **none** of that 3720m
could take a pod — and all of it was deducted anyway. Without the deduction:
`16000 / 7910 = 2.02`, `* 1.10 = 2.23`, `ceil = 3` nodes. It added 2, and two of
the eight pods never scheduled. They were already in `_handled_pods` by then, so
no later poll retried them.

A second run on 2026-08-13, with `max_size` temporarily raised to 10 to rule the
cap out as a confound, showed the sharper form — the scaler idle while pods were
stuck and three nodes of headroom went unused:

```
Pending pods: 2 unscheduled, 2 already accounted for by an earlier resize, 0 new
Free capacity across 7 Ready node(s): 9480m cpu / 40.57Gi memory
decide: pending=0 (threshold 0), ... size 7 (min 3, max 10), headroom 10%
Decision: 0 pending pods <= threshold 0; no action
```

Kubernetes on the same two pods: `0/23 nodes are available: ... 7 Insufficient
cpu`. On paper 9480m is four pods; in reality it was zero, in seven pieces of
1440m and one of 840m.

**How it was fixed.** `group_free_capacity()` now returns a per-node list
(`NodeFree`), and `decide` runs a packing simulation — `_pack`, first-fit
decreasing by dominant resource share, best-fit slot choice — then takes
`max(demand_nodes, fit_nodes)`. Replayed against the merged code, the first
decision becomes `3 -> 6 (+3)` and the second `7 -> 8 (+1)`, both with the reason
`... free in the group, but fragmented across N nodes`.
`tests/test_scaling_fragmentation.py` pins both, plus a control proving that
genuinely usable gaps are still consumed before nodes are added.

### dry_run was not side-effect free — FIXED

> Same upstream commit: `_handled_pods` is gone entirely.

`service.py` recorded pods in `_handled_pods` whether or not the resize was
actually issued. A dry run therefore consumed the pods it "would" have scaled
for, and flipping `dry_run` to `false` afterwards found nothing new to act on;
the flag needed a pod restart to take effect. The set was also what kept the two
stranded pods above from being retried. Every poll now reconsiders all pending
pods from scratch — double-counting is prevented by the packing step instead,
which sees the free space on nodes a previous resize already added.

## Known limitations (observed on the MyMeet cluster, 2026-08-12)

**Memory scales with the whole cluster, not with the node group.** Free-capacity
accounting calls `list_pod_for_all_namespaces()` and deserialises every pod in the
cluster — 702 of them here — although it only needs the pods sitting on the nodes
of the managed group. At 128Mi the pod was OOM-killed (exit 137) on the first
poll, every time. Measured steady state: **208Mi / 81m cpu**, hence 256Mi request
/ 512Mi limit. Narrowing that call with a `spec.nodeName` field selector would
make the footprint independent of cluster size; until then, the limit has to be
revisited whenever the cluster grows, and prod bursting to 45 worker nodes will
push it further.

**"Empty" means "no pod matching the selector", not "no pods".** Scale-down counts
a node as empty when nothing on it matches `label_selectors` — other workloads on
that node are invisible to it, and Yandex, not the scaler, picks which node to
drain. Observed live on dev: the scaler classified `worker-dev-3` as empty while
it was running `bot-dev-main`, `calendar-dev-app` and `scheduler-dev-app` — the
busiest of the three nodes by requests. Only `min_size` held it back:

```
Group ... has 3 Ready node(s), 2 busy with pods matching ['app=bot-dev-worker'], 1 empty
  empty nodes: ['worker-dev-3']
Decision: no empty nodes to remove (empty=1, size=3, min_size=3); no action
```

Keep `min_size` at the count of nodes carrying workloads you cannot afford to
lose, unless the group is genuinely dedicated to the selected pods.

## Current state per environment

| | dev | prod |
|:--|:--|:--|
| Node group | `worker-dev` `cat6ukhj05u7rvd2qqi3` | `worker-prod` `cat8gsfad91brpidohkl` |
| Watches | ns `bot-dev`, `app=bot-dev-worker` | ns `bot-prod`, `app=bot-prod-worker` |
| Scaler runs on | `workload=infra` (tolerates `dedicated=infra`) | same |
| `dry_run` | `false` — live since the 2026-08-13 scale test | `true` — **blocked**, see below |
| Ready to scale for real | after the dry-run logs check out | no |
| Image | pre-`e8496ae` — **still has the packing bug** | not deployed |

**The dev pod is running the old algorithm.** The image in the cluster was built
before the upstream fix was merged; the capacity accounting it uses is the one
described under [the scale test](#live-scale-test-on-dev-2026-08-13). Rebuild and
push to pick up the fix.

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

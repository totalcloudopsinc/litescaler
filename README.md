# lite-scaler

Scales a fixed-size Yandex Cloud Managed Kubernetes node group up when too many
matching pods are stuck `Pending`.

## How it works

A **background loop** lists `Pending` pods in the configured namespace that match
any configured label selector **and are still unscheduled** (no `nodeName`). Pods
that are already on a node but stuck — `ImagePullBackOff`, crash loops, etc. — stay
`phase=Pending` yet do *not* need more capacity, so they are ignored. If the count
of genuinely unschedulable pods exceeds `pending_pod_threshold`
(default `0`, i.e. a single Pending pod is enough), it sizes the resize two ways
and takes the larger:

1. **By demand.** It sums their CPU/memory requests, **subtracts the unreserved
   capacity already available on Ready nodes in the group** (a node just added is
   `Ready` before the scheduler has placed the Pending pods onto it — counting its
   free space stops a second node being added for pods the first will absorb),
   divides the remaining demand by the allocatable capacity of a node **of the
   managed group**, and adds `headroom` (15%).
2. **By fit.** Free capacity is not a pool: a pod only runs where a *single* node
   has room for its whole request. So the pending pods are packed, largest first,
   onto the free space of each node individually; whatever finds no node opens a
   fresh one. Seven nodes with 1440m free each hold 9480m in total and still
   cannot take one 2000m pod — by demand alone that pod looks satisfied and stays
   `Pending` forever, so the fit count overrides it.

It then resizes the node group to `current + nodes_needed` (capped at
`max_size`). If the pods fit in existing free capacity, it does nothing. A pod
requesting more than a whole node is logged as unschedulable and excluded from
both counts — no group size can ever place it.

**Scale-down.** When no matching pod has been `Pending` for
`scale_down_cooldown_polls` consecutive polls (default 3), the loop counts
*empty* nodes in the managed node group — `Ready` nodes with zero scheduled pods
matching the label selectors — and lowers the group size by that count, down to
`min_size` (default 1; set 0 to allow an empty group). Because a Yandex
`fixed_scale` group is resized by count, Yandex picks which node to drain
(gracefully); we only shrink when matching workloads are fully idle, so any
drained node is safe. Any `Pending` matching pod suppresses scale-down and
resets the cooldown — scale-up always wins.

**No double-ordering.** Capacity is never ordered twice for the same pods,
because every poll re-derives the answer from the cluster as it *currently* is:

- while a resize is in flight the loop does nothing at all (see the stability
  gate below), so pods waiting on nodes that are still being created cannot
  trigger a second resize;
- once those nodes are `Ready`, their free space is counted, so pods the
  scheduler has not placed yet already fit and no more nodes are ordered.

A pod that is *still* unschedulable after a resize has landed is therefore
re-considered rather than remembered as handled — the capacity it was given did
not fit it, and suppressing it permanently would leave it `Pending` forever with
the scaler reporting "no action".

**Only the managed group is measured.** Every capacity number comes from nodes
labeled `yandex.cloud/node-group-id=<node_group_id>`: the per-node size used to
divide the demand, the free capacity subtracted from it, the readiness gate, and
the empty-node count for scale-down. In a cluster with several node groups a
node of another group (a small system group, say) must never set the divisor —
that would over- or under-provision this group by whatever the size ratio
happens to be. Nodes of one Yandex node group share an instance template, so
they are the same size; if they ever disagree, the **smallest** is used, which
can only err toward adding a node too many rather than too few. If the group has
no `Ready` node at all (scaled to zero), `node_capacity_fallback` is used and a
warning is logged.

**Wait for in-flight resizes (stability gate).** A `fixed_scale` group reports its
*desired* size immediately, before the nodes actually join (or finish deleting).
Acting on that unrealized size makes the scaler fight its own operation — adding a
second node before the first arrives, or tearing down freshly-added nodes that pods
haven't landed on yet. So before issuing **any** resize the loop checks that the
group has reached its desired size (`Ready` nodes in the group == desired) and that
the previous resize operation has finished; while a resize is in flight it waits and
does nothing.

The `POST /evaluate` endpoint is a **manual override**: you tell it exactly how many
nodes to add, and it scales the group by that amount (still clamped to `max_size`).
It does not consult the pending-pod queue — that automatic behavior stays in the
background loop.

## Configure

Edit `config.yaml` (see the sample in the repo). Key fields:

- `yandex_cloud.service_account_key_file` — path to a YC service-account JSON key.
- `yandex_cloud.node_group_id` — the managed K8s node group to scale.
- `yandex_cloud.cluster_id` — the Managed Kubernetes cluster id (used to fetch the
  master endpoint and CA certificate).
- `yandex_cloud.master_endpoint` — `internal` (default; the scaler runs as a pod
  and reaches the master over the internal endpoint) or `external` (running
  off-cluster).
- `kubernetes.namespace` + `kubernetes.label_selectors` — which Pending pods count.
- `scaling.max_size`, `min_size`, `pending_pod_threshold`, `headroom`,
  `scale_down_cooldown_polls`, `poll_interval_seconds`, `dry_run`.
- `scaling.log_level` — `INFO` (default) logs every decision step; `DEBUG` adds
  per-pod requests and per-node allocatable/requested/free breakdowns. See
  [Inspecting decisions](#inspecting-decisions).

### Inspecting decisions

Every step that feeds a decision is logged, so a `dry_run: true` deployment can be
audited from the log alone. One poll at `INFO` reads top to bottom:

```
2026-08-14 09:12:41+0000 INFO    app.service: --- evaluate: group=cat-ml-gpu ns=default selectors=['tag=demo'] dry_run=True
2026-08-14 09:12:41+0000 INFO    app.k8s: Pending pods in ns=default: 3 total, 3 match ['tag=demo'], 3 still unscheduled
2026-08-14 09:12:41+0000 INFO    app.service: Pending pods: 3 unscheduled
2026-08-14 09:12:41+0000 INFO    app.k8s: Ready nodes in group cat-ml-gpu: 2 (cluster has 4 nodes in total)
2026-08-14 09:12:41+0000 INFO    app.service: Node group state: desired size 2, ready nodes 2, resize operation in progress: False
2026-08-14 09:12:41+0000 INFO    app.service: Pending pods request 24000m cpu / 48.00Gi memory in total
2026-08-14 09:12:41+0000 INFO    app.k8s: Node capacity of group cat-ml-gpu: 16000m cpu / 64.00Gi memory per node (from 2 Ready node(s))
2026-08-14 09:12:41+0000 INFO    app.k8s: Free capacity across 2 Ready node(s) of group cat-ml-gpu: 20000m cpu / 96.00Gi memory in total, largest single node 10000m cpu / 48.00Gi memory
2026-08-14 09:12:41+0000 INFO    app.scaling: decide: pending=3 (threshold 0), requests 24000m cpu / 48.00Gi memory, free in group 20000m cpu / 96.00Gi memory across 2 node(s), ...
2026-08-14 09:12:41+0000 INFO    app.scaling: decide: packing 3 pending pod(s) onto per-node free capacity ['10000m/48.00Gi', '10000m/48.00Gi'] -> 1 new node(s) needed, 0 pod(s) too big for any node
2026-08-14 09:12:41+0000 INFO    app.scaling: decide: unmet demand after free capacity: 4000m cpu / 0.00Gi memory -> 0.25 nodes by cpu, 0.00 nodes by memory; max * (1 + 0.10 headroom) = 0.28 -> ceil = 1; packing needs 1 -> 1 nodes needed
2026-08-14 09:12:41+0000 INFO    app.scaling: decide: 2 + 1 = 3 wanted, capped by max_size 20 -> target 3 (+1)
2026-08-14 09:12:41+0000 INFO    app.service: Decision: 3 pending pods need ~1 nodes (+10% headroom); scaling 2 -> 3
2026-08-14 09:12:41+0000 INFO    app.service: dry_run enabled; skipping resize to 3
```

Every line carries a local timestamp (with UTC offset), the level and the logger
that emitted it; uvicorn's own startup and access lines are timestamped the same
way.

`log_level: DEBUG` additionally names each pending pod with its requests, each
group node with its allocatable/requested/free split, and the node names behind
every count. Anything that could silently skew a decision — no `Ready` node in
the group, a node reporting no allocatable, mixed node sizes within the group, a
pod too big for any node — is a `WARNING`.

### Cluster connection

The scaler builds its Kubernetes connection entirely from the Yandex Cloud API —
it does not read a kubeconfig file or use in-cluster service-account RBAC. On
startup it calls the Managed Kubernetes API with the service-account key to fetch
the master endpoint and CA certificate, and mints a YC IAM token (refreshed
automatically before it expires) to authenticate every request.

**Prerequisite — the service account needs two distinct grants:**

- **`k8s.editor`** (cloud IAM) — to resize the node group. The scaler resizes via
  the YC gRPC API (`NodeGroupService.Update`), so this is a cloud-level role, not
  a Kubernetes one.
- **`k8s.cluster-api.cluster-admin`** — granted to the SA as a YC IAM role, this
  is what lets the minted IAM token call the Kubernetes API. The narrower
  `viewer`/`editor` roles are **not** enough: they map to Kubernetes' `view`/`edit`
  ClusterRoles, which only cover namespaced resources, whereas the scaler reads
  cluster-scoped ones — `nodes` and pods across all namespaces.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
CONFIG_PATH=config.yaml .venv/bin/uvicorn app.api:app --reload
```

## Endpoints

- `GET /healthz` — liveness.
- `GET /status` — current config + last decision.
- `POST /evaluate` — manually add a specific number of nodes (clamped to
  `max_size`, unless `dry_run`). Requires a JSON body `{"nodes_to_add": N}` where
  `N >= 1`; a missing or non-positive value returns `422`.

```bash
curl -X POST localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"nodes_to_add": 3}'
```

## Metrics

Prometheus metrics are served on their own port (`9090` by default), separate
from the API, so the scrape target never overlaps with `/evaluate`:

```yaml
metrics:
  enabled: true
  port: 9090
```

```bash
curl localhost:9090/metrics
```

The base Deployment exposes the port as `metrics` and carries the
`prometheus.io/{scrape,port,path}` pod annotations, so a cluster running the
annotation-based scrape config picks the pod up with no extra configuration.

Exposed are the scaling decision inputs (pending pods and their CPU/memory
demand, free capacity in the group, node size), the group state (desired size,
Ready nodes, resize in progress, configured bounds, `dry_run`), the decisions
themselves (`direction` x `result`, nodes added and removed, evaluations
skipped by a gate) and loop health (Yandex Cloud API errors per operation, IAM
token mints, poll iterations, errors and duration, last poll timestamp, build
info).

See **[MONITORING.md](MONITORING.md)** for the full metric reference, scrape
configuration, alerting rules and dashboard queries.

## Build the image

```bash
./build.sh                      # lite-scaler:latest
./build.sh lite-scaler v1.0    # custom name + tag
```

## Deploy to a cluster (Terraform + Kustomize)

The `deploy/` directory does a one-click, per-environment install. The work is
split by who needs the Yandex Cloud API and what is safe to keep in Git:

- **Terraform** (`deploy/terraform/`) provisions the cloud bits — a dedicated
  service account, its two IAM grants (`k8s.editor` +
  `k8s.cluster-api.cluster-admin`, see [Cluster connection](#cluster-connection)),
  the SA key, and the Kubernetes `Secret` (`lite-scaler-sa`) that carries it.
  The cluster and node group are **pre-existing** — Terraform only references them.
- **Kustomize** (`deploy/kustomize/`) owns the manifests — a `base/` (Deployment +
  default `config.yaml`) plus one overlay per environment
  (`overlays/{dev,prod}/`) that sets the per-env `config.yaml` and image tag.

```
deploy/
  install.sh                 # terraform apply + kubectl apply -k
  terraform/                 # SA, IAM bindings, key, kubernetes_secret
    envs/{dev,prod}.tfvars
  kustomize/
    base/                    # deployment + base config.yaml
    overlays/{dev,prod}/   # per-env config-patch.yaml + image tag
```

Per environment you edit `terraform/envs/<env>.tfvars`,
`kustomize/overlays/<env>/config-patch.yaml`, that overlay's
`kustomization.yaml`, and its `deployment-patch.yaml` (the `nodeSelector` that
pins the scaler to a stable node — not the managed group) — replacing the
`REPLACE_` placeholders. Then:

```bash
# Just authenticate the `yc` CLI as the operator. install.sh mints the YC token
# and points kubectl at the cluster (get-credentials --external) for you.
./deploy/install.sh prod plan     # terraform plan + offline kustomize build + best-effort dry-run
./deploy/install.sh prod          # terraform apply + kubectl apply -k
./deploy/install.sh prod destroy  # kubectl delete -k + terraform destroy (reverse order)
```

Full details and the rationale for the split are in [`deploy/README.md`](deploy/README.md).

## Test

```bash
.venv/bin/pytest -q
```

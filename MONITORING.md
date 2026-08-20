# Monitoring lite-scaler

Reference for the Prometheus metrics lite-scaler exposes: what each one means,
how to scrape them, and which alerts are worth having. For installing and
configuring the scaler itself, see [README.md](README.md).

## Endpoint

Metrics are served on a **separate port from the API** (`9090` by default) by a
dedicated HTTP server, so a scrape can never reach `/evaluate` and a slow scrape
can never block the poll loop.

```yaml
metrics:
  enabled: true    # set false to serve no metrics at all
  port: 9090       # must match the prometheus.io/port pod annotation
```

```bash
curl localhost:9090/metrics
```

Setting `enabled: false` skips the server entirely; the static gauges are still
populated in-process, they are simply never exposed.

## Scraping

The base Deployment already declares the port as `metrics` and sets the
annotations, so a cluster using annotation-based discovery needs no extra
configuration:

```yaml
metadata:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "9090"
    prometheus.io/path: /metrics
```

If your Prometheus does not scrape by annotation, target the pod directly:

```yaml
scrape_configs:
  - job_name: lite-scaler
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_label_app]
        regex: lite-scaler
        action: keep
      - source_labels: [__meta_kubernetes_pod_container_port_name]
        regex: metrics
        action: keep
```

With the Prometheus Operator, a `PodMonitor` on `port: metrics` is equivalent.

A scrape interval at or below `scaling.poll_interval_seconds` (default 30s) is
what you want — the gauges only change once per poll, so scraping slower than
the poll rate silently drops decisions from your graphs.

## Metric reference

### Decision inputs

Gauges, rewritten on every poll. These are the numbers the scaling decision is
computed from — if a resize surprises you, these explain it.

| Metric | Type | Meaning |
|---|---|---|
| `litescaler_pending_pods` | Gauge | Matching Pending pods with no node — the whole demand |
| `litescaler_pending_demand_cpu_millicores` | Gauge | Summed CPU requests of those pods |
| `litescaler_pending_demand_memory_bytes` | Gauge | Summed memory requests of those pods |
| `litescaler_group_free_cpu_millicores` | Gauge | Free CPU across Ready nodes of the group |
| `litescaler_group_free_memory_bytes` | Gauge | Free memory across Ready nodes of the group |
| `litescaler_node_capacity_cpu_millicores` | Gauge | CPU capacity of one node, the divisor in the sizing math |
| `litescaler_node_capacity_memory_bytes` | Gauge | Memory capacity of one node |

**The free/capacity gauges hold their previous value while a poll is gated.** A
gated poll returns before reading capacity, so publishing a zero there would
draw a cliff that looks like the group lost all of its capacity. Read them
together with `litescaler_resize_in_progress` to know whether they are fresh.

`node_capacity_*` comes from a Ready node in the group. When the group has no
Ready node — including at size 0 — it falls back to
`scaling.node_capacity_fallback` from the config. A value that does not match
your real machine type means the fallback is in use and is set wrong, which
will mis-size every resize.

### Group state

| Metric | Type | Meaning |
|---|---|---|
| `litescaler_node_group_size` | Gauge | Desired size (`fixed_scale.size` as Yandex Cloud reports it) |
| `litescaler_ready_nodes` | Gauge | Ready nodes in the group |
| `litescaler_resize_in_progress` | Gauge | 1 while a resize operation is running |
| `litescaler_max_size` | Gauge | `scaling.max_size` from the config |
| `litescaler_min_size` | Gauge | `scaling.min_size` from the config |
| `litescaler_dry_run` | Gauge | 1 when `dry_run` is on |
| `litescaler_build_info{version}` | Gauge | Always 1; the label carries the version |

`node_group_size` is the *desired* size, not the count of live nodes. The gap
between it and `ready_nodes` is the group converging; a gap that persists is a
node group that cannot bring nodes up.

`build_info` takes its version from the `LITESCALER_VERSION` environment
variable, falling back to the package version. Set it from the image tag in the
Deployment so a rollout is visible on the graph.

`dry_run` deserves an alert of its own — it is easy to leave on in an
environment that was supposed to go live, and the scaler looks completely
healthy while never resizing anything.

### Decisions and actions

Counters. These record what the scaler concluded and what it actually did.

| Metric | Type | Labels |
|---|---|---|
| `litescaler_scale_decisions_total` | Counter | `direction`, `result` |
| `litescaler_nodes_added_total` | Counter | `node_group_id` |
| `litescaler_nodes_removed_total` | Counter | `node_group_id` |
| `litescaler_evaluations_gated_total` | Counter | `reason` |

`direction` is what the poll wanted to do, even when nothing was applied:

| Value | Meaning |
|---|---|
| `up` | Wanted more nodes |
| `down` | Wanted to remove empty nodes |
| `none` | Nothing to do this poll |

`result` is what became of it. The values are mutually exclusive and assigned by
first match in this order:

| Value | Meaning |
|---|---|
| `gated` | The poll never decided — the group was mid-resize (see `evaluations_gated_total`) |
| `capped` | `max_size` or `min_size` clamped the target below what was needed |
| `dry_run` | A resize was decided but not sent, because `dry_run` is on |
| `applied` | Everything else, including quiet polls with nothing to do |

Note the two consequences of that ordering. A quiet poll where nothing needed
doing appears as `{direction="none", result="applied"}` — `applied` is the
residual bucket, so *not every `applied` is a resize*. And `capped` covers both
a resize that happened but was clamped smaller than needed, and one that could
not happen at all because the group was already at the bound; separate them with
`direction` and `litescaler_node_group_size`.

`nodes_added_total` / `nodes_removed_total` count only resizes actually sent to
Yandex Cloud — both are positive numbers. `dry_run` and gated polls never
increment them, so they cannot be inflated by a scaler that is not really
scaling. A capped resize *does* count the nodes it moved.

`evaluations_gated_total{reason}` counts polls skipped because the group was
busy:

| `reason` | Meaning |
|---|---|
| `transitioning` | `ready_nodes` does not equal the desired size — the group is converging |
| `operation_in_progress` | The last resize operation this process started has not completed |

Checked in that priority: `operation_in_progress` is tested first, so a poll
that is both only counts as `operation_in_progress`.

Sustained growth here while `litescaler_pending_pods > 0` is the signature
failure to alert on: pods are waiting and the scaler is frozen behind a resize
that is not finishing. It is the difference between "not scaling because it
decided not to" and "not scaling because it never got to decide".

### Yandex Cloud API and loop health

| Metric | Type | Labels |
|---|---|---|
| `litescaler_yc_api_errors_total` | Counter | `op` |
| `litescaler_iam_token_mints_total` | Counter | — |
| `litescaler_poll_iterations_total` | Counter | — |
| `litescaler_poll_errors_total` | Counter | — |
| `litescaler_poll_duration_seconds` | Histogram | `le` |
| `litescaler_last_poll_timestamp_seconds` | Gauge | — |

`op` on `yc_api_errors_total` identifies which gRPC call failed:

| `op` | Call |
|---|---|
| `get_size` | Reading the node group's current size |
| `update` | Sending the resize |
| `get_operation` | Polling a resize operation for completion |
| `iam_token` | Minting an IAM token |

Which `op` fails tells you what breaks. `get_size` failing means the scaler is
blind and does nothing. `update` failing means it decided correctly and could
not act. `iam_token` failing takes out everything, including the Kubernetes
client, since the same credentials authenticate both.

`iam_token_mints_total` should climb slowly — tokens last an hour and are reused
until five minutes before expiry. A mint rate near the poll rate means caching
is broken and you are one IAM rate limit away from an outage.

`poll_iterations_total` proves the loop is alive; `poll_errors_total` counts
iterations that raised. The loop is deliberately built never to die, so an
exception increments the error counter and the next poll runs as usual —
`poll_errors_total` rising steadily is a scaler that is *running* and
*continuously failing*, which no liveness probe will catch.

`poll_duration_seconds` is measured on the monotonic clock, with buckets at
0.25, 0.5, 1, 2.5, 5, 10, 15, 30, 60 and 120 seconds. Durations approaching
`poll_interval_seconds` mean polls are backing up.

`last_poll_timestamp_seconds` is Unix wall-clock time, specifically so a
staleness alert can compare it against Prometheus' own `time()`. It is the one
metric that catches a wedged loop — a poll blocked forever inside a gRPC call
stops updating this while the process stays up and every other metric holds its
last value.

## Alerts

```yaml
groups:
  - name: lite-scaler
    rules:
      - alert: LiteScalerNotPolling
        expr: time() - litescaler_last_poll_timestamp_seconds > 180
        for: 5m
        annotations:
          summary: lite-scaler has not completed a poll in over 3 minutes

      - alert: LiteScalerFrozenWithPendingPods
        expr: |
          litescaler_pending_pods > 0
          and increase(litescaler_evaluations_gated_total[15m]) > 10
        for: 15m
        annotations:
          summary: Pods are pending while every evaluation is gated

      - alert: LiteScalerPinnedAtMaxSize
        expr: |
          increase(
            litescaler_scale_decisions_total{direction="up",result="capped"}[30m]
          ) > 5
        for: 30m
        annotations:
          summary: Scale-ups are being clamped by max_size; demand exceeds the cap

      - alert: LiteScalerApiErrors
        expr: rate(litescaler_yc_api_errors_total[10m]) > 0
        for: 10m
        annotations:
          summary: "Yandex Cloud API calls failing: {{ $labels.op }}"

      - alert: LiteScalerPollsFailing
        expr: rate(litescaler_poll_errors_total[15m]) > 0
        for: 15m
        annotations:
          summary: The poll loop is raising exceptions on every iteration

      - alert: LiteScalerStuckInDryRun
        expr: litescaler_dry_run == 1
        for: 1h
        annotations:
          summary: lite-scaler has been in dry_run for an hour

      - alert: LiteScalerGroupNotConverging
        expr: litescaler_node_group_size != litescaler_ready_nodes
        for: 30m
        annotations:
          summary: Desired size and Ready nodes have disagreed for 30 minutes

      - alert: LiteScalerTokenChurn
        expr: rate(litescaler_iam_token_mints_total[30m]) * 3600 > 10
        annotations:
          summary: IAM tokens are being minted far more often than hourly
```

Tune `LiteScalerNotPolling` to your own `poll_interval_seconds` — the threshold
above assumes the 30s default and allows several missed polls before firing.

## Dashboard queries

Unmet demand, in nodes — how far behind the scaler is right now:

```promql
max(
  litescaler_pending_demand_cpu_millicores / litescaler_node_capacity_cpu_millicores,
  litescaler_pending_demand_memory_bytes / litescaler_node_capacity_memory_bytes
)
```

Headroom actually available in the group, as a fraction of one node:

```promql
litescaler_group_free_cpu_millicores / litescaler_node_capacity_cpu_millicores
```

Decision mix over time, which is the fastest read on whether the scaler is
working:

```promql
sum by (direction, result) (rate(litescaler_scale_decisions_total[30m]))
```

Net node churn:

```promql
  sum(rate(litescaler_nodes_added_total[1h]))
- sum(rate(litescaler_nodes_removed_total[1h]))
```

Distance to the configured ceiling:

```promql
litescaler_max_size - litescaler_node_group_size
```

Poll latency tail:

```promql
histogram_quantile(
  0.95, sum by (le) (rate(litescaler_poll_duration_seconds_bucket[30m]))
)
```

## Reading the metrics together

A few combinations mean more than any single series.

**Pods pending and nothing happening.** Check `result` on the recent decisions.
`gated` means the group is busy — look at `evaluations_gated_total{reason}`.
`capped` means `max_size` is the binding constraint. `applied` with
`direction="none"` means the scaler genuinely thinks the pods fit, so compare
`pending_demand_*` against `group_free_*`: they fit in aggregate but may be
fragmented across nodes such that no single node can take any one pod.

**Node count oscillating.** Compare the `nodes_added_total` and
`nodes_removed_total` rates. Roughly equal rates over a window where demand was
steady means `scale_down_cooldown_polls` is too low for your workload's arrival
pattern, and the scaler is removing nodes it is about to need again.

**Every metric frozen but the pod is Ready.** `last_poll_timestamp_seconds`
going stale while `poll_iterations_total` stops rising is a loop wedged inside a
call with no timeout. The process stays up and every gauge keeps serving its
last value, so only these two catch it.

**Sizing looks wrong.** Check `node_capacity_*` first. If it does not match your
machine type, the group had no Ready node when it was read and
`node_capacity_fallback` is in use — every resize is being sized against a node
shape that does not exist.

## Known gaps

`litescaler_pending_pods` counts **all** matching Pending pods every poll, with
no memory across polls. There is no dedup set yet, so a pod that is already
being scaled for is counted again on the next poll until it schedules. Two
metrics from the original design depend on that set and are therefore **not
exposed**: `litescaler_pending_pods_unhandled` (pending pods not yet counted
toward a scale-up) and `litescaler_handled_pods` (the dedup set's size, whose
growth would show Pending pods sticking). Until they exist, the closest signal
for a stuck queue is `litescaler_pending_pods` staying flat and non-zero across
several polls while `scale_decisions_total` records no `up`.

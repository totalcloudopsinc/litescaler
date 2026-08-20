"""Prometheus instrumentation for lite-scaler."""

from __future__ import annotations

import os

from prometheus_client import Counter, CollectorRegistry, Gauge, Histogram

registry = CollectorRegistry()

_GAUGES: dict[str, Gauge] = {}

_GAUGE_SPECS = (
    ("litescaler_pending_pods", "Pending pods without a node (whole demand)"),
    ("litescaler_pending_demand_cpu_millicores", "Total CPU request of pending pods"),
    ("litescaler_pending_demand_memory_bytes", "Total memory request of pending pods"),
    ("litescaler_group_free_cpu_millicores", "Free CPU on Ready nodes of the group"),
    ("litescaler_group_free_memory_bytes", "Free memory on Ready nodes of the group"),
    ("litescaler_node_capacity_cpu_millicores", "CPU capacity of one node"),
    ("litescaler_node_capacity_memory_bytes", "Memory capacity of one node"),
    ("litescaler_node_group_size", "Desired node group size (fixed_scale.size)"),
    ("litescaler_ready_nodes", "Ready nodes in the group"),
    ("litescaler_resize_in_progress", "1 while a resize operation is running"),
    ("litescaler_max_size", "Configured max_size"),
    ("litescaler_min_size", "Configured min_size"),
    ("litescaler_dry_run", "1 when dry_run is enabled"),
    ("litescaler_last_poll_timestamp_seconds", "Unix time the last poll finished"),
)

_LABELLED_GAUGE_SPECS = (
    ("litescaler_build_info", "Build metadata; always 1", ("version",)),
)

_COUNTER_SPECS = (
    ("litescaler_scale_decisions_total", "Outcome of each poll",
     ("direction", "result")),
    ("litescaler_nodes_added_total", "Nodes added in total", ("node_group_id",)),
    ("litescaler_nodes_removed_total", "Nodes removed in total", ("node_group_id",)),
    ("litescaler_evaluations_gated_total", "Evaluations skipped by a gate",
     ("reason",)),
    ("litescaler_yc_api_errors_total", "Failed Yandex Cloud gRPC calls", ("op",)),
    ("litescaler_iam_token_mints_total", "IAM tokens minted", ()),
    ("litescaler_poll_iterations_total", "Poll loop iterations", ()),
    ("litescaler_poll_errors_total", "Exceptions raised by the poll loop", ()),
)

_POLL_DURATION_BUCKETS = (
    0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, 60.0, 120.0, float("inf"),
)

_COUNTERS: dict[str, Counter] = {}
_HISTOGRAMS: dict[str, Histogram] = {}

_RESULT_GATED = "gated"
_RESULT_CAPPED = "capped"
_RESULT_DRY_RUN = "dry_run"
_RESULT_APPLIED = "applied"


def reset() -> None:
    global registry
    registry = CollectorRegistry()
    _GAUGES.clear()
    _COUNTERS.clear()
    _HISTOGRAMS.clear()
    for name, doc in _GAUGE_SPECS:
        _GAUGES[name] = Gauge(name, doc, registry=registry)
    for name, doc, labels in _LABELLED_GAUGE_SPECS:
        _GAUGES[name] = Gauge(name, doc, labels, registry=registry)
    for name, doc, labels in _COUNTER_SPECS:
        _COUNTERS[name] = Counter(name, doc, labels, registry=registry)
    _HISTOGRAMS["litescaler_poll_duration_seconds"] = Histogram(
        "litescaler_poll_duration_seconds",
        "Wall time of one evaluate() call",
        buckets=_POLL_DURATION_BUCKETS,
        registry=registry,
    )


def observe_poll(
    *,
    pending_pods: int,
    demand_cpu_millicores: int,
    demand_memory_bytes: int,
    node_group_size: int,
    ready_nodes: int,
    resize_in_progress: bool,
) -> None:
    _GAUGES["litescaler_pending_pods"].set(pending_pods)
    _GAUGES["litescaler_pending_demand_cpu_millicores"].set(demand_cpu_millicores)
    _GAUGES["litescaler_pending_demand_memory_bytes"].set(demand_memory_bytes)
    _GAUGES["litescaler_node_group_size"].set(node_group_size)
    _GAUGES["litescaler_ready_nodes"].set(ready_nodes)
    _GAUGES["litescaler_resize_in_progress"].set(1 if resize_in_progress else 0)


def observe_capacity(
    *,
    free_cpu_millicores: int,
    free_memory_bytes: int,
    node_capacity_cpu_millicores: int,
    node_capacity_memory_bytes: int,
) -> None:
    _GAUGES["litescaler_group_free_cpu_millicores"].set(free_cpu_millicores)
    _GAUGES["litescaler_group_free_memory_bytes"].set(free_memory_bytes)
    _GAUGES["litescaler_node_capacity_cpu_millicores"].set(node_capacity_cpu_millicores)
    _GAUGES["litescaler_node_capacity_memory_bytes"].set(node_capacity_memory_bytes)


def _result_label(
    *, gated_reason: str | None, capped: bool, should_scale: bool, dry_run: bool
) -> str:
    if gated_reason:
        return _RESULT_GATED
    if capped:
        return _RESULT_CAPPED
    if should_scale and dry_run:
        return _RESULT_DRY_RUN
    return _RESULT_APPLIED


def record_decision(
    *,
    direction: str,
    nodes_to_add: int,
    capped: bool,
    should_scale: bool,
    dry_run: bool,
    node_group_id: str,
    gated_reason: str | None = None,
) -> None:
    result = _result_label(
        gated_reason=gated_reason, capped=capped,
        should_scale=should_scale, dry_run=dry_run,
    )
    _COUNTERS["litescaler_scale_decisions_total"].labels(
        direction=direction, result=result
    ).inc()

    if gated_reason:
        _COUNTERS["litescaler_evaluations_gated_total"].labels(
            reason=gated_reason
        ).inc()

    if result != _RESULT_APPLIED or not should_scale:
        return
    if nodes_to_add > 0:
        _COUNTERS["litescaler_nodes_added_total"].labels(
            node_group_id=node_group_id
        ).inc(nodes_to_add)
    elif nodes_to_add < 0:
        _COUNTERS["litescaler_nodes_removed_total"].labels(
            node_group_id=node_group_id
        ).inc(-nodes_to_add)


def record_poll(
    *, duration_seconds: float, finished_at: float, failed: bool
) -> None:
    _COUNTERS["litescaler_poll_iterations_total"].inc()
    if failed:
        _COUNTERS["litescaler_poll_errors_total"].inc()
    _HISTOGRAMS["litescaler_poll_duration_seconds"].observe(duration_seconds)
    _GAUGES["litescaler_last_poll_timestamp_seconds"].set(finished_at)


def record_yc_error(op: str) -> None:
    _COUNTERS["litescaler_yc_api_errors_total"].labels(op=op).inc()


def record_iam_token_mint() -> None:
    _COUNTERS["litescaler_iam_token_mints_total"].inc()


def version() -> str:
    from app import __version__

    return os.environ.get("LITESCALER_VERSION") or __version__


def set_static(
    *, max_size: int, min_size: int, dry_run: bool, version: str
) -> None:
    _GAUGES["litescaler_max_size"].set(max_size)
    _GAUGES["litescaler_min_size"].set(min_size)
    _GAUGES["litescaler_dry_run"].set(1 if dry_run else 0)
    _GAUGES["litescaler_build_info"].labels(version=version).set(1)


reset()

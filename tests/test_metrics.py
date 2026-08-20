import pytest

from app import metrics


@pytest.fixture(autouse=True)
def fresh_registry():
    metrics.reset()
    yield


def _value(name, **labels):
    return metrics.registry.get_sample_value(name, labels or None)


def test_observe_poll_sets_the_demand_and_group_state_gauges():
    metrics.observe_poll(
        pending_pods=4,
        demand_cpu_millicores=3500,
        demand_memory_bytes=8 * 1024**3,
        node_group_size=3,
        ready_nodes=2,
        resize_in_progress=True,
    )

    assert _value("litescaler_pending_pods") == 4
    assert _value("litescaler_pending_demand_cpu_millicores") == 3500
    assert _value("litescaler_pending_demand_memory_bytes") == 8 * 1024**3
    assert _value("litescaler_node_group_size") == 3
    assert _value("litescaler_ready_nodes") == 2
    assert _value("litescaler_resize_in_progress") == 1


def test_observe_capacity_sets_the_free_and_node_size_gauges():
    metrics.observe_capacity(
        free_cpu_millicores=1200,
        free_memory_bytes=2 * 1024**3,
        node_capacity_cpu_millicores=2000,
        node_capacity_memory_bytes=8 * 1024**3,
    )

    assert _value("litescaler_group_free_cpu_millicores") == 1200
    assert _value("litescaler_group_free_memory_bytes") == 2 * 1024**3
    assert _value("litescaler_node_capacity_cpu_millicores") == 2000
    assert _value("litescaler_node_capacity_memory_bytes") == 8 * 1024**3


def test_applied_scale_up_counts_decision_and_nodes_added():
    metrics.record_decision(
        direction="up", nodes_to_add=2, capped=False, should_scale=True,
        dry_run=False, node_group_id="cat1",
    )

    assert _value(
        "litescaler_scale_decisions_total", direction="up", result="applied"
    ) == 1
    assert _value("litescaler_nodes_added_total", node_group_id="cat1") == 2
    assert _value("litescaler_nodes_removed_total", node_group_id="cat1") is None


def test_applied_scale_down_counts_nodes_removed_as_a_positive_number():
    metrics.record_decision(
        direction="down", nodes_to_add=-3, capped=False, should_scale=True,
        dry_run=False, node_group_id="cat1",
    )

    assert _value(
        "litescaler_scale_decisions_total", direction="down", result="applied"
    ) == 1
    assert _value("litescaler_nodes_removed_total", node_group_id="cat1") == 3


def test_dry_run_scale_up_is_not_counted_as_nodes_added():
    metrics.record_decision(
        direction="up", nodes_to_add=2, capped=False, should_scale=True,
        dry_run=True, node_group_id="cat1",
    )

    assert _value(
        "litescaler_scale_decisions_total", direction="up", result="dry_run"
    ) == 1
    assert _value("litescaler_nodes_added_total", node_group_id="cat1") is None


def test_capped_outranks_dry_run_and_applied():
    metrics.record_decision(
        direction="up", nodes_to_add=1, capped=True, should_scale=True,
        dry_run=False, node_group_id="cat1",
    )

    assert _value(
        "litescaler_scale_decisions_total", direction="up", result="capped"
    ) == 1


def test_gated_decision_outranks_everything_and_counts_its_reason():
    metrics.record_decision(
        direction="none", nodes_to_add=0, capped=True, should_scale=False,
        dry_run=True, node_group_id="cat1", gated_reason="transitioning",
    )

    assert _value(
        "litescaler_scale_decisions_total", direction="none", result="gated"
    ) == 1
    assert _value(
        "litescaler_evaluations_gated_total", reason="transitioning"
    ) == 1


def test_quiet_poll_records_a_none_applied_decision():
    metrics.record_decision(
        direction="none", nodes_to_add=0, capped=False, should_scale=False,
        dry_run=False, node_group_id="cat1",
    )

    assert _value(
        "litescaler_scale_decisions_total", direction="none", result="applied"
    ) == 1


def test_record_poll_counts_the_iteration_and_stamps_the_clock():
    metrics.record_poll(duration_seconds=1.5, finished_at=1000.0, failed=False)

    assert _value("litescaler_poll_iterations_total") == 1
    assert _value("litescaler_poll_errors_total") == 0
    assert _value("litescaler_last_poll_timestamp_seconds") == 1000.0
    assert _value("litescaler_poll_duration_seconds_sum") == 1.5
    assert _value("litescaler_poll_duration_seconds_count") == 1


def test_failed_poll_still_counts_as_an_iteration():
    metrics.record_poll(duration_seconds=0.2, finished_at=1000.0, failed=True)

    assert _value("litescaler_poll_iterations_total") == 1
    assert _value("litescaler_poll_errors_total") == 1


def test_yc_api_errors_are_counted_per_operation():
    metrics.record_yc_error("update")
    metrics.record_yc_error("update")
    metrics.record_yc_error("iam_token")

    assert _value("litescaler_yc_api_errors_total", op="update") == 2
    assert _value("litescaler_yc_api_errors_total", op="iam_token") == 1


def test_iam_token_mints_are_counted():
    metrics.record_iam_token_mint()

    assert _value("litescaler_iam_token_mints_total") == 1


def test_static_config_gauges_and_build_info():
    metrics.set_static(max_size=20, min_size=1, dry_run=True, version="1.2.3")

    assert _value("litescaler_max_size") == 20
    assert _value("litescaler_min_size") == 1
    assert _value("litescaler_dry_run") == 1
    assert _value("litescaler_build_info", version="1.2.3") == 1


def test_version_falls_back_to_the_package_version(monkeypatch):
    monkeypatch.delenv("LITESCALER_VERSION", raising=False)
    from app import __version__

    assert metrics.version() == __version__


def test_version_can_be_overridden_by_the_environment(monkeypatch):
    monkeypatch.setenv("LITESCALER_VERSION", "2026.08.20-abc123")

    assert metrics.version() == "2026.08.20-abc123"


def test_registry_renders_a_prometheus_scrape():
    from prometheus_client import generate_latest

    metrics.set_static(max_size=5, min_size=0, dry_run=False, version="0.1.0")
    metrics.observe_poll(
        pending_pods=1, demand_cpu_millicores=100, demand_memory_bytes=200,
        node_group_size=1, ready_nodes=1, resize_in_progress=False,
    )
    body = generate_latest(metrics.registry).decode()

    assert "litescaler_pending_pods 1.0" in body
    assert 'litescaler_build_info{version="0.1.0"} 1.0' in body
    assert "# TYPE litescaler_poll_duration_seconds histogram" in body
    assert "# TYPE litescaler_scale_decisions_total counter" in body


def test_poll_duration_buckets_cover_slow_polls():
    metrics.record_poll(duration_seconds=45.0, finished_at=1.0, failed=False)

    for le in ("15.0", "30.0", "60.0", "120.0"):
        assert _value("litescaler_poll_duration_seconds_bucket", le=le) is not None
    assert _value("litescaler_poll_duration_seconds_bucket", le="30.0") == 0
    assert _value("litescaler_poll_duration_seconds_bucket", le="60.0") == 1

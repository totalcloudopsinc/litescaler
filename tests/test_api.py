import pytest
from pydantic import ValidationError

from app.api import ScaleRequest


def test_scale_request_accepts_positive_count():
    assert ScaleRequest(nodes_to_add=3).nodes_to_add == 3


@pytest.mark.parametrize("bad", [0, -1])
def test_scale_request_rejects_non_positive(bad):
    with pytest.raises(ValidationError):
        ScaleRequest(nodes_to_add=bad)


def test_scale_request_requires_field():
    with pytest.raises(ValidationError):
        ScaleRequest()

import asyncio
from unittest.mock import MagicMock

from app import metrics
from app.api import _run_one_poll, start_metrics_server
from app.config import (
    Config, KubernetesConfig, MetricsConfig, ScalingConfig, YandexCloudConfig,
)


@pytest.fixture
def fresh_metrics():
    metrics.reset()
    yield metrics


def _sample(name, **labels):
    return metrics.registry.get_sample_value(name, labels or None)


def _config(**metrics_kw):
    return Config(
        yandex_cloud=YandexCloudConfig(
            service_account_key_file="/x", node_group_id="cat1", cluster_id="cl-1"
        ),
        kubernetes=KubernetesConfig(namespace="ml", label_selectors=["team=ml"]),
        scaling=ScalingConfig(max_size=20, min_size=2, dry_run=True),
        metrics=MetricsConfig(**metrics_kw),
    )


def test_a_successful_poll_counts_an_iteration_and_stamps_the_clock(fresh_metrics):
    service = MagicMock()
    clock = iter([100.0, 102.5])

    asyncio.run(_run_one_poll(
        service, monotonic=lambda: next(clock), wall_clock=lambda: 1_700_000_000.0
    ))

    service.evaluate.assert_called_once_with()
    assert _sample("litescaler_poll_iterations_total") == 1
    assert _sample("litescaler_poll_errors_total") == 0
    assert _sample("litescaler_poll_duration_seconds_sum") == 2.5
    assert _sample("litescaler_last_poll_timestamp_seconds") == 1_700_000_000.0


def test_a_failing_poll_counts_an_error_and_does_not_propagate(fresh_metrics):
    service = MagicMock()
    service.evaluate.side_effect = RuntimeError("kube down")
    clock = iter([100.0, 100.5])

    asyncio.run(_run_one_poll(
        service, monotonic=lambda: next(clock), wall_clock=lambda: 1_700_000_000.0
    ))

    assert _sample("litescaler_poll_iterations_total") == 1
    assert _sample("litescaler_poll_errors_total") == 1
    assert _sample("litescaler_poll_duration_seconds_count") == 1


def test_start_metrics_server_publishes_the_static_config_gauges(
    monkeypatch, fresh_metrics
):
    started = {}
    monkeypatch.setattr(
        "app.api.start_http_server",
        lambda port, registry: started.update(port=port, registry=registry),
    )
    monkeypatch.setenv("LITESCALER_VERSION", "9.9.9")

    start_metrics_server(_config(port=9110))

    assert started["port"] == 9110
    assert started["registry"] is metrics.registry
    assert _sample("litescaler_max_size") == 20
    assert _sample("litescaler_min_size") == 2
    assert _sample("litescaler_dry_run") == 1
    assert _sample("litescaler_build_info", version="9.9.9") == 1


def test_disabled_metrics_do_not_start_a_server(monkeypatch, fresh_metrics):
    started = []
    monkeypatch.setattr(
        "app.api.start_http_server", lambda port, registry: started.append(port)
    )

    start_metrics_server(_config(enabled=False))

    assert started == []

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import (
    Config, YandexCloudConfig, KubernetesConfig, ScalingConfig,
)
from app.k8s import NodeFree
from app.service import ScalerService


def _config(**scaling_kw):
    return Config(
        yandex_cloud=YandexCloudConfig(
            service_account_key_file="/x", node_group_id="cat1", cluster_id="cl-1"
        ),
        kubernetes=KubernetesConfig(namespace="ml", label_selectors=["team=ml"]),
        scaling=ScalingConfig(max_size=20, **scaling_kw),
    )


def _pods(n, start=0):
    return [
        SimpleNamespace(metadata=SimpleNamespace(uid=f"pod-{i}"))
        for i in range(start, start + n)
    ]


def _service(
    config, pending_pods, node_capacity, current_size, empty_nodes=0,
    ready_nodes=None,
):
    kube = MagicMock()
    kube.list_pending_pods.return_value = pending_pods
    kube.get_node_capacity.return_value = node_capacity
    kube.empty_group_node_count.return_value = empty_nodes
    kube.ready_group_node_count.return_value = (
        current_size if ready_nodes is None else ready_nodes
    )
    kube.group_free_capacity.return_value = []
    yc = MagicMock()
    yc.get_current_size.return_value = current_size
    yc.operation_in_progress.return_value = False
    svc = ScalerService(config=config, kube=kube, yc=yc)
    return svc, kube, yc


def test_evaluate_scales_when_needed(monkeypatch):
    config = _config()
    svc, kube, yc = _service(config, _pods(4), (4000, 16 * 1024**3), 2)
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1024**3 * len(p))
    )

    decision = svc.evaluate()

    assert decision.should_scale is True
    assert decision.target_size == 5
    yc.set_size.assert_called_once_with(5)
    assert svc.last_decision is decision


def test_evaluate_no_scale_below_threshold(monkeypatch):
    config = _config(pending_pod_threshold=3)
    svc, kube, yc = _service(config, _pods(2), (4000, 16 * 1024**3), 2)
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (1000, 1 * 1024**3)
    )

    decision = svc.evaluate()

    assert decision.should_scale is False
    yc.set_size.assert_not_called()


def test_evaluate_scales_for_single_pending_pod(monkeypatch):
    config = _config()
    svc, kube, yc = _service(config, _pods(1), (4000, 16 * 1024**3), 2)
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000, 1 * 1024**3)
    )

    decision = svc.evaluate()

    assert decision.should_scale is True
    assert decision.pending_count == 1
    yc.set_size.assert_called_once()


def test_evaluate_dry_run_does_not_resize(monkeypatch):
    config = _config(dry_run=True)
    svc, kube, yc = _service(config, _pods(4), (4000, 16 * 1024**3), 2)
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1024**3 * len(p))
    )

    decision = svc.evaluate()

    assert decision.should_scale is True
    yc.set_size.assert_not_called()


def test_evaluate_uses_capacity_fallback_when_no_node(monkeypatch):
    config = _config()
    svc, kube, yc = _service(config, _pods(4), None, 2)  # no Ready node
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1024**3 * len(p))
    )

    decision = svc.evaluate()
    assert decision.target_size == 5


def test_evaluate_reads_capacity_of_the_target_node_group(monkeypatch):
    config = _config()
    svc, kube, yc = _service(config, _pods(4), (4000, 16 * 1024**3), 2)
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1024**3 * len(p))
    )

    svc.evaluate()

    kube.get_node_capacity.assert_called_once_with(
        config.yandex_cloud.node_group_id
    )


def test_evaluate_does_not_rescale_while_previous_resize_lands(monkeypatch):
    config = _config()
    pods = _pods(4)
    svc, kube, yc = _service(config, pods, (4000, 16 * 1024**3), 2)
    yc.get_current_size.side_effect = [2, 5]  # resize requested after poll 1
    kube.ready_group_node_count.return_value = 2  # new nodes not up yet
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1 * 1024**3)
    )

    first = svc.evaluate()
    assert first.should_scale is True
    yc.set_size.assert_called_once()

    second = svc.evaluate()
    assert second.should_scale is False
    yc.set_size.assert_called_once()  # not called again


def test_evaluate_rescales_pods_the_landed_resize_did_not_place(monkeypatch):
    config = _config()
    pods = _pods(4)
    svc, kube, yc = _service(config, pods, (4000, 16 * 1024**3), 2)
    yc.get_current_size.side_effect = [2, 5]
    kube.ready_group_node_count.side_effect = [2, 5]  # resize landed
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1 * 1024**3)
    )

    svc.evaluate()
    second = svc.evaluate()

    assert second.should_scale is True
    assert second.pending_count == 4
    assert yc.set_size.call_count == 2


def test_evaluate_does_not_rescale_when_the_new_nodes_have_room(monkeypatch):
    config = _config()
    pods = _pods(4)
    svc, kube, yc = _service(config, pods, (4000, 16 * 1024**3), 2)
    yc.get_current_size.side_effect = [2, 5]
    kube.ready_group_node_count.side_effect = [2, 5]
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1 * 1024**3)
    )

    svc.evaluate()
    kube.group_free_capacity.return_value = [
        NodeFree(f"n{i}", 4000, 16 * 1024**3) for i in range(3)
    ]
    second = svc.evaluate()

    assert second.should_scale is False
    assert yc.set_size.call_count == 1


def test_evaluate_scales_when_free_capacity_is_fragmented(monkeypatch):
    config = _config(pending_pod_threshold=0)
    pods = _pods(2)
    svc, kube, yc = _service(config, pods, (7910, 32 * 1024**3), 7)
    kube.group_free_capacity.return_value = [
        NodeFree(f"worker-dev-{i}", 1440, 5 * 1024**3) for i in range(7)
    ]
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1024**3 * len(p))
    )

    decision = svc.evaluate()

    assert decision.should_scale is True
    assert decision.target_size == 8
    yc.set_size.assert_called_once_with(8)


def test_evaluate_skips_scale_up_while_node_group_transitioning(monkeypatch):
    config = _config()
    svc, kube, yc = _service(
        config, _pods(2), (4000, 16 * 1024**3), current_size=1, ready_nodes=0
    )
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1 * 1024**3)
    )

    decision = svc.evaluate()

    assert decision.should_scale is False
    yc.set_size.assert_not_called()


def test_evaluate_skips_scale_down_while_node_group_transitioning():
    config = _config(min_size=0, scale_down_cooldown_polls=1)
    svc, kube, yc = _service(
        config, [], (4000, 16 * 1024**3), current_size=2,
        empty_nodes=2, ready_nodes=1,
    )

    decision = svc.evaluate()

    assert decision.should_scale is False
    yc.set_size.assert_not_called()


def test_evaluate_no_scale_up_when_ready_node_has_free_capacity(monkeypatch):
    config = _config()
    pods = _pods(2, start=200)
    svc, kube, yc = _service(
        config, pods, (4000, 16 * 1024**3), current_size=1, ready_nodes=1
    )

    kube.group_free_capacity.return_value = [NodeFree("n1", 4000, 16 * 1024**3)]
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (1000 * len(p), 1 * 1024**3)
    )

    decision = svc.evaluate()

    assert decision.should_scale is False
    yc.set_size.assert_not_called()


def test_evaluate_skips_scale_while_operation_in_progress(monkeypatch):
    config = _config()
    svc, kube, yc = _service(config, _pods(2), (4000, 16 * 1024**3), 2)
    yc.operation_in_progress.return_value = True
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1 * 1024**3)
    )

    decision = svc.evaluate()

    assert decision.should_scale is False
    yc.set_size.assert_not_called()


def test_scale_by_resizes_to_clamped_target():
    config = _config()
    svc, kube, yc = _service(config, [], (4000, 16 * 1024**3), 2)

    decision = svc.scale_by(3)

    assert decision.should_scale is True
    assert decision.target_size == 5
    yc.set_size.assert_called_once_with(5)
    assert svc.last_decision is decision
    kube.list_pending_pods.assert_not_called()


def test_scale_by_respects_max_size_cap():
    config = _config()
    svc, kube, yc = _service(config, [], (4000, 16 * 1024**3), 18)

    decision = svc.scale_by(10)

    assert decision.target_size == 20
    yc.set_size.assert_called_once_with(20)


def test_scale_by_dry_run_does_not_resize():
    config = _config(dry_run=True)
    svc, kube, yc = _service(config, [], (4000, 16 * 1024**3), 2)

    decision = svc.scale_by(3)

    assert decision.should_scale is True
    yc.set_size.assert_not_called()


def test_scale_by_at_max_does_not_resize():
    config = _config()
    svc, kube, yc = _service(config, [], (4000, 16 * 1024**3), 20)

    decision = svc.scale_by(5)

    assert decision.should_scale is False
    yc.set_size.assert_not_called()


def test_scale_down_fires_after_cooldown():
    config = _config(min_size=0, scale_down_cooldown_polls=3)
    svc, kube, yc = _service(config, [], (4000, 16 * 1024**3), 5, empty_nodes=2)

    assert svc.evaluate().should_scale is False
    assert svc.evaluate().should_scale is False
    yc.set_size.assert_not_called()

    third = svc.evaluate()
    assert third.should_scale is True
    assert third.target_size == 3
    yc.set_size.assert_called_once_with(3)

    yc.set_size.reset_mock()
    assert svc.evaluate().should_scale is False
    yc.set_size.assert_not_called()


def test_pending_pods_reset_idle_counter(monkeypatch):
    config = _config(min_size=0, scale_down_cooldown_polls=2)
    svc, kube, yc = _service(config, [], (4000, 16 * 1024**3), 5, empty_nodes=2)
    monkeypatch.setattr(
        "app.service.sum_pod_requests", lambda p: (2000 * len(p), 1 * 1024**3)
    )
    kube.list_pending_pods.side_effect = [[], _pods(1), [], []]

    svc.evaluate()
    svc.evaluate()
    assert svc.evaluate().should_scale is False
    fourth = svc.evaluate()
    assert fourth.should_scale is True
    assert fourth.target_size == 3


def test_scale_down_respects_min_size():
    config = _config(min_size=4, scale_down_cooldown_polls=1)
    svc, kube, yc = _service(config, [], (4000, 16 * 1024**3), 5, empty_nodes=3)

    decision = svc.evaluate()
    assert decision.should_scale is True
    assert decision.target_size == 4
    yc.set_size.assert_called_once_with(4)


def test_scale_down_dry_run_does_not_resize():
    config = _config(min_size=0, scale_down_cooldown_polls=1, dry_run=True)
    svc, kube, yc = _service(config, [], (4000, 16 * 1024**3), 5, empty_nodes=2)

    decision = svc.evaluate()
    assert decision.should_scale is True
    yc.set_size.assert_not_called()


def test_no_scale_down_when_no_empty_nodes():
    config = _config(min_size=0, scale_down_cooldown_polls=1)
    svc, kube, yc = _service(config, [], (4000, 16 * 1024**3), 5, empty_nodes=0)

    decision = svc.evaluate()
    assert decision.should_scale is False
    yc.set_size.assert_not_called()


def test_scale_by_resets_idle_cooldown():
    config = _config(min_size=0, scale_down_cooldown_polls=2)
    svc, kube, yc = _service(config, [], (4000, 16 * 1024**3), 5, empty_nodes=2)

    svc.evaluate()
    svc.scale_by(2)
    assert svc._idle_polls == 0


def test_from_config_wires_yc_credentials_into_kube_client(monkeypatch):
    import app.service as service_module

    captured = {}

    class FakeKubeClient:
        def __init__(self, credentials):
            captured["kube_credentials"] = credentials

    class FakeYcKubeCredentials:
        def __init__(self, service_account_key_file, cluster_id, endpoint_type):
            captured["cred_args"] = (
                service_account_key_file, cluster_id, endpoint_type
            )

    class FakeYcClient:
        def __init__(self, service_account_key_file, node_group_id):
            captured["yc_args"] = (service_account_key_file, node_group_id)

    monkeypatch.setattr(service_module, "KubeClient", FakeKubeClient)
    monkeypatch.setattr(service_module, "YcKubeCredentials", FakeYcKubeCredentials)
    monkeypatch.setattr(service_module, "YcClient", FakeYcClient)

    config = _config()
    svc = service_module.ScalerService.from_config(config)

    assert isinstance(captured["kube_credentials"], FakeYcKubeCredentials)
    assert captured["cred_args"] == (
        config.yandex_cloud.service_account_key_file,
        config.yandex_cloud.cluster_id,
        config.yandex_cloud.master_endpoint,
    )
    assert captured["yc_args"] == (
        config.yandex_cloud.service_account_key_file,
        config.yandex_cloud.node_group_id,
    )
    assert svc.config is config

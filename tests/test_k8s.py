from types import SimpleNamespace

from app.k8s import (
    NodeFree,
    parse_cpu_to_millicores,
    parse_memory_to_bytes,
    sum_pod_requests,
    pod_matches_selectors,
)


def test_parse_cpu():
    assert parse_cpu_to_millicores("500m") == 500
    assert parse_cpu_to_millicores("2") == 2000
    assert parse_cpu_to_millicores(None) == 0


def test_parse_memory():
    assert parse_memory_to_bytes("1Gi") == 1024**3
    assert parse_memory_to_bytes("512Mi") == 512 * 1024**2
    assert parse_memory_to_bytes("1000000") == 1_000_000
    assert parse_memory_to_bytes("1M") == 1_000_000
    assert parse_memory_to_bytes(None) == 0


def _container(cpu, mem):
    return SimpleNamespace(
        resources=SimpleNamespace(requests={"cpu": cpu, "memory": mem})
    )


def _pod(labels, containers):
    return SimpleNamespace(
        metadata=SimpleNamespace(labels=labels),
        spec=SimpleNamespace(containers=containers),
    )


def test_sum_pod_requests_across_containers():
    pods = [
        _pod({"team": "ml"}, [_container("500m", "1Gi"), _container("500m", "1Gi")]),
        _pod({"team": "ml"}, [_container("1", "2Gi")]),
    ]
    cpu, mem = sum_pod_requests(pods)
    assert cpu == 500 + 500 + 1000
    assert mem == 1024**3 + 1024**3 + 2 * 1024**3


def test_pod_matches_any_selector():
    pod = _pod({"team": "ml", "env": "prod"}, [])
    assert pod_matches_selectors(pod, ["team=ml"]) is True
    assert pod_matches_selectors(pod, ["workload=batch", "team=ml"]) is True
    assert pod_matches_selectors(pod, ["workload=batch"]) is False
    assert pod_matches_selectors(pod, []) is False


def test_pod_matches_handles_no_labels():
    pod = _pod(None, [])
    assert pod_matches_selectors(pod, ["team=ml"]) is False


from app.k8s import node_is_ready  # noqa: E402


def _node(ready: bool):
    status = "True" if ready else "False"
    return SimpleNamespace(
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status=status)]
        )
    )


def test_node_is_ready():
    assert node_is_ready(_node(True)) is True
    assert node_is_ready(_node(False)) is False


def test_node_is_ready_handles_no_conditions():
    node = SimpleNamespace(status=SimpleNamespace(conditions=None))
    assert node_is_ready(node) is False


from app.k8s import pod_is_waiting_for_node  # noqa: E402


def test_pod_waiting_for_node_when_unscheduled():
    pod = SimpleNamespace(spec=SimpleNamespace(node_name=None))
    assert pod_is_waiting_for_node(pod) is True


def test_pod_not_waiting_when_scheduled():
    pod = SimpleNamespace(spec=SimpleNamespace(node_name="node-1"))
    assert pod_is_waiting_for_node(pod) is False


def test_pod_waiting_handles_missing_node_name_attr():
    pod = SimpleNamespace(spec=SimpleNamespace())
    assert pod_is_waiting_for_node(pod) is True


from app.k8s import NODE_GROUP_LABEL, nodes_in_group, occupied_node_names  # noqa: E402


def _group_node(name, ready=True, group="grp-1"):
    status = "True" if ready else "False"
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={NODE_GROUP_LABEL: group}),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status=status)]
        ),
    )


def _scheduled_pod(labels, node_name):
    return SimpleNamespace(
        metadata=SimpleNamespace(labels=labels),
        spec=SimpleNamespace(node_name=node_name),
    )


def test_nodes_in_group_filters_by_label_and_readiness():
    n1 = _group_node("a", ready=True, group="grp-1")
    n2 = _group_node("b", ready=True, group="grp-2")   # other group
    n3 = _group_node("c", ready=False, group="grp-1")  # not ready
    nodes = nodes_in_group([n1, n2, n3], "grp-1")
    assert [n.metadata.name for n in nodes] == ["a"]


def test_nodes_in_group_handles_missing_label():
    n = SimpleNamespace(
        metadata=SimpleNamespace(name="x", labels=None),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status="True")]
        ),
    )
    assert nodes_in_group([n], "grp-1") == []


def test_occupied_node_names_counts_matching_scheduled_pods():
    pods = [
        _scheduled_pod({"team": "ml"}, "a"),
        _scheduled_pod({"team": "other"}, "b"),
        _scheduled_pod({"team": "ml"}, None),
    ]
    assert occupied_node_names(pods, ["team=ml"]) == {"a"}


from unittest.mock import MagicMock  # noqa: E402

from app.k8s import KubeClient  # noqa: E402
from app.k8s import _configuration_from_credentials  # noqa: E402


class _FakeCreds:
    def __init__(self, tokens):
        self.endpoint = "https://10.0.0.1:443"
        self.ca_cert = (
            "-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----"
        )
        self._tokens = iter(tokens)

    def get_token(self):
        return next(self._tokens)


def test_configuration_sets_host_ca_and_bearer():
    creds = _FakeCreds(["tok-1"])
    cfg = _configuration_from_credentials(creds)
    assert cfg.host == "https://10.0.0.1:443"
    with open(cfg.ssl_ca_cert) as fh:
        assert fh.read() == creds.ca_cert
    assert cfg.api_key["authorization"] == "tok-1"
    assert cfg.api_key_prefix["authorization"] == "Bearer"


def test_configuration_refresh_hook_swaps_in_fresh_token():
    creds = _FakeCreds(["tok-1", "tok-2"])
    cfg = _configuration_from_credentials(creds)
    assert cfg.api_key["authorization"] == "tok-1"
    cfg.refresh_api_key_hook(cfg)
    assert cfg.api_key["authorization"] == "tok-2"


def test_kube_client_builds_api_from_credentials():
    creds = _FakeCreds(["tok-1"])
    client_obj = KubeClient(credentials=creds)
    assert client_obj._v1 is not None


def _kube_with(nodes, pods):
    client = KubeClient.__new__(KubeClient)
    client._v1 = MagicMock()
    client._v1.list_node.return_value = SimpleNamespace(items=nodes)
    client._v1.list_namespaced_pod.return_value = SimpleNamespace(items=pods)
    return client


def test_empty_group_node_count_counts_unoccupied_group_nodes():
    nodes = [
        _group_node("a", ready=True, group="grp-1"),
        _group_node("b", ready=True, group="grp-1"),
        _group_node("c", ready=True, group="grp-2"),
    ]
    pods = [_scheduled_pod({"team": "ml"}, "b")]
    client = _kube_with(nodes, pods)

    count = client.empty_group_node_count("ml", ["team=ml"], "grp-1")
    assert count == 1


def test_empty_group_node_count_returns_zero_when_label_missing():
    nodes = [_group_node("a", ready=True, group="grp-2")]
    client = _kube_with(nodes, [])

    count = client.empty_group_node_count("ml", ["team=ml"], "grp-1")
    assert count == 0
    client._v1.list_namespaced_pod.assert_not_called()


def test_ready_group_node_count_counts_only_ready_group_nodes():
    nodes = [
        _group_node("a", ready=True, group="grp-1"),
        _group_node("b", ready=False, group="grp-1"),
        _group_node("c", ready=True, group="grp-2"),
    ]
    client = _kube_with(nodes, [])
    assert client.ready_group_node_count("grp-1") == 1


def _alloc_group_node(name, cpu_milli, mem_bytes, group="grp-1", ready=True):
    status = "True" if ready else "False"
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={NODE_GROUP_LABEL: group}),
        status=SimpleNamespace(
            allocatable={"cpu": str(cpu_milli / 1000), "memory": str(mem_bytes)},
            conditions=[SimpleNamespace(type="Ready", status=status)],
        ),
    )


def _bound_pod(node_name, cpu, mem, phase="Running"):
    return SimpleNamespace(
        metadata=SimpleNamespace(labels={}),
        spec=SimpleNamespace(
            node_name=node_name,
            containers=[
                SimpleNamespace(
                    resources=SimpleNamespace(requests={"cpu": cpu, "memory": mem})
                )
            ],
        ),
        status=SimpleNamespace(phase=phase),
    )


def test_group_free_capacity_subtracts_requests_on_ready_group_nodes():
    nodes = [
        _alloc_group_node("a", 4000, 8 * 1024**3, group="grp-1"),
        _alloc_group_node("b", 4000, 8 * 1024**3, group="grp-2"),
        _alloc_group_node("c", 4000, 8 * 1024**3, group="grp-1", ready=False),
    ]
    pods = [
        _bound_pod("a", "1", "1Gi"),
        _bound_pod("a", "500m", "512Mi", phase="Succeeded"),
        _bound_pod(None, "2", "2Gi"),
        _bound_pod("b", "1", "1Gi"),
    ]
    client = _kube_with(nodes, [])
    client._v1.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=pods)

    free = client.group_free_capacity("grp-1")
    assert free == [NodeFree("a", 4000 - 1000, 8 * 1024**3 - 1024**3)]


def test_group_free_capacity_reports_each_node_separately():
    nodes = [
        _alloc_group_node("a", 4000, 8 * 1024**3, group="grp-1"),
        _alloc_group_node("b", 4000, 8 * 1024**3, group="grp-1"),
    ]
    pods = [_bound_pod("a", "3", "6Gi"), _bound_pod("b", "3", "6Gi")]
    client = _kube_with(nodes, [])
    client._v1.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=pods)

    free = client.group_free_capacity("grp-1")
    assert free == [
        NodeFree("a", 1000, 2 * 1024**3),
        NodeFree("b", 1000, 2 * 1024**3),
    ]


def test_group_free_capacity_never_negative_when_overcommitted():
    nodes = [_alloc_group_node("a", 4000, 8 * 1024**3, group="grp-1")]
    pods = [_bound_pod("a", "6", "12Gi")]
    client = _kube_with(nodes, [])
    client._v1.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=pods)

    assert client.group_free_capacity("grp-1") == [NodeFree("a", 0, 0)]


def test_group_free_capacity_empty_without_group_nodes():
    nodes = [_alloc_group_node("a", 4000, 8 * 1024**3, group="grp-2")]
    client = _kube_with(nodes, [])
    assert client.group_free_capacity("grp-1") == []
    client._v1.list_pod_for_all_namespaces.assert_not_called()


def test_get_node_capacity_ignores_other_node_groups():
    nodes = [
        _alloc_group_node("other", 2000, 4 * 1024**3, group="grp-2"),
        _alloc_group_node("mine", 16000, 64 * 1024**3, group="grp-1"),
    ]
    client = _kube_with(nodes, [])
    assert client.get_node_capacity("grp-1") == (16000, 64 * 1024**3)


def test_get_node_capacity_ignores_not_ready_group_nodes():
    nodes = [
        _alloc_group_node("a", 2000, 4 * 1024**3, group="grp-1", ready=False),
        _alloc_group_node("b", 8000, 32 * 1024**3, group="grp-1"),
    ]
    client = _kube_with(nodes, [])
    assert client.get_node_capacity("grp-1") == (8000, 32 * 1024**3)


def test_get_node_capacity_uses_smallest_node_of_a_mixed_group():
    nodes = [
        _alloc_group_node("big", 16000, 64 * 1024**3, group="grp-1"),
        _alloc_group_node("small", 4000, 8 * 1024**3, group="grp-1"),
    ]
    client = _kube_with(nodes, [])
    assert client.get_node_capacity("grp-1") == (4000, 8 * 1024**3)


def test_get_node_capacity_none_without_group_nodes():
    nodes = [_alloc_group_node("other", 4000, 8 * 1024**3, group="grp-2")]
    client = _kube_with(nodes, [])
    assert client.get_node_capacity("grp-1") is None


def test_get_node_capacity_skips_nodes_reporting_no_allocatable():
    zero = SimpleNamespace(
        metadata=SimpleNamespace(name="broken", labels={NODE_GROUP_LABEL: "grp-1"}),
        status=SimpleNamespace(
            allocatable=None,
            conditions=[SimpleNamespace(type="Ready", status="True")],
        ),
    )
    good = _alloc_group_node("ok", 4000, 8 * 1024**3, group="grp-1")

    assert _kube_with([zero, good], []).get_node_capacity("grp-1") == (
        4000, 8 * 1024**3
    )
    assert _kube_with([zero], []).get_node_capacity("grp-1") is None

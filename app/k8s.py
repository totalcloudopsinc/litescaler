from __future__ import annotations

import logging
import tempfile
from typing import Protocol

from kubernetes import client

logger = logging.getLogger(__name__)

_MEM_MULTIPLIERS = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
    "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5,
    "k": 1000,
}

NODE_GROUP_LABEL = "yandex.cloud/node-group-id"


def parse_cpu_to_millicores(value) -> int:
    if value is None:
        return 0
    value = str(value)
    if value.endswith("m"):
        return int(float(value[:-1]))
    return int(float(value) * 1000)


def parse_memory_to_bytes(value) -> int:
    if value is None:
        return 0
    value = str(value)
    for suffix, mult in _MEM_MULTIPLIERS.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * mult)
    return int(float(value))


def sum_pod_requests(pods) -> tuple[int, int]:
    total_cpu = 0
    total_mem = 0
    for pod in pods:
        for container in pod.spec.containers or []:
            requests = getattr(container.resources, "requests", None) or {}
            total_cpu += parse_cpu_to_millicores(requests.get("cpu"))
            total_mem += parse_memory_to_bytes(requests.get("memory"))
    return total_cpu, total_mem


def pod_matches_selectors(pod, selectors) -> bool:
    labels = pod.metadata.labels or {}
    for selector in selectors:
        key, _, val = selector.partition("=")
        if labels.get(key) == val:
            return True
    return False


def node_is_ready(node) -> bool:
    conditions = node.status.conditions or []
    return any(c.type == "Ready" and c.status == "True" for c in conditions)


def pod_is_waiting_for_node(pod) -> bool:
    return not getattr(pod.spec, "node_name", None)


def nodes_in_group(nodes, node_group_id: str) -> list:
    result = []
    for node in nodes:
        if not node_is_ready(node):
            continue
        labels = node.metadata.labels or {}
        if labels.get(NODE_GROUP_LABEL) == node_group_id:
            result.append(node)
    return result


def occupied_node_names(pods, selectors) -> set:
    occupied = set()
    for pod in pods:
        node_name = getattr(pod.spec, "node_name", None)
        if node_name and pod_matches_selectors(pod, selectors):
            occupied.add(node_name)
    return occupied


class KubeCredentials(Protocol):
    endpoint: str
    ca_cert: str

    def get_token(self) -> str: ...


def _write_ca_file(ca_cert: str) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False)
    handle.write(ca_cert)
    handle.close()
    return handle.name


def _configuration_from_credentials(credentials: KubeCredentials) -> client.Configuration:
    cfg = client.Configuration()
    cfg.host = credentials.endpoint
    cfg.ssl_ca_cert = _write_ca_file(credentials.ca_cert)
    cfg.api_key["authorization"] = credentials.get_token()
    cfg.api_key_prefix["authorization"] = "Bearer"

    def _refresh(configuration: "client.Configuration") -> None:
        configuration.api_key["authorization"] = credentials.get_token()

    cfg.refresh_api_key_hook = _refresh
    return cfg


class KubeClient:
    def __init__(self, credentials: KubeCredentials):
        configuration = _configuration_from_credentials(credentials)
        self._v1 = client.CoreV1Api(client.ApiClient(configuration))
        logger.info("Built Kubernetes client for %s", credentials.endpoint)

    def list_pending_pods(self, namespace: str, selectors: list[str]):
        pods = self._v1.list_namespaced_pod(
            namespace=namespace, field_selector="status.phase=Pending"
        ).items
        return [
            p
            for p in pods
            if pod_matches_selectors(p, selectors) and pod_is_waiting_for_node(p)
        ]

    def matching_pod_uids(self, namespace: str, selectors: list[str]) -> set[str]:
        pods = self._v1.list_namespaced_pod(namespace=namespace).items
        return {p.metadata.uid for p in pods if pod_matches_selectors(p, selectors)}

    def ready_group_node_count(self, node_group_id: str) -> int:
        nodes = self._v1.list_node().items
        return len(nodes_in_group(nodes, node_group_id))

    def group_free_capacity(self, node_group_id: str) -> tuple[int, int]:
        nodes = self._v1.list_node().items
        group = nodes_in_group(nodes, node_group_id)
        if not group:
            return (0, 0)

        used_cpu: dict[str, int] = {}
        used_mem: dict[str, int] = {}
        for pod in self._v1.list_pod_for_all_namespaces().items:
            node_name = getattr(pod.spec, "node_name", None)
            if not node_name:
                continue
            if getattr(pod.status, "phase", None) in ("Succeeded", "Failed"):
                continue
            cpu, mem = sum_pod_requests([pod])
            used_cpu[node_name] = used_cpu.get(node_name, 0) + cpu
            used_mem[node_name] = used_mem.get(node_name, 0) + mem

        free_cpu = 0
        free_mem = 0
        for node in group:
            alloc = node.status.allocatable or {}
            name = node.metadata.name
            free_cpu += max(
                0, parse_cpu_to_millicores(alloc.get("cpu")) - used_cpu.get(name, 0)
            )
            free_mem += max(
                0, parse_memory_to_bytes(alloc.get("memory")) - used_mem.get(name, 0)
            )
        return (free_cpu, free_mem)

    def get_node_capacity(self) -> tuple[int, int] | None:
        nodes = self._v1.list_node().items
        for node in nodes:
            if not node_is_ready(node):
                continue
            alloc = node.status.allocatable or {}
            return (
                parse_cpu_to_millicores(alloc.get("cpu")),
                parse_memory_to_bytes(alloc.get("memory")),
            )
        return None

    def empty_group_node_count(
        self, namespace: str, selectors: list[str], node_group_id: str
    ) -> int:
        nodes = self._v1.list_node().items
        group_nodes = nodes_in_group(nodes, node_group_id)
        if not group_nodes:
            logger.warning(
                "No nodes labeled %s=%s found; skipping scale-down",
                NODE_GROUP_LABEL, node_group_id,
            )
            return 0
        pods = self._v1.list_namespaced_pod(namespace=namespace).items
        occupied = occupied_node_names(pods, selectors)
        return sum(1 for n in group_nodes if n.metadata.name not in occupied)

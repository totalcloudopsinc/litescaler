from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from typing import Protocol

from kubernetes import client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NodeFree:

    name: str
    cpu_millicores: int
    mem_bytes: int

_MEM_MULTIPLIERS = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
    "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5,
    "k": 1000,
}

NODE_GROUP_LABEL = "yandex.cloud/node-group-id"


def format_cpu(millicores: int) -> str:
    return f"{millicores}m"


def format_mem(num_bytes: int) -> str:
    return f"{num_bytes / 1024**3:.2f}Gi"


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
        matching = [p for p in pods if pod_matches_selectors(p, selectors)]
        unscheduled = [p for p in matching if pod_is_waiting_for_node(p)]
        logger.info(
            "Pending pods in ns=%s: %d total, %d match %s, %d still unscheduled",
            namespace, len(pods), len(matching), selectors, len(unscheduled),
        )
        for pod in unscheduled:
            cpu, mem = sum_pod_requests([pod])
            logger.debug(
                "  pending pod %s requests %s cpu / %s memory",
                pod.metadata.name, format_cpu(cpu), format_mem(mem),
            )
        return unscheduled

    def ready_group_node_count(self, node_group_id: str) -> int:
        nodes = self._v1.list_node().items
        group = nodes_in_group(nodes, node_group_id)
        logger.info(
            "Ready nodes in group %s: %d (cluster has %d nodes in total)",
            node_group_id, len(group), len(nodes),
        )
        logger.debug(
            "  group %s node names: %s",
            node_group_id, [n.metadata.name for n in group] or "none",
        )
        return len(group)

    def group_free_capacity(self, node_group_id: str) -> list[NodeFree]:
        nodes = self._v1.list_node().items
        group = nodes_in_group(nodes, node_group_id)
        if not group:
            logger.warning(
                "Free capacity: no Ready node labeled %s=%s among %d cluster "
                "nodes; assuming zero free capacity",
                NODE_GROUP_LABEL, node_group_id, len(nodes),
            )
            return []

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

        free: list[NodeFree] = []
        for node in group:
            alloc = node.status.allocatable or {}
            name = node.metadata.name
            alloc_cpu = parse_cpu_to_millicores(alloc.get("cpu"))
            alloc_mem = parse_memory_to_bytes(alloc.get("memory"))
            node_free_cpu = max(0, alloc_cpu - used_cpu.get(name, 0))
            node_free_mem = max(0, alloc_mem - used_mem.get(name, 0))
            logger.debug(
                "  node %s: allocatable %s / %s, requested %s / %s, free %s / %s",
                name,
                format_cpu(alloc_cpu), format_mem(alloc_mem),
                format_cpu(used_cpu.get(name, 0)), format_mem(used_mem.get(name, 0)),
                format_cpu(node_free_cpu), format_mem(node_free_mem),
            )
            free.append(NodeFree(name, node_free_cpu, node_free_mem))
        logger.info(
            "Free capacity across %d Ready node(s) of group %s: %s cpu / %s "
            "memory in total, largest single node %s cpu / %s memory",
            len(group), node_group_id,
            format_cpu(sum(n.cpu_millicores for n in free)),
            format_mem(sum(n.mem_bytes for n in free)),
            format_cpu(max(n.cpu_millicores for n in free)),
            format_mem(max(n.mem_bytes for n in free)),
        )
        return free

    def get_node_capacity(self, node_group_id: str) -> tuple[int, int] | None:
        nodes = self._v1.list_node().items
        group = nodes_in_group(nodes, node_group_id)
        if not group:
            logger.warning(
                "Node capacity: no Ready node labeled %s=%s among %d cluster nodes",
                NODE_GROUP_LABEL, node_group_id, len(nodes),
            )
            return None

        sizes: list[tuple[int, int]] = []
        for node in group:
            alloc = node.status.allocatable or {}
            cpu = parse_cpu_to_millicores(alloc.get("cpu"))
            mem = parse_memory_to_bytes(alloc.get("memory"))
            if cpu <= 0 or mem <= 0:
                logger.warning(
                    "Node capacity: node %s of group %s reports unusable "
                    "allocatable (%s cpu / %s memory); ignoring it",
                    node.metadata.name, node_group_id,
                    format_cpu(cpu), format_mem(mem),
                )
                continue
            logger.debug(
                "  node %s of group %s: allocatable %s cpu / %s memory",
                node.metadata.name, node_group_id, format_cpu(cpu), format_mem(mem),
            )
            sizes.append((cpu, mem))

        if not sizes:
            logger.warning(
                "Node capacity: no node of group %s reports usable allocatable",
                node_group_id,
            )
            return None

        cpu = min(c for c, _ in sizes)
        mem = min(m for _, m in sizes)
        if len(set(sizes)) > 1:
            logger.warning(
                "Node capacity: group %s has %d distinct node sizes %s; sizing "
                "on the smallest (%s cpu / %s memory)",
                node_group_id, len(set(sizes)),
                [(format_cpu(c), format_mem(m)) for c, m in sorted(set(sizes))],
                format_cpu(cpu), format_mem(mem),
            )
        logger.info(
            "Node capacity of group %s: %s cpu / %s memory per node "
            "(from %d Ready node(s))",
            node_group_id, format_cpu(cpu), format_mem(mem), len(sizes),
        )
        return (cpu, mem)

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
        empty = [n.metadata.name for n in group_nodes if n.metadata.name not in occupied]
        logger.info(
            "Group %s has %d Ready node(s), %d busy with pods matching %s, "
            "%d empty",
            node_group_id, len(group_nodes),
            len(group_nodes) - len(empty), selectors, len(empty),
        )
        logger.debug("  empty nodes: %s", empty or "none")
        return len(empty)

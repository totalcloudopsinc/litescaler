from __future__ import annotations

import logging

from app.config import Config
from app.k8s import KubeClient, format_cpu, format_mem, sum_pod_requests
from app.scaling import Decision, decide, decide_manual, decide_scale_down
from app.yc import YcClient, YcKubeCredentials

logger = logging.getLogger(__name__)


class ScalerService:
    def __init__(self, config: Config, kube, yc):
        self._config = config
        self._kube = kube
        self._yc = yc
        self.last_decision: Decision | None = None
        self._handled_pods: set[str] = set()
        self._idle_polls: int = 0

    @property
    def config(self) -> Config:
        return self._config

    @classmethod
    def from_config(cls, config: Config) -> "ScalerService":
        yc_cloud = config.yandex_cloud
        credentials = YcKubeCredentials(
            service_account_key_file=yc_cloud.service_account_key_file,
            cluster_id=yc_cloud.cluster_id,
            endpoint_type=yc_cloud.master_endpoint,
        )
        kube = KubeClient(credentials=credentials)
        yc = YcClient(
            service_account_key_file=yc_cloud.service_account_key_file,
            node_group_id=yc_cloud.node_group_id,
        )
        return cls(config=config, kube=kube, yc=yc)

    def _node_capacity(self) -> tuple[int, int]:
        node_group_id = self._config.yandex_cloud.node_group_id
        capacity = self._kube.get_node_capacity(node_group_id)
        if capacity is not None:
            return capacity
        fallback = self._config.scaling.node_capacity_fallback
        logger.warning(
            "No usable Ready node in group %s; using capacity fallback from "
            "config (%s cpu / %s GiB per node)",
            node_group_id, fallback.cpu, fallback.memory_gib,
        )
        return (
            int(fallback.cpu * 1000),
            int(fallback.memory_gib * 1024**3),
        )

    def _apply(self, decision: Decision) -> Decision:
        logger.info("Decision: %s", decision.reason)

        if decision.should_scale:
            if self._config.scaling.dry_run:
                logger.info("dry_run enabled; skipping resize to %d", decision.target_size)
            else:
                self._yc.set_size(decision.target_size)

        self.last_decision = decision
        return decision

    def evaluate(self) -> Decision:
        k = self._config.kubernetes
        logger.info(
            "--- evaluate: group=%s ns=%s selectors=%s dry_run=%s",
            self._config.yandex_cloud.node_group_id, k.namespace,
            k.label_selectors, self._config.scaling.dry_run,
        )
        pods = self._kube.list_pending_pods(k.namespace, k.label_selectors)
        existing = self._kube.matching_pod_uids(k.namespace, k.label_selectors)
        forgotten = self._handled_pods - existing
        if forgotten:
            logger.debug(
                "Forgetting %d already-handled pod(s) that no longer exist",
                len(forgotten),
            )
        self._handled_pods &= existing
        new_pods = [p for p in pods if p.metadata.uid not in self._handled_pods]
        logger.info(
            "Pending pods: %d unscheduled, %d already accounted for by an "
            "earlier resize, %d new for this decision",
            len(pods), len(pods) - len(new_pods), len(new_pods),
        )
        current_size = self._yc.get_current_size()
        ready_nodes = self._kube.ready_group_node_count(
            self._config.yandex_cloud.node_group_id
        )
        operation_running = self._yc.operation_in_progress()
        logger.info(
            "Node group state: desired size %d, ready nodes %d, "
            "resize operation in progress: %s",
            current_size, ready_nodes, operation_running,
        )
        if ready_nodes != current_size or operation_running:
            if pods:
                self._idle_polls = 0
            return self._apply(Decision(
                should_scale=False,
                current_size=current_size,
                target_size=current_size,
                nodes_to_add=0,
                pending_count=len(new_pods),
                reason=(
                    f"node group transitioning (desired {current_size}, "
                    f"ready {ready_nodes}); waiting before next resize"
                ),
            ))

        sum_cpu, sum_mem = sum_pod_requests(new_pods)
        logger.info(
            "New pending pods request %s cpu / %s memory in total",
            format_cpu(sum_cpu), format_mem(sum_mem),
        )
        node_cpu, node_mem = self._node_capacity()
        free_cpu, free_mem = self._kube.group_free_capacity(
            self._config.yandex_cloud.node_group_id
        )

        decision = decide(
            pending_count=len(new_pods),
            sum_cpu_millicores=sum_cpu,
            sum_mem_bytes=sum_mem,
            node_cpu_millicores=node_cpu,
            node_mem_bytes=node_mem,
            current_size=current_size,
            config=self._config.scaling,
            free_cpu_millicores=free_cpu,
            free_mem_bytes=free_mem,
        )

        if decision.should_scale:
            result = self._apply(decision)
            self._handled_pods |= {p.metadata.uid for p in new_pods}
            self._idle_polls = 0
            logger.debug(
                "Remembering %d pod(s) as handled; idle counter reset",
                len(new_pods),
            )
            return result

        if pods:
            self._idle_polls = 0
            logger.info(
                "Pods still pending; idle counter reset, scale-down not considered"
            )
            return self._apply(decision)

        self._idle_polls += 1
        if self._idle_polls < self._config.scaling.scale_down_cooldown_polls:
            logger.info(
                "Idle poll %d of %d before scale-down is considered",
                self._idle_polls, self._config.scaling.scale_down_cooldown_polls,
            )
            return self._apply(decision)

        logger.info(
            "Idle for %d poll(s); checking group %s for empty nodes",
            self._idle_polls, self._config.yandex_cloud.node_group_id,
        )
        empty = self._kube.empty_group_node_count(
            k.namespace, k.label_selectors, self._config.yandex_cloud.node_group_id
        )
        down = decide_scale_down(
            empty_node_count=empty,
            current_size=current_size,
            min_size=self._config.scaling.min_size,
        )

        self._idle_polls = 0
        return self._apply(down)

    def scale_by(self, nodes_to_add: int) -> Decision:
        current_size = self._yc.get_current_size()
        logger.info(
            "--- manual scale of group %s: +%d nodes requested, current size %d",
            self._config.yandex_cloud.node_group_id, nodes_to_add, current_size,
        )
        decision = decide_manual(
            nodes_to_add=nodes_to_add,
            current_size=current_size,
            max_size=self._config.scaling.max_size,
        )

        self._idle_polls = 0
        return self._apply(decision)

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from app.config import ScalingConfig

logger = logging.getLogger(__name__)


def _gib(num_bytes: float) -> str:
    return f"{num_bytes / 1024**3:.2f}Gi"


@dataclass(frozen=True)
class Decision:
    should_scale: bool
    current_size: int
    target_size: int
    nodes_to_add: int
    pending_count: int
    reason: str


def decide(
    *,
    pending_count: int,
    sum_cpu_millicores: int,
    sum_mem_bytes: int,
    node_cpu_millicores: int,
    node_mem_bytes: int,
    current_size: int,
    config: ScalingConfig,
    free_cpu_millicores: int = 0,
    free_mem_bytes: int = 0,
) -> Decision:
    logger.info(
        "decide: pending=%d (threshold %d), requests %dm cpu / %s memory, "
        "free in group %dm cpu / %s memory, node size %dm cpu / %s memory, "
        "size %d (min %d, max %d), headroom %.0f%%",
        pending_count, config.pending_pod_threshold,
        sum_cpu_millicores, _gib(sum_mem_bytes),
        free_cpu_millicores, _gib(free_mem_bytes),
        node_cpu_millicores, _gib(node_mem_bytes),
        current_size, config.min_size, config.max_size, config.headroom * 100,
    )

    if pending_count <= config.pending_pod_threshold:
        return Decision(
            should_scale=False,
            current_size=current_size,
            target_size=current_size,
            nodes_to_add=0,
            pending_count=pending_count,
            reason=(
                f"{pending_count} pending pods <= threshold "
                f"{config.pending_pod_threshold}; no action"
            ),
        )

    net_cpu = max(0, sum_cpu_millicores - free_cpu_millicores)
    net_mem = max(0, sum_mem_bytes - free_mem_bytes)

    cpu_nodes = net_cpu / node_cpu_millicores if node_cpu_millicores else 0
    mem_nodes = net_mem / node_mem_bytes if node_mem_bytes else 0
    raw_nodes = max(cpu_nodes, mem_nodes) * (1 + config.headroom)
    nodes_needed = math.ceil(raw_nodes)

    logger.info(
        "decide: unmet demand after free capacity: %dm cpu / %s memory -> "
        "%.2f nodes by cpu, %.2f nodes by memory; "
        "max * (1 + %.2f headroom) = %.2f -> ceil = %d nodes needed",
        net_cpu, _gib(net_mem), cpu_nodes, mem_nodes,
        config.headroom, raw_nodes, nodes_needed,
    )
    if node_cpu_millicores <= 0 or node_mem_bytes <= 0:
        logger.warning(
            "decide: node size is missing a dimension (cpu %dm, memory %s); "
            "that dimension is ignored when sizing the resize",
            node_cpu_millicores, _gib(node_mem_bytes),
        )

    if nodes_needed <= 0:
        return Decision(
            should_scale=False,
            current_size=current_size,
            target_size=current_size,
            nodes_to_add=0,
            pending_count=pending_count,
            reason=(
                f"{pending_count} pending pods fit in existing free capacity; "
                "no action"
            ),
        )

    uncapped_target = current_size + nodes_needed
    target = min(uncapped_target, config.max_size)
    nodes_to_add = target - current_size
    logger.info(
        "decide: %d + %d = %d wanted, capped by max_size %d -> target %d (+%d)",
        current_size, nodes_needed, uncapped_target, config.max_size,
        target, nodes_to_add,
    )

    if target <= current_size:
        return Decision(
            should_scale=False,
            current_size=current_size,
            target_size=current_size,
            nodes_to_add=0,
            pending_count=pending_count,
            reason=(
                f"already at max_size {config.max_size}; cannot add "
                f"{nodes_needed} needed nodes"
            ),
        )

    return Decision(
        should_scale=True,
        current_size=current_size,
        target_size=target,
        nodes_to_add=nodes_to_add,
        pending_count=pending_count,
        reason=(
            f"{pending_count} pending pods need ~{nodes_needed} nodes "
            f"(+{int(config.headroom * 100)}% headroom); "
            f"scaling {current_size} -> {target}"
        ),
    )


def decide_manual(
    *,
    nodes_to_add: int,
    current_size: int,
    max_size: int,
) -> Decision:

    target = min(current_size + nodes_to_add, max_size)
    actual_add = target - current_size

    if actual_add <= 0:
        return Decision(
            should_scale=False,
            current_size=current_size,
            target_size=current_size,
            nodes_to_add=0,
            pending_count=0,
            reason=(
                f"manual scale: already at max_size {max_size}; "
                f"cannot add {nodes_to_add} nodes"
            ),
        )

    if actual_add < nodes_to_add:
        reason = (
            f"manual scale: requested {nodes_to_add} nodes, capped at "
            f"max_size {max_size}; scaling {current_size} -> {target}"
        )
    else:
        reason = (
            f"manual scale: adding {nodes_to_add} nodes; "
            f"scaling {current_size} -> {target}"
        )

    return Decision(
        should_scale=True,
        current_size=current_size,
        target_size=target,
        nodes_to_add=actual_add,
        pending_count=0,
        reason=reason,
    )


def decide_scale_down(
    *,
    empty_node_count: int,
    current_size: int,
    min_size: int,
) -> Decision:
    logger.info(
        "decide_scale_down: %d empty node(s), size %d, min_size %d",
        empty_node_count, current_size, min_size,
    )
    target = max(current_size - empty_node_count, min_size)
    nodes_to_add = target - current_size

    if nodes_to_add >= 0:
        return Decision(
            should_scale=False,
            current_size=current_size,
            target_size=current_size,
            nodes_to_add=0,
            pending_count=0,
            reason=(
                f"no empty nodes to remove (empty={empty_node_count}, "
                f"size={current_size}, min_size={min_size}); no action"
            ),
        )

    return Decision(
        should_scale=True,
        current_size=current_size,
        target_size=target,
        nodes_to_add=nodes_to_add,
        pending_count=0,
        reason=(
            f"{empty_node_count} empty nodes idle; scaling down "
            f"{current_size} -> {target} (min_size {min_size})"
        ),
    )

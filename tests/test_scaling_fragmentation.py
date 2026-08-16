"""Regression tests for free capacity that is fragmented across nodes.

Both cases below are replays of a live incident on the MyMeet dev cluster on
2026-08-13, when this fork still carried the pre-`e8496ae` accounting: free
capacity was summed group-wide, so remainders too small to hold a single pod
were counted as if they could. Upstream fixed it in `e8496ae` by simulating the
packing (`app.scaling._pack`) and taking `max(demand_nodes, fit_nodes)`.

Note that memory is never the binding constraint here — every node had ~6.10Gi
free against a 2.0Gi pod. The fragmentation is purely in CPU, which is what made
the group-wide sum so misleading: on paper 9480m looked like four free pods.

Numbers are verbatim from the scaler's own logs; the timestamps identify the
poll each scenario is taken from.
"""

from app.config import ScalingConfig
from app.scaling import decide

GIB = 1024**3

# worker-dev is standard-v3 8 vCPU / 16 GiB; this is what the kubelet reports.
NODE_CPU = 7910
NODE_MEM = int(12.82 * GIB)

# One `bot-dev-worker` pod, derived from the logged deltas (a node carrying the
# 470m/0.72Gi daemonset baseline reads 6470m/6.72Gi with three pods on it).
POD_CPU = 2000
POD_MEM = 2 * GIB

# Free capacity per node, as logged. Every entry is below POD_CPU, so no node
# can take a pod, yet all of them can take its memory.
SIZE_3_FREE = [(1440, int(6.10 * GIB)), (840, int(3.97 * GIB)), (1440, int(6.10 * GIB))]
SIZE_7_FREE = [(1440, int(6.10 * GIB))] * 6 + [(840, int(3.97 * GIB))]


def cfg(**kw):
    base = dict(max_size=6, min_size=3, pending_pod_threshold=0, headroom=0.10)
    base.update(kw)
    return ScalingConfig(**base)


def call(pending, free_by_node, current_size, config):
    pods = [(POD_CPU, POD_MEM)] * pending
    return decide(
        pending_count=pending,
        sum_cpu_millicores=sum(c for c, _ in pods),
        sum_mem_bytes=sum(m for _, m in pods),
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=current_size,
        config=config,
        free_by_node=free_by_node,
        pod_requests=pods,
    )


def test_no_node_can_host_a_pod_though_the_group_total_suggests_four():
    """The premise, stated as an assertion so the scenario cannot rot."""
    total_cpu = sum(c for c, _ in SIZE_7_FREE)
    assert total_cpu == 9480
    assert total_cpu // POD_CPU == 4, "group-wide arithmetic promises four pods"
    assert max(c for c, _ in SIZE_7_FREE) < POD_CPU, "no single node fits one"
    assert all(m >= POD_MEM for _, m in SIZE_7_FREE), "memory is not the constraint"


def test_scale_up_covers_pods_that_no_remainder_can_absorb():
    """17:50:44 — 8 pending on 3 nodes. The old code added 2 and stranded two pods.

    Demand arithmetic alone says 2: (16000 - 3720) / 7910 * 1.10 = 1.71 -> 2.
    Packing says 3, because none of 1440/840/1440 can seat a 2000m pod and each
    fresh node seats exactly three. `max(2, 3)` is what makes the queue drain.
    """
    d = call(pending=8, free_by_node=SIZE_3_FREE, current_size=3, config=cfg())

    assert d.should_scale is True
    assert d.nodes_to_add == 3
    assert d.target_size == 6
    assert "fragmented across 3 nodes" in d.reason


def test_scale_up_when_demand_arithmetic_alone_would_do_nothing():
    """17:56:27 — 2 pending on 7 nodes, 9480m 'free'. The old code did nothing.

    This is the sharper case: subtracting free capacity from demand leaves a
    negative number, so the demand branch asks for zero nodes. Only the packing
    simulation sees that both pods are homeless. Kubernetes agreed at the time —
    `0/23 nodes are available: ... 7 Insufficient cpu`.
    """
    d = call(pending=2, free_by_node=SIZE_7_FREE, current_size=7, config=cfg(max_size=10))

    assert d.should_scale is True
    assert d.nodes_to_add == 1  # both pods fit on one fresh 7910m node
    assert d.target_size == 8
    assert "fragmented across 7 nodes" in d.reason


def test_usable_remainders_are_still_consumed_before_adding_nodes():
    """The fix must not overshoot: real gaps still absorb pods.

    Same 8 pods and nearly the same group total as the live 3-node case (9640m
    vs 3720m), but here the room is in usable pieces — two nodes seat two pods
    each. Four pods land on existing nodes and only two new ones are needed,
    one fewer than when the identical demand met unusable slivers.
    """
    usable = [(4400, int(6.10 * GIB)), (4400, int(6.10 * GIB)), (840, int(3.97 * GIB))]
    d = call(pending=8, free_by_node=usable, current_size=3, config=cfg())

    assert d.nodes_to_add == 2
    assert d.target_size == 5

    stranded = call(pending=8, free_by_node=SIZE_3_FREE, current_size=3, config=cfg())
    assert stranded.nodes_to_add == d.nodes_to_add + 1

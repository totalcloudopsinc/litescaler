"""Characterisation tests for free-capacity accounting across a node group.

These document CURRENT behaviour, including a defect observed live on the MyMeet
dev cluster on 2026-08-13 — they are not a specification of desired behaviour.
Should the accounting ever become per-node aware, `test_stranded_free_capacity_
shrinks_the_resize` is expected to fail; that failure is the fix landing, and the
expected values should then be swapped for the ones the control test asserts.

Numbers are taken verbatim from the scaler's own log at the moment it decided:

    node worker-dev-1: allocatable 7910m, requested 6470m, free 1440m
    node worker-dev-2: allocatable 7910m, requested 6470m, free 1440m
    node worker-dev-3: allocatable 7910m, requested 7070m, free  840m
    Free capacity across 3 Ready node(s): 3720m cpu / 16.17Gi memory
    decide: pending=8, requests 16000m cpu, free in group 3720m cpu,
            node size 7910m, size 3 (min 3, max 6), headroom 10%
    decide: unmet demand after free capacity: 12280m cpu -> 1.55 nodes by cpu;
            max * (1 + 0.10 headroom) = 1.71 -> ceil = 2 nodes needed
    decide: 3 + 2 = 5 wanted, capped by max_size 6 -> target 5 (+2)

Two of the eight pods never scheduled afterwards.
"""

from app.config import ScalingConfig
from app.scaling import decide

NODE_CPU = 7910
NODE_MEM = int(12.82 * 1024**3)

POD_CPU = 2000
POD_MEM = 2 * 1024**3

# Per-node CPU remainders at the moment of the decision.
NODE_FREE_CPU = [1440, 1440, 840]
FREE_CPU_TOTAL = sum(NODE_FREE_CPU)          # 3720m, as logged
FREE_MEM_TOTAL = int(16.17 * 1024**3)

PENDING = 8
DEMAND_CPU = PENDING * POD_CPU               # 16000m, as logged
DEMAND_MEM = PENDING * POD_MEM


def cfg(**kw):
    base = dict(max_size=6, min_size=3, pending_pod_threshold=0, headroom=0.10)
    base.update(kw)
    return ScalingConfig(**base)


def test_no_single_node_could_host_one_pod():
    """The premise: 3720m looks like spare room, but it is in unusable slivers."""
    assert max(NODE_FREE_CPU) < POD_CPU, "no individual node can host one pod"
    assert sum(free // POD_CPU for free in NODE_FREE_CPU) == 0, "zero pods fit"


def test_stranded_free_capacity_shrinks_the_resize():
    """decide() deducts the SUM of unusable remainders and under-provisions.

    Reproduces the logged decision exactly: 2 nodes added where 3 were needed.
    """
    d = decide(
        pending_count=PENDING,
        sum_cpu_millicores=DEMAND_CPU,
        sum_mem_bytes=DEMAND_MEM,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=3,
        config=cfg(),
        free_cpu_millicores=FREE_CPU_TOTAL,
        free_mem_bytes=FREE_MEM_TOTAL,
    )

    assert d.should_scale is True
    assert d.nodes_to_add == 2          # logged: "3 + 2 = 5 wanted ... target 5 (+2)"
    assert d.target_size == 5

    # 5 nodes cannot hold 16 pods of 2000m on 7910m nodes (3 per node, and one
    # node is partly taken by bot-dev-main/calendar/scheduler) — hence 2 stragglers.


def test_control_excluding_stranded_remainders_asks_for_the_third_node():
    """Feed only the remainders that could actually take a pod — none of them —
    and the same demand yields the 3 nodes that would have cleared the queue."""
    usable_free_cpu = sum(free for free in NODE_FREE_CPU if free >= POD_CPU)
    assert usable_free_cpu == 0

    d = decide(
        pending_count=PENDING,
        sum_cpu_millicores=DEMAND_CPU,
        sum_mem_bytes=DEMAND_MEM,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=3,
        config=cfg(),
        free_cpu_millicores=usable_free_cpu,
        free_mem_bytes=0,
    )

    assert d.should_scale is True
    assert d.nodes_to_add == 3          # 16000/7910 = 2.02, * 1.10 = 2.23 -> ceil 3
    assert d.target_size == 6           # == max_size, so it is reachable here

import math

from app.config import ScalingConfig
from app.scaling import Decision, decide


def cfg(**kw):
    base = dict(max_size=20, pending_pod_threshold=3, headroom=0.15)
    base.update(kw)
    return ScalingConfig(**base)


NODE_CPU = 4000
NODE_MEM = 16 * 1024**3


def test_below_threshold_no_scale():
    d = decide(
        pending_count=3,
        sum_cpu_millicores=10_000,
        sum_mem_bytes=10 * 1024**3,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=2,
        config=cfg(),
    )
    assert d.should_scale is False
    assert d.target_size == 2
    assert d.nodes_to_add == 0
    assert "threshold" in d.reason.lower()


def test_cpu_bound_scale_with_headroom():
    d = decide(
        pending_count=4,
        sum_cpu_millicores=8000,
        sum_mem_bytes=1 * 1024**3,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=2,
        config=cfg(),
    )
    assert d.should_scale is True
    assert d.nodes_to_add == 3
    assert d.target_size == 5


def test_memory_bound_dominates():
    d = decide(
        pending_count=10,
        sum_cpu_millicores=2000,
        sum_mem_bytes=64 * 1024**3,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=1,
        config=cfg(),
    )
    assert d.nodes_to_add == 5
    assert d.target_size == 6


def test_clamped_to_max_size():
    d = decide(
        pending_count=50,
        sum_cpu_millicores=400_000,
        sum_mem_bytes=1 * 1024**3,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=18,
        config=cfg(max_size=20),
    )
    assert d.target_size == 20
    assert d.should_scale is True


def test_already_at_max_no_scale():
    d = decide(
        pending_count=50,
        sum_cpu_millicores=400_000,
        sum_mem_bytes=1 * 1024**3,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=20,
        config=cfg(max_size=20),
    )
    assert d.should_scale is False
    assert d.target_size == 20
    assert "max" in d.reason.lower()


def test_zero_capacity_no_zero_division():
    d = decide(
        pending_count=5,
        sum_cpu_millicores=8000,
        sum_mem_bytes=4 * 1024**3,
        node_cpu_millicores=0,
        node_mem_bytes=0,
        current_size=2,
        config=cfg(),
    )
    assert d.should_scale is False
    assert d.target_size == d.current_size


def test_threshold_boundary_exact_no_scale():
    d = decide(
        pending_count=3,
        sum_cpu_millicores=8000,
        sum_mem_bytes=4 * 1024**3,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=2,
        config=cfg(pending_pod_threshold=3),
    )
    assert d.should_scale is False


def test_threshold_boundary_one_over_scales():
    d = decide(
        pending_count=4,
        sum_cpu_millicores=8000,
        sum_mem_bytes=1 * 1024**3,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=2,
        config=cfg(pending_pod_threshold=3),
    )
    assert d.should_scale is True


def test_free_capacity_covers_demand_no_scale():
    d = decide(
        pending_count=4,
        sum_cpu_millicores=8000,
        sum_mem_bytes=2 * 1024**3,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=2,
        config=cfg(),
        free_cpu_millicores=8000,
        free_mem_bytes=4 * 1024**3,
    )
    assert d.should_scale is False
    assert d.target_size == 2
    assert "free capacity" in d.reason.lower()


def test_free_capacity_only_covers_part_scales_for_shortfall():
    d = decide(
        pending_count=4,
        sum_cpu_millicores=8000,
        sum_mem_bytes=1 * 1024**3,
        node_cpu_millicores=NODE_CPU,
        node_mem_bytes=NODE_MEM,
        current_size=1,
        config=cfg(),
        free_cpu_millicores=4000,
        free_mem_bytes=0,
    )
    assert d.should_scale is True
    assert d.nodes_to_add == 2
    assert d.target_size == 3


from app.scaling import decide_manual  # noqa: E402


def test_manual_adds_requested_nodes():
    d = decide_manual(nodes_to_add=3, current_size=2, max_size=20)
    assert d.should_scale is True
    assert d.nodes_to_add == 3
    assert d.target_size == 5
    assert d.pending_count == 0
    assert "manual" in d.reason.lower()


def test_manual_clamped_to_max_size():
    d = decide_manual(nodes_to_add=10, current_size=18, max_size=20)
    assert d.should_scale is True
    assert d.target_size == 20
    assert d.nodes_to_add == 2
    assert "capped" in d.reason.lower()


def test_manual_already_at_max_no_scale():
    d = decide_manual(nodes_to_add=5, current_size=20, max_size=20)
    assert d.should_scale is False
    assert d.target_size == 20
    assert d.nodes_to_add == 0
    assert "max" in d.reason.lower()


from app.scaling import decide_scale_down


def test_scale_down_removes_empty_nodes():
    d = decide_scale_down(empty_node_count=2, current_size=5, min_size=0)
    assert d.should_scale is True
    assert d.target_size == 3
    assert d.nodes_to_add == -2
    assert d.pending_count == 0
    assert "empty" in d.reason.lower()


def test_scale_down_clamped_to_min_size():
    d = decide_scale_down(empty_node_count=3, current_size=2, min_size=1)
    assert d.should_scale is True
    assert d.target_size == 1
    assert d.nodes_to_add == -1


def test_scale_down_no_empty_nodes_no_action():
    d = decide_scale_down(empty_node_count=0, current_size=5, min_size=0)
    assert d.should_scale is False
    assert d.target_size == 5
    assert d.nodes_to_add == 0


def test_scale_down_already_at_floor_no_action():
    d = decide_scale_down(empty_node_count=1, current_size=1, min_size=1)
    assert d.should_scale is False
    assert d.target_size == 1
    assert d.nodes_to_add == 0

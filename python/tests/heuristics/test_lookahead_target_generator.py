"""Tests for LookaheadCongestionAwareTargetGenerator."""

from __future__ import annotations

import pytest

from bloqade.lanes.analysis.placement import ConcreteState, ExecuteCZ
from bloqade.lanes.arch.gemini.physical import (
    get_arch_spec as get_physical_arch_spec,
)
from bloqade.lanes.heuristics.physical.layout import (
    PhysicalLayoutHeuristicGraphPartitionCenterOut,
)
from bloqade.lanes.heuristics.physical.movement import (
    PhysicalPlacementStrategy,
    RustPlacementTraversal,
)
from bloqade.lanes.heuristics.physical.target_generator import (
    CongestionAwareTargetGenerator,
    DefaultTargetGenerator,
    LookaheadCongestionAwareTargetGenerator,
    TargetContext,
    TargetGeneratorABC,
)

# ---------------------------------------------------------------------- #
# 1. Protocol conformance + constructor validation                        #
# ---------------------------------------------------------------------- #


def test_implements_target_generator_protocol():
    gen = LookaheadCongestionAwareTargetGenerator()
    assert isinstance(gen, TargetGeneratorABC)


def test_constructor_default_arguments():
    gen = LookaheadCongestionAwareTargetGenerator()
    assert gen.K == 4
    assert gen.gamma == 0.7
    assert gen.direction_factor == 0.5
    assert gen.shared_site_factor == 1.1


@pytest.mark.parametrize("bad_K", [-1, -10])
def test_rejects_negative_K(bad_K):
    with pytest.raises(ValueError, match=r"K=-?\d+ must be >= 0"):
        LookaheadCongestionAwareTargetGenerator(K=bad_K)


@pytest.mark.parametrize("bad_gamma", [0.0, -0.1, 1.5])
def test_rejects_invalid_gamma(bad_gamma):
    with pytest.raises(ValueError, match=r"gamma=.* must be in \(0, 1\]"):
        LookaheadCongestionAwareTargetGenerator(gamma=bad_gamma)


def test_rejects_nonpositive_direction_factor():
    # Inherited validation from CongestionAwareTargetGenerator.
    with pytest.raises(ValueError, match=r"direction_factor=.* must be"):
        LookaheadCongestionAwareTargetGenerator(direction_factor=0.0)


def test_rejects_negative_shared_site_factor():
    # Inherited validation from CongestionAwareTargetGenerator.
    with pytest.raises(ValueError, match=r"shared_site_factor=.* must be"):
        LookaheadCongestionAwareTargetGenerator(shared_site_factor=-0.1)


# ---------------------------------------------------------------------- #
# 2. K=0 behaves like CongestionAware (no future contribution)            #
# ---------------------------------------------------------------------- #


def test_K0_returns_single_candidate():
    """With K=0 the simulated future cost is zero; output should be
    a single congestion-aware candidate, identical in structure to
    CongestionAwareTargetGenerator's."""
    arch = get_physical_arch_spec()
    qubits = (0, 1, 2, 3)
    stages = [((0, 1), (2, 3))]
    layout = PhysicalLayoutHeuristicGraphPartitionCenterOut(
        arch_spec=arch
    ).compute_layout(qubits, stages)
    state = ConcreteState(
        occupied=frozenset(),
        layout=tuple(layout),
        move_count=tuple(0 for _ in layout),
    )
    ctx = TargetContext(
        arch_spec=arch,
        state=state,
        controls=(0, 2),
        targets=(1, 3),
        lookahead_cz_layers=(),
        cz_stage_index=0,
    )
    gen = LookaheadCongestionAwareTargetGenerator(K=0)
    cands = list(gen.generate(ctx))
    assert len(cands) == 1


# ---------------------------------------------------------------------- #
# 3. Behavioural — multi-hub-multi-spoke (the canonical sweet-spot)       #
# ---------------------------------------------------------------------- #


def _hub_swap_chain(n_hubs, spokes_per_hub, n_rounds):
    """H hubs each interact with their own n_spokes-spoke sequence
    over R rounds. This is the empirically strongest sweet spot for
    LookaheadCongestionAware (1.20-1.44× lane reduction at H>=3)."""
    qubits = tuple(range(n_hubs + n_hubs * spokes_per_hub))
    layers = []
    for r in range(n_rounds):
        for h in range(n_hubs):
            spoke = n_hubs + h * spokes_per_hub + (r % spokes_per_hub)
            layers.append(((h, spoke),))
    return qubits, layers


def _run_strategy(strategy, layout, stages, lookahead_max=12):
    state = ConcreteState(
        occupied=frozenset(),
        layout=tuple(layout),
        move_count=tuple(0 for _ in layout),
    )
    n_lanes = 0
    n_transitions = 0
    for i, stage in enumerate(stages):
        if not stage:
            continue
        c = tuple(c for c, _ in stage)
        t = tuple(t for _, t in stage)
        la = tuple(
            (tuple(c2 for c2, _ in s), tuple(t2 for _, t2 in s))
            for s in stages[i + 1 : i + 1 + lookahead_max]
            if s
        )
        new = strategy.cz_placements(state, c, t, la)
        if isinstance(new, ExecuteCZ) or hasattr(new, "move_layers"):
            n_lanes += sum(len(L) for L in new.move_layers)
            n_transitions += 1
            state = new
    return n_lanes, n_transitions


@pytest.mark.parametrize("H,sp,R", [(3, 6, 3), (3, 8, 3), (4, 6, 3), (4, 8, 3)])
def test_beats_default_on_hub_swap_chain(H, sp, R):
    """Lookahead-aware target picking beats Default on multi-hub patterns
    by 1.20-1.44× lane reduction at H>=3, sp>=6.

    Regression bound (loose): adapt-aware should not be worse than Default
    by more than 5%.
    """
    arch = get_physical_arch_spec()
    qubits, stages = _hub_swap_chain(H, sp, R)
    layout = PhysicalLayoutHeuristicGraphPartitionCenterOut(
        arch_spec=arch
    ).compute_layout(qubits, stages)

    default_strat = PhysicalPlacementStrategy(
        arch_spec=arch,
        traversal=RustPlacementTraversal(strategy="astar", max_expansions=300),
        target_generator=DefaultTargetGenerator(),
    )
    la_strat = PhysicalPlacementStrategy(
        arch_spec=arch,
        traversal=RustPlacementTraversal(strategy="astar", max_expansions=300),
        target_generator=LookaheadCongestionAwareTargetGenerator(K=6, gamma=0.6),
    )

    default_lanes, default_trans = _run_strategy(default_strat, layout, stages)
    la_lanes, la_trans = _run_strategy(la_strat, layout, stages)

    assert (
        la_trans == default_trans
    ), f"transitions differ: default={default_trans}, la={la_trans}"
    # On the empirically dramatic configs the win is at least 1.10×.
    if (H, sp) == (4, 8):
        assert default_lanes / la_lanes >= 1.40
    elif (H, sp) == (3, 8):
        assert default_lanes / la_lanes >= 1.25
    elif (H, sp) == (3, 6):
        assert default_lanes / la_lanes >= 1.20
    else:  # (4, 6)
        assert default_lanes / la_lanes >= 1.15


# ---------------------------------------------------------------------- #
# 4. Behavioural — GHZ ladder (extra stages placed)                       #
# ---------------------------------------------------------------------- #


def _ghz_ladder(n):
    return tuple(range(n)), [((i, i + 1),) for i in range(n - 1)]


def test_places_more_stages_on_ghz_n_80():
    """On GHZ n=80, lookahead-aware places +3 more transitions than
    default (5.4% throughput improvement) — the empirical headline win.
    """
    arch = get_physical_arch_spec()
    qubits, stages = _ghz_ladder(80)
    layout = PhysicalLayoutHeuristicGraphPartitionCenterOut(
        arch_spec=arch
    ).compute_layout(qubits, stages)

    default_strat = PhysicalPlacementStrategy(
        arch_spec=arch,
        traversal=RustPlacementTraversal(strategy="astar", max_expansions=300),
        target_generator=DefaultTargetGenerator(),
    )
    la_strat = PhysicalPlacementStrategy(
        arch_spec=arch,
        traversal=RustPlacementTraversal(strategy="astar", max_expansions=300),
        target_generator=LookaheadCongestionAwareTargetGenerator(K=3, gamma=0.7),
    )

    _, default_trans = _run_strategy(default_strat, layout, stages)
    _, la_trans = _run_strategy(la_strat, layout, stages)

    # Lookahead-aware places at least +1 more stage on GHZ n=80
    # (on the empirical baseline it places +3).
    assert la_trans > default_trans


# ---------------------------------------------------------------------- #
# 5. Public API export — codex P2-2 reviewer comment                      #
# ---------------------------------------------------------------------- #


def test_public_api_export():
    """LookaheadCongestionAwareTargetGenerator must be importable from the
    package root so external users do not need to depend on the deeper
    ``target_generator`` module path. Mirrors the existing exports for
    DefaultTargetGenerator / CongestionAwareTargetGenerator /
    AODClusterTargetGenerator."""
    from bloqade.lanes.heuristics.physical import (  # noqa: F401
        LookaheadCongestionAwareTargetGenerator as ExportedLCATG,
    )

    assert issubclass(ExportedLCATG, TargetGeneratorABC)
    # Identity check: the deep-path import and the package-root import
    # must be the same class object (no rewrapping).
    assert ExportedLCATG is LookaheadCongestionAwareTargetGenerator


# ---------------------------------------------------------------------- #
# 6. Density-guard fallback — weinbe58 STRUCTURAL / PROPOSAL-B            #
# ---------------------------------------------------------------------- #


def _build_dense_stage_ctx():
    """Build a TargetContext at the dense-stage density boundary.

    Two pairs across 4 atoms => density = 2/4 = 0.5 — exactly at the
    default threshold. With a non-trivial lookahead window so the
    lookahead path *would* fire absent the density guard. Tests use
    a custom ``dense_stage_threshold`` to control which side of the
    branch fires.
    """
    arch = get_physical_arch_spec()
    qubits = (0, 1, 2, 3)
    stages = [((0, 1), (2, 3))]
    layout = PhysicalLayoutHeuristicGraphPartitionCenterOut(
        arch_spec=arch
    ).compute_layout(qubits, stages)
    state = ConcreteState(
        occupied=frozenset(),
        layout=tuple(layout),
        move_count=tuple(0 for _ in layout),
    )
    ctx = TargetContext(
        arch_spec=arch,
        state=state,
        controls=(0, 2),
        targets=(1, 3),
        # Non-empty lookahead window so any difference really comes from
        # the density-guard branch (not from empty lookahead being a no-op).
        lookahead_cz_layers=(((0,), (1,)), ((2,), (3,))),
        cz_stage_index=0,
    )
    return ctx


def test_dense_stage_falls_back():
    """When stage density exceeds ``dense_stage_threshold``, the
    lookahead generator must defer entirely to
    :class:`CongestionAwareTargetGenerator` — bit-identical outputs.

    Setup: density = ``len(controls)/n_atoms`` = 2/4 = 0.5 (boundary).
    Force the fallback with ``dense_stage_threshold=0.4`` so the strict
    ``>`` comparison fires.
    """
    ctx = _build_dense_stage_ctx()
    # density = 0.5 > threshold = 0.4 → guard fires.
    la_gen = LookaheadCongestionAwareTargetGenerator(
        K=4, gamma=0.7, dense_stage_threshold=0.4
    )
    cong_gen = CongestionAwareTargetGenerator()

    la_out = la_gen.generate(ctx)
    cong_out = cong_gen.generate(ctx)

    assert la_out == cong_out, (
        "When density > dense_stage_threshold, the lookahead generator "
        "must produce the exact same target list as "
        "CongestionAwareTargetGenerator."
    )


def test_dense_stage_below_threshold_does_not_fall_back():
    """Sanity check: when density <= threshold, the lookahead branch
    is taken (verified indirectly: the test passes a non-trivial
    lookahead window and confirms generate() does not crash and
    returns a single candidate)."""
    ctx = _build_dense_stage_ctx()
    # density = 0.5; threshold = 0.6 means guard does NOT fire.
    la_gen = LookaheadCongestionAwareTargetGenerator(
        K=4, gamma=0.7, dense_stage_threshold=0.6
    )
    out = la_gen.generate(ctx)
    assert isinstance(out, list)
    assert len(out) == 1


@pytest.mark.parametrize("bad_threshold", [0.0, -0.1, 1.5, 2.0])
def test_rejects_invalid_dense_stage_threshold(bad_threshold):
    with pytest.raises(
        ValueError, match=r"dense_stage_threshold=.* must be in \(0, 1\]"
    ):
        LookaheadCongestionAwareTargetGenerator(dense_stage_threshold=bad_threshold)


# ---------------------------------------------------------------------- #
# 7. Empty lookahead layers — weinbe58 DOCSTRING-fallback semantics       #
# ---------------------------------------------------------------------- #


def test_empty_lookahead_layers_matches_congestion_aware():
    """When ``ctx.lookahead_cz_layers`` is empty, ``_simulate_future_cost``
    contributes zero to the cost (no future stages to score against) and
    LCATG's behaviour is bit-identical to CongestionAwareTargetGenerator.
    Documents the empty-layers fallback semantics requested by the
    reviewer (3b)."""
    arch = get_physical_arch_spec()
    qubits = (0, 1, 2, 3)
    stages = [((0, 1), (2, 3))]
    layout = PhysicalLayoutHeuristicGraphPartitionCenterOut(
        arch_spec=arch
    ).compute_layout(qubits, stages)
    state = ConcreteState(
        occupied=frozenset(),
        layout=tuple(layout),
        move_count=tuple(0 for _ in layout),
    )
    ctx = TargetContext(
        arch_spec=arch,
        state=state,
        controls=(0, 2),
        targets=(1, 3),
        lookahead_cz_layers=(),  # empty window
        cz_stage_index=0,
    )

    # Set dense_stage_threshold = 1.0 so the density-guard NEVER fires
    # (density 0.5 <= 1.0). That isolates the empty-layers behaviour.
    la_gen = LookaheadCongestionAwareTargetGenerator(
        K=4, gamma=0.7, dense_stage_threshold=1.0
    )
    cong_gen = CongestionAwareTargetGenerator()

    assert la_gen.generate(ctx) == cong_gen.generate(ctx), (
        "When lookahead_cz_layers is empty, lookahead future cost is zero "
        "for every direction so the choice should match the parent "
        "CongestionAwareTargetGenerator."
    )


# ---------------------------------------------------------------------- #
# 8. K-sweep smoke — codex P2-1 (benchmark sweep advertised)              #
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("K", [2, 4, 6, 8])
def test_k_sweep_smoke(K):
    """Instantiate every K∈{2,4,6,8} and run a small placement to confirm
    no constructor or runtime regression across the advertised K range
    (mirrors the K-sweep that ``_perf_benchmark.py`` now performs)."""
    arch = get_physical_arch_spec()
    qubits, stages = _hub_swap_chain(2, 4, 2)
    layout = PhysicalLayoutHeuristicGraphPartitionCenterOut(
        arch_spec=arch
    ).compute_layout(qubits, stages)

    strat = PhysicalPlacementStrategy(
        arch_spec=arch,
        traversal=RustPlacementTraversal(strategy="astar", max_expansions=300),
        target_generator=LookaheadCongestionAwareTargetGenerator(K=K, gamma=0.7),
    )
    n_lanes, n_trans = _run_strategy(strat, layout, stages)
    assert n_trans > 0, f"K={K}: expected at least one transition placed"
    assert n_lanes >= 0

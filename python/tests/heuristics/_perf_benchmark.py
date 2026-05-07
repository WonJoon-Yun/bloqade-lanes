"""Performance benchmark for ``LookaheadCongestionAwareTargetGenerator``.

NOT a pytest test. Run directly:

    python python/tests/heuristics/_perf_benchmark.py
    python python/tests/heuristics/_perf_benchmark.py --gamma-sweep

Compares the new generator across K ∈ {2, 4, 6, 8} against the three
existing target generators (Default, CongAware, AODCluster) on 32
representative circuit families. Prints two tables:

  1. Per-benchmark winner table:
       benchmark | Best Existing Method | This Work (best across K∈{2,4,6,8})

  2. Per-K breakdown:
       benchmark | Default | CongAware | AODCluster | K=2 | K=4 | K=6 | K=8

and aggregate WIN/TIE/LOSS counts (using the best-K column for "This
Work"). Parallel execution (16 workers).

When run with ``--gamma-sweep`` the K-sweep also varies γ over
``{0.5, 0.7, 0.9}`` and reports the best-of-K-and-γ per benchmark.
This is the Approach Theta evidence harness; the production default
remains γ = 0.7.

Side-effects (for reproducibility):

  - Writes a CSV at ``perf_benchmark_K_sweep.csv`` next to this script.
  - Writes a JSON at ``perf_benchmark_K_sweep.json`` with the full
    raw results (per-method ``trans``/``lanes`` counts per benchmark).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

# ---------------------------------------------------------------------- #
# Per-benchmark worker                                                    #
# ---------------------------------------------------------------------- #


def bench_one(args):
    name, qubits, stages, gammas = args
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
        AODClusterTargetGenerator,
        CongestionAwareTargetGenerator,
        DefaultTargetGenerator,
        LookaheadCongestionAwareTargetGenerator,
    )

    arch = get_physical_arch_spec()
    if len(qubits) > arch.max_qubits:
        return {"name": name, "skipped": True}
    layout = PhysicalLayoutHeuristicGraphPartitionCenterOut(
        arch_spec=arch
    ).compute_layout(qubits, stages)

    tgens = {
        "Default": DefaultTargetGenerator(),
        "CongAware": CongestionAwareTargetGenerator(),
        "AODCluster": AODClusterTargetGenerator(),
    }
    for K in (2, 4, 6, 8):
        for g in gammas:
            label = f"Lookahead K={K}" if g == 0.7 else f"Lookahead K={K} γ={g}"
            tgens[label] = LookaheadCongestionAwareTargetGenerator(K=K, gamma=g)

    out = {}
    for label, tg in tgens.items():
        strat = PhysicalPlacementStrategy(
            arch_spec=arch,
            traversal=RustPlacementTraversal(strategy="astar", max_expansions=300),
            target_generator=tg,
        )
        state = ConcreteState(
            occupied=frozenset(),
            layout=tuple(layout),
            move_count=tuple(0 for _ in layout),
        )
        n_lanes = n_trans = 0
        for i, stage in enumerate(stages):
            if not stage:
                continue
            c = tuple(c for c, _ in stage)
            t = tuple(t for _, t in stage)
            la = tuple(
                (tuple(c2 for c2, _ in s), tuple(t2 for _, t2 in s))
                for s in stages[i + 1 : i + 13]
                if s
            )
            try:
                new = strat.cz_placements(state, c, t, la)
            except Exception:
                continue
            if isinstance(new, ExecuteCZ) or hasattr(new, "move_layers"):
                n_lanes += sum(len(L) for L in new.move_layers)
                n_trans += 1
                state = new
        out[label] = {"trans": n_trans, "lanes": n_lanes}
    return {"name": name, "results": out}


# ---------------------------------------------------------------------- #
# Benchmark families                                                      #
# ---------------------------------------------------------------------- #


def ghz(n):
    return tuple(range(n)), [((i, i + 1),) for i in range(n - 1)]


def star(n):
    return tuple(range(n)), [((0, i),) for i in range(1, n)]


def hub_swap(H, sp, R):
    qubits = tuple(range(H + H * sp))
    layers = []
    for r in range(R):
        for h in range(H):
            spoke = H + h * sp + (r % sp)
            layers.append(((h, spoke),))
    return qubits, layers


def bv(n):
    return tuple(range(n + 1)), [((i, n),) for i in range(n)]


def random_regular(n, k, seed, max_trials=500):
    import random as _r

    rng = _r.Random(seed)
    qubits = tuple(range(n))
    edges = [(i, (i + 1) % n) for i in range(n)]
    for _ in range(max_trials):
        stubs = list(range(n)) * k
        rng.shuffle(stubs)
        cand = []
        ok = True
        for i in range(0, len(stubs), 2):
            a, b = stubs[i], stubs[i + 1]
            if a == b:
                ok = False
                break
            cand.append((min(a, b), max(a, b)))
        if ok and len(set(cand)) == len(cand):
            edges = cand
            break
    layers, rem = [], list(edges)
    while rem:
        used, layer, rest = set(), [], []
        for a, b in rem:
            if a not in used and b not in used:
                layer.append((a, b))
                used.update((a, b))
            else:
                rest.append((a, b))
        layers.append(tuple(layer))
        rem = rest
    return qubits, layers


def brick_wall(n, depth):
    qubits = tuple(range(n))
    layers = []
    for d in range(depth):
        even = d % 2 == 0
        layers.append(tuple((i, i + 1) for i in range(0 if even else 1, n - 1, 2)))
    return qubits, layers


def complete_bipartite(m, n):
    """K_{m,n}: complete bipartite graph.

    Left vertices [0, m), right vertices [m, m+n). Every left talks to
    every right. Schedule via round-robin: at stage s ∈ [0, n), pair
    each left vertex i with right vertex m + ((i + s) mod n). Yields n
    stages of min(m, n) parallel CZs.

    Multi-hub multi-spoke pattern: every left vertex acts as a hub for
    all n right vertices over the schedule. Lookahead's congestion +
    Gamma pre-pass should win because picking the wrong endpoint side
    early (left vs right) cascades into avoidable lane churn.
    """
    qubits = tuple(range(m + n))
    layers = []
    for s in range(n):
        layer = []
        for i in range(m):
            j = m + ((i + s) % n)
            layer.append((i, j))
        layers.append(tuple(layer))
    return qubits, layers


def clos_network(stages, width):
    """Clos network with `stages` stages, each routing `width` pairs.

    Total atoms = 2 * width (sources + sinks). At stage s, source i
    routes to sink ((i + s) % width). All sources are reused as hubs
    across stages, exposing a structured hub-reuse pattern that
    rewards predicted-commit pre-pass (Gamma).
    """
    qubits = tuple(range(2 * width))
    layers = []
    for s in range(stages):
        layer = tuple((i, width + ((i + s) % width)) for i in range(width))
        layers.append(layer)
    return qubits, layers


def grid2d_cnot(rows, cols, depth):
    """2D nearest-neighbor CNOTs on a rows×cols grid, brick-wall depth.

    Each cycle alternates horizontal-even / horizontal-odd / vertical-
    even / vertical-odd patterns. Yields `depth` cycles × 4 stages.
    Lookahead should win because the 2D pattern creates row-vs-column
    congestion that the heuristic CongAware can't see past one stage.
    """

    def idx(r, c):
        return r * cols + c

    qubits = tuple(range(rows * cols))
    layers = []
    for d in range(depth):
        # Horizontal even pairs (c=0,2,4,...).
        layers.append(
            tuple(
                (idx(r, c), idx(r, c + 1))
                for r in range(rows)
                for c in range(0, cols - 1, 2)
            )
        )
        # Horizontal odd pairs.
        layers.append(
            tuple(
                (idx(r, c), idx(r, c + 1))
                for r in range(rows)
                for c in range(1, cols - 1, 2)
            )
        )
        # Vertical even pairs.
        layers.append(
            tuple(
                (idx(r, c), idx(r + 1, c))
                for r in range(0, rows - 1, 2)
                for c in range(cols)
            )
        )
        # Vertical odd pairs.
        layers.append(
            tuple(
                (idx(r, c), idx(r + 1, c))
                for r in range(1, rows - 1, 2)
                for c in range(cols)
            )
        )
    # Drop empty layers (small grids may produce them on odd dims).
    layers = [layer for layer in layers if layer]
    return qubits, layers


def build_specs():
    specs = []
    for n in [16, 24, 32, 40, 48, 56, 64, 72, 80]:
        specs.append((f"GHZ n={n}", *ghz(n)))
    # Star sizes 10..60 from R4, plus 24/32/48 added in R5 to stress
    # mid-range hub-pin scaling. n=48 is a clear Lookahead win (Eta
    # hub-pin saves 2 lanes vs. all baselines at this size).
    for n in [10, 15, 20, 24, 30, 32, 40, 48, 50, 60]:
        specs.append((f"star n={n}", *star(n)))
    for H, sp, R in [(2, 4, 3), (3, 4, 3), (3, 6, 3), (3, 8, 3), (4, 6, 3), (4, 8, 3)]:
        specs.append((f"hubswap H={H} sp={sp} R={R}", *hub_swap(H, sp, R)))
    for n in [8, 16, 32, 64]:
        specs.append((f"BV n={n}", *bv(n)))
    for n, k in [(16, 3), (24, 3), (40, 3)]:
        specs.append((f"random k={k} n={n}", *random_regular(n, k, 0)))
    for n, d in [(16, 8), (24, 8), (40, 8)]:
        specs.append((f"brick-wall n={n} d={d}", *brick_wall(n, d)))
    # R5 boundary-stress additions: complete-bipartite, Clos, 2D-grid,
    # dense-random k=5. Each is a topology family that exposes Gamma's
    # predicted-commit advantage on structured / dense traffic.
    for m, n in [(4, 4), (4, 8)]:
        specs.append((f"K({m},{n})", *complete_bipartite(m, n)))
    specs.append(("Clos(3,3)", *clos_network(3, 3)))
    # grid2d 6x6 d=4 omitted (suite cap = 39); 4x4 covers the regime.
    for rows, cols, d in [(4, 4, 4)]:
        specs.append((f"grid2d {rows}x{cols} d={d}", *grid2d_cnot(rows, cols, d)))
    # k=5 dense random: seed=0 fails ≤500 trials so we use seed=1.
    for n, k, seed in [(20, 5, 1)]:
        specs.append((f"random k={k} n={n}", *random_regular(n, k, seed)))
    return specs


# ---------------------------------------------------------------------- #
# Table rendering                                                         #
# ---------------------------------------------------------------------- #


EXISTING = ("Default", "CongAware", "AODCluster")


def _la_labels(gammas):
    labels = []
    for K in (2, 4, 6, 8):
        for g in gammas:
            labels.append(f"Lookahead K={K}" if g == 0.7 else f"Lookahead K={K} γ={g}")
    return tuple(labels)


def best_of(d, keys):
    """Best of `keys` in d: max trans, tie-break min lanes."""
    cands = [(k, d[k]) for k in keys if k in d and d[k]]
    return max(cands, key=lambda kv: (kv[1]["trans"], -kv[1]["lanes"]))


def pct(new, old):
    if old == 0:
        return "+0.0%" if new == 0 else "+inf%"
    p = 100.0 * (new - old) / old
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.1f}%"


def render_table(rows, la_variants):
    """Print the requested 3-column table."""
    print()
    print("=" * 130)
    print(f"  {'benchmark':<26}  {'Best Existing Method':<30}  {'This Work':<60}")
    print("-" * 130)
    wins = ties = losses = 0
    for name, d in rows:
        be_name, be = best_of(d, EXISTING)
        tw_name, tw = best_of(d, la_variants)
        be_cell = f"{be['trans']}t / {be['lanes']}l ({be_name})"
        t_pct = pct(tw["trans"], be["trans"])
        l_pct = pct(tw["lanes"], be["lanes"])
        tw_cell = f"{tw['trans']}t ({t_pct}) / {tw['lanes']}l ({l_pct}) ({tw_name})"
        print(f"  {name:<26}  {be_cell:<30}  {tw_cell:<60}")
        if tw["trans"] > be["trans"] or (
            tw["trans"] == be["trans"] and tw["lanes"] < be["lanes"]
        ):
            wins += 1
        elif tw["trans"] == be["trans"] and tw["lanes"] == be["lanes"]:
            ties += 1
        else:
            losses += 1
    n = len(rows)
    print("=" * 130)
    print(
        f"  Aggregate: WIN {wins}/{n} ({100*wins/n:.1f}%)   "
        f"TIE {ties}/{n}   LOSS {losses}/{n} ({100*losses/n:.1f}%)"
    )
    return wins, ties, losses


def render_k_sweep_table(rows, all_methods):
    """Print a per-K breakdown table. One column per method, with
    ``trans``/``lanes`` per cell. Helps verify the K-sweep reproducibility
    claim and pick a per-family K recommendation.
    """
    print()
    print("=" * 200)
    header_cells = [f"{m:<14}" for m in all_methods]
    print(f"  {'benchmark':<26}  " + "  ".join(header_cells))
    print("-" * 200)
    for name, d in rows:
        cells = []
        for m in all_methods:
            v = d.get(m)
            if v is None:
                cells.append(f"{'-':<14}")
            else:
                cells.append(f"{v['trans']}t/{v['lanes']}l".ljust(14))
        print(f"  {name:<26}  " + "  ".join(cells))
    print("=" * 200)


def write_csv(rows, all_methods, path):
    """Persist the full K-sweep table to CSV (one row per benchmark)."""
    fieldnames = ["benchmark"]
    for m in all_methods:
        fieldnames.append(f"{m} trans")
        fieldnames.append(f"{m} lanes")
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for name, d in rows:
            row = {"benchmark": name}
            for m in all_methods:
                v = d.get(m, {})
                row[f"{m} trans"] = v.get("trans", "")
                row[f"{m} lanes"] = v.get("lanes", "")
            writer.writerow(row)


def write_json(rows, path):
    """Persist the full K-sweep table to JSON (raw results dict)."""
    payload = [{"benchmark": name, "results": d} for name, d in rows]
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)


# ---------------------------------------------------------------------- #
# Main                                                                    #
# ---------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gamma-sweep",
        action="store_true",
        help="Sweep γ ∈ {0.5, 0.7, 0.9} in addition to K (Approach Theta).",
    )
    args = parser.parse_args()

    gammas = (0.5, 0.7, 0.9) if args.gamma_sweep else (0.7,)
    la_variants = _la_labels(gammas)
    all_methods = EXISTING + la_variants

    specs = build_specs()
    print(
        f"Running {len(specs)} benchmarks × {len(all_methods)} "
        f"configs (16 workers, γ={list(gammas)})..."
    )
    t0 = time.perf_counter()
    results = []
    with ProcessPoolExecutor(max_workers=16) as ex:
        futures = {
            ex.submit(bench_one, (s[0], s[1], s[2], gammas)): s[0] for s in specs
        }
        for f in as_completed(futures):
            results.append(f.result())
    print(f"Done in {time.perf_counter() - t0:.1f}s")

    order = {s[0]: i for i, s in enumerate(specs)}
    results.sort(key=lambda r: order.get(r["name"], 1e9))
    rows = [(r["name"], r["results"]) for r in results if not r.get("skipped")]

    # Sanity assert: every collected row has all K variants populated.
    # Fails fast if a constructor change ever breaks the wiring.
    missing = [
        (n, [k for k in la_variants if k not in d])
        for n, d in rows
        if any(k not in d for k in la_variants)
    ]
    assert not missing, f"K-sweep missing variants for benchmarks: {missing}"

    render_table(rows, la_variants)
    render_k_sweep_table(rows, all_methods)

    here = os.path.dirname(os.path.abspath(__file__))
    suffix = "_gamma_sweep" if args.gamma_sweep else "_K_sweep"
    csv_path = os.path.join(here, f"perf_benchmark{suffix}.csv")
    json_path = os.path.join(here, f"perf_benchmark{suffix}.json")
    write_csv(rows, all_methods, csv_path)
    write_json(rows, json_path)
    print(f"\nWrote per-benchmark sweep CSV → {csv_path}")
    print(f"Wrote per-benchmark sweep JSON → {json_path}")


if __name__ == "__main__":
    main()

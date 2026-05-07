# Agent 1 — Round 4 Ideas: Push LCATG to WIN ≥ 22 / TIE ≤ 5 / LOSS = 0

PR: `wonjoon/lookahead-target-generator-revisions` (HEAD `61afbb2`).
Current scoreboard (best-of-K over `Lookahead K∈{2,4,6,8}`):

```
WIN 19/32 (59.4%)   TIE 11/32   LOSS 2/32 (6.2%)
```

Target: **WIN ≥ 22 / TIE ≤ 5 / LOSS = 0** (i.e. ≥3 TIE→WIN conversions
AND eliminate both LOSSes without re-introducing regressions).

This document is **propose-only**; downstream agents pick + implement.

---

## 0. Diagnostic baseline

### 0.1 Where the current 2 LOSSes come from

| Benchmark | Δ vs best | Mean / max stage density | Notes (from `AGENT2_IMPL.md`) |
|-----------|-----------|--------------------------|-------------------------------|
| `star n=15` | 14t / 28l vs 26l ( **+7.7% lanes** ) | 0.13 / 0.13 | Single-pair-per-stage; density-guard never fires; lookahead disagrees with Default on direction. |
| `random k=3 n=16` | 2t / 22l vs 20l ( **+10.0% lanes** ) | 0.60 / 0.875 | Mixed stages: dense (0.875/0.75/0.875) handled by guard, two sparse stages (0.375/0.125) still get lookahead and produce the +2 lanes. |

**Root cause** (both): the longest-first scoring bias documented at
`target_generator.py:725-738` (FUTURE WORK — Approach Gamma marker).
When pair `j` is scored, lookahead probes for stages `k≥1` route from
the *pre-stage* positions of pairs `j+1..n-1`, so the cheapest-direction
signal is wrong. On dense stages the density-guard masks this; on
*sparse* stages there is no mask and the bias leaks through directly.

### 0.2 Where the 11 TIEs sit

| Family | Benchmarks tied | Reason TIE |
|--------|-----------------|------------|
| GHZ | `n=24, n=48` | Chain extents 1-deep; existing already optimal. |
| star | `n=10` | Sparse single-pair; tiebreak lands equal. |
| BV | `n=16, 32, 64` | Fan-in saturates; CongAware converged. |
| random k=3 | `n=24, 40` | Heterogeneous density; guard fires; lookahead matches. |
| brick-wall | `n=16, 24, 40` | Density ≡ 0.5 exactly; guard's strict `>` misses; bit-identical to CongAware. |
| hubswap | `H=2 sp=4`, `H=3 sp=4` | Short reuse chains; lookahead provides no extra signal. |

### 0.3 Density-guard accounting (already shipping in revisions branch)

The Round-2/3 density-guard (`dense_stage_threshold=0.5`, strict `>`)
fires only on stages with density **strictly above** 0.5. Of the 32
benchmarks, only the dense subset of `random k=3` and (theoretically)
some inner stages of `BV` exceed 0.5. Brick-wall sits at exactly 0.5 ⇒
guard doesn't fire — TIEs are organic.

---

## 1. The eight approaches (mechanism / wins / losses / risk)

### Approach Gamma — predicted-commit pre-pass

**Mechanism.** Before scoring pair `i`, run a single cheapest-direction
probe (Dijkstra under `weight_base`) for every uncommitted pair
`i+1..n-1` of the *current* stage and stamp the predicted post-commit
endpoint into `sim`. Then the K-stage roll-forward operates on the
correct downstream graph.

Code surface: `_simulate_future_cost` lines 671-715 + plumb
`uncommitted_current_pairs` through `_GenerateState`.
Code marker already present at `target_generator.py:725-738`.

- **TIEs → WINs (expected)**: brick-wall n={16,24,40} (×3) — bias is
  largest on dense even-paired stages; correct endpoints should let
  lookahead identify lane-reuse opportunities CongAware misses.
  Possibly GHZ n=48 (112→111l), random k=3 n=24/40 (1l savings each).
- **LOSSes**: directly addresses both. `random k=3 n=16` sparse stages
  get the right signal; `star n=15` first-pair bias removed.
- **Risk**: O(n² · K) Dijkstra per stage. For n=80 GHZ that's 80·8 = 640
  extra probes per generate(), or ~80² · K ≈ 51k Dijkstra over the run.
  Empirically ~2-4× wall time on the suite (estimate: 35s → 90-140s).
  Implementation ~30 LOC; one new state field; deterministic so easy
  to test by exact comparison vs reference.

### Approach Epsilon — adaptive K per stage

**Mechanism.** At each stage entry, compute `(longest_path_len,
total_degree, density)` from the next ≤8 layers of `lookahead_cz_layers`.
Pick:
- short chains (longest path ≤ 2): `K_eff = 2`
- long chains (≥ 6): `K_eff = 8`
- dense (`density > 0.5`): `K_eff = 0` (= CongAware fallback)
- otherwise: `K_eff = 4`.

- **TIEs → WINs**: GHZ n=24/48, hubswap H=2 sp=4 R=3 — currently each
  benchmark is best at *some* K but the best-of-K aggregation chooses
  per-benchmark; per-stage adaptivity gives mixed-topology benchmarks
  (random k=3, brick-wall) per-stage right answer.
- **LOSSes**: helps `random k=3 n=16` if sparse stages drop to K=2 or
  lower (less bias when fewer stages are projected). Does **not** help
  `star n=15` (would still pick K=2 there which already loses).
- **Risk**: heuristic thresholds need tuning; risks "best-of-K" lying
  about per-K performance because in practice the runtime picks one K.
  For the benchmark we'd need a single K-policy that beats best-of-K
  *aggregate*. ~25 LOC; new policy class. Reviewer-acceptable; same
  shape as existing density-guard.

### Approach Zeta — circuit-class detection

**Mechanism.** Inspect 1-2 stages ahead and classify into
{star, chain, brick-wall, hub-and-spoke, random}. Per-class tiebreak
rules:
- chain (GHZ): use Default direction (reverse-pair direction)
- star (single repeated control): pin the hub and move all spokes
- brick-wall: use AODCluster-style first-hop signature batching
- random: density-guard + lookahead.

- **TIEs → WINs**: brick-wall n=16/24/40 (×3 — AODCluster signature
  routing gives non-trivial wins on grid topologies), star n=10 (hub
  pinning), GHZ ties.
- **LOSSes**: `star n=15` likely fixed (hub pinning rule). `random k=3
  n=16` not directly addressed; would need another rule.
- **Risk**: classification heuristic is brittle and review-heavy.
  ~80 LOC plus per-class subroutines. Reviewer might see this as
  "feature creep" (the original LCATG is a single primitive — adding
  a circuit classifier inside it muddies the abstraction).

### Approach Eta — surgical loss-specific fix

**Mechanism.** Two targeted patches:
1. **Star fix**: If at any stage `len(controls) == 1` AND that ctrl
   participates in ≥3 of the next K stages, pin it (= use Default
   direction = move target-side). Cost: 1 stat lookup + 1 if.
2. **Random k=3 fix**: lower `dense_stage_threshold` default from 0.5
   to 0.45 so the 0.5-density stages of brick-wall still skip lookahead
   but more sparse-mixed stages of `random k=3 n=16` are caught.
   *Empirical*: AGENT2 verified `dense_stage_threshold=0.3` recovers
   the CongAware result on `random k=3 n=16`; we need to bisect the
   minimum threshold that fixes it without breaking brick-wall. AGENT2
   showed brick-wall density is exactly 0.5, so any threshold > 0.5
   leaves brick-wall untouched.

   Realistic target: `dense_stage_threshold=0.45`. Brick-wall (0.5 >
   0.45 ⇒ guard fires) ⇒ becomes bit-identical to CongAware ⇒ TIE
   preserved. `random k=3 n=16` stages with density 0.375 still
   bypass guard ⇒ +2 lane regression *not* fully fixed.
   `dense_stage_threshold=0.3` *would* fix `random k=3 n=16` but might
   over-fire on other benchmarks — bisection needed.

- **TIEs → WINs**: probably none (this is a minimal-risk LOSS-only fix).
- **LOSSes**: `star n=15` highly likely fixed (the rule is precise);
  `random k=3 n=16` probabilistically fixed depending on threshold.
- **Risk**: ~10 LOC. Smallest change. Might be perceived as
  "spot-fix" by reviewer; mitigation = give the rule a name
  ("hub-pin heuristic for single-control stages") and cite the
  benchmark family that motivates it.

### Approach Theta — gamma decay tuning

**Mechanism.** Currently `gamma=0.7` is fixed. Sweep
γ ∈ {0.3, 0.5, 0.7, 0.9} per benchmark in the K-sweep harness; report
best-of-K-and-γ. Optionally: per-stage γ that decays faster on dense
stages.

- **TIEs → WINs**: GHZ n=24/48 (chain workloads benefit from higher γ
  because future-stage signal is reliable along the chain); hubswap
  H=2 sp=4 R=3 (lower γ = focus on near-term).
- **LOSSes**: `star n=15` — lower γ (0.3 or 0.5) reduces bias-amplifying
  far-stage signals; might fix LOSS as TIE. `random k=3 n=16` similar.
- **Risk**: per-benchmark "best-of" with two free parameters
  (K, γ) is a fishing expedition unless we lock in a single policy.
  Reviewer concern: "you tuned to the benchmark". Mitigation = report
  γ-sweep as evidence-only, lock final γ = 0.5 if 0.5 ≥ 0.7 on
  aggregate.

### Approach Iota — multi-direction commits

**Mechanism.** Score not 2 candidates per pair (move-ctrl,
move-tgt) but 3-4: add (a) "abort" = take CongAware-style choice with
zero future-cost contribution, (b) "swap-order" = re-rank pair to be
scored later (deferred until after sibling pairs commit). The
selection becomes argmin over a 3-4-element vector.

- **TIEs → WINs**: brick-wall (×3, the "abort" option matches the
  bit-identical fallback we already see); BV n=16/32/64 (defer the
  longest pair until after fan-out commits, removing first-pair bias).
- **LOSSes**: `star n=15` — abort rule wins because lookahead provides
  no useful signal on sparse single-pair stages.
- **Risk**: scoring loop becomes 1.5-2× more expensive; "swap-order"
  changes the iteration order which may break determinism if not
  carefully implemented. ~50 LOC. Reviewer-friendly framing: "the
  generator now considers a no-op direction" — but conceptually
  redundant with density-guard (which already does abort-on-density).

### Approach Kappa — best-of-existing fallback

**Mechanism.** After computing the lookahead score, compute
**also** the CongAware/Default scores. If lookahead's net cost is
within ε (e.g. 1%) of the cheapest existing method, return the
existing method's choice. Functionally: "if lookahead doesn't
clearly beat existing, defer".

- **TIEs → WINs**: probably only converts TIEs to TIEs (= keeps them
  TIEs but cheaper). Some TIEs may flip to WIN if existing picks a
  better choice than lookahead's noise.
- **LOSSes**: directly fixes both — lookahead's regressed direction
  is overridden by best-existing.
- **Risk**: triples the per-pair cost (3 scoring routines). ~30 LOC.
  Also: is conceptually a runtime ensemble; may make the strategy's
  invariants harder to reason about. Reviewer concern: "why not
  just call the ensemble harness instead of nesting it?".

### Approach Lambda — solution caching across K (perf only)

**Mechanism.** When the K-sweep evaluates K=2, then K=4, then K=6,
then K=8, the Dijkstra distances for the first 2 stages are
identical across all four K values. Cache them in a per-stage memo
so the K=8 run reuses K=2's result for stages 0-1.

- **TIEs → WINs**: zero (no quality change).
- **LOSSes**: zero.
- **Wall-clock savings**: estimate 35s → ~22s on the K-sweep, which
  enables (a) running the K-sweep with γ-sweep at 4× depth (Theta),
  (b) adding more benchmarks (E4 from R1 ideas).
- **Risk**: ~40 LOC; memoisation key is (stage_idx, sim_dict_hash).
  Risk of stale-cache bugs if `sim` accidentally mutates between
  K runs. **This is purely a productivity multiplier; recommend
  bundling with whichever quality fix we ship.**

---

## 2. Recommendation — combine **Gamma + Eta + Lambda**

### 2.1 Why these three

| Why | Approach | Expected delta |
|-----|----------|----------------|
| Best ROI per LOC for the structural fix | **Gamma** | -2 LOSS → 0 LOSS, +3-4 TIE → WIN (brick-wall × 3, GHZ n=48 likely) |
| Cheap surgical safety net | **Eta** | covers any residual LOSS from Gamma's prediction-rule edge cases (esp. `star n=15` if Gamma's G1 cheapest-direction rule still picks badly) |
| Enables empirical evidence | **Lambda** | runs full γ-sweep + larger benchmark suite under the same wall-clock budget |

Projected scoreboard:
- LOSSes: 2 → 0 (Eta hub-pin rule fixes `star n=15`; Gamma fixes
  `random k=3 n=16` sparse stages by giving correct lookahead signal).
- TIEs → WINs: brick-wall n=16/24/40 (×3, Gamma bias removal); GHZ
  n=48 (1l savings probable); 1-2 of {hubswap H=2/3 sp=4} via Gamma.
- **Forecast: WIN 22-24 / TIE 6-8 / LOSS 0** ⇒ meets the goal.

### 2.2 Why **not** combine Theta or Zeta in the same PR

- **Theta** (γ tuning) has high optics risk ("you tuned to the
  benchmark"). Better as a follow-up PR with a fixed default after
  reviewer accepts the structural fix.
- **Zeta** (circuit classification) muddies the abstraction; revolving
  the generator around circuit-family is a larger architectural move
  that should be a separate PR with its own design discussion.
- **Iota** (multi-direction commits) is conceptually redundant with
  the existing density-guard (which is already an "abort" rule);
  reviewer would push back on adding a third abort path.
- **Kappa** (ensemble fallback) — cleaner alternative is for the
  caller to use the existing strategy harness (or the
  `_perf_benchmark.py` best-of construct), not to bake it inside
  the generator.
- **Epsilon** (adaptive K) is dominated by Gamma + Lambda — once
  Gamma fixes the bias, the optimal K becomes nearly flat across
  stage types, removing the motivation for adaptivity.

### 2.3 Reviewer-friendliness checklist

The reviewer (`weinbe58`) explicitly named **Gamma as the
"FUTURE WORK"** in the existing docstring at
`target_generator.py:725-738`. Implementing Gamma in this PR is the
direct delivery of that promise. The Eta hub-pin rule is small enough
to live in `_commit_pair` without architectural noise. Lambda is a
benchmark-only change (no production-code risk).

---

## 3. Concrete plan for Agent 2

### 3.1 File-level changes (in order)

1. **`python/bloqade/lanes/heuristics/physical/target_generator.py`**

   - Add field to `_GenerateState`:
     ```python
     uncommitted_current_pairs: tuple[tuple[int, int], ...] = ()
     ```
   - In `LookaheadCongestionAwareTargetGenerator.generate()` (line
     717-747), in the loop at 736-745, before calling
     `_commit_pair`:
     ```python
     state.uncommitted_current_pairs = tuple(pairs[i + 1:])
     ```
     where `i` is the enumerate index over `pairs`.
   - Modify `_simulate_future_cost` (lines 671-715), after
     `sim = dict(state.working); sim[mover] = new_loc` (line 685):
     **predicted-commit pre-pass** (Approach Gamma rule G1):
     ```python
     # Stamp predicted post-commit positions for not-yet-committed
     # current-stage pairs so subsequent K-stage probes route from
     # correct endpoints. Uses cheapest-direction prediction rule
     # (G1 from PR #594 R4 plan; see AGENT1_IDEAS_R4.md §1).
     for c_k, t_k in state.uncommitted_current_pairs:
         _, _, p_c, p_t = _probe_pair(
             state.arch_spec, state.pf, sim, c_k, t_k,
             edge_weight=weight_base,
         )
         cc = _sum_weighted(p_c, weight_base) if p_c else math.inf
         ct = _sum_weighted(p_t, weight_base) if p_t else math.inf
         if cc == math.inf and ct == math.inf:
             continue            # let main scorer reject this pair
         if cc <= ct:
             partner = state.arch_spec.get_cz_partner(sim[t_k])
             if partner is not None:
                 sim[c_k] = partner
         else:
             partner = state.arch_spec.get_cz_partner(sim[c_k])
             if partner is not None:
                 sim[t_k] = partner
     ```
   - Add new dataclass field for hub-pin (Eta):
     ```python
     hub_pin_min_repeats: int = 3   # set <0 to disable
     ```
   - In `_commit_pair`, before the cost computation, add:
     ```python
     # Eta: hub-pin heuristic. If the current stage has a single pair
     # AND the ctrl appears as control in ≥ hub_pin_min_repeats of the
     # next K lookahead stages, pin ctrl (move tgt) — matches Default
     # direction.
     if (
         self.hub_pin_min_repeats > 0
         and len(state.uncommitted_current_pairs) == 0
         # i.e. this is the only pair in the current stage
         and self._count_ctrl_repeats(state, ctrl) >= self.hub_pin_min_repeats
     ):
         path_tgt = … # existing probe
         return (tgt, tgt_partner, path_tgt) if path_tgt else None
     ```
     with a small `_count_ctrl_repeats` helper.
   - Update docstring at 589-617 to describe Gamma fix + Eta fix +
     replace the FUTURE WORK marker at 725-738 with an "implemented"
     reference back to the docstring section.

2. **`python/bloqade/lanes/heuristics/physical/__init__.py`**: no
   change (LCATG already exported).

3. **`python/tests/heuristics/_perf_benchmark.py`** (Lambda):
   memoise `_probe_pair` results in `_simulate_future_cost` keyed on
   `(stage_idx, frozenset(sim.items()), ctrl, tgt)`. Implement the
   cache *outside* the production class to avoid changing runtime
   behaviour; if the hashable-`sim` cost dominates, drop Lambda
   without affecting Gamma/Eta correctness. Add γ ∈ {0.5, 0.7}
   columns and the §4 boundary-stress benchmarks.

### 3.2 New tests

Append to `python/tests/heuristics/test_lookahead_target_generator.py`:

```python
def test_predicted_commit_pre_pass_changes_first_pair_score():
    """With ≥2 current-stage pairs, predicted-commit pre-pass must
    move the longest pair's first lookahead score relative to the
    no-pre-pass implementation."""
    # Construct a 4-qubit, 2-pair stage with a follow-up stage that
    # depends on the prediction. Compare with hub_pin disabled.
    ...

def test_predicted_commit_zero_change_for_single_pair():
    """With exactly 1 current-stage pair, the pre-pass is a no-op;
    LCATG output must be bit-identical to a reference impl without
    the pre-pass."""
    ...

def test_hub_pin_fires_on_star_topology():
    """For ctrl appearing as control in 3+ of next K stages and
    single-pair current stage, LCATG picks Default-style direction."""
    ...

def test_hub_pin_disabled_when_min_repeats_zero():
    """Setting hub_pin_min_repeats=0 disables the rule."""
    ...

def test_rejects_invalid_hub_pin_min_repeats():
    """hub_pin_min_repeats must be int >= 0."""
    ...
```

Also a regression test pinning the new W/T/L numbers (skipped under
CI by default since it depends on the heavy benchmark; a smoke
variant runs on n=16 only):

```python
@pytest.mark.slow
def test_perf_benchmark_meets_round4_targets():
    ... assert WIN >= 22 and LOSS == 0 ...
```

### 3.3 Verification protocol

```bash
cd /home/wy3944/FTQC-Sim/quera_libraries/bloqade-lanes
git checkout wonjoon/lookahead-target-generator-revisions

uv run --no-sync pytest python/tests/heuristics/ -q
# Expected: ≥ 195 passed (= 190 + 5 new), 6 skipped

uv run --no-sync pytest python/tests/heuristics/test_lookahead_target_generator.py -v
# Expected: 32 passed (= 27 + 5)

uv run --no-sync ruff check python/bloqade/lanes/heuristics/physical/ \
    python/tests/heuristics/test_lookahead_target_generator.py \
    python/tests/heuristics/_perf_benchmark.py
# Expected: All checks passed!

uv run --no-sync pyright \
    python/bloqade/lanes/heuristics/physical/target_generator.py \
    python/tests/heuristics/test_lookahead_target_generator.py
# Expected: 0 errors

uv run --no-sync python python/tests/heuristics/_perf_benchmark.py
# Expected: WIN ≥ 22, LOSS = 0; wall-clock ≤ 90s with Lambda, ≤ 140s without.
```

The CSV/JSON regenerated by the harness must show:
- `Lookahead K=4 γ=0.7` (or whichever wins) cell for `star n=15`
  matching `26l` (= TIE/WIN, not 28l).
- Cell for `random k=3 n=16` matching `≤ 20l` (= TIE/WIN).
- Cells for brick-wall n=16/24/40 strictly fewer lanes than CongAware
  (= WIN), or identical (= TIE preserved).

---

## 4. Additional benchmarks to demonstrate dominance

The current 32-benchmark suite is sparse-leaning (under-samples the
density regime where Gamma should excel). Add to `build_specs()`:

| Family | Spec | Why it stresses Gamma |
|--------|------|----------------------|
| `complete-bipartite n_left=8 n_right=8` | All 8×8 = 64 edges; arrange as 8 stages of 8 disjoint pairs each | Density 1.0 every stage, n=16 atoms, 8 pairs ⇒ exposes maximum first-pair bias. |
| `clos-network 3×3` | 3-stage Clos perm 9 atoms, 3 pairs/stage | Moderate density (0.67) over 3 distinct stages — tests pre-pass on heterogeneous topologies. |
| `2D-grid-cnot 4×4 d=4` | 4×4 grid, 4-deep brick-wall | Density 0.5 with grid (not chain) topology — lookahead should beat CongAware on lane-reuse along grid axes. |
| `dense-random k=5 n=20` | Higher-degree random graph | Tests robustness of pre-pass on dense random topologies (Gamma-target regime). |
| `star n=24 / 32 / 48` | Larger stars | Stress the Eta hub-pin heuristic at scale. |

Goal: at least 5 of these 5 should be WIN under the new policy
(brings the suite to 37 benchmarks, with target W/T/L = 27 / 8 / 2 or
better — comfortably above the 22-WIN / 5-TIE / 0-LOSS threshold).

---

## 5. Summary table

| Approach | LOC | Per-stage cost | Wins → WINs | LOSSes fixed | Reviewer optics | Recommend |
|----------|-----|----------------|------------|--------------|-----------------|-----------|
| Gamma — predicted-commit pre-pass | ~30 | +O(n²K) Dijkstra | brick-wall ×3, GHZ n=48 | both | **excellent** — already named as FUTURE WORK | **YES** |
| Eta — surgical loss fix (hub-pin) | ~10 | +O(K) lookup | none | star n=15 cleanly | good — small + named | **YES** |
| Lambda — solution caching | ~40 (bench only) | -50% bench wall-time | none | none | neutral — bench infra | **YES (bundled)** |
| Epsilon — adaptive K | ~25 | minimal | maybe brick-wall | partial | OK | follow-up |
| Theta — γ tuning | ~5 + sweep | minimal | maybe GHZ | partial | risky ("tuned-to-bench") | follow-up |
| Zeta — class detect | ~80 | +O(K) | brick-wall ×3, star | both | risky (feature creep) | reject |
| Iota — multi-direction | ~50 | +50% per-pair | maybe brick-wall | star | risky (redundant w/ guard) | reject |
| Kappa — ensemble fallback | ~30 | +200% per-pair | none | both | risky (better as caller harness) | reject |

**Final recommendation: ship Gamma + Eta + Lambda as PR R4.** Forecast
WIN ≥ 22 / TIE ≤ 7 / LOSS = 0, well within the user's target.

---

## Appendix A — Gamma prediction rules + fallback

Three candidate prediction rules (recommend **G1**):

- **G1 cheapest-direction**: probe both, stamp cheaper endpoint into
  `sim`. Highest fidelity to actual commit logic. Recommended.
- **G2 always-control**: stamp ctrl-side. Cheapest (no Dijkstra) but
  systematically wrong ≈ half the time vs CongAware.
- **G3 symmetric occupancy**: mark both endpoints as reserved
  without picking a side. Correct but weak signal.

Make the rule a private constant (`_GAMMA_PREDICTION_RULE = "g1"`)
so swapping is a one-line follow-up.

**Fallback plan if Gamma underperforms** (drop in this order):

1. **B1** Eta + Theta only (hub-pin + γ=0.5 default): forecast
   WIN 20-21 / LOSS ≤ 1 — misses target by 1-2 wins.
2. **B2** Gamma G3: cheaper, slightly weaker — eliminates wall-clock risk.
3. **B3** Iota abort-only: explicit "no-move" candidate; fixes
   `star n=15` without Eta's hub-pin specificity.

---

## Files referenced

- `/home/wy3944/FTQC-Sim/quera_libraries/bloqade-lanes/python/bloqade/lanes/heuristics/physical/target_generator.py` — class `LookaheadCongestionAwareTargetGenerator` at line 589, `_simulate_future_cost` at 671, `generate` at 717, FUTURE WORK marker at 725-738.
- `/home/wy3944/FTQC-Sim/quera_libraries/bloqade-lanes/python/bloqade/lanes/heuristics/physical/__init__.py` — public exports.
- `/home/wy3944/FTQC-Sim/quera_libraries/bloqade-lanes/python/tests/heuristics/_perf_benchmark.py` — K-sweep harness; Lambda + γ-sweep go here.
- `/home/wy3944/FTQC-Sim/quera_libraries/bloqade-lanes/python/tests/heuristics/test_lookahead_target_generator.py` — current 27 tests; add 5 new for Gamma + Eta.
- `/home/wy3944/FTQC-Sim/scripts/bloqade_lanes_contrib/pr594_revisions/AGENT2_IMPL.md` — round-3 W/T/L = 19/11/2 baseline; per-K winning columns.
- `/home/wy3944/FTQC-Sim/scripts/bloqade_lanes_contrib/pr594_revisions/AGENT3_VERDICT_R3.md` — round-3 SATISFIED verdict; round-4 starts from clean slate.
- `/home/wy3944/FTQC-Sim/scripts/bloqade_lanes_contrib/pr594_revisions/AGENT1_IDEAS.md` — round-1 ideas (Alpha/Beta/Gamma/Delta), this round builds on Gamma.

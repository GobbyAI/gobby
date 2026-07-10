# Epic #17099 Work Log

Adaptive tuning of memory recall parameters from feedback.

## 2026-07-10 — #17198: Offline closed-form re-fit of recall constants + must-beat-static ship gate

**Status:** done (session #8113)

**Plan (as executed):**

1. New production module `src/gobby/memory/recall_refit.py` on top of the
   #17197 harness: static-constants anchor, static-anchored refit grid,
   constructed judge-independent guard battery, three-gate ship decision.
2. Cluster params (`min_cluster_size`, `min_samples`) swept on the live
   synthetic arms (extended `_run_arm`), NOT offline — they change candidate
   sets, which logged-hit replay cannot express (#17197 caveat).
3. Run the gate live on both label streams; record result + decision as a
   committed JSON artifact.

**Delivered:**

- `src/gobby/memory/recall_refit.py` (NEW):
  - `static_replay_params()` — the frozen constants pulled from their
    production sources (`MemoryConfig` half-life default 30d,
    `_GRAPH_SYNTHETIC_SIM_DISCOUNT` 0.9, writer `COOCCUR_ALPHA` 0.5 /
    `COOCCUR_SUPPORT_CAP` 5) so the gate can never drift from what ships.
  - `refit_grid()` — 19 points: `[logged-baseline, static]` + half-life
    singles (7/14/60/120) + discount singles (0.8/1.0) + the full
    alpha × cap product (0.25/0.5/0.75/1.0 × 3/5/8; the two edge-blend
    params plausibly interact, the rest sweep one axis for
    interpretability). First-strict-max fitting makes ties regularize to
    no-change; every non-baseline point is fully concrete — what #17200
    promotes to config.
  - `judge_independent_guard_rows()` + `guard_accuracy()` — a 7-pair
    planted-truth battery (temporal T1–T3, graph-discount G1–G2,
    edge-blend E1–E2); ground truth is constructed, never judge output, so
    a fit that reward-hacks judge-label artifacts (arXiv:2210.10760) cannot
    also satisfy it. Static scores 1.0; the shippable envelope excludes
    h=7d (violates the stale-relevant prior), alpha=0.25 (support-only
    blend), alpha=1.0 (cosine-only blend), and degenerate discounts.
    Axes are separable by construction (temporal pairs semantic-only;
    graph pairs share decay), verified pointwise in tests.
  - `run_ship_gate()` / `run_ship_gate_from_store()` → `GateDecision`
    (+ `to_record()` JSON): three gates, all required — sufficient data
    (≥50 train / ≥20 holdout pairs), strictly-beats-static on the holdout
    (static replayed on the identical split with identical train-side
    propensities; safe exploration, arXiv:2002.00467), and no guard
    regression vs static. Explicit reasons on every decision.
- Cluster-param sweep on the live arms: `_run_arm` gained
  `cluster_min_cluster_size`/`cluster_min_samples` pass-through; the arms
  test sweeps (3,1)/(5,2)/(8,3) with recluster + cluster expansion and
  records a beats-static verdict alongside the existing (alpha, cap) sweep.
- `test_recall_benchmark_labeled_fit` now runs the ship gate end-to-end over
  the seeded hub tables: planted stream is tiny AND its judge-label optimum
  (h=7) sits outside the guard envelope, so the gate refuses on both counts
  while still beating static on the holdout — the reward-hacking guard has
  observable teeth.
- Tests: `tests/memory/test_recall_refit.py` (15 unit tests: static anchor,
  grid shape/order/concreteness, guard envelope verified for every grid
  point, degenerate failures, ship path fitting h=60 from planted
  inversions, reject paths for data floor / guard regression /
  static-already-optimal, holdout identity between arms, JSON round-trip,
  empty-rows) — all planted datasets arithmetically determined, not
  asserted by faith.

**Live run (recorded in `.gobby/plans/17198-refit-decision.json`):**

- Offline gate, `digest` stream: 2 joinable rows, 0 pairs → REJECT
  (insufficient data). The forward-collection stream (#17195) only shipped
  yesterday; the conservative reject is the designed outcome until labels
  accrue.
- Offline gate, `llm_judge` stream: 0 joinable labels — the 297 retro labels
  use synthetic `retro:<session>:<n>` request ids that never join
  `recall_signal_hits` (retro protocol labels historical recalls predating
  hit logging). Structurally fit-incapable through the replay join; digest
  is the fit stream going forward.
- Cluster sweep (live arms): static (5,2) recall@5=1.000/MRR=1.000;
  (3,1) over-fragments (14 clusters, recall 0.827); (8,3) ties without
  improvement → keep static (5,2).
- **Decision: no fitted set ships; static constants stay.** The machinery is
  the deliverable — rerun `run_ship_gate_from_store(store,
  label_source="digest")` once forward labels accrue.

**Validation:** ruff format/check clean; mypy clean on the new module;
unit 15/15; labeled benchmark (incl. gate integration) passed vs test
Postgres hub; live arms (incl. cluster sweep) passed vs FalkorDB;
`gobby test-quality audit` on both touched test files: 0 issues; decision
JSON parses.

**Epic impact:** #17199 (conditional LTR) and #17200 (promote fitted
defaults) unblock. #17200 consumes `GateDecision.to_record()`; nothing ships
until a digest-stream fit passes all three gates. Caveat for the eventual
real fit: rows logged before 2026-07-10 under-represent densified
co-occurrence (see #17492 note) and outcome logging is newer than hit
logging, so early fits will see few joinable rows.

## 2026-07-10 — #17197: Extend offline recall benchmark harness to real labeled data

**Status:** done (session #8113)

**Delivered:**

- `src/gobby/memory/recall_fit.py` (NEW) — the labeled-data fit/replay core.
  `ReplayRow` + `replay_row_from_signal_row` adapter, exact replay algebra
  (`decay' = decay ** (h_logged/h_new)` for exponential half-life; logged
  graph-synthetic discount recovered algebraically when absent; first-order
  alpha/cap re-blend from attributed edge components), `replayed_sort_key`
  mirroring the `build_results` ordering (semantic-first similarity axis,
  RRF `ranking_score` tiebreak) so the fit is evaluated on the FULL
  SearchService ranking path, not `find_related_memory_ids`.
  IPS position propensities = smoothed label coverage per
  `(injection_group, injection_position)` with unlabeled injected rows in
  denominators; clipped inverse weights. Pairwise objective forms pairs only
  between labeled rows in the same request (never-retrieved/unlabeled ≠
  negative). Per-project deterministic request split +
  `fit_partial_pooled` (pooled grid fit, per-project fits shrunk by
  `lam = n_pairs/(n_pairs + m)`), `fit_and_evaluate` returning
  fitted-vs-logged-baseline holdout comparison (`LabeledFitReport`) — the
  exact shape #17198's must-beat-static gate consumes.
- `RecallSignalStore.fetch_replay_rows` (NEW) — LEFT-JOIN loader: ALL
  injected hits with an optional label from ONE explicit `label_source`
  (required kwarg — digest vs llm_judge streams never silently mix; newest
  `labeled_at` wins within a stream), plus request replay context
  (`weighting`, `graph_synthetic_similarity_discount`).
- `tests/memory/test_recall_benchmark.py` — docstring now documents the two
  sides (synthetic corpus retained as recall-side/false-negative eval;
  labeled data precision-side ONLY); `_run_labeled_fit` labeled counterpart
  of `_run_arm`; `test_recall_benchmark_labeled_fit` seeds planted rows
  through the REAL hub tables (store → join → fit) and asserts the fit
  recovers the planted 7d half-life and beats the logged baseline 1.000 vs
  0.000 on the per-project holdout.
- Tests: `tests/memory/test_recall_fit.py` (27 unit tests: replay algebra
  incl. decay re-exponentiation exactness and boost preservation,
  propensities/clipping, pairwise semantics incl. no-pairs-across-requests
  and unlabeled exclusion, deterministic per-project split, shrinkage
  behavior, planted end-to-end recovery, param validation, adapter) + 4
  `fetch_replay_rows` join tests in `tests/storage/test_recall_signals.py`.

**Also fixed (rule 8):** `_evaluate` in the benchmark iterated
`find_related_memory_ids(...)` directly, but the reader now returns
`RelatedMemoryTraversal` — the synthetic arms test was broken on the branch
(`TypeError: not iterable`). Fixed to `.memory_ids`; arms test passes again
against live FalkorDB.

**Validation:** ruff format/check + mypy clean; unit 27/27; storage 17/17;
labeled benchmark passed vs test Postgres hub; synthetic arms passed vs live
FalkorDB; `gobby test-quality audit` on the three touched test files: 0
issues.

**Epic impact:** #17198 is now unblocked with a working fit surface: load one
label stream via `fetch_replay_rows`, adapt, `fit_and_evaluate` over a wider
(alpha, cap) grid, read fitted-vs-static holdout numbers off
`LabeledFitReport`. Caveats recorded in the module docstring: alpha/cap
replay is first-order (single-edge attribution); cluster params are NOT
offline-replayable — they change candidate sets, so they stay on the live
synthetic arms.

## 2026-07-10 — #17492: Non-LLM CO_OCCURS densify/backfill pass

**Status:** done (session #8113)

**Delivered:**

- `src/gobby/memory/services/knowledge_graph/densify.py` —
  `densify_cooccurrence()` + `CooccurrenceDensifyResult`. Pair enumeration from
  MENTIONED_IN (per-memory cap of `COOCCUR_MAX_ENTITIES` on sorted keys),
  weighted-mode embedding fetch with graceful skip of pairs lacking stored
  vectors, bounded batches (200 pairs) delegated to
  `KnowledgeGraphWriter.merge_cooccurrence_edges` so support/weight/idempotency
  semantics are the write path's own.
- `KnowledgeGraphService.densify_cooccurrence()` delegate gated by
  `graph_edge_weighting` (same as the write path).
- MCP tool `densify_knowledge_graph_cooccurrence` on gobby-memory. CLI skipped
  (task marked it optional). The optional rebuild-async secondary was not done —
  it remains noted in the task description and is not needed by the epic.
- Tests: `tests/memory/test_cooccurrence_densify.py` (10 tests: pair
  derivation/cap/dedup, support+weight via write path, idempotency,
  no-embedding skip, unweighted mode, batching, empty graph, edge counts,
  project scoping) + 3 MCP-tool tests in
  `tests/mcp_proxy/test_memory_tools_kg.py`.

**Live run (project d45545c5):** 2,024 memories scanned, 6,503 pairs, 33
batches, ~9.7s per pass (300s timeout is a non-issue). Edges 1,784 → 6,728; all
6,728 carry weights (range 0.31–0.96, avg 0.44). Rerun idempotent
(6,728 → 6,728). Traversal-shape query returns weighted CO_OCCURS neighbors
ranked by weight; live `search_memories` results come back `search_via=graph`.

**Epic impact:** weighted-regime feature rows logged from now on see a
densified co-occurrence layer, so the co-occurrence component is properly
represented in the data the offline fit (#17198) will consume. Rows logged
between 2026-07-02 and 2026-07-10 saw only write-time CO_OCCURS accrual
(1,784 edges at densify time) — worth remembering as a covariate shift if
fitting on that window.

**Why now:** unblocked, and every day the live graph stays un-densified, the
weighted-regime feature rows accruing under #17490 under-represent the
co-occurrence component that the offline fit (#17198) is supposed to tune.
\#17197 (harness) is pure code and can follow.

**Plan:**

1. New module `src/gobby/memory/services/knowledge_graph/densify.py`:
   `densify_cooccurrence(falkor, writer, project_id, *, weighted, batch_size,
   max_entities_per_memory)` returning a `CooccurrenceDensifyResult`
   (memories_scanned, pairs_total, pairs_skipped_no_embedding, pairs_merged,
   batches, edges_before/after).
   - Enumerate per-memory entity keys with one aggregation query over the
     MENTIONED_IN bipartite structure (`_project_scope` on both ends, matching
     the write path).
   - Per memory: `sorted(set(keys))[:COOCCUR_MAX_ENTITIES]` then canonical a<b
     combinations (write path caps at first 8 in extractor order; the retrofit
     has no extractor order, so sorted-then-cap is the deterministic analog).
   - Weighted mode: fetch stored `_Entity.embedding` vectors (scoped query,
     coerce to list[float]); pairs where either end lacks an embedding are
     skipped and counted.
   - Merge in bounded batches via the existing
     `KnowledgeGraphWriter.merge_cooccurrence_edges` — this reuses the exact
     write-path semantics: support recomputed idempotently from the live graph,
     weight = `cooccurrence_weight(cosine, support, alpha=0.5, cap=5)`,
     MERGE + SET (no duplicates), zero-support pairs delete stale edges.
2. `KnowledgeGraphService.densify_cooccurrence(project_id=None)` thin delegate,
   `weighted=self._graph_edge_weighting` (same gate as the write path).
3. MCP tool `densify_knowledge_graph_cooccurrence` on gobby-memory (recluster
   tool pattern). CLI skipped — task marks it optional; MCP covers the retrofit.
4. Tests `tests/memory/test_cooccurrence_densify.py` (RecordingFalkor pattern
   from test_graph_edge_weighting.py): pair derivation/cap/dedup, support
   counting via write path, weight computation, idempotency, no-embedding skip,
   unweighted mode, batching.
5. Run the tool on the live graph; verify edges_after > 0 and weighted
   CO_OCCURS edges visible to traversal.

**Key reuse:** `merge_cooccurrence_edges` recomputes support from the live
graph inside the write, so the densify pass only enumerates pairs + embeddings;
idempotency and weight semantics come for free and can't drift from the write
path.

# Memory-Recall Usefulness Label + Labeled-Row Data Contract

Status: **normative** (epic #17099 Phase 0a gate deliverable, task #17192)
Owners: memory subsystem
Consumers: #17193 (retrospective judge + ablation calibration), #17195 (digest
forward labels), #17196 (hub tables + injection-outcome capture), #17197
(offline harness), #17198 (closed-form re-fit + ship gate), #17199 (LTR,
conditional), #17201 (drift detection).

This document is the single source of truth for **what a "useful recall" label
is**, and for the **row shapes** every downstream fit, eval, and drift monitor
consumes. If an implementation disagrees with this contract, the
implementation is wrong or this document must be amended first.

## 1. Problem framing

Gobby injects recalled memories into agent context as a `<project-memory>`
block (`build_memory_context`, `src/gobby/memory/context.py`). The recall
ranker's constants are hand-tuned (#17096/#17097). To fit them from real
usage we need, per injected memory, a defensible answer to "did this memory
help this turn?" joined cleanly against the features the ranker had at recall
time.

Two research findings constrain the design (full review:
`docs/research/memory-recall-adaptive-tuning.md`):

1. **"Referenced in the output" is a weak, biased label.** ContextCite
   (arXiv:2409.00729) separates *contributive* (causally used) from
   *corroborative* (merely cited) context; a 2024 RAG citation-faithfulness
   study (arXiv:2412.18004) found up to **57% of citations are
   post-rationalized**. Therefore `referenced_overlap` is a **feature**, never
   the training target.
2. **LLM judges carry position, length, and self-preference bias**
   (arXiv:2306.05685, arXiv:2502.01534, arXiv:2410.21819, arXiv:2305.17926).
   Therefore the judge label is only valid under the de-biasing protocol in
   §4, and must be calibrated against ablation/leave-one-out on a subsample.

## 2. Join keys and identity

The canonical labeled-row key is:

```text
(project_id, session_id, recall_request_id, memory_id)
```

- `project_id` — Gobby project UUID (nullable only for global-`gobby_kg`
  recalls; pooled fits treat NULL as its own pool).
- `session_id` — the **platform session UUID** (the Gobby session row id),
  NOT the CLI external id. All session-keyed state on the injection path
  (e.g. the `injected_memory_ids` session variable) is keyed by the platform
  id; mixing in external ids silently breaks the join.
- `recall_request_id` — UUID minted once per recall request
  (`src/gobby/memory/recall.py`) and threaded through the search service into
  the recall-signal event. This is the correlation id between features,
  injection outcome, and label.
- `memory_id` — the memory row UUID.

**Known gap this contract closes (verified 2026-07-02):** the injected set
currently survives only in transient message payloads and the
`injected_memory_ids` session variable, **without** `recall_request_id`. The
feature stream (`recall_signal.jsonl`) has the request id but no injection
outcome. #17196 MUST thread `recall_request_id` through the injection path
(fast-recall inline formatter `EffectsMixin._format_search_memories_result`
and the helper-delivery path via `MemoryRecallResult.recall_request_id`) so
the injection-outcome record (§5) can be written with the full key.

## 3. Labeled-row contract

A labeled row is the analytic join `features ⋈ injection_outcome ⋈ label` on
the key above. Fits and evals consume rows of this shape; storage may
normalize into the tables of §5–§6 plus the promoted signal table.

### 3.1 Features (from the recall-signal event, schema v2)

Source: `recall_signal.jsonl` events
(`src/gobby/memory/recall_signal_log.py`, `RECALL_SIGNAL_SCHEMA_VERSION = 2`),
promoted to a hub table by #17196. Request-level fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `caller` | text | Recall entry point (e.g. `memory.recall`, `memory.search`). Fits use injection-path callers only. |
| `query` | text | The recall query string. |
| `merged_ids` | text[] | Candidate set after merge, before the returned cut. |
| `returned_ids` | text[] | Final returned ranking (defines `rank`). |
| `rrf_applied` | bool | Whether RRF fusion ran (see #17105 — RRF primacy is settled; do not re-litigate). |
| `graph_synthetic_similarity_discount` | real | Discount applied to graph-sourced synthetic similarity. |
| `ranking_score_map` / `graph_score_map` | jsonb | Per-id score maps. |
| `weighting` | jsonb | Weighting-regime snapshot (§7). |

Per-hit fields (one per returned memory):

| Feature | Type | Meaning |
| --- | --- | --- |
| `rank` | int | Position in `returned_ids` (recall ranking, NOT injection position). |
| `search_via` | text | Retrieval path (`via` in the rendered suffix). |
| `similarity` | real? | Blended similarity used for ranking. |
| `raw_semantic_score` | real? | Pre-blend semantic score. |
| `temporal_decay_factor` | real? | Decay multiplier applied to this hit. |
| `ranking_score` | real? | Final ranking score (`score` in the rendered suffix). |
| `ranking_mode` | text | Ranking mode identifier. |
| `graph_score` | real? | Graph-path score when graph recall contributed. |

### 3.2 Edge-weight component features (forward, nullable)

The fit needs the ranker's **component breakdown**, not just the blended
score. Components follow the #17096 formula
(`cooccurrence_weight`, `src/gobby/memory/services/knowledge_graph/writer.py`;
`KnowledgeGraphReader._edge_score`, `.../reader.py`):

```text
edge_weight = alpha * max(cosine, 0)  +  (1 - alpha) * min(support, cap) / cap
candidate_score = edge_weight * temporal_decay(updated_at, half_life)   # decay flag on
```

| Feature | Type | Meaning |
| --- | --- | --- |
| `edge_cosine` | real? | `max(cosine, 0)` term for the strongest contributing edge. |
| `edge_support_norm` | real? | `min(support, cap) / cap` term. |
| `edge_weight_blend` | real? | The blended `edge_weight`. |
| `edge_decay_factor` | real? | Decay multiplier at candidate selection (1.0 when decay off). |

Schema v2 does not emit these yet; they are **nullable** columns populated
once the feature feed is extended (epic Phase 1e, part of #17196/#17197
scope). Rows with NULL components are still valid for fits over the blended
scores.

### 3.3 Transcript-derived features (extractor-versioned)

Computed by the #17193 harness (retrospectively) and the forward pipeline;
every row records `feature_extractor_version` so estimators can be revised
without corrupting old rows.

| Feature | Type | Definition |
| --- | --- | --- |
| `retained_turn` | bool? | The rendered `<project-memory>` entry for this memory was still present in the model-visible context at the end of the injection turn (i.e., not removed by compaction/summarization before the assistant produced its response), measured from the transcript. |
| `referenced_overlap` | real? | Normalized lexical-overlap estimate in `[0,1]` between the memory content and the assistant's response for the injection turn (estimator implementation-defined, recorded via `feature_extractor_version`). **Feature only — never the target** (§1). |
| `injection_position` | int? | From the injection-outcome record (§5). |

### 3.4 Label

| Field | Type | Definition |
| --- | --- | --- |
| `judge_useful` | bool | PRIMARY label: de-biased LLM-judge verdict (§4) that the memory materially helped the assistant's response for the injection turn. |
| `judge_confidence` | real? | Judge self-reported confidence in `[0,1]`, when the protocol elicits it. |
| `ablation_delta` | real? | Calibration subset only: measured quality/log-prob delta from regenerating the turn with vs. without the memory (leave-one-out). Sign convention: positive = memory helped. |

## 4. Label definition: de-biased judge, ablation-calibrated

The PRIMARY label is **judge-based**, not overlap-based, with these
non-negotiable protocol requirements:

1. **Different model family** from the generator whose transcript is judged
   (preference-leakage guard, arXiv:2502.01534; self-preference guard,
   arXiv:2410.21819).
2. **Position-randomized** presentation of the candidate memories within the
   judged context (position-bias guard, arXiv:2305.17926).
3. **Length-controlled** — the prompt/protocol must prevent verbosity from
   proxying usefulness (arXiv:2306.05685).
4. The judge sees the injection turn (user prompt, injected block, assistant
   response) and rules per `memory_id`; a one-line rationale is stored.
5. `judge_model`, `judge_protocol_version`, `position_randomized`, and
   `length_controlled` are recorded on every label row (§6). Rows produced
   under a protocol that violates 1–3 are invalid for fitting.

**Calibration (the reason the judge is trustworthy):** on a subsample, run a
controlled ablation/leave-one-out — regenerate the turn with and without the
memory and measure the quality/log-prob delta (`ablation_delta`). ContextCite
(arXiv:2409.00729) treats ablation as the gold standard for "did this context
matter"; AttriBoT (arXiv:2411.15102) makes LOO tractable. #17193 owns the
calibration run and its GO/NO-GO decision matrix (judge↔ablation agreement is
one of its gates). Digest-sourced labels (#17195, `label_source = 'digest'`)
are a secondary stream under the same de-biasing requirements and must never
be mixed into a fit as if judge-sourced — `label_source` keeps the streams
separable.

**Semi-supervision rules:**

- Never-retrieved memories are **unlabeled, not negative**.
- Returned-but-filtered memories (§5 `outcome = 'filtered'`) have features
  but no label; they inform propensity estimation only.
- "Model referenced A over available-but-unused B" forms skip-above
  preference pairs for the LTR objective (#17198/#17199), IPS-weighted by
  injection-position propensity (arXiv:1608.04468).

## 5. Injection-outcome record (implemented by #17196)

**New record — does not exist today** (verified 2026-07-02). One row per
`(recall_request_id, memory_id)` for every memory in `returned_ids` of an
injection-path recall, written at injection-decision time.

| Column | Type | Constraints | Meaning |
| --- | --- | --- | --- |
| `project_id` | text | | Project UUID. |
| `session_id` | text | not null | Platform session UUID (§2). |
| `recall_request_id` | text | not null | Correlation id (§2). |
| `memory_id` | text | not null | Memory UUID. |
| `outcome` | text | not null; `injected` \| `filtered` | Final injection decision. |
| `drop_reason` | text | null iff `outcome = 'injected'` | Why a returned memory was not injected (enum below). |
| `injection_position` | int | null iff `outcome = 'filtered'` | 0-based ordinal of this memory's rendered entry within the final `<project-memory>` block, in render order. |
| `injection_group` | text | nullable | The `memory_type` render group: `context` \| `preference` \| `pattern` \| `fact`. |
| `turn_seq` | int | nullable | Origin turn sequence when available (`MemoryRecallResult.origin_turn_seq`). |
| `caller` | text | not null | Recall entry point, copied from the signal event. |
| `created_at` | timestamptz | not null | Write time. |

Primary key: `(recall_request_id, memory_id)`.

`drop_reason` enum, grounded in the actual filter sites on the injection path
(`EffectsMixin._format_search_memories_result`,
`src/gobby/workflows/engine/effects.py`; `build_memory_context`,
`src/gobby/memory/context.py`):

| Value | Filter site |
| --- | --- |
| `already_injected` | Dedup against the `injected_memory_ids` session variable. |
| `review_lesson` | `_is_review_lesson_memory` exclusion. |
| `empty_content` | Content empty after bullet-strip in `build_memory_context`. |
| `payload_empty` | Whole payload empty → no block rendered (all returned ids get this). |
| `budget` | Reserved: token/count budget truncation, if introduced. |
| `other` | Anything else; free-text detail goes in a nullable `drop_detail` column. |

**Position semantics (critical for IPS):** `build_memory_context` groups
memories by `memory_type` (context → preference → pattern → fact) before
rendering, so **injection position ≠ recall rank**. `injection_position` MUST
be captured at render time, after grouping, as the ordinal of the memory's
line in the final block. `injection_group` is recorded because position
propensity is conditional on group boundaries. IPS correction for the fit
(arXiv:1608.04468, trust-bias arXiv:2008.10242) uses `injection_position`
propensities, never `rank`.

## 6. `recall_usefulness` labels table (implemented by #17196)

One row per label event. Relabeling is append-only (new protocol version →
new row), never destructive.

| Column | Type | Constraints |
| --- | --- | --- |
| `id` | bigserial | PK |
| `project_id` | text | |
| `session_id` | text | not null |
| `recall_request_id` | text | not null |
| `memory_id` | text | not null |
| `label_source` | text | not null; `llm_judge` \| `ablation` \| `digest` \| `human` |
| `judge_useful` | boolean | not null |
| `judge_confidence` | real | nullable |
| `judge_model` | text | not null for `llm_judge`/`digest` (family + version) |
| `judge_protocol_version` | text | not null |
| `position_randomized` | boolean | not null (false allowed only for `ablation`/`human`) |
| `length_controlled` | boolean | not null (same exception) |
| `ablation_delta` | real | nullable; calibration subset only |
| `ablation_method` | text | nullable (e.g. `loo_regen_judge`, `loo_logprob`) |
| `rationale` | text | nullable one-liner |
| `feature_extractor_version` | text | nullable; version of §3.3 estimators used at label time |
| `labeled_at` | timestamptz | not null |

Uniqueness: `(recall_request_id, memory_id, label_source,
judge_protocol_version)`.

Join contract: a **fit-eligible labeled row** exists iff the label row joins
an injection-outcome row with `outcome = 'injected'` and a promoted
recall-signal hit row on the §2 key. Labels without features (e.g.
retrospective rows predating signal logging) are calibration-only.

Backfill: the #17193 retrospective harness backfills this table from
transcripts (parsing rendered `(memory_id: …, score: …, via: …)` suffixes).
Backfilled rows lacking a `recall_request_id` use the synthetic id
`retro:<session_id>:<turn_seq>` and are marked by `label_source` +
`judge_protocol_version`; they never join the signal table and are excluded
from IPS-weighted fits (usable for judge calibration and volume checks only).

Migration numbering: next free `src/gobby/storage/migrations/` number at
implementation time (head was `307_*.sql` when this contract was written —
do not trust stale numbers in older plans). Follow the
`274_memory_dream.sql` hub-table pattern.

## 7. Weighting-regime fencing (non-negotiable)

Every signal event carries a `weighting` snapshot
(`_weighting_snapshot`, `recall_signal_log.py`): `graph_edge_weighting`,
`graph_edge_decay`, `edge_half_life_days`, `materialize_cooccurrence`,
`cluster_recall_expansion`, `cluster_expansion_per_entity`,
`cluster_min_cluster_size`, `cluster_min_samples`,
`temporal_decay_half_life_days`.

- The **regime key** is the tuple of boolean flags
  `(graph_edge_weighting, graph_edge_decay, materialize_cooccurrence,
  cluster_recall_expansion)`.
- A single fit or eval MUST NOT pool rows across different regime keys.
- Events before `2026-07-02T05:19Z` are unweighted-regime (the weighted
  regime was enabled then, #17490); rows before that instant additionally
  predate the CO_OCCURS retrofit (#17492) and the hook-path recall fix
  (#17491) — treat `caller = "memory.recall"` volume as starting only after
  #17491 shipped.
- Fits condition on the snapshot values (e.g. half-lives) as hyperparameters
  of the regime, not as per-row features.

## 8. Non-goals

- **Recall-side (false-negative) labels** — this contract is precision-side
  only: it covers memories that were returned by recall. Broadening to missed
  memories is deferred (#17204); the synthetic benchmark remains the
  recall-side eval.
- **Online tuning semantics** — deferred (#17202); this contract feeds the
  offline fit and its must-beat-static ship gate (#17198) only.
- **Per-project label pooling policy** — fits are global with partial pooling
  (per-project random effect); this contract only guarantees `project_id` is
  on every row so pooling is possible.

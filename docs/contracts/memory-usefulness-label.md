# Memory-Recall Usefulness Label + Labeled-Row Data Contract

Status: **normative** (epic #17099 Phase 0a gate deliverable, task #17192)
Data source note (2026-08-26, #21009): automatic prompt-time recall
(`memory.recall` caller, `MemoryRecallRunner`, `build_memory_context`) was
retired; rows with that caller are the archived injection cohort. New rows
come from agent-driven `mcp_proxy.memory.search_memories` requests; the
outcome and caller vocabulary for that cohort is defined in #21000 L4.
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

Daemon-owned recall mints `recall_request_id` in `MemoryRecallRunner.run`,
threads it through `MemoryRecallResult`, and keeps it on the queued delivery.
Generic recalled memories render through `build_memory_context`; review lessons
use the separate installed-rule path through
`DeliveryFormattingMixin._format_review_lessons_result`.

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

Labels are **judge-based**, not overlap-based. Transcript/response usefulness
labels use these non-negotiable protocol requirements; §4.1 defines the
production shadow ranking-label variant:

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

### 4.1 Shadow query-relevance labels

`label_source = 'digest_shadow'` is the production ranking-label stream. During
the digest pass, it judges the full scored candidate list (the top
`min(n_hits, 8)` hits) against the stored recall query. The assistant response
is never shown. Each comparative prompt uses deterministically shuffled neutral
keys, fixed excerpt budgets, and one verdict per presented candidate. Because
there is no generator response or comparator model in this protocol, the
different-model-family requirement above does not apply; position and length
controls remain mandatory.

Admission is fail-closed. Every top-k hit must carry
`content_hash = sha256(memory.content.encode("utf-8"))`, and the exact stored
content must still match that hash at judge time. A missing, hidden, or changed
candidate makes the whole request ineligible rather than allowing labels over a
different representation. The immutable `recall_shadow_prompt_snapshot` is the
sole audit-of-record: it stores the exact system prompt, query, presentation
order, neutral keys, excerpts, content hashes, judge model and configuration
fingerprint, and exact prompt hash. Reviewers never reconstruct a shadow prompt
from live memory rows.

Every fit, drift evaluation, and scored audit is fenced by label source,
protocol version, judge-model key, judge-config fingerprint, weighting regime,
candidate scope, request-creation cutoff, and snapshot-completion cutoff.
Requests must have a complete exact-protocol label set and a committed prompt
snapshot. Cohort ambiguity or an incomplete fence fails closed.

**Calibration (the reason the judge is trustworthy):** on a subsample, run a
controlled ablation/leave-one-out — regenerate the turn with and without the
memory and measure the quality/log-prob delta (`ablation_delta`). ContextCite
(arXiv:2409.00729) treats ablation as the gold standard for "did this context
matter"; AttriBoT (arXiv:2411.15102) makes LOO tractable. #17193 owns the
calibration run and its GO/NO-GO decision matrix (judge↔ablation agreement is
one of its gates). Legacy `label_source = 'digest'` rows are preserved as
read-only historical evidence for the retired response-usage judge. No producer
may write new `digest` rows; they never enter the shadow fit.

**Semi-supervision rules:**

- Never-retrieved memories are **unlabeled, not negative**.
- Returned-but-filtered memories (§5 `outcome = 'filtered'`) may receive
  `digest_shadow` query-relevance labels because the shadow protocol judges the
  full scored list, independently of injection.
- Skip-above pairs are not fit-eligible because recall does not yet capture a
  durable exposure/reference signal. Adding that signal is separate follow-up
  work; candidate availability alone must not be treated as exposure.

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

`drop_reason` enum, grounded in the daemon-owned recall filters
(`MemoryRecallRunner._filter_candidates`, `src/gobby/memory/recall.py`) and
generic memory rendering (`build_memory_context`,
`src/gobby/memory/context.py`). Review lessons use
`DeliveryFormattingMixin._format_review_lessons_result` and the separate
`injected_review_lesson_ids` deduplication state.

| Value | Filter site |
| --- | --- |
| `already_injected` | Dedup against the `injected_memory_ids` session variable. |
| `review_lesson` | `MemoryRecallRunner._filter_candidates` tag exclusion. |
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
| `label_source` | text | not null; `llm_judge` \| `ablation` \| `digest` (historical, read-only) \| `digest_shadow` \| `human` |
| `judge_useful` | boolean | not null |
| `judge_confidence` | real | nullable |
| `judge_model` | text | not null for `llm_judge`/`digest`/`digest_shadow` (provider + model) |
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

Join contract: a legacy **injection fit-eligible labeled row** exists iff the
label row joins an injection-outcome row with `outcome = 'injected'` and a
promoted recall-signal hit row on the §2 key. A `digest_shadow` fit instead
admits a complete request under §4.1, then projects either all candidates or
the injected subset according to the fenced candidate scope. Labels without
features (e.g. retrospective rows predating signal logging) are
calibration-only.

Human ship-audit verdicts do not belong in `recall_usefulness`. Their
authoritative store is `recall_shadow_audit_verdicts`, unique on
`(cohort_digest, request_id, memory_id)` and carrying the deterministic
`sample_digest` and bound `prompt_hash`. A verdict is valid only for that exact
cohort, sample, and immutable presentation.

Backfill: the #17193 retrospective harness backfills this table from
transcripts (parsing rendered `(memory_id: …, score: …, via: …)` suffixes).
Backfilled rows lacking a `recall_request_id` use the synthetic id
`retro:<session_id>:<turn_seq>` and are marked by `label_source` +
`judge_protocol_version`; they never join the signal table and are excluded
from IPS-weighted fits (usable for judge calibration and volume checks only).

Migration 323 adds `digest_shadow`, content identity, constants provenance,
shadow claim/snapshot/audit state, and holdout gate reservation tables.

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

## 7a. Query-construction fencing and the frozen v1 cohort (non-negotiable)

The `weighting` snapshot also carries `query_construction_version` — the era
of the query that drove the retrieval (`RECALL_QUERY_CONSTRUCTION_VERSION`,
`recall_constants.py`). Changing how the query is built changes the
population, not a per-row feature, so it fences like the regime key.

- **The legacy era is the absent key.** Rows written before the key existed
  carry none, so `weighting->>'query_construction_version' IS NULL` selects
  the pre-v2 era exactly and no migration or backfill is needed.
- **Every cohort-scoped read filters on it**, with the NULL-aware
  `IS NOT DISTINCT FROM` so a `None` cohort resolves to the legacy era rather
  than matching nothing. A single fit, eval, audit, or drift replay MUST NOT
  pool rows across construction versions, and `GateCohort.identity()` carries
  the version so two eras cannot collapse onto one `holdout_consumption_key`.
- **The polling path is fenced too, and its parameter is required rather than
  defaulted.** `shadow_cohort_query`, `fetch_unshadowed_requests`, and
  `claim_shadow_request` take the version explicitly; a defaulted `None` would
  match only the legacy era and stamp the new protocol's labels onto
  pre-cutover retrievals.

**The v1 cohort is frozen, not migrated.** At cutover:

1. The running v1 poller drains its backlog normally; its labels stay v1.
2. `SHADOW_PROTOCOL_VERSION` and the poller's construction version flip
   together. Legacy rows stop matching the poller's filter.
3. `gobby memory recall-signals supersede-legacy-cohort` retires whatever
   remains unjudged, writing `recall_shadow_judge_state` rows with
   `status='terminal'` and `last_error='query_construction_version_superseded'`.
   It inserts rather than updates — an unclaimed request has no state row at
   all — never touches a `complete` row, and is idempotent.

Existing v1 `recall_usefulness` rows and their prompt snapshots are never
rewritten, deleted, or re-judged: they stay valid evidence under the protocol
and query era that produced them, and the fence is what keeps them out of v2
cohorts.

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

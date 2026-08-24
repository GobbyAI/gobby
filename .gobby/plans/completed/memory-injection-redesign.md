# Memory Injection Redesign

**Plan ID:** memory-injection-redesign

## C1 Context
`kind: framing`

Project-memory recall injects three memories into essentially every substantive prompt, chosen by a pipeline that cannot separate relevant from irrelevant, behind an LLM gate that never says no.

Measured on the live hub:

| Observation | Evidence |
|---|---|
| Classifier adds p50 4.2s, p90 6.5s, max 22.8s blocking latency | `~/.gobby/logs/daemon.log`, n=42, `feature=memory.recall.classify` |
| It gates nothing | 1,321 August requests, `avg(jsonb_array_length(returned_ids)) = 8.00`, never empty |
| No relevance separation | injected median cosine 0.612, rejected 0.525 |
| The embedded query is a bag of <=10 keywords | `scrub_memory_recall_query` strips to terms, then `search.py:143` runs YAKE over the residue |
| Usefulness is low | `17195-digest-v1` 4/54 (7.4%); `17193-v1` 61/297 (20.5%) |
| Review lessons leak into general recall | `REVIEW_LESSON_TAG` is `review_lesson`; lessons are tagged `review-lesson`. 96 August injections from 44 lessons |
| Injection volume cannot fall | `collect_active_results` backfills until `limit` survives the floor; `_filter_ranked` then takes top-3 unconditionally |

Outcome: recall retrieves for the current turn on a natural-language query, injects only what clears an independent relevance floor (including nothing), stays off the daemon event loop, and cannot deliver a partial memory body.

The wiki is not a substitute. Synthesized concept prose totals 64,908 chars against the corpus's 1,537,730, with 30 of 148 pages prompt-leak dumps and 30 empty. The corpus carries 160 rows of negative knowledge, 684 with prohibitions, and 401 with decision rationale that no generation pass produces.

## C2 Constraints
`kind: framing`

- No backward compatibility required (0.5.0 unshipped).
- Memory bodies are never delivered partially. Deliverable 3.3 enforces this in the delivery code by deleting content-offset slicing and paginating whole memories. The 3,000-char write cap in 3.1 is defense in depth for payload size; the delivery code is the enforcement mechanism.
- **No schema migration.** Two consequences bind the design:
  - `recall_injection_outcomes.drop_reason` is constrained to `already_injected | review_lesson | empty_content | payload_empty | budget | other` (`baseline.sql:1413`). New selection drops therefore use `drop_reason='other'` with a `drop_detail` discriminator, matching the existing `below_min_score` and `rank_limit` rows written by `_filter_ranked`.
  - The query-construction fence rides in the existing `recall_signal_requests.weighting` jsonb, which already carries `recall_constants_source`, `temporal_decay_half_life_days`, and nine other retrieval constants. `fetch_fit_rows` and `fetch_replay_rows` already SELECT `r.weighting`, so the value reaches every replay row with no column change.
- Query telemetry reuses the existing `recall_signal_requests.query` column (2.4). The migration derived-carrier set is not triggered.
- `recall_usefulness` v1 rows stay valid and frozen. v1 already carries two unfenced drifts (`MAX_QUERY_CHARS` 2026-07-30; judge model swap 2026-07-31 moving the positive rate 32.0% -> 46.4%).
- Shadow judging, drift monitoring, and `recall_fit`/`recall_refit` stay off the prompt path.
- **Fail-open scope.** Every new failure on the retrieval path — the batched pre-search read, query construction, search itself — degrades to injecting nothing for that turn. Telemetry writes are best-effort and sit outside this rule: a failed injection-outcome write or signal-log write is logged and swallowed, and never retracts, alters, or blocks what the turn already delivered.
- The daemon `MemoryManager` is already constructed with the bounded DB executor (`run_db=runner.db_executor.run`, `runner_init/services.py:305`), so `MemoryManager.run_db` is a real executor bridge rather than the `asyncio.to_thread` fallback. Deliverable 1.3 relies on that and adds no wiring.
- Production size at 850 lines or more among targeted files: `mcp_proxy/tools/memory.py` **999** (split in 3.1), `memory/recall_refit.py` **924** (split in 4.1), `storage/recall_shadow_signals.py` **919** (split in 4.1). Other targeted files: `review_learning/service.py` 805, `memory/recall_fit.py` 791, `memory/facade.py` 710, `memory/recall.py` 645, `storage/recall_signals.py` 496, `memory/services/search.py` 485, `memory/protocol.py` 471, `cli/memory/signals.py` 470, `memory/services/_search_paths.py` 453, `memory/recall_signal_log.py` 264, `mcp_proxy/tools/memory_recall.py` 258, `cli/memory/crud.py` 243, `hooks/memory_recall_delivery.py` 236, `memory/backends/storage_adapter.py` 227, `memory/services/repository.py` 209, `memory/recall_constants.py` 178, `memory/backends/null.py` 146, `storage/recall_shadow_sampling.py` 121.
- **Consumer sweep.** Every consumer edge the index reports was inspected, and each one below is expected to require no change. Expansion validation makes `consumer-coverage` blocking, so each is carried in its deliverable's inventory as a `::*` entry whose scope-reason says "consumer sweep only". That form declares the file in scope for verification without asserting a symbol-level rewrite, and it does not pull the consumer's own consumers into the plan the way an exact-symbol entry would. An implementing agent that finds a real diff needed in one of these files should make it; the expectation recorded here is no diff.

  | Section | Sweep-only consumer | Why it does not change |
  |---|---|---|
  | 1.4, 2.1, 2.4 | `src/gobby/memory/manager.py` | Construction-level edges only. `MemoryManager.__init__` builds `SearchService(...)` (`manager.py:158`), calls `make_recall_signal_sink(...)` (`manager.py:171`), and assigns `StorageAdapter(...)` to a `MemoryBackendProtocol` slot (`manager.py:101`). No call site, and every added parameter is an optional keyword. |
  | 1.4 | `tests/memory/test_create_supersedes.py`, `test_manager_knowledge_graph_wiring.py`, `test_memory_manager_1.py` | Call `list_memories`; none defines a protocol fake. `tags_any` is optional and defaults to current behavior. |
  | 3.1 | `src/gobby/mcp_proxy/registries.py`, `tests/mcp_proxy/test_memory_tools_kg.py`, `tests/mcp_proxy/tools/test_internal_action_tools.py` | `create_memory_registry` keeps its signature; the write tools move inside it. |
  | 3.3 | `src/gobby/workflows/engine/delivery_formatting.py` | Calls `queue.queue(session_id, recall_request_id=..., origin_turn_seq=..., ...)`. The cursor shape is internal to the queue. |
  | 4.1 | `tests/memory/test_recall_benchmark.py` | Uses `fetch_replay_rows`; the cohort filter is an added optional keyword. |
  | 4.1 | `src/gobby/memory/recall_drift.py`, `src/gobby/runner_maintenance/telemetry_loops.py` | Reference `RecallSignalStore` as a type annotation (`recall_drift.py:378`) and a constructor call (`telemetry_loops.py:142`). Adding a base class to the store changes neither. |

- Other `search_memories` call sites confirmed unaffected: `cli/memory/crud.py:83`, `mcp_proxy/tools/memory.py:256`, `mcp_proxy/tools/memory.py:378`, `memory/facade.py:230`, `memory/recall.py:398`, `review_learning/service.py:413`, `servers/routes/memory.py:345`.

## P1: Latency and Correctness
`kind: framing`

**Goal**: Remove the blocking LLM call, fix the tag bug, and get synchronous DB work onto the executor bridge.

### 1.1 Delete the substantive-prompt classifier [category: code]
`kind: deliverable`

Targets:
- `src/gobby/memory/recall.py::MemoryRecallRunner._classify`
- `src/gobby/memory/recall.py::MemoryRecallRunner.run`
- `src/gobby/memory/recall.py::MemoryRecallRunner.__init__`
- `src/gobby/memory/recall.py::_heuristic_decision`
- `src/gobby/memory/recall.py::_parse_classifier_response`
- `src/gobby/memory/recall.py::_decision`
- `src/gobby/memory/recall.py::PromptDecisionKind`
- `src/gobby/mcp_proxy/tools/memory_recall.py::register_memory_recall_tool`
- `src/gobby/config/sessions.py::MemoryRecallConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: generated config contract is regenerated wholesale, never edited symbol-by-symbol
- `src/gobby/memory/generation_schemas.py::*` — scope-reason: RECALL_CLASSIFICATION_SCHEMA is a module-level constant with no enclosing symbol
- `src/gobby/config/app.py::*` — scope-reason: the daemon config load-order validator spans module-level validators
- `tests/memory/test_recall.py::*` — scope-reason: test module updated across its cases for the changed recall contract
- `tests/mcp_proxy/tools/test_memory_recall.py::*` — scope-reason: test module updated across its cases for the changed recall contract
- `tests/mcp_proxy/test_mcp_proxy_runtime_config_resolution.py::*` — scope-reason: test module updated across its cases for the changed recall contract
- `tests/config/test_config_sessions.py::*` — scope-reason: test module updated across its cases for the changed recall contract
- `tests/config/test_feature_base.py::*` — scope-reason: test module updated across its cases for the changed recall contract

Remove `_classify`, `CLASSIFIER_SYSTEM_PROMPT`, `_heuristic_decision`, `_parse_classifier_response`, the `_ALLOWED_*_REASONS` sets, and `RECALL_CLASSIFICATION_SCHEMA`. `run()` proceeds from `_hard_skip_reason` straight to query construction. Drop `llm_service` from `MemoryRecallRunner.__init__` and from the per-call construction site in `_current_runner`.

`PromptDecisionKind` keeps only `HARD_SKIP`; `_log_decision` continues recording hard-skip reasons. Remove `MemoryRecallConfig.timeout`. The daemon config load-order validator asserts `memory_recall.timeout < workflow.timeout < hooks.adapter_timeout < hooks.provider_timeout`; it loses its first term and must still validate.

**Acceptance:**

- 1.1.1 - No LLM call occurs on the recall path. file: `src/gobby/memory/recall.py`.
- 1.1.2 - A prompt passing `_hard_skip_reason` reaches search with no classification step. symbol: `MemoryRecallRunner.run`.
- 1.1.3 - `memory_recall.timeout` is removed and config load-order validation passes. file: `src/gobby/config/sessions.py`.
- 1.1.4 - `RECALL_CLASSIFICATION_SCHEMA` has no remaining references. file: `src/gobby/memory/generation_schemas.py`.

### 1.2 Fix the review-lesson tag and remove the dead second filter [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/memory/recall.py::MemoryRecallRunner._filter_ranked`
- `src/gobby/memory/recall.py::_has_review_lesson_tag`
- `tests/memory/test_recall.py::*` — scope-reason: test module updated across its cases for the changed recall contract

`REVIEW_LESSON_TAG` becomes `"review-lesson"`, matching what `review_learning/lessons.py:267` writes.

`tags_none` is enforced inside `build_results` (`_search_results.py:54`) with a bare `continue`, so a correctly-tagged lesson never reaches `_filter_ranked`. The `_has_review_lesson_tag` branch in `_filter_ranked` is therefore unreachable once the tag is correct; remove it along with the helper rather than leaving a drop reason that can never be recorded. The `review_lesson` value stays in the `recall_injection_outcomes` check constraint and in `INJECTION_DROP_REASONS`; historical rows keep it, and no migration is needed to stop writing it.

`inject-review-lessons-for-touched-files` remains the sole delivery channel for lessons.

**Acceptance:**

- 1.2.1 - `REVIEW_LESSON_TAG` equals the tag lessons are written with. file: `src/gobby/memory/recall.py`.
- 1.2.2 - `_has_review_lesson_tag` and its `_filter_ranked` branch are removed. symbol: `MemoryRecallRunner._filter_ranked`.
- 1.2.3 - A `review-lesson` tagged memory is excluded at the search layer and never appears in recall results. test: `tests/memory/test_recall.py::test_review_lessons_excluded_from_prompt_recall`.

### 1.3 Move recall DB work off the daemon event loop [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/memory/recall.py::MemoryRecallRunner._injected_memory_ids`
- `src/gobby/memory/recall.py::MemoryRecallRunner._record_selection_outcomes`
- `src/gobby/memory/recall.py::MemoryRecallRunner.__init__`
- `src/gobby/memory/recall.py::MemoryRecallRunner.run`
- `src/gobby/mcp_proxy/tools/memory_recall.py::register_memory_recall_tool`
- `tests/memory/test_recall.py::*` — scope-reason: test module updated across its cases for the changed recall contract
- `tests/mcp_proxy/tools/test_memory_recall.py::*` — scope-reason: test module updated across its cases for the changed recall contract

`_injected_memory_ids` calls `SessionVariableManager(self.db).get_variables()` synchronously inside an async method, and `_record_selection_outcomes` performs a synchronous `RecallSignalStore` write. Both run on the daemon loop, and `memory.recall_signal_hub` is `true` in the live config, so both are active.

The executor handle needs no new plumbing. `MemoryRecallRunner` already holds `memory_manager`; `MemoryManager.run_db(func, *args, **kwargs)` (`manager.py:347`) is the public bridge, and the daemon already constructs the manager with the bounded `DatabaseExecutor` (`runner_init/services.py:305`). Route both operations through `await self.memory_manager.run_db(...)`.

Replace the two reads with one batched off-loop call that returns the `injected_memory_ids` ledger and the previous turn's `last_turn_markdown` for 2.2, so a turn makes one executor round trip instead of two. Move outcome recording off-loop the same way.

**No runner caching.** `_current_runner` constructs a `MemoryRecallRunner` per call, and it stays that way. The construction is five attribute assignments plus `make_injection_outcome_recorder` (`recall_signal_log.py:82`), which reads one config flag and returns a closure — `MemoryRecallRunner.__init__` is `recall.py:269-288`, twenty lines with no I/O. There is no cost to amortize, so caching would buy nothing and would introduce a staleness hazard: `_current_runner` resolves `MemoryRecallConfig` on every call, so a cache keyed on `MemoryManager` identity alone would pin `min_score`, `selection_min_score`, and `enabled` at whatever they held when the manager was built. Runtime config edits reach the next turn precisely because the runner is rebuilt each time.

A resolver returning `None` keeps its current behavior: `_current_runner` returns `None` and the call reports memory services unavailable, so the turn injects nothing.

Retrieval failures fail open to zero injection. A raised or timed-out batched read leaves the runner with no dedupe ledger and no digest slice; it must return zero memories for that turn rather than proceeding on partial state. The outcome write is telemetry and is best-effort: a failure is logged and swallowed, and never retracts or alters what the turn already delivered.

**Acceptance:**

- 1.3.1 - No synchronous DB call executes on the daemon loop during recall. symbol: `MemoryRecallRunner.run`.
- 1.3.2 - One batched off-loop read serves both the dedupe ledger and the digest slice. file: `src/gobby/memory/recall.py`.
- 1.3.3 - A batched read that raises yields zero injected memories for that turn. test: `tests/memory/test_recall.py::test_batched_read_failure_injects_nothing`.
- 1.3.4 - A failed off-loop outcome write is swallowed and does not change what the turn delivered. test: `tests/memory/test_recall.py::test_outcome_write_failure_preserves_delivery`.
- 1.3.5 - A `selection_min_score` change made at runtime takes effect on the next recall call with no daemon restart and no stale cached runner. test: `tests/mcp_proxy/tools/test_memory_recall.py::test_runtime_config_change_reaches_next_call`.
- 1.3.6 - A resolver returning `None` reports memory services unavailable and injects nothing. symbol: `register_memory_recall_tool`.

### 1.4 Push the review-lesson path-tag match into the query [category: code]
`kind: deliverable`

Targets:
- `src/gobby/review_learning/service.py::ReviewLearningService._candidate_lesson_memories`
- `src/gobby/review_learning/lessons.py::build_tags`
- `src/gobby/cli/memory/crud.py::*` — scope-reason: the unscoped-lesson backfill is a bounded one-off command alongside existing CRUD helpers in this module
- `src/gobby/memory/facade.py::MemoryManagerFacadeMethods.alist_memories`
- `src/gobby/memory/services/repository.py::MemoryRepository.alist_memories`
- `src/gobby/memory/protocol.py::MemoryBackendProtocol.list_memories`
- `src/gobby/memory/backends/storage_adapter.py::StorageAdapter.list_memories`
- `src/gobby/memory/backends/null.py::NullBackend.list_memories`
- `tests/review_learning/test_recall_limits.py::*` — scope-reason: test module updated across its cases for the changed lesson-recall contract
- `tests/review_learning/test_lessons.py::*` — scope-reason: test module updated across its cases for the new scope tag
- `tests/memory/test_backends.py::*` — scope-reason: test module updated across its cases for the widened async list contract
- `tests/memory/test_memory_protocol.py::*` — scope-reason: test module updated across its cases for the widened backend protocol
- `src/gobby/memory/manager.py::*` — scope-reason: consumer sweep only; construction-level edges and an optional `tags_any` keyword, so this file is verified unchanged
- `tests/memory/test_create_supersedes.py::*` — scope-reason: consumer sweep only; calls `list_memories` without defining a protocol fake, verified unchanged
- `tests/memory/test_manager_knowledge_graph_wiring.py::*` — scope-reason: consumer sweep only; calls `list_memories` without defining a protocol fake, verified unchanged
- `tests/memory/test_memory_manager_1.py::*` — scope-reason: consumer sweep only; calls `list_memories` without defining a protocol fake, verified unchanged

The read is already off the loop — `StorageAdapter.list_memories` awaits `self._run_storage(...)`. Row volume is the defect: `_LEGACY_SCAN_LIMIT = 200` makes every qualifying `before_tool` fetch 200 rows and match file-path tags in Python for a `limit` of 3, on every tool call that `inject-review-lessons-for-touched-files` matches.

The sync storage layer already supports the needed filter: `MemoryQueryMixin.list_memories` (`storage/memories_query.py:44`) accepts `tags_any` and renders it through `_tag_filter_clause`. The async path drops it. Thread `tags_any: list[str] | None = None` through the async surfaces that omit it — the backend protocol, the null backend, the storage adapter, the repository, and the facade — so `_candidate_lesson_memories` can pass the computed `path_tag` set and let Postgres do the match.

**Make scope explicit instead of inferring it from tag absence.** Path tags are an open namespace (`path:<short_hash>`, `review_learning/file_paths.py:63`) and `tags_none` matches whole tags only, so "has no path tag" is inexpressible with the existing filters. Any content-based fallback therefore has to over-fetch and partition in Python — which is the defect — or truncate a ranked page and silently drop applicable lessons below the cut. Neither is acceptable, so the missing information becomes a tag.

Add `scope:unscoped`, stamped on any lesson that resolves no file path:

- **Writer.** `build_tags` (`lessons.py:252-290`) already extends `tags` with one `path_tag(path)` per extracted file path. When that extension yields nothing, append `scope:unscoped`.
- **Backfill.** 16 live rows need it. Of 148 eligible confirmed lessons (`memory_type='pattern'`, tagged `review-lesson` and `confirmed`, excluding `lesson-domain:plan` which `CODE_DOMAIN_EXCLUDED_TAGS` already filters), 132 carry a `path:` tag and 16 do not — 11 of those carry `migrated-from:gobby-cli`, so no local path can be recovered for them and re-deriving tags is not an option. Stamp them with an idempotent `backfill-unscoped-lessons` command in `src/gobby/cli/memory/crud.py`, alongside the other bounded one-off row mutation this plan adds (3.2). It adds the tag only to eligible lessons carrying no `path:` tag, and re-running it is a no-op.

Two bounded queries then replace the 200-row page. Both keep the current `tags_all=["review-lesson", "confirmed"]`, `tags_none=CODE_DOMAIN_EXCLUDED_TAGS`, `memory_type="pattern"`, `include_global=False` filters and the storage layer's existing `is_global ASC, updated_at DESC` order:

1. **Path-matched query** — adds `tags_any=[path_tag(p) for p in touched_paths]`, at the candidate bound. Postgres performs the match Python performs today.
2. **Unscoped query** — adds `scope:unscoped` to `tags_all`, at the candidate bound. Bounded, and no applicable lesson can be ranked out of it, because the filter selects exactly the lessons that apply to every file.

Path-matched results precede unscoped results, as they do today. Peak rows per `before_tool` drops from 200 to twice the candidate bound, and stays there as the corpus grows — it is currently 148 and adding roughly 50 a month, so `_LEGACY_SCAN_LIMIT` starts truncating within about a month.

**Amended 2026-08-23, approved by Josh in session #10977.** The candidate bound is `limit * _CANDIDATE_OVERFETCH` (4), not `limit`. Drafted at `limit`, this section missed that `recall_review_lessons_for_files` drops non-actionable lessons *after* the fetch via `has_actionable_guidance`, so a page of exactly `limit` rows can yield zero lessons. `tests/mcp_proxy/tools/test_review_learning.py::test_empty_file_lessons_do_not_consume_limit` already proves an empty lesson must not crowd out an actionable one, and it regressed to `count == 0` under a literal `limit`. The alternatives considered were paging with `offset` until the actionable yield was met — which satisfies the literal wording but issues up to 6 queries per `before_tool` instead of 2 — and deleting the invariant test. The fixed multiple keeps peak rows at 80 or fewer, since `limit` caps at 10, and constant as the corpus grows, which is this section's actual objective.

**Stated behavior change.** A lesson tagged `path:X` stops surfacing while editing an unrelated file `Y`. Today it can: `_candidate_lesson_memories` buckets on `matched_path is None`, which lumps genuinely unscoped lessons together with lessons scoped to other files, and the second bucket backfills toward `limit`. Suppressing that is the intent of path tagging, and it removes noise rather than dropping applicable guidance. Unscoped lessons stay reachable for every file, and path-scoped lessons stay reachable for their own files.

**Acceptance:**

- 1.4.1 - `alist_memories` accepts `tags_any` and pushes it to the SQL tag filter. symbol: `MemoryRepository.alist_memories`.
- 1.4.2 - Lesson recall for touched files no longer fetches a fixed 200-row page. symbol: `ReviewLearningService._candidate_lesson_memories`.
- 1.4.3 - `build_tags` stamps `scope:unscoped` on a lesson whose finding yields no file path. symbol: `build_tags`.
- 1.4.4 - The backfill stamps only eligible lessons carrying no `path:` tag, and a second run changes nothing. test: `tests/review_learning/test_lessons.py::test_unscoped_backfill_is_idempotent`.
- 1.4.5 - Path-matched lessons precede unscoped lessons, and neither query exceeds `limit * _CANDIDATE_OVERFETCH` rows, a bound that stays constant as the corpus grows. See the amendment above for why this is not `limit`. test: `tests/review_learning/test_recall_limits.py::test_path_matched_precede_unscoped_within_limit`.
- 1.4.6 - An unscoped lesson is reachable for any touched file regardless of corpus size. test: `tests/review_learning/test_recall_limits.py::test_unscoped_lesson_reachable_beyond_legacy_page`.

## P2: Retrieval Quality
`kind: framing`

**Goal**: Give the vector index natural language, and let a turn retrieve nothing.

### 2.1 Split query representations across the search legs [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/memory/services/search.py::SearchService.search`
- `src/gobby/memory/facade.py::MemoryManagerFacadeMethods.search_memories`
- `tests/memory/test_search_ranking.py::*` — scope-reason: test module updated across its cases for the changed search contract
- `tests/memory/test_recall_benchmark_e2e.py::*` — scope-reason: test module updated across its cases for the changed search contract
- `src/gobby/memory/manager.py::*` — scope-reason: consumer sweep only; `MemoryManager.__init__` constructs `SearchService(...)` and `embed_text` is an optional keyword, so this file is verified unchanged

`search()` derives one embedding from `extract_keywords(query) or query` and passes the same `query` to the BM25 leg. The seam exists already: `query_embedding` feeds the vector and graph legs, `query` feeds `_keyword_ranked`.

Add optional `embed_text: str | None = None`, threaded through the facade. When supplied, embed it verbatim and skip YAKE. When omitted, behavior is unchanged for every existing caller (see the C2 sweep). The BM25 leg keeps receiving the term-bag `query`.

**Acceptance:**

- 2.1.1 - `search()` accepts `embed_text` and embeds it verbatim when present. symbol: `SearchService.search`.
- 2.1.2 - Callers omitting `embed_text` retain YAKE-derived embedding. test: `tests/memory/test_search_ranking.py::test_embed_text_absent_preserves_yake_path`.

### 2.2 Build a natural-language recall query with conditional digest enrichment [category: code] (depends: 2.1, 1.3)
`kind: deliverable`

Targets:
- `src/gobby/memory/recall.py::MemoryRecallRunner.run`
- `src/gobby/memory/recall.py::scrub_memory_recall_query`
- `src/gobby/memory/recall_constants.py::*` — scope-reason: RECALL_QUERY_CONSTRUCTION_VERSION is a module-level constant with no enclosing symbol
- `tests/memory/test_recall.py::*` — scope-reason: test module updated across its cases for the changed recall contract
- `tests/memory/test_recall_constants.py::*` — scope-reason: test module updated across its cases for the new shared constant

`run()` builds two strings: the existing scrubbed term bag for BM25, and a natural-language `embed_text` from the raw prompt.

Four constants govern construction. They are exact, and 4.1 binds them to a cohort fence so a later change cannot drift silently:

```python
# src/gobby/memory/recall_constants.py
RECALL_QUERY_CONSTRUCTION_VERSION = "nl-embed-v1"

# src/gobby/memory/recall.py
RECALL_THIN_QUERY_TERMS = 8      # enrichment trigger, in scrubbed terms
RECALL_DIGEST_TAIL_CHARS = 600   # hard cap on the appended digest tail
MAX_QUERY_CHARS = 1_200          # existing; also caps the assembled embed_text
```

`RECALL_QUERY_CONSTRUCTION_VERSION` lives in `recall_constants.py`, not `recall.py`. `recall.py:18` already imports `recall_signal_log`, and 4.1 needs `_weighting_snapshot` in `recall_signal_log.py` to read the version, so defining it in `recall.py` would close an import cycle. `recall_constants.py` imports only stdlib, is already imported by `recall_signal_log.py:19` and `services/search.py:9`, and is the natural shared home.

`RECALL_THIN_QUERY_TERMS = 8` is the enrichment trigger, measured on the scrubbed term bag rather than the raw prompt because the term bag is the signal already logged and already bounded. Over the last 60 days, 4,474 `memory.recall` requests carry a non-empty query; 11.9% of them hold fewer than 8 terms, and the median holds 141 characters. A single term-count constant replaces a char-or-word pair: the two overlap almost entirely (9.5% under 40 chars against 11.9% under 8 terms) and one threshold is enough.

When the scrubbed bag holds fewer than `RECALL_THIN_QUERY_TERMS` terms, append the last `RECALL_DIGEST_TAIL_CHARS` characters of the previous turn's `last_turn_markdown`, taken from the 1.3 batched read. The assembled `embed_text` is then truncated to `MAX_QUERY_CHARS` using the same head-and-tail elision `scrub_memory_recall_query` already applies, so the enriched query obeys the bound the unenriched one obeys.

Run the digest slice through the existing `strip_injected_context` helper, then through a recall-local stripper for `<project-memory>` blocks, so previously-injected memory text cannot feed back into the query that retrieves memories. The shared helper keeps its current behavior; it has six production consumers (`digest.py`, `clear_continuation.py`, `summary_refresh.py`, `summary_transcripts.py`, and two handoff modules) that must not inherit a recall-specific rule.

**Acceptance:**

- 2.2.1 - A substantive prompt is embedded as natural language rather than a keyword bag. symbol: `MemoryRecallRunner.run`.
- 2.2.2 - `RECALL_QUERY_CONSTRUCTION_VERSION` is importable by both `recall.py` and `recall_signal_log.py` with no import cycle. file: `src/gobby/memory/recall_constants.py`.
- 2.2.3 - A scrubbed bag under `RECALL_THIN_QUERY_TERMS` is enriched with a digest tail of at most `RECALL_DIGEST_TAIL_CHARS`. test: `tests/memory/test_recall.py::test_thin_query_enriched_with_bounded_digest_tail`.
- 2.2.4 - The assembled `embed_text` never exceeds `MAX_QUERY_CHARS`. test: `tests/memory/test_recall.py::test_embed_text_respects_max_query_chars`.
- 2.2.5 - A recall-local stripper removes `<project-memory>` blocks from the digest slice, leaving `strip_injected_context` unchanged. symbol: `MemoryRecallRunner.run`.

### 2.3 Add an independent selection floor [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/config/sessions.py::MemoryRecallConfig`
- `src/gobby/memory/recall.py::MemoryRecallRunner._filter_ranked`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: generated config contract is regenerated wholesale, never edited symbol-by-symbol
- `src/gobby/config/app.py::*` — scope-reason: the daemon config load-order validator spans module-level validators
- `tests/config/test_config_sessions.py::*` — scope-reason: test module updated across its cases for the changed recall config
- `tests/config/test_feature_base.py::*` — scope-reason: test module updated across its cases for the changed recall config
- `tests/memory/test_recall.py::*` — scope-reason: test module updated across its cases for the changed recall contract

Raise the search floor `memory_recall.min_score` 0.45 -> 0.55, and add a distinct `memory_recall.selection_min_score` (default **0.70**) applied by `_filter_ranked`.

The search floor alone cannot reduce injection volume. `collect_active_results` (`_search_backfill.py:24`) doubles the candidate pool for up to 3 rounds until `limit` results survive it, and `_filter_ranked` re-checks the same `config.min_score`, which is therefore already satisfied and drops nothing. Only a floor the backfill loop does not chase can make a turn inject less than three.

**Calibration.** Joining `recall_usefulness` to `recall_signal_hits` on `(recall_request_id, memory_id)` for `caller='memory.recall'` gives 22,685 labeled hits with a similarity. The two judge cohorts are reported separately because the 2026-07-31 model swap was unfenced and they are not poolable:

| Threshold | haiku kept | haiku precision | luna kept | luna precision |
|---|---|---|---|---|
| baseline | 100% | 31.9% | 100% | 46.3% |
| 0.55 | 47.0% | 40.6% | 57.8% | 56.5% |
| 0.60 | 33.3% | 45.2% | 37.8% | 61.3% |
| **0.65** | **18.6%** | **52.3%** | **19.3%** | **67.2%** |
| 0.70 | 6.3% | 67.5% | 5.2% | 72.9% |

Per turn, over 4,215 requests in the last 60 days: 0.55 leaves 9.7% of turns empty at a mean of 2.29 memories; 0.60 leaves 24.0% empty at 1.73; **0.65 leaves 50.6% empty at a mean of 1.00**; 0.70 leaves 78.5% empty at 0.35.

0.65 was the knee on the axis this table measures. It is the highest threshold at which both cohorts still retain roughly a fifth of hits, both gain the same +20 percentage points of precision (haiku 31.9 -> 52.3, luna 46.3 -> 67.2), and the mean per-turn injection lands at exactly one memory. These are precision-at-injected numbers: the labeled population is the top-3 that shipped, which is exactly the population `selection_min_score` filters.

**Axis correction (#20831).** Every number in the table above is measured against `recall_signal_hits.similarity`, which is the DECAYED score -- `score * user_boost * temporal_decay(updated_at, half_life)`. Applying the floor on that axis made it a recency test wearing a relevance test's name: at the 30-day half-life the decay factor is exactly 0.5, so a floor of 0.65 demanded `score * boost >= 1.30`, unattainable at any cosine. The observed 60-day maximum of 0.9006 aged out of eligibility at 15.5 days, and the live smoke test injected the one candidate of eight that had nothing to do with the query while dropping the candidate that answered it.

`selection_min_score` therefore tests the **undecayed** score, recovered as `similarity / temporal_decay_factor`. The floor asks only whether a memory is on topic; `temporal_decay` and the user boost are left to order candidates. The value is recovered by division rather than read from `raw_semantic_score`, because 27.8% of scored hits (9,507/34,163 over 60 days) are `graph_synthetic` and carry no raw cosine -- reading it would permanently disable the #17104 recall expander.

Changing the axis moves the number. Carrying 0.65 over unchanged would be a silent loosening of roughly threefold, since the live decayed p82 is 0.649: zero-inject turns would fall 46.7% -> 18.8% and haiku precision 52.3 -> 42.3. **The default is 0.70 on the undecayed axis**, which admits the smoke-test answer memory (undecayed 0.7086) and accepts a moderate, deliberate loosening. Phase 4 re-fits this constant from labeled data on the now-stable axis.

**Null similarity.** 2,530 of 35,792 hits over the same window (7.1%) carry no score. `_filter_ranked` currently admits them, because its guard reads `similarity is not None and ...`. Reverse the default: a candidate with no finite similarity is dropped, since an unscored candidate cannot be shown to clear the floor. In practice these are keyword-only hits, and dropping them makes a keyword-only corpus match permanently injection-ineligible on any query. That is intended and pinned by test (#20831): BM25 does produce a number, but it is unbounded and shares no axis with the cosine, so admitting it would need a defined score path this design deliberately does not invent.

**Drop reasons.** No schema migration, so both new drops reuse the existing enum value with a discriminator, exactly as the current `below_min_score` and `rank_limit` rows do:

- below the floor: `drop_reason='other'`, `drop_detail='selection_min_score'`
- unscored: `drop_reason='other'`, `drop_detail='null_similarity'`

**Acceptance:**

- 2.3.1 - `min_score` default is 0.55 and `selection_min_score` exists as a separate key defaulting to 0.70, tested against the undecayed score. file: `src/gobby/config/sessions.py`.
- 2.3.2 - A turn whose candidates all fall below `selection_min_score` injects zero memories. test: `tests/memory/test_recall.py::test_selection_floor_can_yield_zero_memories`.
- 2.3.3 - A candidate below the floor records `drop_reason='other'` with `drop_detail='selection_min_score'`. test: `tests/memory/test_recall.py::test_selection_floor_drop_detail`.
- 2.3.4 - A null-similarity candidate is dropped with `drop_detail='null_similarity'` rather than admitted. symbol: `MemoryRecallRunner._filter_ranked`.
- 2.3.5 - The floor tests `similarity / temporal_decay_factor`, so a candidate whose undecayed score clears it is injected even when its decayed score does not. test: `tests/memory/test_recall.py::test_the_selection_floor_tests_the_undecayed_score`.

### 2.4 Log the query that actually drove retrieval [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/memory/services/search.py::SearchService._emit_search_debug`
- `src/gobby/memory/services/search.py::SearchService.search`
- `src/gobby/memory/services/_search_paths.py::SearchPathHost._emit_search_debug`
- `src/gobby/memory/services/_search_paths.py::search_with_graph`
- `src/gobby/memory/services/_search_paths.py::search_qdrant_keyword`
- `src/gobby/memory/recall_signal_log.py::make_recall_signal_sink`
- `src/gobby/memory/recall_signal_log.py::build_recall_signal_event`
- `tests/memory/test_recall_signal_log.py::*` — scope-reason: test module updated across its cases for the changed telemetry contract
- `tests/memory/test_search_ranking.py::*` — scope-reason: test module updated across its cases for the threaded diagnostic query
- `src/gobby/memory/manager.py::*` — scope-reason: consumer sweep only; `MemoryManager.__init__` calls `make_recall_signal_sink(...)` and `embed_text` is an optional keyword, so this file is verified unchanged

`recall_signal_requests.query` currently stores the string passed to `search()`. With two representations, the shadow judge would score the BM25 term bag while retrieval was actually driven by the embed text, so the labels would no longer describe the retrieval they are labeling.

The debug emission is not a single site. `_emit_search_debug` is declared on the `SearchPathHost` protocol (`_search_paths.py:120`), implemented on `SearchService` (`search.py:309`), and awaited from three places: the keyword-only fallback in `search.py:463`, and once each inside `search_with_graph` (`_search_paths.py:300`) and `search_qdrant_keyword` (`_search_paths.py:442`). All three pass `query=query`, so the diagnostic text has to be threaded to every one of them.

Add an optional `embed_text: str | None = None` keyword to `_emit_search_debug` on both the protocol and the implementation, and to `search_with_graph` and `search_qdrant_keyword`. `SearchService.search` forwards its own `embed_text` (2.1) into both path functions and into its fallback emission. The implementation logs `embed_text or query`, so a caller that supplies nothing keeps logging exactly what it logs today.

`recall_signal_requests.query` must hold the embed text because it is the judge's prompt input: `shadow_relevance.py:250` builds `query_text=str(request.get("query") or "")` and renders it as `STORED USER QUERY` in the judge prompt. A judge scoring the term bag against candidates retrieved by the embed text would produce labels that do not describe their own retrieval.

**Keep the term bag too.** It is not reconstructible from the stored value once enrichment exists: a thin query's `embed_text` is the prompt plus a digest tail, and no deterministic function recovers the term bag from that. Write it into the `weighting` jsonb as `bm25_query` whenever it differs from the logged `query`, so a hybrid replay can reproduce both legs. `weighting` is a constants snapshot carrying one content field by exception — that is the cost of the no-migration constraint, and it is cheaper than a column plus its derived-carrier cascade. `fetch_fit_rows` and `fetch_replay_rows` already SELECT `r.weighting`, so replay rows carry it with no query change.

`recall_fit` replay is otherwise unaffected because it consumes per-hit numeric features, not the query string.

**Acceptance:**

- 2.4.1 - `recall_signal_requests.query` records the embed text when one was used. symbol: `SearchService._emit_search_debug`.
- 2.4.2 - All three emission sites receive the diagnostic text, including the graph and Qdrant-keyword paths. symbol: `search_with_graph`.
- 2.4.3 - A graph-path search driven by `embed_text` logs the embed text, not the term bag. test: `tests/memory/test_search_ranking.py::test_graph_path_logs_embed_text`.
- 2.4.4 - When `embed_text` differs from the term bag, `weighting.bm25_query` holds the term bag, so both hybrid legs are replayable. test: `tests/memory/test_recall_signal_log.py::test_enriched_request_records_both_query_legs`.
- 2.4.5 - Callers without `embed_text` continue logging the original query and write no `bm25_query`. symbol: `build_recall_signal_event`.

## P3: Delivery Bounds
`kind: framing`

**Goal**: Make partial delivery impossible in the delivery code, and bound memories at write time.

### 3.1 Split the memory tool module and cap content at 3,000 characters [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/memory.py::create_memory_registry`
- `src/gobby/mcp_proxy/tools/memory.py::create_memory`
- `src/gobby/mcp_proxy/tools/memory.py::update_memory`
- `src/gobby/mcp_proxy/tools/memory.py::delete_memory`
- `src/gobby/mcp_proxy/tools/memory.py::restore_memory`
- `src/gobby/mcp_proxy/tools/memory_write.py`
- `src/gobby/memory/services/lifecycle.py::MemoryLifecycleService.create_memory`
- `src/gobby/memory/services/lifecycle.py::MemoryLifecycleService.update_memory`
- `tests/mcp_proxy/tools/test_memory.py::*` — scope-reason: test module updated across its cases for the relocated write tools and the write cap
- `tests/mcp_proxy/tools/test_memory_tools.py::*` — scope-reason: test module updated across its cases for the relocated write tools and the write cap
- `tests/mcp_proxy/tools/test_tool_verbosity.py::*` — scope-reason: test module updated across its cases for the relocated write tools
- `src/gobby/mcp_proxy/registries.py::*` — scope-reason: consumer sweep only; `create_memory_registry` keeps its signature, so this file is verified unchanged
- `tests/mcp_proxy/test_memory_tools_kg.py::*` — scope-reason: consumer sweep only; verified unchanged against the relocated write tools
- `tests/mcp_proxy/tools/test_internal_action_tools.py::*` — scope-reason: consumer sweep only; verified unchanged against the relocated write tools

`src/gobby/mcp_proxy/tools/memory.py` is at 999 lines, so any addition reaches the 1,000-line ceiling. **Split** the write-path tool registrations out of `memory.py` and **move** `create_memory`, `update_memory`, `delete_memory`, and `restore_memory` into a new `src/gobby/mcp_proxy/tools/memory_write.py`, exposing a `register_memory_write_tools(registry, ...)` entry point. `create_memory_registry` calls it in place of the four inline `@registry.tool` definitions, alongside its existing `register_memory_recall_tool` and `register_memory_dream_tools` calls. Do the move before adding validation, so neither file crosses the ceiling at any point.

Enforce a 3,000-char `content` limit in `MemoryLifecycleService.create_memory` and `update_memory`, with an error naming the actual length and the limit.

3,000 is set from the live distribution, not from budget headroom. Across 2,819 live memories the p99 is 2,468 chars and the p99.9 is 3,179; the counts over each candidate cap are 2,000 -> 69 rows, 2,500 -> 23, **3,000 -> 4**, 3,500 -> 1. A 2,000 cap would put 46 rows within 500 chars of the line, turning routine records into remediation work for no retrieval benefit. Worst case injection is 3 x 3,000 plus `(memory_id: ...)` suffixes, about 9.2K, inside the existing ~9,500-char inline budget; 2.3 puts the realistic mean at 1.00 memories per turn. No new injection constant is needed.

The four rows that were above 3,000 have already been condensed by hand under task #20725, so the cap lands on a corpus that already honors it: `b109e1de` 3,886 -> 2,951, `cf60d54b` 3,174 -> 2,803, `7da0b002` 3,201 -> 2,675, `3850064a` 3,274 -> 2,258. All four are `review-lesson` documents whose oversize came from provenance blocks that nothing reads: `_parse_lesson_content` (`review_learning/service.py:733`) extracts only `pattern_id`, `principle`, `prevention`, and `path`, and `format_review_lesson_guidance` renders only those. Tags were left unchanged on all four. The live corpus is now 2,819 memories with a max content length of 2,995 and zero rows over the cap, so no remediation deliverable is needed — the implementing agent does not repeat this work.

**Acceptance:**

- 3.1.1 - Write-path tools live in a new module and both files stay under 1,000 lines. file: `src/gobby/mcp_proxy/tools/memory_write.py`.
- 3.1.2 - `create_memory_registry` registers the write tools through the new module's entry point. symbol: `create_memory_registry`.
- 3.1.3 - `create_memory` rejects content over 3,000 chars with the length and limit in the error. symbol: `MemoryLifecycleService.create_memory`.
- 3.1.4 - `update_memory` enforces the same cap. symbol: `MemoryLifecycleService.update_memory`.
- 3.1.5 - No live memory exceeds 3,000 chars: the four historical over-cap rows were condensed under task #20725 before this deliverable, and a corpus check confirms max content length 2,995 across 2,819 live memories. behavior: "corpus honors the write cap" in `src/gobby/memory/services/lifecycle.py`.

### 3.2 Keep rationale out of delivered memory text on both routes [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `src/gobby/memory/recall.py::_memory_to_payload`
- `tests/memory/test_recall.py::*` — scope-reason: test module updated across its cases for the changed delivery payload

Inline delivery already omits `rationale`: `_format_project_memory` calls `format_memory_metadata_suffix` with `memory_id` only. `_memory_to_payload` still carries `rationale` into the queued delivery body, so the queued route surfaces the writer's justification while the inline route does not. Drop it at the payload boundary, which removes it from both routes at once. `recall_signal` and dream retain their own provenance access.

**Acceptance:**

- 3.2.1 - `_memory_to_payload` omits `rationale`. symbol: `_memory_to_payload`.
- 3.2.2 - Inline and queued delivery render identical memory bodies. test: `tests/memory/test_recall.py::test_inline_and_queued_bodies_match`.

### 3.3 Delete content-offset slicing and paginate whole memories [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/memory_recall.py::_next_chunk`
- `src/gobby/hooks/memory_recall_delivery.py::MemoryRecallDeliveryQueue.queue`
- `src/gobby/hooks/memory_recall_delivery.py::_cursor`
- `src/gobby/hooks/memory_recall_delivery.py::_valid_cursor`
- `tests/mcp_proxy/tools/test_memory_recall.py::*` — scope-reason: test module updated across its cases for the whole-memory pagination contract
- `src/gobby/workflows/engine/delivery_formatting.py::*` — scope-reason: consumer sweep only; the cursor shape is internal to the queue and `queue.queue(...)` keeps its signature, so this file is verified unchanged

The 3,000-char write cap makes mid-body slicing unreachable in practice, but the code that performs it still runs. `_next_chunk` binary-searches `segment_end` over the content and emits `content[content_offset:segment_end]`, exposing `content_offset` and `memory_complete` in the payload. An invariant enforced only by an upstream write path is not enforced.

Delete the slicing. `_next_chunk` packs **whole memories** into a chunk, adding one memory at a time while the serialized payload stays under `MAX_DIRECT_MCP_SERIALIZED_CHARS`, and emits at least one whole memory per chunk even when that single memory exceeds the budget. Oversize-single is the fail-open case: an over-budget response is recoverable; a body silently cut in half is not. With the 3.1 cap the case is unreachable, and the code no longer depends on that being true.

The cursor loses `content_offset` and becomes `{memory_index, chunk_index}`. `MemoryRecallDeliveryQueue.queue` stops seeding `content_offset: 0`; `_cursor` and `_valid_cursor` validate the two remaining keys. The payload drops `content_offset` and `memory_complete`, since every delivered memory is now complete by construction.

**Acceptance:**

- 3.3.1 - `_next_chunk` contains no content slicing and emits only whole memory bodies. symbol: `_next_chunk`.
- 3.3.2 - The delivery cursor carries `memory_index` and `chunk_index` only. symbol: `_valid_cursor`.
- 3.3.3 - A memory larger than the serialized budget is emitted whole in its own chunk rather than split. test: `tests/mcp_proxy/tools/test_memory_recall.py::test_oversize_memory_emitted_whole`.
- 3.3.4 - A multi-memory delivery paginates one or more whole memories per chunk until exhausted. test: `tests/mcp_proxy/tools/test_memory_recall.py::test_pagination_packs_whole_memories`.

## P4: Experiment Fencing and Offline Evaluation
`kind: framing`

**Goal**: Make the change measurable and keep the v1 cohort intact.

### 4.1 Fence query construction durably and open the v2 cohort [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `src/gobby/memory/recall_signal_log.py::_weighting_snapshot`
- `src/gobby/memory/recall_ship_gate.py::GateCohort`
- `src/gobby/memory/recall_ship_gate.py::GateCohort.identity`
- `src/gobby/storage/recall_signals.py::RecallSignalStore`
- `src/gobby/storage/recall_signals.py::RecallSignalStore.fetch_replay_rows`
- `src/gobby/storage/recall_shadow_signals.py::RecallShadowSignalStoreMixin.shadow_cohort_query`
- `src/gobby/storage/recall_shadow_signals.py::RecallShadowSignalStoreMixin.fetch_shadow_replay_rows`
- `src/gobby/storage/recall_shadow_signals.py::RecallShadowSignalStoreMixin.fetch_unshadowed_requests`
- `src/gobby/storage/recall_shadow_signals.py::RecallShadowSignalStoreMixin.claim_shadow_request`
- `src/gobby/storage/recall_shadow_signals.py::RecallShadowSignalStoreMixin.insert_usefulness_labels_atomic`
- `src/gobby/storage/recall_shadow_labels.py`
- `src/gobby/storage/recall_shadow_sampling.py::RecallShadowSamplingMixin.fetch_shadow_replay_rows`
- `src/gobby/storage/recall_shadow_sampling.py::RecallShadowSamplingMixin.sample_usefulness_labels`
- `src/gobby/memory/shadow_relevance.py::*` — scope-reason: SHADOW_PROTOCOL_VERSION is a module-level constant and the poller call sites span module-level functions
- `src/gobby/cli/memory/signals.py::*` — scope-reason: every cohort construction call site follows the identity field addition
- `src/gobby/memory/recall_refit.py::*` — scope-reason: gate cohort binding spans module-level functions with no single enclosing symbol
- `src/gobby/memory/recall_ship_gate_run.py`
- `tests/memory/test_recall_constants.py::*` — scope-reason: test module asserts on the logged weighting snapshot across its cases
- `tests/memory/test_recall_refit.py::*` — scope-reason: test module updated across its cases for the widened cohort identity
- `tests/storage/test_recall_signals.py::*` — scope-reason: test module updated across its cases for the new cohort filter
- `tests/cli/test_cli_memory_signals.py::*` — scope-reason: test module updated across its cases for the changed cohort construction call sites
- `src/gobby/memory/recall_drift.py::*` — scope-reason: consumer sweep only; references `RecallSignalStore` as a type annotation, and adding a base class does not change it
- `src/gobby/runner_maintenance/telemetry_loops.py::*` — scope-reason: consumer sweep only; constructs `RecallSignalStore(db)`, unaffected by the added base class
- `tests/memory/test_recall_benchmark.py::*` — scope-reason: consumer sweep only; uses `fetch_replay_rows`, whose cohort filter is an added optional keyword

An in-memory identity field fences nothing on its own. The version has to be written with each request and read back as a query filter on **every** cohort-scoped path, or a cohort can still mix query eras.

**Persist.** `_weighting_snapshot` adds `"query_construction_version": RECALL_QUERY_CONSTRUCTION_VERSION` to the `weighting` jsonb it builds for every `recall_signal_requests` row, importing the constant from `recall_constants.py` (see 2.2 for why that module and not `recall.py`). Nothing else in the sink chain changes, so `make_recall_signal_sink` and its `MemoryManager` construction site keep their signatures. Rows written before this change carry no such key, which is precisely the pre-v2 fence: `weighting->>'query_construction_version' IS NULL` selects the legacy era exactly.

**Fence.** `GateCohort` gains `query_construction_version: str | None`, included in `identity()` and validated in `__post_init__` like the other identity fields. Cohort-scoped storage reads filter on it in SQL using `r.weighting->>'query_construction_version' IS NOT DISTINCT FROM %s`, so a `None` cohort selects the legacy rows and a set value selects only its own era. `fetch_fit_rows` already returns `r.weighting`, so fit rows carry the value with no query change.

**Fence the polling path, not just replay.** Shadow judging does not enter through the replay queries. It calls `fetch_unshadowed_requests` (`recall_shadow_signals.py:631`) and then `claim_shadow_request` (`:668`), both of which reach `shadow_cohort_query("polling", ...)` with no construction version. Left alone, a v2 judge would poll with the parameter's default and — because `IS NOT DISTINCT FROM NULL` matches only the legacy era — claim exclusively legacy requests, labeling pre-cutover retrievals with a v2 protocol version. The parameter is therefore **required, not defaulted**, on `shadow_cohort_query`, `fetch_unshadowed_requests`, and `claim_shadow_request`, and every caller passes it explicitly.

**Disposition of the pending legacy backlog.** At cutover some requests written under the legacy version are still unjudged. They are not judged under v2, because a v2 protocol label on a legacy-query retrieval is exactly the contamination this fence exists to prevent. The cutover sequence is:

1. Before flipping `SHADOW_PROTOCOL_VERSION`, let the running v1 poller drain the legacy backlog normally. It polls with the legacy value and its labels stay v1.
2. Flip the protocol version and the poller's construction version together. Legacy rows stop matching the poller's filter and are no longer claimed.
3. Sweep whatever remains through a named, idempotent operation: `supersede_legacy_cohort(label_source, judge_protocol_version)`, added to the new `src/gobby/storage/recall_shadow_labels.py` mixin this deliverable creates — the sweep writes `recall_shadow_judge_state`, the same label-lifecycle concern that mixin owns. It is exposed as `gobby memory recall-signals supersede-legacy-cohort` beside the existing `gate`, `audit-labels`, and `drift` subcommands.

   The sweep **inserts**, it does not update. `recall_shadow_judge_state` rows are created at claim time — `claim_shadow_request` is the only writer, via `INSERT ... ON CONFLICT (recall_request_id, label_source, judge_protocol_version) DO UPDATE` — so a request that was never claimed has no state row at all, and a plain `UPDATE ... SET status='terminal'` would silently no-op on exactly the backlog that matters. The operation selects every `recall_signal_requests` row with `weighting->>'query_construction_version' IS NULL` that has no `complete` state row for the given `(label_source, judge_protocol_version)`, then upserts `status='terminal'`, `next_attempt_at=NULL`, `claim_token=NULL`, `last_error='query_construction_version_superseded'` under the same primary key. Re-running it selects nothing new and rewrites the same terminal rows, so it is safe to repeat and safe to run before the flip. It never touches a `complete` row, so no existing label is disturbed.

**Cut over.** Bump `SHADOW_PROTOCOL_VERSION` to `digest-shadow-query-relevance-v2`. v1 rows stay frozen and valid; v2 begins under the new query. Shadow judging keeps running throughout.

Two targeted files sit above the size-growth threshold and are split before the cohort work lands:

- `recall_refit.py` is at 924 lines. **Split** it: **move** the gate execution path (`run_ship_gate` and `run_ship_gate_from_store`, roughly 370 lines) out of `recall_refit.py` into the new `src/gobby/memory/recall_ship_gate_run.py`, leaving the replay grid, guard battery, and decision serialization behind.
- `recall_shadow_signals.py` is at 919 lines. **Split** it: **move** the atomic label-write path (`insert_usefulness_labels_atomic`, roughly 212 lines) out of `recall_shadow_signals.py` into a new sibling mixin, `RecallShadowLabelStoreMixin` in `src/gobby/storage/recall_shadow_labels.py`, leaving cohort resolution, polling, replay, and audit reads behind. Add the new mixin to the `RecallSignalStore` base list (`recall_signals.py:88-92`, currently `RecallShadowSignalStoreMixin`, `RecallShadowSamplingMixin`, `RecallShadowClaimTransitionMixin`, `RecallShadowGateStoreMixin`); without that line the moved `insert_usefulness_labels_atomic` and the new `supersede_legacy_cohort` are both unreachable from the store every caller uses.

Apply the cohort field and filter updates afterward.

**Acceptance:**

- 4.1.1 - Every new `recall_signal_requests` row carries `query_construction_version` inside `weighting`. symbol: `_weighting_snapshot`.
- 4.1.2 - `GateCohort.identity()` includes the query-construction version. symbol: `GateCohort.identity`.
- 4.1.3 - Cohort-scoped replay queries filter on the persisted version, and a legacy cohort selects only rows written before the key existed. symbol: `RecallShadowSignalStoreMixin.fetch_shadow_replay_rows`.
- 4.1.4 - `fetch_unshadowed_requests` and `claim_shadow_request` require an explicit construction version and pass it to `shadow_cohort_query`. symbol: `RecallShadowSignalStoreMixin.claim_shadow_request`.
- 4.1.5 - A poller running the v2 construction version never claims a legacy-era request. test: `tests/storage/test_recall_signals.py::test_v2_poller_does_not_claim_legacy_requests`.
- 4.1.6 - `supersede_legacy_cohort` inserts terminal state rows for legacy requests that have no prior `recall_shadow_judge_state` row, and re-running it changes nothing. test: `tests/storage/test_recall_signals.py::test_supersede_legacy_cohort_inserts_and_is_idempotent`.
- 4.1.7 - The sweep leaves every `complete` state row untouched. test: `tests/storage/test_recall_signals.py::test_supersede_legacy_cohort_preserves_complete_rows`.
- 4.1.8 - A replay spanning the cutover cannot return rows from both eras in one cohort. test: `tests/storage/test_recall_signals.py::test_cohort_cannot_mix_query_construction_versions`.
- 4.1.9 - New shadow labels carry protocol version v2. file: `src/gobby/memory/shadow_relevance.py`.
- 4.1.10 - The gate execution path and the atomic label-write path live in new modules, and every touched file stays under 1,000 lines. file: `src/gobby/storage/recall_shadow_labels.py`.
- 4.1.11 - `RecallSignalStore` inherits the new label mixin and exposes both `supersede_legacy_cohort` and the moved `insert_usefulness_labels_atomic`. symbol: `RecallSignalStore`.
- 4.1.12 - v1 row count and content are unchanged after cutover. behavior: "v1 cohort frozen" in `docs/contracts/memory-usefulness-label.md`.

### 4.2 Replay the candidate-filter design against what v1 can support [category: test] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/memory/recall_fit.py::*` — scope-reason: the replay harness spans module-level functions and dataclasses with no single enclosing symbol
- `src/gobby/cli/memory/signals.py::*` — scope-reason: the replay report command joins the existing recall-signals command group at module level
- `tests/memory/test_recall_fit.py::*` — scope-reason: test module updated across its cases for the new replay report

v1 snapshots store the scrubbed query and candidate excerpts only. The shadow judge never sees digest content or the assistant response, so a digest-enriched candidate filter **cannot** be replayed against v1. Scope the replay to what v1 supports: a post-retrieval candidate filter over (stored query, candidate excerpts) returning 0-3, compared against the shipped static constants.

**Request-level metrics.** Pairwise accuracy alone scores only requests where the filter selected something, so a filter that abstains constantly can look excellent. The report is request-level and must carry, for each arm:

- `requests_evaluated` — total replayed requests
- `abstention_rate` — share returning zero candidates
- `abstain_correct` — share of abstentions where no candidate in the request carried a useful label (a right silence)
- `abstain_regret` — share of abstentions where at least one candidate carried a useful label (a missed injection)
- `mean_selected` — mean count returned per request
- `pairwise_accuracy` — the existing request-balanced metric, reported with its own denominator so it is never read as a whole-population number
- the same six for the static-constant baseline, at matched `mean_selected` where the arms can be matched

**Durable artifact.** Results land in a `CandidateFilterReplayReport` dataclass with a `to_record()` method, following the `run_drift_check_from_store` convention already used by `gobby memory recall-signals drift` (`cli/memory/signals.py:468`). A new `recall-signals replay-candidate-filter` subcommand emits it, and `--out <path>` writes the JSON to disk so the numbers survive the terminal. Every report states the cohort identity it ran under, including the `query_construction_version` fence from 4.1.

HippoRAG 2's comparable post-retrieval filter buys +1.7 Recall@5 for 4x per-query latency and returns nothing in 18% of queries. This must clear a comparable bar before anyone proposes putting it on a turn. No live-path change ships from this deliverable.

**Acceptance:**

- 4.2.1 - A replay report compares the no-digest candidate filter to static constants on v1 labels. file: `src/gobby/memory/recall_fit.py`.
- 4.2.2 - The report carries request-level abstention rate, abstain-correct, abstain-regret, and mean selected count for both arms. symbol: `CandidateFilterReplayReport.to_record`.
- 4.2.3 - `--out <path>` writes the report JSON to a durable file, and the report names the cohort identity it ran under. file: `src/gobby/cli/memory/signals.py`.
- 4.2.4 - The report states explicitly that digest-conditioned evaluation requires v2 data. file: `src/gobby/memory/recall_fit.py`.

## P5 Verification
`kind: verification`

End-to-end acceptance.

- Recall adds no LLM latency: no new `feature=memory.recall.classify` entries in `~/.gobby/logs/daemon.log` after cutover.
- `recall_signal_requests.query` for `caller='memory.recall'` contains natural-language text after cutover, not a space-joined term bag.
- Every post-cutover request carries the fence: `select count(*) from recall_signal_requests where caller='memory.recall' and created_at > '<cutover>' and weighting->>'query_construction_version' is null` returns 0.
- No v2 label sits on a legacy-era retrieval: `select count(*) from recall_usefulness u join recall_signal_requests r on r.recall_request_id=u.recall_request_id where u.judge_protocol_version='digest-shadow-query-relevance-v2' and r.weighting->>'query_construction_version' is null` returns 0.
- No legacy request is left claimable: `select count(*) from recall_shadow_judge_state s join recall_signal_requests r on r.recall_request_id=s.recall_request_id where r.weighting->>'query_construction_version' is null and s.status in ('claimed','retryable')` returns 0.
- The sweep inserted rather than no-opped: `select count(*) from recall_shadow_judge_state where status='terminal' and last_error='query_construction_version_superseded'` matches the pre-sweep count of unjudged legacy requests.
- Every eligible confirmed lesson is reachable by exactly one route: `select count(*) from memories where deleted_at is null and memory_type='pattern' and tags ? 'review-lesson' and tags ? 'confirmed' and not tags ? 'lesson-domain:plan' and not tags ? 'scope:unscoped' and not exists (select 1 from jsonb_array_elements_text(tags) t where t like 'path:%')` returns 0.
- Both hybrid legs are replayable for enriched turns: `select count(*) from recall_signal_requests where caller='memory.recall' and created_at > '<cutover>' and weighting ? 'bm25_query'` is greater than 0, and every such row's `bm25_query` differs from its `query`.
- At least one live turn injects zero memories, recorded as `drop_reason='other'` with `drop_detail='selection_min_score'`:
  `select count(*) from recall_injection_outcomes where caller='memory.recall' and drop_reason='other' and drop_detail='selection_min_score' and created_at > '<cutover>'` is greater than 0.
- No post-cutover outcome row violates the enum: `select count(*) from recall_injection_outcomes where created_at > '<cutover>' and drop_reason is not null and drop_reason not in ('already_injected','review_lesson','empty_content','payload_empty','budget','other')` returns 0.
- Review lessons are absent from recall results after cutover:
  `select count(*) from recall_signal_hits h join recall_signal_requests r on r.recall_request_id=h.recall_request_id join memories m on m.id::text=h.memory_id where r.caller='memory.recall' and r.created_at > '<cutover>' and m.tags ? 'review-lesson'` returns 0.
- `select count(*) from memories where deleted_at is null and length(content) > 3000` returns 0.
- `recall_usefulness` holds rows under both v1 and v2, with v1 counts matching the pre-cutover snapshot.
- `gcode grep -F "content_offset" src/gobby/mcp_proxy/tools/memory_recall.py src/gobby/hooks/memory_recall_delivery.py` returns no matches.
- `wc -l` shows every split file under 1,000: `src/gobby/mcp_proxy/tools/memory.py`, `memory_write.py`, `src/gobby/memory/recall_refit.py`, `recall_ship_gate_run.py`, `src/gobby/storage/recall_shadow_signals.py`, `recall_shadow_labels.py`.
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_recall.py tests/mcp_proxy/tools/test_memory_recall.py tests/memory/test_search_ranking.py tests/review_learning/test_recall_limits.py tests/storage/test_recall_signals.py -v`
- `uv run ruff format src/ && uv run ruff check src/ && uv run mypy src/`

## F1 Follow-up
`kind: framing`

The corpus grows about 700 rows a month. `memory-capture-nudge.yaml` already tells agents not to save derived architecture facts, yet roughly 95% of writes are exactly that. Nothing gates ordinary task-execution writes: `guard-plan-memory-writes` fires only in plan mode, once, and is acknowledgeable. A write-side admission gate is the next lever and is out of scope here.

## E1 Execution Notes
`kind: framing`

Planning depth is Gobby lightweight: elicitation complete, no artifact enhancement, no adversarial review, no build handoff.

This file is canonical. `~/.claude/plans/won-t-a-project-memory-structured-stream.md` is a display mirror and is stale once this artifact changes.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Delete the substantive-prompt classifier
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: No LLM call occurs on the recall path. file: `src/gobby/memory/recall.py`.

    1.1.2: A prompt passing `_hard_skip_reason` reaches search with no classification
    step. symbol: `MemoryRecallRunner.run`.

    1.1.3: `memory_recall.timeout` is removed and config load-order validation passes.
    file: `src/gobby/config/sessions.py`.

    1.1.4: `RECALL_CLASSIFICATION_SCHEMA` has no remaining references. file: `src/gobby/memory/generation_schemas.py`.'
  labels:
  - covers:memory-injection-redesign:1.1:1.1.1
  - covers:memory-injection-redesign:1.1:1.1.2
  - covers:memory-injection-redesign:1.1:1.1.3
  - covers:memory-injection-redesign:1.1:1.1.4
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Fix the review-lesson tag and remove the dead second filter
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `REVIEW_LESSON_TAG` equals the tag lessons are written
    with. file: `src/gobby/memory/recall.py`.

    1.2.2: `_has_review_lesson_tag` and its `_filter_ranked` branch are removed. symbol:
    `MemoryRecallRunner._filter_ranked`.

    1.2.3: A `review-lesson` tagged memory is excluded at the search layer and never
    appears in recall results. test: `tests/memory/test_recall.py::test_review_lessons_excluded_from_prompt_recall`.'
  labels:
  - covers:memory-injection-redesign:1.2:1.2.1
  - covers:memory-injection-redesign:1.2:1.2.2
  - covers:memory-injection-redesign:1.2:1.2.3
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Move recall DB work off the daemon event loop
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: No synchronous DB call executes on the daemon loop
    during recall. symbol: `MemoryRecallRunner.run`.

    1.3.2: One batched off-loop read serves both the dedupe ledger and the digest
    slice. file: `src/gobby/memory/recall.py`.

    1.3.3: A batched read that raises yields zero injected memories for that turn.
    test: `tests/memory/test_recall.py::test_batched_read_failure_injects_nothing`.

    1.3.4: A failed off-loop outcome write is swallowed and does not change what the
    turn delivered. test: `tests/memory/test_recall.py::test_outcome_write_failure_preserves_delivery`.

    1.3.5: A `selection_min_score` change made at runtime takes effect on the next
    recall call with no daemon restart and no stale cached runner. test: `tests/mcp_proxy/tools/test_memory_recall.py::test_runtime_config_change_reaches_next_call`.

    1.3.6: A resolver returning `None` reports memory services unavailable and injects
    nothing. symbol: `register_memory_recall_tool`.'
  labels:
  - covers:memory-injection-redesign:1.3:1.3.1
  - covers:memory-injection-redesign:1.3:1.3.2
  - covers:memory-injection-redesign:1.3:1.3.3
  - covers:memory-injection-redesign:1.3:1.3.4
  - covers:memory-injection-redesign:1.3:1.3.5
  - covers:memory-injection-redesign:1.3:1.3.6
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Push the review-lesson path-tag match into the query
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.4.1: `alist_memories` accepts `tags_any` and pushes it to
    the SQL tag filter. symbol: `MemoryRepository.alist_memories`.

    1.4.2: Lesson recall for touched files no longer fetches a fixed 200-row page.
    symbol: `ReviewLearningService._candidate_lesson_memories`.

    1.4.3: `build_tags` stamps `scope:unscoped` on a lesson whose finding yields no
    file path. symbol: `build_tags`.

    1.4.4: The backfill stamps only eligible lessons carrying no `path:` tag, and
    a second run changes nothing. test: `tests/review_learning/test_lessons.py::test_unscoped_backfill_is_idempotent`.

    1.4.5: Path-matched lessons precede unscoped lessons, and neither query exceeds
    `limit * _CANDIDATE_OVERFETCH` rows, a bound that stays constant as the corpus
    grows (amended 2026-08-23, approved by Josh in session #10977). test: `tests/review_learning/test_recall_limits.py::test_path_matched_precede_unscoped_within_limit`.

    1.4.6: An unscoped lesson is reachable for any touched file regardless of corpus
    size. test: `tests/review_learning/test_recall_limits.py::test_unscoped_lesson_reachable_beyond_legacy_page`.'
  labels:
  - covers:memory-injection-redesign:1.4:1.4.1
  - covers:memory-injection-redesign:1.4:1.4.2
  - covers:memory-injection-redesign:1.4:1.4.3
  - covers:memory-injection-redesign:1.4:1.4.4
  - covers:memory-injection-redesign:1.4:1.4.5
  - covers:memory-injection-redesign:1.4:1.4.6
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Split query representations across the search legs
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '2.1.1: `search()` accepts `embed_text` and embeds it verbatim
    when present. symbol: `SearchService.search`.

    2.1.2: Callers omitting `embed_text` retain YAKE-derived embedding. test: `tests/memory/test_search_ranking.py::test_embed_text_absent_preserves_yake_path`.'
  labels:
  - covers:memory-injection-redesign:2.1:2.1.1
  - covers:memory-injection-redesign:2.1:2.1.2
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Build a natural-language recall query with conditional digest enrichment
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '1.3'
  validation_criteria: '2.2.1: A substantive prompt is embedded as natural language
    rather than a keyword bag. symbol: `MemoryRecallRunner.run`.

    2.2.2: `RECALL_QUERY_CONSTRUCTION_VERSION` is importable by both `recall.py` and
    `recall_signal_log.py` with no import cycle. file: `src/gobby/memory/recall_constants.py`.

    2.2.3: A scrubbed bag under `RECALL_THIN_QUERY_TERMS` is enriched with a digest
    tail of at most `RECALL_DIGEST_TAIL_CHARS`. test: `tests/memory/test_recall.py::test_thin_query_enriched_with_bounded_digest_tail`.

    2.2.4: The assembled `embed_text` never exceeds `MAX_QUERY_CHARS`. test: `tests/memory/test_recall.py::test_embed_text_respects_max_query_chars`.

    2.2.5: A recall-local stripper removes `<project-memory>` blocks from the digest
    slice, leaving `strip_injected_context` unchanged. symbol: `MemoryRecallRunner.run`.'
  labels:
  - covers:memory-injection-redesign:2.2:2.2.1
  - covers:memory-injection-redesign:2.2:2.2.2
  - covers:memory-injection-redesign:2.2:2.2.3
  - covers:memory-injection-redesign:2.2:2.2.4
  - covers:memory-injection-redesign:2.2:2.2.5
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Add an independent selection floor
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '2.3.1: `min_score` default is 0.55 and `selection_min_score`
    exists as a separate key defaulting to 0.65. file: `src/gobby/config/sessions.py`.

    2.3.2: A turn whose candidates all fall below `selection_min_score` injects zero
    memories. test: `tests/memory/test_recall.py::test_selection_floor_can_yield_zero_memories`.

    2.3.3: A candidate below the floor records `drop_reason=''other''` with `drop_detail=''selection_min_score''`.
    test: `tests/memory/test_recall.py::test_selection_floor_drop_detail`.

    2.3.4: A null-similarity candidate is dropped with `drop_detail=''null_similarity''`
    rather than admitted. symbol: `MemoryRecallRunner._filter_ranked`.'
  labels:
  - covers:memory-injection-redesign:2.3:2.3.1
  - covers:memory-injection-redesign:2.3:2.3.2
  - covers:memory-injection-redesign:2.3:2.3.3
  - covers:memory-injection-redesign:2.3:2.3.4
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Log the query that actually drove retrieval
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '2.4.1: `recall_signal_requests.query` records the embed text
    when one was used. symbol: `SearchService._emit_search_debug`.

    2.4.2: All three emission sites receive the diagnostic text, including the graph
    and Qdrant-keyword paths. symbol: `search_with_graph`.

    2.4.3: A graph-path search driven by `embed_text` logs the embed text, not the
    term bag. test: `tests/memory/test_search_ranking.py::test_graph_path_logs_embed_text`.

    2.4.4: When `embed_text` differs from the term bag, `weighting.bm25_query` holds
    the term bag, so both hybrid legs are replayable. test: `tests/memory/test_recall_signal_log.py::test_enriched_request_records_both_query_legs`.

    2.4.5: Callers without `embed_text` continue logging the original query and write
    no `bm25_query`. symbol: `build_recall_signal_event`.'
  labels:
  - covers:memory-injection-redesign:2.4:2.4.1
  - covers:memory-injection-redesign:2.4:2.4.2
  - covers:memory-injection-redesign:2.4:2.4.3
  - covers:memory-injection-redesign:2.4:2.4.4
  - covers:memory-injection-redesign:2.4:2.4.5
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Split the memory tool module and cap content at 3,000 characters
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '3.1.1: Write-path tools live in a new module and both files
    stay under 1,000 lines. file: `src/gobby/mcp_proxy/tools/memory_write.py`.

    3.1.2: `create_memory_registry` registers the write tools through the new module''s
    entry point. symbol: `create_memory_registry`.

    3.1.3: `create_memory` rejects content over 3,000 chars with the length and limit
    in the error. symbol: `MemoryLifecycleService.create_memory`.

    3.1.4: `update_memory` enforces the same cap. symbol: `MemoryLifecycleService.update_memory`.

    3.1.5: No live memory exceeds 3,000 chars: the four historical over-cap rows were
    condensed under task #20725 before this deliverable, and a corpus check confirms
    max content length 2,995 across 2,819 live memories. behavior: "corpus honors
    the write cap" in `src/gobby/memory/services/lifecycle.py`.'
  labels:
  - covers:memory-injection-redesign:3.1:3.1.1
  - covers:memory-injection-redesign:3.1:3.1.2
  - covers:memory-injection-redesign:3.1:3.1.3
  - covers:memory-injection-redesign:3.1:3.1.4
  - covers:memory-injection-redesign:3.1:3.1.5
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Keep rationale out of delivered memory text on both routes
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: '3.2.1: `_memory_to_payload` omits `rationale`. symbol: `_memory_to_payload`.

    3.2.2: Inline and queued delivery render identical memory bodies. test: `tests/memory/test_recall.py::test_inline_and_queued_bodies_match`.'
  labels:
  - covers:memory-injection-redesign:3.2:3.2.1
  - covers:memory-injection-redesign:3.2:3.2.2
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Delete content-offset slicing and paginate whole memories
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '3.3.1: `_next_chunk` contains no content slicing and emits
    only whole memory bodies. symbol: `_next_chunk`.

    3.3.2: The delivery cursor carries `memory_index` and `chunk_index` only. symbol:
    `_valid_cursor`.

    3.3.3: A memory larger than the serialized budget is emitted whole in its own
    chunk rather than split. test: `tests/mcp_proxy/tools/test_memory_recall.py::test_oversize_memory_emitted_whole`.

    3.3.4: A multi-memory delivery paginates one or more whole memories per chunk
    until exhausted. test: `tests/mcp_proxy/tools/test_memory_recall.py::test_pagination_packs_whole_memories`.'
  labels:
  - covers:memory-injection-redesign:3.3:3.3.1
  - covers:memory-injection-redesign:3.3:3.3.2
  - covers:memory-injection-redesign:3.3:3.3.3
  - covers:memory-injection-redesign:3.3:3.3.4
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Fence query construction durably and open the v2 cohort
  category: code
  task_type: feature
  depends_on:
  - '2.4'
  validation_criteria: '4.1.1: Every new `recall_signal_requests` row carries `query_construction_version`
    inside `weighting`. symbol: `_weighting_snapshot`.

    4.1.2: `GateCohort.identity()` includes the query-construction version. symbol:
    `GateCohort.identity`.

    4.1.3: Cohort-scoped replay queries filter on the persisted version, and a legacy
    cohort selects only rows written before the key existed. symbol: `RecallShadowSignalStoreMixin.fetch_shadow_replay_rows`.

    4.1.4: `fetch_unshadowed_requests` and `claim_shadow_request` require an explicit
    construction version and pass it to `shadow_cohort_query`. symbol: `RecallShadowSignalStoreMixin.claim_shadow_request`.

    4.1.5: A poller running the v2 construction version never claims a legacy-era
    request. test: `tests/storage/test_recall_signals.py::test_v2_poller_does_not_claim_legacy_requests`.

    4.1.6: `supersede_legacy_cohort` inserts terminal state rows for legacy requests
    that have no prior `recall_shadow_judge_state` row, and re-running it changes
    nothing. test: `tests/storage/test_recall_signals.py::test_supersede_legacy_cohort_inserts_and_is_idempotent`.

    4.1.7: The sweep leaves every `complete` state row untouched. test: `tests/storage/test_recall_signals.py::test_supersede_legacy_cohort_preserves_complete_rows`.

    4.1.8: A replay spanning the cutover cannot return rows from both eras in one
    cohort. test: `tests/storage/test_recall_signals.py::test_cohort_cannot_mix_query_construction_versions`.

    4.1.9: New shadow labels carry protocol version v2. file: `src/gobby/memory/shadow_relevance.py`.

    4.1.10: The gate execution path and the atomic label-write path live in new modules,
    and every touched file stays under 1,000 lines. file: `src/gobby/storage/recall_shadow_labels.py`.

    4.1.11: `RecallSignalStore` inherits the new label mixin and exposes both `supersede_legacy_cohort`
    and the moved `insert_usefulness_labels_atomic`. symbol: `RecallSignalStore`.

    4.1.12: v1 row count and content are unchanged after cutover. behavior: "v1 cohort
    frozen" in `docs/contracts/memory-usefulness-label.md`.'
  labels:
  - covers:memory-injection-redesign:4.1:4.1.1
  - covers:memory-injection-redesign:4.1:4.1.2
  - covers:memory-injection-redesign:4.1:4.1.3
  - covers:memory-injection-redesign:4.1:4.1.4
  - covers:memory-injection-redesign:4.1:4.1.5
  - covers:memory-injection-redesign:4.1:4.1.6
  - covers:memory-injection-redesign:4.1:4.1.7
  - covers:memory-injection-redesign:4.1:4.1.8
  - covers:memory-injection-redesign:4.1:4.1.9
  - covers:memory-injection-redesign:4.1:4.1.10
  - covers:memory-injection-redesign:4.1:4.1.11
  - covers:memory-injection-redesign:4.1:4.1.12
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Replay the candidate-filter design against what v1 can support
  category: test
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: A replay report compares the no-digest candidate filter
    to static constants on v1 labels. file: `src/gobby/memory/recall_fit.py`.

    4.2.2: The report carries request-level abstention rate, abstain-correct, abstain-regret,
    and mean selected count for both arms. symbol: `CandidateFilterReplayReport.to_record`.

    4.2.3: `--out <path>` writes the report JSON to a durable file, and the report
    names the cohort identity it ran under. file: `src/gobby/cli/memory/signals.py`.

    4.2.4: The report states explicitly that digest-conditioned evaluation requires
    v2 data. file: `src/gobby/memory/recall_fit.py`.'
  labels:
  - covers:memory-injection-redesign:4.2:4.2.1
  - covers:memory-injection-redesign:4.2:4.2.2
  - covers:memory-injection-redesign:4.2:4.2.3
  - covers:memory-injection-redesign:4.2:4.2.4
  tdd: false
  source_section: '4.2'
  assigned_agent: backend-developer
```

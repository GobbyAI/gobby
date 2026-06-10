# Review: code_index + search + ai

- **Scope:** `src/gobby/code_index/` (storage, models, sync_worker, maintenance, trigger, codewiki_trigger, cleanup, gcode_gateway, context, summarizer), `src/gobby/search/` (embeddings, unified, keyword, keywords, models, backends/), `src/gobby/ai/` (registry, text_generation, audio, vision) — ~7,600 lines. Adjacent surfaces traced: `servers/routes/code_index.py`, `servers/routes/llm.py`, `servers/routes/voice.py`, `mcp_proxy/tools/skills/search_skills.py`, `skills/search.py`, `memory/manager.py`, `adapters/acp_client.py`, `adapters/codex_impl/client.py`, `cli/installers/git_hooks.py`.
- **Reviewer:** Claude Fable 5 (6 parallel deep-review agents; every Blocker independently re-verified against source by the synthesizer)
- **Commit / branch:** e75c1d4 / 0.5.0
- **Summary:** 6 Blocker · 38 Important · 20 Nit — the index freshness layer has no deletion story (tombstone vectors flagged as synced), keyword search ignores its fitted corpus and can leak cross-project results, semantic search degrades silently and latches degraded, and the AI adapter layer converts provider errors into successful empty text that halts candidate fallback.

## Findings — Blockers

### [BLOCKER] Deleted/renamed files leave tombstone vectors in Qdrant forever — no reconciliation path exists
- **Where:** `src/gobby/code_index/sync_worker.py:348` (per-file vector delete runs only when re-syncing a still-existing row); `cleanup.py:85` / `context.py:104` (collection-level delete, purge/invalidate only); `storage.py:292-311` (`get_orphan_files` — zero production callers, verified), `storage.py:518-524` (`delete_file` — zero callers)
- **Failure mode:** when a file is deleted or renamed, gcode's incremental index updates the PG rows, but the daemon-owned Qdrant projection is only pruned per-file while re-syncing an existing `code_indexed_files` row or at whole-project purge. A removed row never flows through `_sync_file` again, so its symbol vectors persist indefinitely; renames leave the old path's points alongside the new. Neither the trigger, sync worker, nor maintenance calls the orphan helpers — they are dead code — and the daemon and git hook invoke `gcode index` without `--sync-projections` (zero matches in src/, verified), so gcode-side pruning never runs either.
- **Why it matters:** semantic search permanently returns symbols from deleted files; the vector store grows without bound; the freshness layer's core contract is violated with no self-healing short of manual `gcode invalidate`. The same shape applies to the FalkorDB graph projection (see Important below).
- **Minimal fix:** add an orphan-reconciliation pass to `_run_maintenance` (wire up `get_orphan_files`) issuing `vector_store.delete(filters={"file_path": ..., "project_id": ...})` for removed paths.
- **Confidence:** high (85%) — residual doubt only on whether the Rust binary prunes Qdrant during plain `gcode index`; daemon never requests it and the sync worker owns Qdrant writes in this architecture.

### [BLOCKER] File edited to zero symbols: stale vectors kept, yet file marked `vectors_synced=TRUE`
- **Where:** `src/gobby/code_index/sync_worker.py:340-341` (`if not symbols: return` BEFORE the per-file vector delete at `:348` — verified in source), `:247` (`mark_vectors_synced` stamped unconditionally after `_sync_vectors` returns)
- **Failure mode:** a previously-indexed file edited to yield no symbols (gutted, comment-only) early-returns before deleting its old points, then gets flagged synced — the old symbols' vectors stay in Qdrant permanently and nothing will ever retry.
- **Why it matters:** contract violated while reporting success; stale search hits the freshness machinery believes are fresh.
- **Minimal fix:** move the `vector_store.delete(filters=...)` call above the empty-symbols early-exit.
- **Confidence:** high.

### [BLOCKER] `invalidate()` reports unqualified success while leaving stale projection data behind
- **Where:** `src/gobby/code_index/context.py:81-108`; consumed at `servers/routes/code_index.py:368-393`
- **Failure mode:** `invalidate()` deletes hub rows first, then best-efforts the graph clear and Qdrant collection delete — both failure paths degrade to `logger.warning` (verified in source) — and returns `None` unconditionally; the route answers `{"status": "ok"}` regardless. If FalkorDB/Qdrant is down at that moment, projections permanently retain symbols whose hub rows are gone; nothing retries (vector cleanup recurs only on a future invalidate or root-vanished purge). Subsequent semantic/graph results can surface deleted symbols.
- **Why it matters:** contract is "Clear all index data"; partial failure is reported as success to gcode, which proceeds believing the slate is clean — durable cross-store drift.
- **Minimal fix:** collect per-store outcomes and return/raise so the route can report partial failure; or persist a "projection cleanup pending" marker the maintenance loop retries.
- **Confidence:** high on the code path; trigger frequency low (requires projection-store failure coinciding with invalidate).

### [BLOCKER] Keyword backend ignores the fitted corpus and searches the whole table unscoped — cross-project result leakage
- **Where:** `src/gobby/search/keyword.py:198-214` (`KeywordAsyncSearchBackend.search_async` → `search` passes no `filters` — verified), `:127-157` (`BM25SearchBackend.search` applies filters only when passed; SQL runs over the entire table), contract at `search/backends/__init__.py:45-51`
- **Failure mode:** `fit_async(items)` merely caches items; `search_async` runs an unscoped query, so results can contain IDs never fitted. Confirmed real: skills indexing fits only the current project's skills (+global), the `skills` table has `project_id`/`enabled` columns, the skills `_TableConfig` defines no filters (`keyword.py:74-78`), and default `SearchConfig.mode` is `"auto"` — any daemon without a reachable embedding endpoint falls back to this path, and `search_skills` can return other projects' / disabled / non-indexed skills (unknown IDs pass through as `skill_name=skill_id`). Corollary: `fit_async([])` then search returns whole-table hits — fitting nothing yields results. The fitted set is only honored by the in-memory fallback, which fires solely on zero DB hits.
- **Why it matters:** cross-project leakage plus fitted-corpus contract violated while reporting success.
- **Minimal fix:** thread the fitted ID set (or at minimum project/enabled scoping via `_TableConfig.filters`) into the keyword query; add a `filters` parameter to `KeywordAsyncSearchBackend.search_async` and pass scoping from `UnifiedSearcher`.
- **Confidence:** high.

### [BLOCKER] `_using_fallback` is never reset by `fit_async` — sticky fallback survives a successful refit while stats report "embedding"
- **Where:** `src/gobby/search/unified.py:196` (set), `:227-229` (fit reset omits it — verified: only `_items`/`_fitted`/`_fitted_mode` reset), `:268/:290` (`_active_backend = "embedding"` on successful refit), `:356-357` (search short-circuits on the flag before mode dispatch — verified)
- **Failure mode:** one transient embedding failure latches `_using_fallback=True`; a later successful `fit_async` rebuilds the embedding index and sets `_active_backend="embedding"`, but `search_async` checks `_using_fallback` first and serves keyword results forever. `get_active_backend()` returns `"embedding"` while `get_stats()` reports `using_fallback=True` — contradictory state; the embedding index is built and paid for but never used. Skills reindex on every mutation, so refit-after-fallback is a mainline sequence. No test covers fallback-then-successful-refit.
- **Why it matters:** silent permanent quality degradation until `clear()` or restart, reported as healthy.
- **Minimal fix:** reset `_using_fallback`/`_fallback_reason` at the top of `fit_async` alongside the other state.
- **Confidence:** high.

### [BLOCKER] ACP provider failures are swallowed and returned as successful empty text — halting candidate fallback
- **Where:** `src/gobby/ai/text_generation.py:682-694` (`_collect_acp_text` — verified: error events fail the `content_delta`/`result` type filter and are dropped), `adapters/acp_client.py:617-625` (JSON-RPC errors yielded as `StreamEvent(event_type="error", ...)`, never raised), `text_generation.py:182-198` (`_try_generate_result_candidates` treats the `""` return as success, logs `success=True`, and STOPS the fallback chain)
- **Failure mode:** a gemini/grok/qwen auth failure or model error mid-profile produces an empty summary/expansion instead of falling through to the next candidate or raising. The `result` branch is also dead for this client: `result` events carry only `{"stats": ...}`, which `_stream_event_text` can never extract text from.
- **Why it matters:** contract violated while reporting success — feature callers (session summaries, task expansion) receive `""` with no error and better candidates are never tried. No test covers an ACP error event.
- **Minimal fix:** raise on `event_type == "error"` in `_collect_acp_text`; optionally treat all-empty output as failure so the service advances to the next candidate (which would also mitigate the codex equivalent below).
- **Confidence:** high.

## Findings — Important (code_index)

### [IMPORTANT] TOCTOU: unconditional `mark_vectors_synced` can stamp stale embeddings as fresh after a concurrent reindex
- **Where:** `sync_worker.py:222` (snapshot read) → slow `_sync_vectors` (remote embedding calls) → `:247` (mark); `storage.py:355-362` (`UPDATE ... SET vectors_synced = TRUE WHERE id = %s` — no hash guard)
- **Failure mode:** worker reads symbols, spends seconds embedding; the trigger reindexes the file meanwhile (resets `vectors_synced=FALSE`, replaces symbols); worker upserts pre-edit embeddings and flips the flag for the new content. Same shape for `mark_graph_synced` and `update_symbol_summary` (summary generated from a disk read with no hash check, written unconditionally by id).
- **Minimal fix:** conditional marks: `... WHERE id = %s AND content_hash = %s` using the snapshot's hash (column exists in schema).
- **Confidence:** high on mechanism; the 2s debounce + multi-second embedding latency makes the window real.

### [IMPORTANT] Transient `root.is_dir()` failure triggers immediate destructive purge of the entire project index
- **Where:** `sync_worker.py:152-160` (every 5s) and `maintenance.py:88-96`; purge at `cleanup.py:42-100`
- **Failure mode:** `is_dir()` returns False on transient conditions (stale NFS handle, unmounted volume, worktree being recreated), and one false reading deletes all PG rows, the graph projection, and the Qdrant collection — recovery requires full reindex + re-embedding + re-summarization (billed LLM work). The check runs 60×/minute.
- **Minimal fix:** require N consecutive missing observations (maintenance cadence) before purging; `_sync_pass` should merely skip missing roots.
- **Confidence:** high on mechanism; worktree/external-volume users are the realistic victims.

### [IMPORTANT] Pending-sync queues have no failure bookkeeping — permanent failures starve the queue head; backoff helpers are dead code
- **Where:** `storage.py:323-353` (`get_pending_sync_files`, `ORDER BY indexed_at LIMIT %s`); `storage.py:376-386` (`mark_graph_sync_attempted` — zero production callers); `sync_worker.py:101-105` (embed model probed ONCE at startup, never retried), `:164-170` (`vectors=config.embedding_enabled` even when `embed_model`/`vector_store` is None); `summarizer.py:78-80` + `storage.py:606-630` (failed summaries re-selected every 300s forever, failures logged at DEBUG)
- **Failure mode:** three instances of one shape. (a) Embeddings down at daemon start → `embed_model` stays None forever; ≥batch_size permanently-unsyncable vector-pending rows pin the batch head (sorted oldest-first, LIMIT 50), so newer files' *graph* sync never runs either. (b) Persistently failing files (oversized, rejected content) are re-selected every pass with no attempted-at filter — the schema column and marker method exist and are never used. (c) Permanently failing summaries retry every maintenance pass indefinitely (post-billing timeouts = slow unbounded spend) with zero visible signal at default log level.
- **Minimal fix:** retry `_build_embed_model` in the loop; exclude unsyncable vector rows from the batch; record attempt timestamps on failure and deprioritize within a cooloff; log summary failures at WARNING with per-pass aggregates.
- **Confidence:** high on mechanics.

### [IMPORTANT] Trigger flush drops files permanently on gcode failure/timeout — no requeue
- **Where:** `trigger.py:93` (`pop` before subprocess), `:116` (30s timeout), `:118-141` (failure paths only log)
- **Failure mode:** files are removed from the pending map before `gcode index --files` runs; any failure discards the batch. Staleness until the file is edited again or the 300s maintenance backstop. The 30s cap is plausible to hit on large debounce coalesces.
- **Minimal fix:** on failure, union the failed set back into `_pending_by_root` and re-arm with backoff.
- **Confidence:** high.

### [IMPORTANT] No git hooks for checkout/merge/rewrite — branch switches stale for up to 5 minutes in all projections
- **Where:** `cli/installers/git_hooks.py:28-180` — `HOOK_TEMPLATES` contains only `pre-commit` and `post-commit`; the editor-event trigger fires only from edit tools
- **Failure mode:** `git checkout/pull/rebase`, stash pops, and worktree creation change hundreds of files with no editor event and no local post-commit; sole recovery is the 300s maintenance reindex. Vector/graph search serves the old branch's symbols meanwhile.
- **Minimal fix:** add `post-checkout`/`post-merge`/`post-rewrite` templates mirroring the post-commit flow.
- **Confidence:** high on the gap.

### [IMPORTANT] Graph projection never pruned for deleted files; reconciliation helper dead
- **Where:** `storage.py:388-397` (`reset_graph_sync_for_project` — no callers); graph clear only at purge/invalidate; per-file graph sync is add/update-only (`sync_worker.py:396-407`)
- **Failure mode:** same shape as the vector tombstone Blocker — a deleted file's nodes/edges stay in FalkorDB until manual rebuild/purge; `callers`/blast-radius results include phantom code.
- **Confidence:** medium (70%) — daemon-side code definitively never prunes; whether the gcode binary does during plain `gcode index` is unconfirmed.

### [IMPORTANT] Summarizer prompt injection from indexed source into stored summaries
- **Where:** `summarizer.py:24-29` (single-string prompt), `:57-63` (raw source interpolated inside a ``` fence; truncation can split the fence)
- **Failure mode:** source containing ``` plus instructions escapes the fence; the result is persisted to `code_symbols.summary` and surfaced as trusted-looking text in gcode search output consumed by agents — a persistence channel for laundering instructions into agent context with index authority.
- **Minimal fix:** escape/strip backticks in source, mark the block data-only in the prompt, sanitize/length-cap the stored summary.
- **Confidence:** high on mechanism; medium on downstream impact.

### [IMPORTANT] User-supplied `file_path`/`symbol_id` passed to gcode argv with no hygiene
- **Where:** `gcode_gateway.py:149-165`, `:179-230`; values originate from HTTP at `servers/routes/code_index.py:142-151`, `:216-231`
- **Failure mode:** no `--` sentinel, no rejection of hyphen-leading values, no daemon-side traversal check — safety currently delegated entirely to the gcode binary's clap parser defaults, which the daemon does not control or pin behaviorally.
- **Minimal fix:** reject values starting with `-` and absolute/`..` paths before spawning (or use `--file=<value>` single-token form).
- **Confidence:** high that no hygiene exists; low-medium exploitable today.

### [IMPORTANT] Gateway's `GcodeProjectNotFoundError` never mapped at the API boundary — stale project rows yield 500 + exception spam instead of 404
- **Where:** taxonomy at `gcode_gateway.py:53-64`; mapping gap at `servers/routes/code_index.py:71-80` (`_graph_http_exception` special-cases only the context-level error). `maintenance.py:117` and `sync_worker.py:298` consume the subclass, proving it's a routable condition.
- **Minimal fix:** add `GcodeProjectNotFoundError` → 404.
- **Confidence:** high.

### [IMPORTANT] Synchronous psycopg config read on the event loop in the codewiki refresh path
- **Where:** `codewiki_trigger.py:106` (`codewiki_on_commit_enabled(...)` → `config_store.py:110-118` `fetchone`) called directly from the async route `refresh_codewiki` (`servers/routes/code_index.py:396-418`)
- **Failure mode:** every post-commit hook POST blocks the daemon loop on a PG round-trip — the lone regression in a subsystem that otherwise offloads DB work correctly.
- **Minimal fix:** offload via `_run_db`/`asyncio.to_thread`.
- **Confidence:** high.

### [IMPORTANT] `search_symbols_by_name` has no exact-first ranking and no deterministic tiebreak — exact matches can be truncated out by LIMIT
- **Where:** `storage.py:135-151` (substring LIKE both sides, `ORDER BY name LIMIT %s`)
- **Failure mode:** for short common leaf refs (`run`, `add`) hundreds of rows match; names sorting earlier displace the exact match beyond the limit. Both real consumers (consumer_sweep `limit=20`, route fallback limit 25) do "search then filter exact" — when the exact row falls outside the window, resolution silently fails or becomes order-dependent. No `id` tiebreak → nondeterministic truncation; no offset → callers can't page.
- **Minimal fix:** `ORDER BY (name = %s) DESC, (name LIKE %s ESCAPE '\') DESC, name, id`, or an `exact: bool` parameter.
- **Confidence:** high on mechanism.

### [IMPORTANT] `qualified_name` is leaf-only by convention but documented nowhere — and one consumer was built on the opposite assumption
- **Where:** `models.py:40` (bare field, no semantics comment); the production writer is the external Rust `gcode` binary; live rows and fixtures agree (`greet`, `Calculator.add` — never module-qualified); `docs/contracts/plan-coverage.md:69` documents `symbol: gobby.module.Symbol`
- **Failure mode:** an undocumented cross-language contract — the binary and `models.py` must stay in lockstep on uuid5 ID derivation (reproduced: `Symbol.make_id` matches live binary-written IDs) AND `qualified_name` shape, with zero enforcement. The consumer-sweep Blocker in `docs/reviews/plans-sync.md` is the realized breakage.
- **Minimal fix:** document the shape on `models.py:40` and in the plan-coverage contract; add a conformance test against a freshly indexed fixture.
- **Confidence:** high.

### [IMPORTANT] `search_content_fts` LIKE fallback is written in SQLite dialect — crashes on PostgreSQL if ever taken, and is unreachable by construction
- **Where:** `storage.py:823-838` — `substr(content, max(1, instr(content, %s) - 60), 120)`: PG has neither `instr()` nor scalar two-arg `max()`; `like_query = f"%{query}%"` unescaped; dead `params` init at `:827`
- **Failure mode:** the advertised resilience path raises `UndefinedFunction` if resurrected; meanwhile real FTS failures are swallowed upstream (`keyword.py:159-163` returns `[]` at DEBUG), so pg_search misconfiguration yields silent empty results instead of any fallback. No test exercises the fallback.
- **Minimal fix:** rewrite in PG dialect with `_escape_like`, or delete the fallback and surface backend failure distinctly from "no matches".
- **Confidence:** high.

### [IMPORTANT] CodeIndexStorage carries a full parallel write path with zero production callers — a second implementation of the Rust indexer's semantics, kept green only by tests
- **Where:** `storage.py` — `upsert_symbols`/`upsert_file`/`upsert_imports`/`upsert_calls`/`upsert_content_chunks`/`get_stale_files`/`get_orphan_files`/per-file deletes and more (exhaustive caller grep: the daemon's live surface is the search/read/mark/delete-project subset only)
- **Failure mode:** tests assert invariants production never executes — notably the summary-preservation rule (`summary=CASE WHEN excluded.content_hash != ... THEN NULL`, `storage.py:86-87`, tested against the Python upsert) is only correct in production if the Rust writer nulls summaries the same way. Behavioral drift between the two writers (stale summaries, wrong sync flags) would be invisible until search quietly rots.
- **Minimal fix:** delete the unused write path, or mark it the reference implementation and add a cross-implementation conformance test (index a fixture with the real binary, assert summary-nulling/flag behavior).
- **Confidence:** high on caller absence; medium on actual divergence.

## Findings — Important (search/)

### [IMPORTANT] EmbeddingBackend index state mutated non-atomically across an await with no lock — concurrent search gets crash or silent ID/vector mis-association
- **Where:** `search/backends/embedding.py:127-141` (`fit_async` assigns `_item_ids`/`_item_contents`, then awaits `generate_embeddings` before `_item_embeddings` is replaced); read side `:194` (`zip(..., strict=True)`)
- **Failure mode:** during the (multi-second) await, `_fitted` is still True, `_item_ids` is the NEW list and `_item_embeddings` the OLD. Concurrent search: count changed → ValueError; count unchanged (common: skill content update) → new IDs silently zipped against old vectors. The MCP path makes this real: rebuilds run in a second thread (`skills/search.py:235-236` spawns `asyncio.run`) while the daemon loop serves concurrent `search_skills`; two dirty searches can both enter `build()`. In AUTO mode the ValueError is swallowed → `_using_fallback` latches (see Blocker) — one transient race permanently degrades skill search.
- **Minimal fix:** build into locals and publish atomically after the await; add a lock (reachable from two threads).
- **Confidence:** high on mechanism.

### [IMPORTANT] Embedding-API result order assumed; `response.data` never sorted by `index`
- **Where:** `search/embeddings.py:553` and `:480` (`[item.embedding for item in response.data]`)
- **Failure mode:** the OpenAI contract attaches `index` precisely because order isn't guaranteed; any OpenAI-compatible server (LM Studio, Ollama, proxies) returning out-of-order data silently associates text[i] with embedding[j]. Wrong vectors enter the 60s cache and the in-memory index while count/dim validation passes.
- **Minimal fix:** `sorted(response.data, key=lambda d: d.index)` at both sites.
- **Confidence:** medium (mainstream providers preserve order; one-line fix removes the dependency).

### [IMPORTANT] Dimension mismatch degrades to silent "no matches" when `dim` is unset
- **Where:** `search/backends/embedding.py:29-30` (`if len(vec1) != len(vec2): return 0.0`); dim optional at `:68` and `unified.py:90`
- **Failure mode:** model dimension change between fit and query (LM Studio model swap behind the same name) with `expected_dim` unthreaded → every similarity exactly 0.0 → `> 0` filter drops everything → `[]` presented as "no matching skills/memories" with zero logging — bypassing the dim validation built in embeddings.py:497-517.
- **Minimal fix:** warn once and/or raise on length mismatch in `_cosine_similarity`; intra-index length mismatch is never legitimate.
- **Confidence:** high on path; medium on trigger frequency.

### [IMPORTANT] `needs_refit`/`mark_update` staleness contract is a no-op in both backends
- **Where:** `search/backends/embedding.py:204-206` (`needs_refit` = `not _fitted` only), `keyword.py:216-217` (hardcoded False), `unified.py:539-547` (`mark_update` — keyword backend has no such attribute, embedding untouched; closing comment is false)
- **Failure mode:** after add/update/remove + `mark_update()`, `needs_refit()` returns False and stale vectors keep serving. Callers survive only by routing around the API (SkillSearch keeps its own `_pending_updates` counter — meaning up to threshold−1 (default 9) skill changes are invisible to semantic search until the counter trips). Any new consumer trusting the documented protocol gets silently stale results.
- **Minimal fix:** implement a dirty flag set from `mark_update()` and included in `needs_refit()`, or delete the API and document refit-on-change as the caller's job.
- **Confidence:** high.

### [IMPORTANT] Hybrid merge sums incomparable score scales (per-query max-normalized BM25 vs raw cosine)
- **Where:** `unified.py:436-439` (weighted sum); keyword scores per-query max-normalized (`keyword.py:165-166`, top hit always exactly 1.0); embedding scores raw cosine (`backends/embedding.py:192-202`)
- **Failure mode:** the best keyword hit is pegged to 1.0 regardless of absolute relevance — a one-common-term match outranks mediocre-but-real semantic hits; items present in only one backend's list get 0.0 from the other, penalizing single-backend hits vs rank fusion. When the embedding side fails (`unified.py:423-431`), combined scores silently deflate to ≤keyword_weight while `SkillSearchResult.similarity` is documented "[0,1]" — absolute thresholds downstream break. Each backend also contributes only `top_k*2`, losing items whose fused score would qualify.
- **Minimal fix:** Reciprocal Rank Fusion (rank-based, scale-free), or identical normalization both sides re-scaled by active total weight.
- **Confidence:** high on mechanism.

### [IMPORTANT] Sync psycopg searches and pure-Python O(N×d) cosine scans run on the daemon event loop (systemic)
- **Where:** `keyword.py:207-208` (`search_async` → sync `search` → blocking `hub.transaction()` round-trip), awaited on the MCP loop via `search_skills.py:132` — the adjacent line in the same handler explicitly offloads its DB call "to avoid blocking event loop"; `BM25SearchBackend.get_stats` blocking whole-table `count(*)` (`keyword.py:172-183`); `backends/embedding.py:192-200` (per-item Python cosine, millions of float ops for a few thousand items at 768–3072 dims, no await between start and return; query norm recomputed per item)
- **Minimal fix:** wrap the sync backend call in `asyncio.to_thread`; precompute normalized vectors at fit time (similarity = dot), hoist the query norm, offload the scan above a size threshold.
- **Confidence:** high.

### [IMPORTANT] BM25 failures swallowed at DEBUG and presented as empty/degraded results
- **Where:** `keyword.py:158-163` (catch-all → `logger.debug` → `[]`), cascade at `:210-214`
- **Failure mode:** missing bm25 index, dropped connection, or a query-parser error all return `[]`; the in-memory overlap scorer silently substitutes (if items cached) or memory search reports "no matches" for an infrastructure failure — invisible at normal log levels.
- **Minimal fix:** WARNING with table name (rate-limited); distinguish parse errors (return []) from infrastructure errors (raise/surface).
- **Confidence:** high.

### [IMPORTANT] Sanitizer leaves Tantivy query DSL live: `-` negation and bare AND/OR/NOT pass through
- **Where:** `keyword.py:316-319` (`sanitize_pg_search_query` keeps `-` and alphanumeric tokens); indexes are ParadeDB `USING bm25` whose `@@@` RHS parses as a Tantivy query
- **Failure mode:** stripping other punctuation *creates* clause-leading hyphens — "fix (-v flag)" → "fix -v flag" → documents containing "v" excluded; bare "AND"/"NOT" yields a parser error which the previous finding swallows into `[]`; uppercase operators in natural queries change semantics silently.
- **Minimal fix:** strip leading `-` from tokens and lowercase/escape bare boolean operators, or use parameterized term queries instead of the string DSL.
- **Confidence:** medium-high — confirm against the installed pg_search parser with one integration test.

### [IMPORTANT] Nondeterministic tie ordering and tie truncation in hybrid merge
- **Where:** `unified.py:433` (set iteration), `:443` (sort on score only), `:445` (truncation)
- **Failure mode:** equal combined scores (common via max-normalization pegging) are ordered by set-iteration order — varying across processes due to hash randomization — then `[:top_k]` drops tied items nondeterministically across restarts. Hybrid is the only unstable path (BM25 and the fallback scorer both tiebreak by id).
- **Minimal fix:** `combined.sort(key=lambda x: (-x[1], x[0]))`.
- **Confidence:** high.

### [IMPORTANT] In-memory fallback scorer: ASCII-only tokenizer (zero non-Latin recall), no TF/IDF, and fires on legitimate zero-hit results
- **Where:** `keyword.py:255-256` (`[a-z0-9]+`), `:232-252` (score = matched-term fraction), trigger at `:212-214` (any zero-hit BM25 result, not just errors)
- **Failure mode:** CJK/Cyrillic/accented queries tokenize to nothing or mangled stems where the primary path is Unicode-aware — the safety net silently behaves differently from the path it replaces; no IDF/TF weighting; and it can "find" items the DB query correctly rejected. Note: despite the subsystem's TF-IDF billing in CLAUDE.md, no IDF exists anywhere in this layer — BM25 is delegated to pg_search and this overlap heuristic is the only local scorer.
- **Minimal fix:** Unicode-aware tokenization matching the sanitizer; gate the fallback on backend *error* rather than zero hits.
- **Confidence:** high.

### [IMPORTANT] `_filter_clauses` silently drops unknown filter keys; skills table config defines no filters at all (latent)
- **Where:** `keyword.py:292-306` (unknown `filter_name` → clause skipped, no error/log), `:74-78` (skills `_TableConfig` has no `filters` despite `project_id`/`enabled` columns)
- **Failure mode:** a caller passing an unmapped filter gets unscoped results presented as filtered. Today's callers pass only mapped names, but this is the mechanism that makes the unscoped-keyword Blocker unfixable for skills without config changes.
- **Minimal fix:** raise/warn on unmapped filter names; add `project_id`/`enabled` to the skills config.
- **Confidence:** high.

### [IMPORTANT] (adjacent, confirmed caller) Memory embedding-availability latch never resets — vectors permanently skipped after one startup-time failure
- **Where:** `memory/manager.py:263-274` (and `memory/services/dedup.py:118-126`)
- **Failure mode:** if the first-ever embed attempt fails (daemon starts before LM Studio/Ollama — a mainline local-first sequence), `_embeddings_available` latches False; the only assignment back to True is unreachable. Every memory created for the daemon's remaining lifetime silently has no vector; no backfill job exists. The reviewed module provides the right primitive (`is_embedding_reachable`, 30s TTL, `embeddings.py:800`) that this caller should use.
- **Minimal fix:** replace the one-way latch with a TTL'd recheck.
- **Confidence:** high.

## Findings — Important (ai/)

### [IMPORTANT] Codex adapter ignores turn errors and treats empty turns as success
- **Where:** `text_generation.py:466-488`; `codex_impl/client.py:611-680` (`run_turn` registers no failure event; `turn/completed` status/error payload never inspected)
- **Failure mode:** a turn completing with an error and no message items returns `"".strip()` — success with empty text, terminating the candidate chain exactly as the ACP Blocker, on the path that is the FIRST candidate in every default profile.
- **Minimal fix:** inspect `turn/completed` for error status and raise; treat empty aggregate text as failure.
- **Confidence:** high on behavior.

### [IMPORTANT] Codex text generation has no overall timeout — can hang the caller forever
- **Where:** `text_generation.py:466-488`; `codex_impl/client.py:666-671` (`while not turn_completed.is_set()` polling forever)
- **Failure mode:** a wedged app-server or protocol drift to `turn/failed` blocks whatever awaited `generate_result` indefinitely — Droid has 600s, ACP has `prompt_timeout`, Codex has nothing, and the service layer has no per-candidate timeout.
- **Minimal fix:** wrap in `asyncio.wait_for` mirroring the Droid adapter; ensure `client.stop()` still runs.
- **Confidence:** high that no timeout exists.

### [IMPORTANT] Candidate parsing drift: `rpartition` vs `partition` breaks slash-containing model IDs
- **Where:** `text_generation.py:640-644` (`_parse_candidate` uses `rpartition("/")`) vs `registry.py:405` and `config/feature_base.py:90` (both use `partition`)
- **Failure mode:** `local:lm-studio/qwen/qwen3-coder-30b` (LM Studio model IDs are routinely `publisher/model`) passes validation and registers as provider `local:lm-studio`, model `qwen/qwen3-coder-30b` — but request-time parsing splits on the LAST slash, producing provider `local:lm-studio/qwen` → "No text_generate binding registered" — the candidate can never succeed.
- **Minimal fix:** `partition("/")` in `_parse_candidate` (first slash is canonical everywhere else).
- **Confidence:** high.

### [IMPORTANT] Whisper availability over-reported; audio has no failure fallback across bindings
- **Where:** `registry.py:467-505` (`_whisper_audio_binding` available purely from config flags, never checks `WhisperSTT.is_available`); `audio.py:156-191` (single-shot select, no next-binding attempt on `AudioProviderUnavailableError`)
- **Failure mode:** whisper enabled-but-uninstalled plus a working `openai_compatible_audio` binding → every transcribe fails even though a usable provider exists; `status_snapshot` lies.
- **Minimal fix:** feed real availability into the binding, or iterate available bindings on failure.
- **Confidence:** high.

### [IMPORTANT] Duplicate/reserved audio provider ids crash registry construction; config never validates them
- **Where:** `registry.py:257-264` (raises on duplicate per capability); `config/voice.py:8-39` (no uniqueness/reserved-name validation); startup swallow at `runner_init/services.py:43-49` → `llm_service = None` → every LLM/voice route 500s
- **Failure mode:** one config line disables the entire AI capability surface at runtime with only a startup log line; `_daemon_audio_adapters` (`audio.py:319-322`) meanwhile silently last-wins on the same misconfiguration — inconsistent semantics.
- **Minimal fix:** pydantic validator rejecting duplicate/reserved provider ids at parse time.
- **Confidence:** high.

### [IMPORTANT] Explicit provider+model requests rejected by the feature-candidate allowlist, with a misleading error
- **Where:** `registry.py:184-190` (`supports_model` treats non-empty `models` as hard allowlist; models populated from the union of feature-config candidates, `:397-413`), `:318-322`, `:351-352` (failure reason claims "No binding registered for provider")
- **Failure mode:** `provider="codex", model="gpt-5"` (valid for the CLI) yields zero candidates because the model isn't in any feature config; claude escapes via family-alias normalization, codex/local do not. Surfaces as a 400 on `POST /api/llm/generate` with an actively misleading message.
- **Minimal fix:** distinguish "provider registered but model not matched" in the failure reason; consider advisory (not strict) model matching for CLI-backed adapter styles.
- **Confidence:** high on behavior.

### [IMPORTANT] All-candidates-unavailable becomes RuntimeError → HTTP 500 instead of the designed capability_unavailable 400
- **Where:** `text_generation.py:139-144`, `:159-164` (original error re-raised only when `len(candidates) == 1`); `servers/routes/llm.py:106-114`
- **Failure mode:** profiles always have ≥2 candidates, so per-candidate `CapabilityUnavailableError`s are stringified into a generic RuntimeError → `except Exception` → 500 on the mainline "no CLIs installed" path, breaking the route's structured-400 contract.
- **Minimal fix:** when all candidate errors are `CapabilityUnavailableError`, raise an aggregating `CapabilityUnavailableError`.
- **Confidence:** high.

### [IMPORTANT] Availability frozen at registry build time for long-lived services (TOCTOU)
- **Where:** `registry.py:365`, `:375-391` (`shutil.which` probe at build); held for process lifetime by `llm/service.py:75`, `runner_init/services.py:46`, `runner_lifecycle_subsystems.py:332`; HTTP routes rebuild per request and disagree with daemon-held services
- **Failure mode:** installing a CLI later → unavailable until restart; uninstalling → stale-available binding (text recovers via candidate loop; vision/audio fail the request); status endpoints diverge from actual daemon routing.
- **Minimal fix:** re-resolve `installed()` at selection time for CLI-backed bindings (cheap), or TTL-rebuild like `routes/voice.py`'s `_cached_audio_registry`.
- **Confidence:** high on mechanism.

### [IMPORTANT] Voice route gates registry-available OpenAI-compatible bindings behind whisper's config flags
- **Where:** `servers/routes/voice.py:228-232` (early-returns on `voice.enabled`/`stt_enabled`) vs `registry.py:521-562` (openai_compatible availability depends only on transcription/translation flags)
- **Failure mode:** local whisper disabled + remote-compatible binding configured → capability "available" in `status_snapshot` while every transcribe request is refused before the registry is consulted.
- **Minimal fix:** drop the early returns and let `select()` raise the structured error (already mapped at `:276-278`).
- **Confidence:** medium-high.

## Findings — Nits

### [NIT] code_index polish
- `sync_worker.py:405-407` — `_sync_graph` returns True on both branches; the skipped-reason conditional is dead, and future retryable skip reasons would silently mark synced.
- `trigger.py:57-58` — debounce with no max-wait: sustained <2s edits postpone flush indefinitely (300s backstop only).
- `cleanup.py:55-90` — purge ordering: graph cleared first, vector collection deleted last with project row already gone — Qdrant-down during purge leaks the collection forever.
- `maintenance.py:76` — docstring claims "recover unsynced files"; no recovery exists; `get_unsynced_files`/`get_orphan_files`/`delete_file`/`reset_graph_sync_for_project` are the missing reconciliation layer, present and dead.
- `sync_worker.py:228` / `trigger.py:99` — sync filesystem/PATH probes on the loop, inconsistent with `asyncio.to_thread` used nearby.
- `maintenance.py:109,133-140` — 120s SIGKILL on `gcode index --project` may loop forever on very large cold-start indexes.
- `maintenance.py:176` — `Path(project.root_path)` without `.expanduser()` while siblings expand — a `~`-prefixed root silently yields zero summaries.
- `context.py:52-56` — dead `except GcodeGatewayError` around a constructor that can't raise it; test pins the fiction via monkeypatch.
- `gcode_gateway.py:158-164` — vestigial version gate always true under the 0.9.9 pin; `assert` used for control flow.
- `gcode_gateway.py:316-319` — `FileNotFoundError` wrapped but `PermissionError` leaks as 500 instead of the 503 every other unavailability path produces.
- `gcode_gateway.py:277-302` — `_ensure_version` unsynchronized; first-burst duplicate version probes (benign).
- `context.py:85` vs `:101` — invalidate gating asymmetry: graph clear requires `graph_enabled`, vector delete ignores `embedding_enabled`.
- `context.py:159-172` — `rebuild_graph(limit=...)` silently ignores explicit values while the route still advertises the parameter.

### [NIT] code_index storage/models polish
- `storage.py:29-31` — `db: HubDatabase | HubDatabase` duplicated union member; stale docstring.
- `storage.py:419/:422/:448/:465` — upserts return `len(input)` despite `ON CONFLICT DO NOTHING`; docstrings claim "count inserted".
- `storage.py:409-420` — `upsert_imports` deletes by `file_path` but inserts `imp.source_file`; replace semantics break if they differ.
- `models.py:256` — `ImportRelation.imported_names` silently dropped on persistence.
- `storage.py:313-318` — `get_unsynced_files` docstring claims graph+vector; filters only graph; zero callers — delete.
- `storage.py:684-690` — chunk conflict update omits `language`.
- `models.py:25-29` — `make_external_symbol_id` collides `module=None` with `module=""`.
- `models.py:271-273` — `__post_init__` silently overrides explicit `callee_target_kind`.
- `models.py:41` — stale `kind` comment (live index also emits `property` etc.).

### [NIT] search polish
- `embeddings.py:267-268` — docstring promises concurrent-request dedup the cache doesn't provide (no in-flight coalescing).
- `embeddings.py:378,398,466,572` — search layer lazily imports from `gobby.cli.services` on the event loop (layering inversion + first-call stall).
- `embeddings.py:347-348` — cache hands out shared mutable vector references; future in-place mutation corrupts cached entries.
- `models.py:53-56` vs `:86-101` — `SearchConfig.mode` validated lazily; typo surfaces as mid-operation ValueError instead of config-parse error.
- `embeddings.py:546` — per-call `AsyncOpenAI` client (pool churn); cache eviction only runs inside calls (up to ~200MB resident after a burst).
- `keyword.py:275-279` — `row_value` claims tuple-row support; both branches identical; would raise outside the try.
- `unified.py:83-86,117-119` — vestigial `fts_*` parameters stored, never read; `keyword.py:282-284` placeholder ignores its args; `unified.py:227` corpus duplicated in two caches.
- `keywords.py:66-76` — YAKE n-grams exploded to single words, discarding the bigram signal YAKE was configured (n=2) to find.

### [NIT] ai polish
- `audio.py:366-367` — dead `_extract_text`, no callers.
- `text_generation.py:414-425,466-488,505-515` / `audio.py:206-232` — `max_tokens`, `language`, `prompt`, `model` silently dropped by several adapters; no signal to callers.
- `text_generation.py:270-277` — `request.candidates` silently overrides explicit provider/model; reject the combination or document precedence.
- `registry.py:105-115` — case-sensitive model matching vs mixed-case bundled local defaults (`local/Qwen3-Coder-...`) — near-unmatchable against typically-lowercase LM Studio/Ollama IDs.
- `audio.py:115-117` vs `vision.py:55`/`text_generation.py:114-115` — adapter-key normalization inconsistent; mixed-case custom injections miss silently in vision/text.
- `text_generation.py:54-58` — `TextGenerateJSONAdapter` missing from `__all__` and `ai/__init__.py`.

## Systemic patterns

1. **Silent degradation as the universal failure posture.** Every layer converts failure into success-shaped emptiness: invalidate warns and returns ok; BM25 errors → `[]` at DEBUG; embedding failures → keyword fallback that latches; dim mismatches → 0.0 similarity → "no matches"; ACP/Codex provider errors → successful empty text; summarizer failures → debug log + eternal retry. Individually defensible, collectively the subsystem can be substantially broken for hours with zero operator signal. An "empty output is failure" guard at a handful of seams (candidate loop, invalidate result, BM25 catch) would convert most of these to honest errors.
2. **Add-only projections / no failure bookkeeping.** PG rows (gcode-owned) and Qdrant/FalkorDB projections (daemon-owned) have create/update flows but no delete/rename reconciliation; the helper methods for it all exist as dead code. Background queues (pending-sync, unsummarized) re-select the same failing head rows forever — the schema's attempt-tracking column has a writer that's never called.
3. **Unconditional "mark done by id" after slow async work.** `mark_vectors_synced`, `mark_graph_synced`, `update_symbol_summary` all race a concurrent reindex; one uniform `AND content_hash = %s` guard fixes the class.
4. **Dual-writer architecture without a contract.** The Rust `gcode` binary and the Python storage layer share uuid5 ID derivation, `qualified_name` shape, and summary-invalidation semantics — none documented, none conformance-tested; tests validate the implementation production doesn't run, and the realized consumer-sweep breakage (plans-sync review) came from exactly this gap.
5. **State machines by scattered flags.** `_fitted`/`_fitted_mode`/`_using_fallback`/`_active_backend` mutate in five places with no transition function, producing contradictory observable state; registry availability means different things per binding (config flags vs `shutil.which` vs nothing).
6. **Sync/CPU work on the event loop** persists at specific seams (keyword search DB round-trips, pure-Python cosine scans, codewiki config read, lazy heavy imports) in a subsystem that otherwise offloads correctly.

**Health read:** the async subprocess core (gcode gateway), the embedding provider-policy/cache layers, and the registry's normalization/family-alias logic are genuinely solid and well-tested — but the freshness layer has no deletion story, the search layer's three core promises (search what you fitted, comparable hybrid scores, honest failure reporting) are each broken, and the AI execution adapters turn provider errors into empty successes that halt fallback. Most of the risk concentrates in failure paths that tests never exercise.

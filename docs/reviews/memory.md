# Review: memory

- **Scope:** `src/gobby/memory/` — manager/protocol/backends (`manager.py`, `protocol.py`,
  `context.py`, `identity.py`, `scoring.py`, `backends/`), embeddings/vector store
  (`vectorstore.py`, `services/indexing.py`, `services/maintenance.py`), digest
  (`digest.py`, `title_heuristics.py`), knowledge graph (`falkor_client.py`,
  `services/knowledge_graph/`), recall/search/dedup/dream (`recall.py`, `services/search.py`,
  `services/dedup.py`, `services/crossref.py`, `dream/`), plus the storage seam
  (`storage/memories.py`, migrations 271/272/274/275/276) and the `gobby-memory` MCP tool
  surface (`mcp_proxy/tools/memory.py`). Cross-seam reads into sessions/transcripts,
  llm/, runner_init wiring, and tests.
- **Reviewer:** Claude Fable 5 — 6-agent parallel fan-out, all Blockers synthesizer-verified.
- **Commit / branch:** `0.5.0` @ HEAD `1bed2259f` (working tree clean at review time).
- **Architecture note (corrects a common misconception):** memory embeddings live in
  **Qdrant**, the knowledge graph in **FalkorDB**; Postgres `memories` is the source of
  truth. There is **no pgvector and no `ai_embeddings`/`embeddings` table**; migration 271
  renames *config keys* only (no vector data touched). `memories` has **no `status`
  column** — supersession is create-new + hard-delete-old. Cross-store writes are
  intentionally non-atomic with graceful degradation; the real correctness questions are
  ghost recall (well guarded) and missing-vector backfill (not guarded — see below).
- **Summary:** 7 Blocker · 23 Important · 8 Nit — the destructive paths (delete tool,
  dream consolidation) lack project scoping and approval gates, the embedding lifecycle
  silently and permanently disables itself on one blip with no backfill, background dedup
  never actually dedupes, and the hybrid-search RRF fusion it carefully computes is
  discarded at the final sort. Cypher injection is well controlled and ghost recall is
  prevented; the failures are in lifecycle, scoping, and ranking.

## Findings

### [BLOCKER] Embedding-availability latch never recovers — one transient failure silently disables all future embeddings
- **Where:** `memory/manager.py:263` (`if self._embeddings_available is False: return` — verified, short-circuits before the embed) vs `:267` (`self._embeddings_available = True`, the only reset, reached only *after* a successful embed at `:266`); the same latch in `services/dedup.py:89`.
- **Failure mode:** The first embed exception sets the flag `False` (`:269-274`, logging "suppressing further warnings **until provider recovers**"). From then on every `_embed_and_upsert` returns at `:263` before the embed call, so `:267` is unreachable — the flag can never return to `True` for the daemon's life. Every new/updated memory is persisted with no vector, silently unrecallable via the semantic path (keyword masks it). Only a restart + manual `reindex_embeddings` repairs it; `reconcile_stores` doesn't detect it (next finding). The "until provider recovers" comment is false. Found by two reviewers.
- **Minimal fix:** Don't permanently latch — always attempt the embed and only rate-limit the warning (the vector-store path already uses `log_rate_limited_warning`), or add a time-based reset.
- **Confidence:** high (verified).

### [BLOCKER] Project-scoped `reindex_embeddings` deletes all project vectors before re-embedding — partial/total recall gap on any embedding failure
- **Where:** `services/indexing.py:268` (`delete(filters={"project_id"})`) → `:273` (embed loop) → `:277/:282` (upsert); contrast the global path `_run_global_embedding_reindex` → `vectorstore.py:668` `rebuild()` which upserts-first then deletes stale (its docstring notes "retrying rebuild is safe").
- **Failure mode:** The project path wipes the project's vectors first, then re-embeds memory-by-memory. Any embed failure mid-loop (rate-limit exhaustion, provider down, dimension mismatch) is caught and returns `{"success": False}` — but the vectors are already gone and only the pre-failure subset rewritten. Semantic recall for the project silently returns incomplete/empty results until a future successful reindex. Asymmetric with the global path, which never has a gap.
- **Minimal fix:** Mirror the global ordering: embed into a staging set, upsert (overwrites in place by memory id), then delete only stale ids. Never delete before embeddings are in hand.
- **Confidence:** high.

### [BLOCKER] Background dedup self-matches the just-created memory — cross-memory dedup never runs
- **Where:** `manager.py:376-396` (`create_memory` upserts the vector, *then* fires `_fire_background_dedup`) → `services/dedup.py:92-174` (`process` embeds the same content and `vector_store.search(limit=5)` with no self-exclusion).
- **Failure mode:** The new memory's own freshly-upserted vector is the top hit at score ≈1.0 (`> NEAR_EXACT_THRESHOLD = 0.95`), so `process` hits the "near-exact → NOOP" branch and returns before examining any real duplicate. The "similar → update if richer" path is dead code on the only path that invokes dedup. All dedup tests mock `search` to return a *different* id, so the self-match is untested. Duplicates accumulate unbounded.
- **Minimal fix:** Pass the new `memory_id` into `process` and skip it (`if id == new_id: continue`), run dedup before the upsert, or filter `score >= 0.999` self-identity; add a self-match test.
- **Confidence:** high (flow traced end-to-end).

### [BLOCKER] Dream consolidation auto-applies destructive merge/delete/rewrite from LLM output with no approval gate
- **Where:** `dream/service.py:23` (`DreamRunOptions.dry_run = False` — verified default), `:110-176` (`execute_run` → `apply_dream_plan(dry_run=options.dry_run)`); cron `dream/cron.py:52-82` → `run_memory_dream(...)` (default `dry_run=False`); `dream/apply.py:_merge:247-302` (overwrites the keeper's content with LLM `action.content` and hard-deletes the others), `_delete:185-216`.
- **Failure mode:** The nightly cron applies LLM-proposed merge/delete/refresh/supersede directly. `validate_dream_plan` catches id-hallucination but a hallucinated *canonical content* for a merge of two real candidates passes if the LLM self-reports high confidence — the survivor is rewritten with fabricated text and the originals destroyed, unattended. LLM self-reported confidence is treated as a safety gate, which it is not.
- **Minimal fix:** Default cron to `dry_run=True` (plan-only) or gate mutating actions behind explicit operator approval; require a config opt-in for unattended mutation.
- **Confidence:** high (verified dry_run default + cron wiring).

### [BLOCKER] Dream merge/delete cascade-deletes cross-refs that revert cannot restore — irrecoverable loss
- **Where:** `dream/apply.py:185-302` (`_merge`/`_delete` call `manager.delete_memory`) → `memory_crossrefs` FK `ON DELETE CASCADE` (schema:729-735); revert `dream/apply.py:67-159` → `dream/storage.py:288-299` (`restore_memory_row` re-inserts only `_MEMORY_COLUMNS`).
- **Failure mode:** Deleting a merged-away duplicate cascade-deletes all its crossref rows and (via `delete_memory`) its FalkorDB node/edges. The dream snapshot captures only the deleted memory's `memories` columns — not crossrefs or graph edges. Revert restores the row but the relationship graph is gone permanently; the keeper also never inherits the merged memories' crossrefs, so even a successful merge silently drops their relationship graph. The "safety net" revert gives a false sense that mutations are reversible.
- **Minimal fix:** Migrate the duplicate's crossrefs to the keeper before deletion; snapshot crossrefs (and graph edges) and restore them on revert; route revert through `manager` so vectors/graph are reconciled.
- **Confidence:** high.

### [BLOCKER] `search_knowledge_graph` MCP tool leaks entities across all projects
- **Where:** `mcp_proxy/tools/memory.py:554-557` (`search_knowledge_graph(query, limit)` — verified: no `project_id`, no `get_current_project_id()`, unlike every sibling memory tool) → `services/knowledge_graph/service.py:543` → `reader.py:340-350` (substring fallback `MATCH (n:_Entity) WHERE toLower(n.name) CONTAINS ... RETURN ... LIMIT` with no project filter on the shared `gobby_kg` graph).
- **Failure mode:** The vector pre-path post-filters to global entities only, so any non-global hit falls through to the unscoped substring query — returning entity keys/names/properties from every project. A session in project A reads project B's graph. The graph is otherwise the one place isolation is carefully enforced (vector search, memory-link, related-entity, code-linking all scope by project).
- **Minimal fix:** Thread `project_id=get_current_project_id()` through `service.search_graph`/`reader.search_graph`; add `WHERE n.project_id = $project_id OR ($project_id IS NULL AND n.project_id IS NULL)`; scope the inner vector search.
- **Confidence:** high (verified).

### [BLOCKER] MCP `delete_memory` is not project-scoped — cross-project deletion via predictable IDs
- **Where:** `mcp_proxy/tools/memory.py:356` (`async def delete_memory(memory_id: str)` — verified: no project_id) → `manager.py:490` → `storage/memories.py:341-347` (`DELETE FROM memories WHERE id = %s`, unconditional). Memory IDs are `uuid5(MEMORY_UUID_NAMESPACE, normalized_content)` (`memories.py:155`) — derivable from content.
- **Failure mode:** An agent in project A computes the ID of a known memory string and deletes project B's memory; the FK cascade also drops its crossrefs and (via manager) its Qdrant vector + graph node. `search_memories` correctly scopes to the current project; only the destructive path lacks scoping.
- **Minimal fix:** Pass `get_current_project_id()` from the tool; enforce `(project_id = %s OR project_id IS NULL)` in the storage delete, returning False (not raising) when out of scope.
- **Confidence:** high (verified).

### [IMPORTANT] `reconcile_stores` only detects orphans, never missing embeddings — the embedding-latch and revert gaps are invisible to it
- **Where:** `services/indexing.py:195-249` — computes `orphaned = qdrant_ids - storage_ids` (deletes vectors whose memory is gone) but never `storage_ids - qdrant_ids` (memories with no vector). `dream/apply.py:357-362` calls it after revert, which restored PG rows with no vectors.
- **Failure mode:** A memory exists and is correct in PG but has no Qdrant vector (latch failure, restored-via-revert, or any transient create-time embed failure) — invisible to semantic search indefinitely, and the safety-net tool can't detect it. Keyword fallback masks it.
- **Minimal fix:** Add a `missing` pass that re-embeds `storage_ids - qdrant_ids`; have revert re-embed restored rows.

### [IMPORTANT] Dream revert restores DB rows but not embeddings or graph nodes — reverted memories become search-invisible
- **Where:** `dream/apply.py:67-159` → `dream/storage.py:288-302` (`restore_memory_row`/`delete_memory_row` touch only the `memories` table) vs `manager.delete_memory:490-507` (removes from Qdrant + FalkorDB).
- **Failure mode:** Revert re-inserts the SQL row but never re-embeds/re-adds the graph node → the restored memory has no embedding and is dropped from any min_score-filtered recall; reverting a `supersede`-created memory leaves a dangling vector.
- **Minimal fix:** Route revert through `manager` (re-embed on restore; delete from vector/graph on row delete).

### [IMPORTANT] Global dream runs span all projects; exact-duplicate merges collapse memories across project boundaries
- **Where:** `dream/candidates.py:27-120` (`_in_scope` returns True for every memory when `project_id is None`; cron passes `runner.project_id`, which is None for a non-project daemon); `dream/duplicates.py:10-32` (groups by exact normalized content across the full candidate set); `dream/planner.py:130-148` (`confidence:1.0` merge); `_merge` keeps `sorted(refs)[0]` (smallest UUID, not canonical/earliest).
- **Failure mode:** Two memories with identical content but different `project_id` are auto-merged: the keeper is an arbitrary-project smallest-UUID row, the other project's copy hard-deleted. Cross-project bleed + deletion at confidence 1.0.
- **Minimal fix:** Scope duplicate grouping and merges by `project_id`; never merge across projects; pick a deterministic meaningful keeper.

### [IMPORTANT] Dream run has no per-run lock — manual trigger overlaps the cron run and double-mutates
- **Where:** `dream/service.py:110-176` (`execute_run` takes no mutex; the cron's advance-next_run_at only prevents the *same job* re-dispatching, not an independent manual/MCP/HTTP invocation).
- **Failure mode:** A manual dream run during the nightly cron applies a second set of merges/deletes against memories the first is mutating, with independent snapshot ledgers — non-deterministic outcomes and revert confusion.
- **Minimal fix:** Single global advisory lock / running sentinel checked in `execute_run`.

### [IMPORTANT] Hybrid RRF fusion is computed then discarded — final ranking is dominated by raw semantic similarity; graph/keyword-only hits are always truncated
- **Where:** `services/search.py:441-454` — sort key is `(similarity is not None, similarity, ranking_score)`, so every memory *with* a cosine sorts strictly above every memory *without* one; `ranking_score` (the RRF score, built in `_search_with_graph:227-247`/`_search_qdrant_keyword:316-340`) is only a tiebreaker. Non-semantic hits have `similarity is None` (`:407-415`) and are dropped by `scored[:limit]` whenever there are ≥limit semantic results.
- **Failure mode:** Pure-keyword and pure-graph matches (exactly what vector search misses) can never surface when the semantic pool is full; a memory strong across all three signals but with slightly lower cosine ranks below a semantic-only memory. The carefully-built three-way fusion doesn't drive the order. (Semantic+decay dominance is partly intended per a test, but total graph/keyword starvation reads unintended.) The one test meant to pin fusion ordering (`test_parallel_search_with_rrf_merge`) asserts only membership, not order.
- **Minimal fix:** Sort by RRF `ranking_score` as primary when `rrf_applied`, or impute a comparable score for non-semantic hits so they interleave; fix the test to assert order.

### [IMPORTANT] `min_score` filter does not apply to graph/keyword-only results
- **Where:** `services/search.py:405-411` — `if effective_min_score > 0 and similarity is not None and similarity < effective_min_score: continue`; `similarity` is None for non-semantic hits, so the floor is bypassed.
- **Failure mode:** A caller's `min_score` relevance floor (e.g. `recall.py:139-150`) silently doesn't filter weak keyword/graph hits, letting low-relevance results into recall/injection.
- **Minimal fix:** Assign non-semantic hits a comparable score and apply the floor, or document min_score as semantic-only with a separate threshold.

### [IMPORTANT] Keyword recall drops global (project_id IS NULL) memories when scoped; the vector filter does too
- **Where:** `search/keyword.py:299-305` (`alias.project_id = %s` strict equality) and `vectorstore.py:323-362` (Qdrant `MatchValue` exact match) vs every PG read path which uses `(project_id = %s OR project_id IS NULL)`.
- **Failure mode:** With a project scope, global memories are excluded from both the keyword branch and the vector branch (they only resurface if Qdrant happens to return them, never via keyword), so a scoped recall silently misses globals — worst in keyword-only mode (which the embedding latch forces). Contract drift between PG (`OR IS NULL`) and the search backends.
- **Minimal fix:** Make both filters project-scope-aware (`= X OR IS NULL` / a `should`-OR with an `IsNull` condition).

### [IMPORTANT] Cross-reference creation has no project filter — cross-project edges written into `memory_crossrefs`
- **Where:** `services/crossref.py:60-90` (`create` runs `vector_store.search(limit=max_links+1)` with no `filters`); `get_related` only avoids leaking them because it re-fetches each target scoped — bypassable if called with `project_id=None`.
- **Failure mode:** Creating a memory in project A writes crossref rows to similar memories in projects B/C; those foreign hits also consume `max_links` slots, starving legitimate same-project links.
- **Minimal fix:** Pass `filters={"project_id": memory.project_id}` (with global handling) into the create search.

### [IMPORTANT] `create_memory` global dedup can return a memory belonging to a different project
- **Where:** `manager.py:347-364` → `storage/memories.py:261-306` (`content_exists`/`get_memory_by_content` ignore `project_id` — documented as intentional global dedup).
- **Failure mode:** A project-B `create_memory(content="X")` where "X" exists in project A returns A's record (different `project_id`); B never gets its own copy and the returned memory's `project_id` ≠ requested. A contract surprise crossing the project boundary.
- **Minimal fix:** Document the global-dedup contract at the surface, or scope dedup to `(project_id = X OR project_id IS NULL)` if isolation is intended.

### [IMPORTANT] Dimension mismatch at startup only logs an error and continues serving against the wrong-dimension collection
- **Where:** `vectorstore.py:166-185` (`_initialize_locked` else-branch logs `error` and returns normally on dim mismatch) vs `:523/:565` which self-heal with `recreate_on_mismatch`.
- **Failure mode:** The daemon comes up "healthy" while every write/query for that collection raises at runtime until an operator runs `gobby memory rebuild`; the error is emitted once at init and easy to miss. (The embed-layer `expected_dim` fail-fast prevents silent garbage-similarity, so this is health/contract drift, not a recall-corruption Blocker.)
- **Minimal fix:** Auto-recreate on init mismatch (consistent with the other paths) or mark the store unavailable; surface in `gobby status`.

### [IMPORTANT] Global rebuild deletes the old collection before any new vector lands on a dimension change
- **Where:** `vectorstore.py:523-563` (`_prepare_collection_for_rebuild` drops+recreates on a dim change) then `:668-724` (`rebuild` embeds+upserts) — a mid-run embed failure leaves an empty/partial collection.
- **Failure mode:** Dimension-migration is exactly when a rebuild runs and exactly when a new-model embed call is most likely to hit auth/availability errors — wiping the entire store.
- **Minimal fix:** Build under a temp collection, populate fully, then atomically swap.

### [IMPORTANT] `MAX_REINDEX_LIMIT` truncation + delete-first drops vectors for memories beyond 100k
- **Where:** `services/indexing.py:264` (`list_memories(limit=100_000)`) + `:268` (delete-all-project). A project over 100k memories has all vectors deleted but only the first 100k re-embedded, with no warning when the cap is hit.
- **Minimal fix:** Page through all memories (the crossref path already does via `fetch_all_project_memories`); warn when count == cap.

### [IMPORTANT] `update_memory` reports success while leaving a stale vector when re-embedding silently fails
- **Where:** `manager.py:update_memory` → `_embed_and_upsert:254-285` (catches all embedding exceptions and returns; latches `_embeddings_available=False`). The DB row updates; the vector keeps the old content's embedding.
- **Failure mode:** Semantic search matches the stale embedding — text changed, vector didn't. No dirty-flag/backfill; maintenance has no stale-embedding detector.
- **Minimal fix:** Surface the failure so `update_memory` can mark a `needs_reindex` flag for a maintenance sweep.

### [IMPORTANT] Empty/whitespace entity name from the LLM raises past `add_to_graph`'s typed contract → infinite reprocessing
- **Where:** `services/knowledge_graph/service.py:145` (`_normalize_entities` not wrapped) → `normalization.py:31-38` → `identity.py:12-19` (`normalize_entity_name` *raises* on empty, so the dead guard `if not normalized_name` never fires); trigger `extraction.py:131-135` only checks `"entity" in e`.
- **Failure mode:** A whitespace-only entity name raises out of `add_to_graph` (bypassing its `KnowledgeGraphResult` contract) → the caller logs and does NOT mark `graph_processed=TRUE` → `get_pending_graph_memories` re-fetches it every lifecycle loop forever (repeated LLM spend, head-of-queue starvation).
- **Minimal fix:** Skip empty names in `normalize_entities` (try/except or pre-check); reject empty `e["entity"]` in extraction.

### [IMPORTANT] No retry cap: `DETERMINISTIC_FAILURE` graph memories are reprocessed forever
- **Where:** `sessions/lifecycle.py:280-289` + `manager.py:806-812`; `storage/memories.py:375-403` (`graph_processed` is a boolean with no attempt tracking; `get_pending_graph_memories` orders oldest-first, limit 20).
- **Failure mode:** A permanently-failing memory (malformed content, repeatable LLM error, the empty-name crash) burns LLM calls indefinitely and starves the head of the pending queue, blocking newer memories.
- **Minimal fix:** Add a `graph_attempts` counter with a terminal failed state after N deterministic failures.

### [IMPORTANT] FalkorDB `vector_search` global over-fetch + Python post-filter can return zero results for small projects under skew
- **Where:** `falkor_client.py:672-712` — the vector index isn't project-partitioned; fetches `min(limit*5, 200)` neighbors globally, then post-filters by project in Python.
- **Failure mode:** In a shared graph where one project dominates, the 200-candidate window can be fully consumed by another project's neighbors → a legitimate same-project match never returned. Silent recall incompleteness worsening with graph size.
- **Minimal fix:** Push the project filter into Cypher (post-`YIELD WHERE node.project_id`), or maintain per-project indexes.

### [IMPORTANT] KG orphan cleanup deletes code-linked entities; relationship-type normalization mismatch in supersede selection
- **Where:** `knowledge_graph/maintenance.py:99-136` (orphan predicate `NOT (e)-[:MENTIONED_IN]->(:Memory)` — `DETACH DELETE`s an entity whose only memories were deleted even if it has `RELATES_TO_CODE` edges, losing code↔memory links); `extraction.py:206-233` vs `writer.py:85-96` (`fetch_existing_relations` returns normalized stored types while new relations use raw un-normalized strings → the LLM dedup compares mismatched forms → fails to delete or deletes the wrong superseded edge).
- **Minimal fix:** Spare entities with `RELATES_TO_CODE`; normalize new relation types before the supersede comparison.

### [IMPORTANT] Sync DB I/O on the event loop in async memory methods
- **Where:** `manager.py` `delete_memory`/`update_memory`/`_enqueue_for_graph` (sync storage calls inside async signatures, while the `a*` siblings correctly route through `run_db`); `services/search.py:159,168,569-598` (sync `list_memories` + sync `update_access_stats` per returned memory on every recall); plus the digest path (`digest.py:671,712,609` — `session_manager.get`/`persist_digest_state`/`update_title` un-offloaded while file reads are correctly `to_thread`-ed).
- **Failure mode:** Every recall blocks the loop once per result writing access stats; delete/update/digest-persist block on Postgres round-trips — defeating the daemon's DB executor.
- **Minimal fix:** Route through `run_db`/`to_thread` like the `a*` variants; make `update_access_stats` async.

### [IMPORTANT] Digest turn-counter / pair-index divergence corrupts long sessions
- **Where:** `digest.py:411-413` (`digested_count = next_turn_number - 1` — a *turn-label* count used as a *pair-list index*), `:706-708` (N pairs collapse into one `### Turn N` entry → counter advances by 1, re-reading N-1 pairs next time → duplicate facts), `:680-681` (hard-coded 50-pair window: past 50 turns the slice empties and the digest degrades to "last pair only"), `:707` (LLM `turn_markdown` embedded verbatim can emit `### Turn 87` and poison `_get_next_turn_number`).
- **Failure mode:** Silent digest corruption/duplication on busy or long sessions; a single echoed heading desyncs the counter for the rest of the session.
- **Minimal fix:** Persist an explicit `last_digested_pair_index` (advance by `len(undigested_pairs)`) instead of inferring from headers; parse turn numbers from a non-forgeable sentinel; expose `num_pairs` on `DigestConfig`.

### [IMPORTANT] Unknown/empty `session.source` silently dispatches digest/title to the Claude parser
- **Where:** `title_heuristics.py:217` (`get_parser(source or "")`) and `digest.py:680` → `get_parser` (`transcripts/__init__.py:54` defaults unknown sources to `ClaudeTranscriptParser`).
- **Failure mode:** A null/unknown `session.source` parses a Gemini/Codex/Qwen transcript with the Claude parser → garbled/empty digest input and titles, silently.
- **Minimal fix:** Make `get_parser` raise/warn on unknown sources; guard the digest path when source is falsy.

### [IMPORTANT] `sessions.delete()` can hit an FK RESTRICT from `memories.source_session_id`
- **Where:** schema `postgres_baseline_schema.sql:679` (`source_session_id REFERENCES sessions(id)` with no `ON DELETE` → RESTRICT) vs `storage/sessions/_crud.py:410-417` (bare `DELETE FROM sessions` with no reference guard, unlike the ghost-session pruner which does guard it).
- **Failure mode:** Deleting a session that has any agent-created memory raises an FK violation and rolls back.
- **Minimal fix:** `ON DELETE SET NULL` on `source_session_id` (provenance is optional), or null it inside `delete()`'s transaction.

## Nit-sweep resolution (#16700)

The original nit-level observations were rechecked against the final review-fix code.
No stale or duplicate nit remains actionable in this document:

- **Vector count diagnostics:** fixed by #18126. An unavailable or uninitialized Qdrant
  client now raises the vector-store domain error and `get_stats` reports `vector_count=-1`;
  only an initialized empty collection reports `0`.
- **Cross-reference cleanup helpers:** retained intentionally. `delete_crossrefs` is used when
  content revisions rebuild secondary indices, and `delete_project_crossrefs` is used by the
  project cross-reference rebuild. They are not redundant with hard-delete FK cascades.
- **Dedup result dead state:** the unused `DedupResult.deleted` field and stale return-contract
  text were removed by #16700.
- **MCP scope note:** removed as a duplicate of the destructive-delete and graph-search
  project-scope findings above; those fixes are tracked by their focused review leaves.
- **Session-proximity dedup:** already fixed. `MemoryCrudMixin.create_memory` compares full
  normalized content within the same project/session window rather than a 100-character prefix.
- **FalkorDB close and neighbor scope:** cleared as non-bugs. `FalkorClient` has one owning
  `_db` connection and its focused close test verifies that owner is closed; project-qualified
  entity keys prevent cross-project edges, so neighbor expansion through a scoped center cannot
  cross projects.
- **Relationship deletion selection:** fixed by #18129. Only exact canonical existing triples
  returned by the LLM can reach the writer; malformed, paraphrased, hallucinated, and duplicate
  selections are ignored or deduplicated with diagnostics.
- **Migration 271 conflict behavior:** obsolete after the 0.5.0 migration flatten. Replayable
  migrations now begin at 306 and the baseline is authoritative.
- **Digest title recovery:** fixed by #18127. Recovery persists the attempted digest hash,
  skips unchanged digests, permits a changed digest to retry, and bounds the title prompt to the
  newest 12,000 digest characters.

## Systemic patterns

1. **Cross-store writes are best-effort and silently swallow failures, with no missing-direction repair.** PG commits first, then Qdrant/FalkorDB mutate outside any shared transaction with `try/except…log`. Ghost recall (vector exists, PG row gone) is well guarded by PG re-hydration in `_build_results`. The inverse — PG rows without vectors (latch failure, revert, transient create failure) — has no automatic backfill; `reconcile_stores` is delete-only. The system silently leans on a scheduled `reindex_embeddings`.
2. **Project scoping is a per-call convention, not a structural invariant.** Reads scope correctly; the vector filter drops the `IS NULL` arm, crossref-create and KG-substring-search and the delete/dream paths drop scoping entirely. Several cross-project leaks/deletions all stem from this. An "every memory/graph query carries a project filter" invariant test would catch the class.
3. **DB-vs-index divergence on mutation/revert.** Three backends kept in sync only through `manager`; any path that bypasses `manager` for restore/delete (dream `storage.py` raw SQL) leaves vectors/graph/crossrefs inconsistent. Centralize all mutation through `manager`.
4. **LLM self-reported confidence treated as a safety gate** in dream apply and recall — not a real safeguard for autonomous destructive operations.
5. **Turn label vs pair index conflation** in digest — three failure modes (multi-pair collapse, 50-pair window, content injection) all from inferring a consumed-pair count from a human-readable label.
6. **Tests assert membership, not order or identity** — ranking/fusion tests check set-containment; dedup tests never simulate the self-match; the load-bearing guarantees are effectively untested.

## Verified non-bugs (cleared — don't re-chase)

- **Cypher injection is well controlled** — every dynamic fragment (labels, relationship types, property/index names) flows through `_validate_cypher_identifier`/`_normalize_relationship_type`; all values use `$param` binding. No string-interpolated LLM/user value reaches Cypher.
- **Entity dedup is case/whitespace/unicode-safe** — `entity_key` → `normalize_entity_name` applies NFKC + casefold + whitespace-collapse; length-prefixed components prevent delimiter collisions.
- **No ghost recall** — `_build_results` re-fetches every candidate id from PG with project scope and drops misses, so a Qdrant point whose PG row was deleted can never surface.
- **Recall fail-open across backends** — `_search_with_graph`/`_search_qdrant_keyword` use `gather(return_exceptions=True)`, catch each backend, re-raise CancelledError, RRF-merge survivors; a down vector store falls back to keyword+graph (not empty).
- **`memory_crossrefs` FK is `ON DELETE CASCADE`** — deleting a memory removes both edge directions in-DB atomically.
- **Embedding dimension validation is wired and fail-fast** (`_validate_embeddings_dim`, `expected_dim` threaded from config); cosine metric consistent across create/rebuild; query vs document nomic-prefixing differentiated correctly — no garbage-similarity from dimension/metric mismatch on the hot path.
- **Batch embedding partial failure raises** (`_validate_embedding_response` enforces count + non-empty); rate-limit/timeout handled with backoff+jitter and `client.close()` in `finally`.
- **Dream `validate_dream_plan` demotes hallucinated/unknown ids to `review`** (no mutation); supersede/delete take before-snapshots and roll back the SQL rows they touched on mid-action failure; revert is idempotent.
- **`temporal_decay` math is correct** (half-life, clamps negative age, disabled returns 1.0); RRF primitive `1/(k+rank+1)` with `reverse=True` is correct (the bug is the *final* sort discarding it).
- **Migrations 271/272 are config-keys-only and idempotent**; the dream constraint enum (274/276) matches every action string `apply.py` emits.
- **`%s` placeholders are correct** per repo convention; FalkorDB `$param` Cypher binding is also correct (not the stale SQL `$N` note).

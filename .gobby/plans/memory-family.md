Plan artifact: `.gobby/plans/memory-family.md`

# Gobby 1.0 Stage 2: native Memory and search family

**Plan ID:** memory-family

## Overview
`kind: framing`

Port the Memory and search family (epic #21563, planning task #25254) from
Python ownership to native Rust services. The bounded inventory is 80 proxied
ledger rows. Seventy-four live in
`docs/contracts/parity-ledger-status.d/memory-skills.json` and cite #21563:
23 `gobby memory` CLI rows, 25 HTTP rows (22 under `/api/memories` and three
under `/memory/dream`) and 26 `gobby-memory` MCP tools. Six live in
`docs/contracts/parity-ledger-status.d/remainder.json` and cite #21563: the
`gobby-review-learning` MCP tools `list_check_keys`, `recall_review_context`,
`recall_review_lessons_by_class`, `recall_review_lessons_for_files`,
`record_review_lesson` and `retire_review_lesson`. The plan also owns the
memories collection rebuild seam #25070, which the AI plan's switch build
(#25116) consumes. The rendered `docs/contracts/parity-ledger.md` is
generated. This plan does not expand work or authorize a restart, cutover or
push.

The work is necessary: all 80 rows are proxied and gdaemon has no memory
handler. gcore already holds the client halves the family needs:
`crates/gcore/src/qdrant.rs`, `crates/gcore/src/falkor.rs::GraphClient`,
`crates/gcore/src/search.rs::rrf_merge` and `::sanitize_pg_search_query`,
`crates/gcore/src/ai/embeddings.rs::embed_batch`,
`crates/gcore/src/graph_analytics.rs` and the config resolvers in
`crates/gcore/src/config/resolve.rs`. Restraint stops at one library crate,
`crates/gmemory` (package `gobby-memory`, lib `gobby_memory`), statically
linked into gdaemon (ROADMAP decision 16); one `RouteFamily` named `memory`
claiming `/api/memories` and `/memory/dream`; two `ToolFamily` descriptors,
`gobby-memory` and `gobby-review-learning`; one native `gdaemon memory`
command tree that the public `gobby memory` entry forwards to; and one shared
gcore module for the embedding serving lease and projection-change ledger
(Decision 4). No new process, executable or database table is added. One new
external library is flagged for Josh (Decision 2).

**Boundaries.** The rules and workflows family owns recall injection: the rule
templates under `src/gobby/install/shared/workflows/rules/memory-lifecycle/`
and `.../review-learning/` call `surface_memories` and the review-learning
recall tools, and `src/gobby/workflows/engine/delivery_formatting.py`,
`mcp_injections.py` and `injection_tracking.py` format and track delivery.
This family owns the tools those rules call, not the delivery. The AI family
(#21587, `.gobby/plans/gdaemon-ai-providers-family.md`) owns `/api/embeddings`,
`/api/llm`, the embedding switch and the `gobby embeddings` CLI; this plan
consumes its text-generation service (#25097). The Qdrant CLI and
`gobby-plans:checkpoint_plan_review_lesson_mint` stay with #21568. #22606
(memory community detection) is blocked by this epic but sits under deferred
#24819 and is out of scope. The projects family owns the cross-runtime project
write fence (#25059); every native writer here uses it.

**Non-goals: background graph queue and dream-hidden purge.**
`src/gobby/sessions/lifecycle.py::SessionLifecycleManager._kg_queue_loop` and
`::_purge_dream_hidden_memories` stay in Python with the sessions family that
schedules them. No ledger row serves them. Native writes keep producing
`graph_status = 'pending'` rows that loop consumes, and 2.4 and 4.1 expose the
per-memory extraction and purge functions a future native loop calls. The
nightly dream schedule is bound by the cron plan's #25024, which binds the
service 4.3 names.

**Cross-plan contention.** `crates/gdaemon/src/main.rs::Command` and `::main`
are also targeted by `.gobby/plans/gdaemon-skills-family.md` 5.1 and
`.gobby/plans/gdaemon-ai-providers-family.md` 7.5; 7.1 here edits them. The
landings serialize and the later one rebases. `crates/gcore/src/qdrant.rs`
gains two read functions in 1.3 and 2.3; the AI plan's #25112 adds alias
operations to the same module, and the later landing rebases.
`crates/gai/src/lib.rs` gains one re-export in 2.4 after #25097 lands
(Decision 6).

## Decision Record
`kind: framing`

1. **Assignment.** The Orchestrator assigned planning task #25254 under epic
   #21563. Plan Writer gobby#18291 and Plan Adversary gobby#18294 work to
   consensus; Triage gobby#17480 is the coordination route, and the
   Orchestrator relaunches the Plan Enhancer once this draft is committed.
   The 80-row inventory plus seam #25070 is the confirmed scope. Requirements
   from Triage: macOS and Linux only, no latency gates, and a "Debt not to
   port" section.
2. **Entity reclustering keeps HDBSCAN (Orchestrator MEM-D1, option A;
   flagged for Josh).** The live configuration sets
   `memory.cluster_recall_expansion` to true (the default is false), so
   `src/gobby/memory/services/knowledge_graph/reader.py` expands recall
   through the `_Entity.cluster_id` labels that
   `recluster_knowledge_graph_entities` writes with scikit-learn's HDBSCAN.
   The lockfile holds no HDBSCAN, linfa or ndarray crate. Chosen: keep the
   tool and the expansion, and add the pure-Rust `hdbscan` crate from
   crates.io, pinned to exactly `0.12.0` by 2.5, as the AI plan's Decision 9
   authorized `multer`. It is dual-licensed MIT OR Apache-2.0, needs no
   native library on macOS or Linux, and brings one new transitive crate,
   `kdtree` 0.7.0 (MIT OR Apache-2.0); its `num-traits` and `thiserror` 1.x
   dependencies are already locked and its optional `rayon` feature stays
   off. It matches scikit-learn's settings for this call: euclidean distance
   by default, excess-of-mass stability selection (its only selection mode),
   `min_samples` defaulting to the minimum cluster size, and a core distance
   that counts the point itself. `petal-clustering` 0.14.0 was rejected:
   it declares Rust 1.90, above the workspace's 1.88, and pulls in
   `ndarray`. The silhouette score is computed by hand. Labels keep HDBSCAN semantics: noise
   is -1 and clusters are numbered from 0. The new dependency goes to Josh
   with the stamped plan. If Josh rejects it, the Orchestrator reopens 2.5
   before approval for the recorded fallback: gcore's Leiden
   (`crates/gcore/src/graph_analytics.rs`) over a k-nearest-neighbour graph of
   entity embeddings, with no noise class, under an exact waiver.
3. **Memory's session-variable writes (Orchestrator MEM-D2, option A).**
   `get_memory` appends to `accessed_memory_ids` (bounded at 1000, each
   record carrying the claimed task id), and `review_task_memories` writes
   `_memory_task_review_records` (bounded at 50) and
   `_memory_review_stop_delivered`. No native `session_variables` writer
   exists; `.gobby/plans/rust-rule-evaluation.md` D1.2 assigns the general
   native commit to sessions #21562, which is unplanned. Leaf 6.1 owns a
   narrow native writer for exactly those three keys. It is key-scoped, runs
   under `SessionVariableMutation`'s explicit advisory key from #25262 (the
   Orchestrator confirmed the edge), and matches
   `src/gobby/workflows/state_manager.py::SessionVariableManager.upsert_bounded_list_variable`.
   The sessions family absorbs it when that family lands; its plan records
   this seam.
4. **Native embedding serving lease (Orchestrator MEM-D3, option A; amends
   the approved AI plan, flagged for Josh).** AI 7.4 states that the native
   daemon holds no serving lease and writes no collection. That stops being
   true once gmemory writes memory vectors: GC's `can_collect` honors only
   live acknowledgements. `embedding_generation_acks` is keyed by
   `daemon_instance_id`, and Python uses `UUID(runner.machine_id)`
   (`src/gobby/runner_init/services.py`), so a native writer on the same
   machine would collide with Python's row. Leaf 2.1 adds one shared gcore
   module, `crates/gcore/src/embedding_generation.rs`. It prepares,
   acknowledges, renews and fences a serving lease, asserts serving before
   every native vector write, appends projection changes and reads managed
   projection targets. Its managed lifecycle ports Python's renewal,
   transient backoff, deadline fencing, reacquisition, projection repair and
   rebuild-on-mismatch, so a lease that loses serving recovers instead of
   staying fenced. gdaemon holds one lease with instance id
   `UUIDv5(machine_id, "gdaemon")`, distinct from Python's and stable across
   restarts. #25071's native tool-embeddings writer reuses the module. The
   Orchestrator records AI 7.4's premise as amended.
5. **Placement.** The family composes only where the hub pool exists, as
   `crates/gdaemon/src/serve.rs::run` composes `gobby_runtime::RuntimeFamily`
   inside `if let Some(pool) = &pool`: hub and standalone. A node composes
   none, and its requests keep their current backend. Dream is a hub-only
   duty (epic #21563, S4.2). The constraint is carried in the types: the dream
   service constructor takes the hub `Pool` by value, not an `Option`, and is
   built only inside that block, so no call site guards it.
6. **LLM and embeddings.** Dream planning and knowledge-graph extraction call
   the AI family's text-generation service in process. The AI crate exports
   only its family-push function, so 2.4 adds one re-export of the #25097
   service to `crates/gai/src/lib.rs`, keeping crates/AGENTS.md's rule that
   families reach each other only through public APIs. The `memory.dream` and
   `memory.kg` feature profiles (`feature_mid`, `feature_low`) and candidate
   lists resolve through that service. Embeddings reuse the landed
   `crates/gcore/src/ai/embeddings.rs::embed_batch` through
   `tokio::task::spawn_blocking`, as AI 1.2 does; no AI route is called over
   HTTP.
7. **Write coordination.** Every native vector and graph write runs inside
   #25059's shared cross-runtime project write fence
   (`crates/gcore/src/project_fence.rs`), which replaces Python's in-process
   `src/gobby/projects/write_fence.py::ProjectWriteFence` authority across
   both runtimes. Memory create, update and delete append projection changes
   and dual-write to managed projection targets through 2.1's module.
   `ReviewLearningPatternMutation` keeps its explicit key
   `review_learning_pattern:{project_id}:{pattern_key}`
   (`src/gobby/storage/hub/postgres_pool.py::advisory_lock_keys`).
8. **Cross-family reads.** The tasks (#21560) and sessions (#21562) families
   are unported. Native memory reads their rows directly through the shared
   pool, read-only: `surface_memories` reads the session's `project_id` and
   `last_assistant_content`; `get_memory` reads the claimed task id;
   `review_task_memories` reads task and session rows. Its only writes outside
   memory tables are Decision 3's three session-variable keys.
9. **CLI architecture.** Python's record commands build
   `MemoryManager(db, config.memory)` in process with no vector store,
   embedder or graph (`src/gobby/cli/memory/__init__.py::get_memory_manager`),
   so they write database rows only and recall searches keywords only; the
   daemon's reconciliation projects vectors later. The dream, graph and index
   commands call the daemon over HTTP. The native command tree keeps that
   split. Record commands run the 1.1 store in process over a CLI pool and
   write no vectors, so they need no fence or lease. The other commands are
   HTTP clients of the same routes.
10. **#25070 placement.** P3's heading declares `(#25070)`, so expansion
    parents the seam's phase sub-epic under #25070. #25116's existing edge
    on #25070 then waits on the seam leaf. The Orchestrator removes #25070's
    `needs-planning` label after expansion. #25024 is blocked on the epic;
    it binds `MemoryDreamService` from 4.3.
11. **Debt-first review (memory 9066f1eb).** The disposition list below
    decides every surfaced debt item. Two candidates proved not to be debt.
    The CLI's in-process manager writes no vectors (Decision 9), so it
    bypasses neither fence nor lease. `surface_memories` failing soft is
    documented: it "returns an empty result rather than failing the caller".
12. **Prompt rendering.** Dream planning and graph extraction render
    `prompts` records through `src/gobby/prompts/loader.py::PromptLoader`,
    which uses Jinja with strict undefined variables. No Rust template engine
    is in the lockfile, and the four bundled memory prompts use only
    `{{ name }}` and one `{{ name | default('literal') }}`. Restraint stops at
    a renderer for exactly that subset in the memory crate (2.4). An override
    that uses other syntax fails with a typed validation error naming the
    template path and calls no model; it never renders partially. Python
    renders such an override, so the refusal is a changed behavior on the
    eight surfaces that report a graph rebuild or a dream run, each under an
    exact waiver (Debt not to port). A second native consumer of prompts
    would move this renderer to gcore rather than copy it.

## Debt not to port
`kind: framing`

Josh's debt-first ruling (memory `9066f1eb`) governs this plan. Keep all 80
public surfaces; parity matches their intentional contracts, not accidents.

**KEEP**, with the reason each stays:

- The dream route status contract: 202 admitted, 200 coalesced, 409
  `DREAM_RUN_CONFLICT`, 500 for a failed launch, 400 otherwise, and 503 when
  the coordinator is unavailable
  (`src/gobby/servers/routes/memory_dream.py::_http_status`). The CLI and the
  `gobby-memory` dream tools read them.
- One in-process knowledge-graph rebuild job per daemon, with status lost on
  restart. A rebuild is idempotent and re-runnable, and a durable job record
  would need a schema change that no caller asks for.
- 404 with the domain `ValueError` message for unknown or foreign memory IDs.
  These are deliberate domain messages, not diagnostics.
- 502 when FalkorDB is unreachable on the graph routes.
- `surface_memories` failing soft to an empty result (Decision 11).
- The CLI's database-only record commands and keyword-only `recall`
  (Decision 9).
- `rebuild_crossrefs`'s documented `limit` argument (default 500) on the MCP
  tool, while the HTTP route processes every memory.
- Dream reports written to `.gobby/reports/dream/` under the project root,
  atomically, as `src/gobby/memory/dream/report.py::report_path` names them.
- `backfill-unscoped-lessons`. It is idempotent, cheap, and still stamps
  lessons recorded without a path tag.

**DROP**, each through an exact-surface #24963 waiver:

- **Raw exception text (D5).** 21 handlers in
  `src/gobby/servers/routes/memory.py::create_memory_router` raise
  `HTTPException(500, detail=str(e))`, and `_execute_graph_rebuild` stores
  `error=str(e)` in the job state that `GET /api/memories/graph/rebuild/status`
  returns. Twenty-one `gobby-memory` tools return
  `{"success": False, "error": str(e)}` or interpolate the exception
  (`review_task_memories`), and all six review-learning tools do the same.
  Native handlers return `Internal server error` in the same field through
  #24970's adapters and log diagnostics privately; intentional domain errors
  keep their text. Owners 1.1, 1.2, 1.3, 2.2, 2.3 and 2.4 for HTTP, 6.2 and
  6.3 for MCP.
- **Untracked invalidate rebuild.** `POST /api/memories/invalidate` clears
  the indices, then starts `rebuild_indices` as an untracked task whose
  failure only reaches the log, and which can race a graph rebuild or a
  second invalidate. Native runs that rebuild in the same single job slot as
  the graph rebuild, so `GET /api/memories/graph/rebuild/status` reports its
  progress and failure. While a job runs, invalidate clears nothing and
  returns 202 with the running snapshot and `already_running: true`, as the
  background graph rebuild does. The CLI prints that nothing was cleared in
  that case. Owners 2.3 (HTTP) and 7.2 (CLI).
- **Swallowed crossref failures.** `POST /api/memories/crossrefs/rebuild` and
  the `rebuild_crossrefs` tool log per-memory failures as warnings and report
  success. Native adds `crossrefs_failed` (a count) to the response; the
  existing fields keep their meaning. Owners 2.3 and 6.2.
- **Dream revert conflicts reported as 404.**
  `src/gobby/servers/routes/memory_dream.py::create_memory_dream_router`'s
  revert handler maps every failure to 404, including `revert_forfeited` and
  `revert_failed` (`src/gobby/memory/dream/apply.py::revert_dream_run`).
  Native keeps 404 for an unknown run and returns 409 with the same `detail`
  for the two state conflicts. The CLI prints only the detail, so its output
  is unchanged. Owner 4.3.
- **Backup paths outside the project backup directory.** `restore_memories`
  calls `src/gobby/sync/memories.py::MemoryBackupManager.restore`, which
  resolves the configured relative `.gobby/memories.jsonl` against the
  caller's project context or the daemon's working directory, although the
  tool describes `~/.gobby/backups/<project-uuid>/memories.jsonl`.
  `backup_memories` falls back the same way when no project is current.
  Native reads and writes the current project's backup file, using the
  personal project when none is current, as `list_memories` does. Owner 6.2.
- **Prompt overrides outside the rendered subset.** Python renders any
  `prompts` record the memory jobs load with full Jinja
  (`src/gobby/prompts/loader.py::PromptLoader.render`): a global override of
  the three graph templates, or a project or global override of the dream
  template at the configured `prompt_path`. Native renders Decision 12's
  subset and refuses other syntax with a validation-class error naming the
  template path, before any model call. A graph rebuild resolves and parses
  its three templates before it marks any memory pending, so a refusal fails
  the whole job and leaves every row's graph state unchanged: a foreground
  rebuild returns the error, and the job status ends `failed` with it,
  unredacted. A dream run refuses at its first planner page, before any
  action applies, and ends `failed` with the error, the branch Python takes
  for a Jinja error, which `src/gobby/memory/dream/planner.py`'s page
  isolation does not catch. Triggers keep their admission responses
  (`POST /memory/dream`, `memory_dream`, a background rebuild's 202 and
  `invalidate`); the change shows on the eight surfaces that report the job.
  Corpus cases for other rows use the bundled templates. Owners 2.4 and 4.3
  for HTTP, 6.2 for MCP and 7.2 for CLI.

**Waiver inventory:**

| ID | Exact ledger surface | Owner |
| --- | --- | --- |
| `memory.http-list.internal-error-redaction` | `HTTP GET /api/memories` | 1.1 |
| `memory.http-get.internal-error-redaction` | `HTTP GET /api/memories/{memory_id}` | 1.1 |
| `memory.http-stats.internal-error-redaction` | `HTTP GET /api/memories/stats` | 1.1 |
| `memory.http-graph.internal-error-redaction` | `HTTP GET /api/memories/graph` | 1.2 |
| `memory.http-graph-counts.internal-error-redaction` | `HTTP GET /api/memories/graph/counts` | 1.2 |
| `memory.http-graph-entities.internal-error-redaction` | `HTTP GET /api/memories/graph/entities` | 1.2 |
| `memory.http-entity-neighbors.internal-error-redaction` | `HTTP GET /api/memories/graph/entities/{entity_key}/neighbors` | 1.2 |
| `memory.http-search.internal-error-redaction` | `HTTP GET /api/memories/search` | 1.3 |
| `memory.http-create.internal-error-redaction` | `HTTP POST /api/memories` | 2.2 |
| `memory.http-update.internal-error-redaction` | `HTTP PUT /api/memories/{memory_id}` | 2.2 |
| `memory.http-delete.internal-error-redaction` | `HTTP DELETE /api/memories/{memory_id}` | 2.2 |
| `memory.http-restore.internal-error-redaction` | `HTTP POST /api/memories/{memory_id}/restore` | 2.2 |
| `memory.http-promote.internal-error-redaction` | `HTTP POST /api/memories/{memory_id}/promote` | 2.2 |
| `memory.http-demote.internal-error-redaction` | `HTTP POST /api/memories/{memory_id}/demote` | 2.2 |
| `memory.http-move.internal-error-redaction` | `HTTP POST /api/memories/{memory_id}/move` | 2.2 |
| `memory.http-invalidate.internal-error-redaction` | `HTTP POST /api/memories/invalidate` | 2.3 |
| `memory.http-invalidate.tracked-rebuild` | `HTTP POST /api/memories/invalidate` | 2.3 |
| `memory.http-reconcile.internal-error-redaction` | `HTTP POST /api/memories/reconcile` | 2.3 |
| `memory.http-crossrefs-rebuild.internal-error-redaction` | `HTTP POST /api/memories/crossrefs/rebuild` | 2.3 |
| `memory.http-crossrefs-rebuild.reported-failures` | `HTTP POST /api/memories/crossrefs/rebuild` | 2.3 |
| `memory.http-embeddings-reindex.internal-error-redaction` | `HTTP POST /api/memories/embeddings/reindex` | 2.3 |
| `memory.http-graph-rebuild.internal-error-redaction` | `HTTP POST /api/memories/graph/rebuild` | 2.4 |
| `memory.http-graph-rebuild.unsupported-prompt-syntax` | `HTTP POST /api/memories/graph/rebuild` | 2.4 |
| `memory.http-graph-rebuild-status.internal-error-redaction` | `HTTP GET /api/memories/graph/rebuild/status` | 2.4 |
| `memory.http-graph-rebuild-status.unsupported-prompt-syntax` | `HTTP GET /api/memories/graph/rebuild/status` | 2.4 |
| `memory.http-graph-clear.internal-error-redaction` | `HTTP POST /api/memories/graph/clear` | 2.4 |
| `memory.http-dream-status.unsupported-prompt-syntax` | `HTTP GET /memory/dream/{run_id}` | 4.3 |
| `memory.http-dream-revert.state-conflict-status` | `HTTP POST /memory/dream/{run_id}/revert` | 4.3 |
| `memory.mcp-backup-memories.internal-error-redaction` | `MCP gobby-memory:backup_memories` | 6.2 |
| `memory.mcp-backup-memories.project-backup-path` | `MCP gobby-memory:backup_memories` | 6.2 |
| `memory.mcp-create-memory.internal-error-redaction` | `MCP gobby-memory:create_memory` | 6.2 |
| `memory.mcp-delete-memory.internal-error-redaction` | `MCP gobby-memory:delete_memory` | 6.2 |
| `memory.mcp-demote-memory-from-global.internal-error-redaction` | `MCP gobby-memory:demote_memory_from_global` | 6.2 |
| `memory.mcp-densify-knowledge-graph-cooccurrence.internal-error-redaction` | `MCP gobby-memory:densify_knowledge_graph_cooccurrence` | 6.2 |
| `memory.mcp-get-memory.internal-error-redaction` | `MCP gobby-memory:get_memory` | 6.2 |
| `memory.mcp-get-related-memories.internal-error-redaction` | `MCP gobby-memory:get_related_memories` | 6.2 |
| `memory.mcp-list-memories.internal-error-redaction` | `MCP gobby-memory:list_memories` | 6.2 |
| `memory.mcp-memory-dream-status.unsupported-prompt-syntax` | `MCP gobby-memory:memory_dream_status` | 6.2 |
| `memory.mcp-memory-stats.internal-error-redaction` | `MCP gobby-memory:memory_stats` | 6.2 |
| `memory.mcp-move-memory.internal-error-redaction` | `MCP gobby-memory:move_memory` | 6.2 |
| `memory.mcp-promote-memory-to-global.internal-error-redaction` | `MCP gobby-memory:promote_memory_to_global` | 6.2 |
| `memory.mcp-rebuild-crossrefs.internal-error-redaction` | `MCP gobby-memory:rebuild_crossrefs` | 6.2 |
| `memory.mcp-rebuild-crossrefs.reported-failures` | `MCP gobby-memory:rebuild_crossrefs` | 6.2 |
| `memory.mcp-rebuild-knowledge-graph.internal-error-redaction` | `MCP gobby-memory:rebuild_knowledge_graph` | 6.2 |
| `memory.mcp-rebuild-knowledge-graph.unsupported-prompt-syntax` | `MCP gobby-memory:rebuild_knowledge_graph` | 6.2 |
| `memory.mcp-recluster-knowledge-graph-entities.internal-error-redaction` | `MCP gobby-memory:recluster_knowledge_graph_entities` | 6.2 |
| `memory.mcp-reindex-embeddings.internal-error-redaction` | `MCP gobby-memory:reindex_embeddings` | 6.2 |
| `memory.mcp-restore-memories.internal-error-redaction` | `MCP gobby-memory:restore_memories` | 6.2 |
| `memory.mcp-restore-memories.project-backup-path` | `MCP gobby-memory:restore_memories` | 6.2 |
| `memory.mcp-restore-memory.internal-error-redaction` | `MCP gobby-memory:restore_memory` | 6.2 |
| `memory.mcp-review-task-memories.internal-error-redaction` | `MCP gobby-memory:review_task_memories` | 6.2 |
| `memory.mcp-search-knowledge-graph.internal-error-redaction` | `MCP gobby-memory:search_knowledge_graph` | 6.2 |
| `memory.mcp-search-memories.internal-error-redaction` | `MCP gobby-memory:search_memories` | 6.2 |
| `memory.mcp-update-memory.internal-error-redaction` | `MCP gobby-memory:update_memory` | 6.2 |
| `memory.mcp-list-check-keys.internal-error-redaction` | `MCP gobby-review-learning:list_check_keys` | 6.3 |
| `memory.mcp-recall-review-context.internal-error-redaction` | `MCP gobby-review-learning:recall_review_context` | 6.3 |
| `memory.mcp-recall-review-lessons-by-class.internal-error-redaction` | `MCP gobby-review-learning:recall_review_lessons_by_class` | 6.3 |
| `memory.mcp-recall-review-lessons-for-files.internal-error-redaction` | `MCP gobby-review-learning:recall_review_lessons_for_files` | 6.3 |
| `memory.mcp-record-review-lesson.internal-error-redaction` | `MCP gobby-review-learning:record_review_lesson` | 6.3 |
| `memory.mcp-retire-review-lesson.internal-error-redaction` | `MCP gobby-review-learning:retire_review_lesson` | 6.3 |
| `memory.cli-dream.unsupported-prompt-syntax` | `CLI gobby memory dream` | 7.2 |
| `memory.cli-dream-status.unsupported-prompt-syntax` | `CLI gobby memory dream status` | 7.2 |
| `memory.cli-invalidate.tracked-rebuild` | `CLI gobby memory invalidate` | 7.2 |
| `memory.cli-rebuild-graph.unsupported-prompt-syntax` | `CLI gobby memory rebuild-graph` | 7.2 |

Each owner adds its entries to `docs/contracts/parity-waivers.json` and depends
on #24963, which owns that file's schema, loader and consumers. Entry fields
are `id`, `surface`, `behaviour`, `decision` and `replacement`, all required.
IDs use `<family>.<surface-slug>.<behaviour-slug>`. Each `surface` is exactly
the key above, and no wildcard surface is valid. Each `decision` cites this
plan, for example `.gobby/plans/memory-family.md#2.3`. The behaviour sentence
and replacement name the bounded changed branch. Corpus vectors keep the
Python outcome and assert the chosen native outcome. The redaction entries
claim only that raw diagnostics are reachable, not that one has leaked a
secret.

**S1 shared readiness:** ledger generation, corpus replay and compare consume
the one #24963 manifest. A changed behavior reports `Waived` with its ID,
decision and passing native assertions, never a mask or `Skipped`. At least one
ordinary `Passed` case is required. Zero-Passed, all-Skipped, unwaived Skipped,
unknown or stale IDs, missing decisions and unmatched differences fail
readiness.

**S2 shared error owner:** #24970 supplies the single seven-class native
classification (validation, missing, forbidden, conflict, capacity,
unavailable, internal) and the HTTP, MCP and CLI adapters. This family maps
its domain errors onto those classes and declares its existing envelopes
through the adapters; it adds no family-local error framework. Preserve
observed status, envelope and CLI exit behavior unless an exact waiver above
authorizes the change.

**S3 compare safety:** compare mode runs Python and native side by side only
for read rows: the list, get, stats, search and graph `GET` rows,
`GET /api/memories/graph/rebuild/status` and `GET /memory/dream/{run_id}`.
Search's surfaced-statistics counters make it a write; compare keeps the
Python write and discards native counter effects in an isolated transaction.
Every row that writes memories, vectors, graph nodes, crossrefs, dream runs,
backups or session variables is proven against isolated stubs and cloned
state, never by running the effect twice in production.

## Constraints
`kind: framing`

- Observed research baseline: HEAD `77798bc4ed3783c810393940012ac1e2c0dfe958`.
  Existing files and symbols were inspected with `gcode outline`, `gcode
  symbol-at` and bounded literal searches. Paths under `crates/gmemory`,
  `crates/gcore/src/embedding_generation.rs`, the new test files and the
  proposed test symbols are new. `crates/gai/src/lib.rs` is created by the AI
  plan's #25094 before 2.4 runs.
- Placement follows Decision 5. `crates/gdaemon/src/serve.rs` (818 lines at
  baseline, against the 850-line trigger) gains only the few lines of one
  `gobby_memory` call in each cfg-gated `run`; the family's composition
  logic lives in the crate.
- Leaf order is a single chain because every leaf after 1.1 edits
  `crates/gmemory/src/lib.rs`, `crates/gmemory/src/http/mod.rs` or
  `docs/contracts/parity-waivers.json`. 2.1 touches only gcore and runs in
  parallel after 0.1. Production files stay under 850 lines by module split:
  HTTP handlers, search, graph, writes, dream and MCP each have their own
  module files, and test modules live in `tests/` or `<module>/tests.rs`,
  never inline (crates/AGENTS.md).
- 1.1 declares the crate's dependency set except `gobby-ai` (2.4) and the
  HDBSCAN crate (2.5): `gobby-core` with its `postgres` and qdrant/falkor
  features, `gobby-runtime` for `crates/gruntime/src/config.rs::active_state`
  and the secret store, axum, tokio, serde, serde_json, uuid, chrono, clap
  with `derive` for 7.1, and `reqwest` with `blocking` for 7.2. Only 1.1,
  2.4, 2.5 and 8.2 edit `crates/gmemory/Cargo.toml` or `Cargo.lock`.
- gcore's Qdrant, FalkorDB and embeddings clients are blocking. Async handlers
  call them through `tokio::task::spawn_blocking`.
- Storage reuses existing tables: `memories`, `memory_crossrefs`,
  `memory_dream_runs`, `memory_dream_snapshots`, `memory_dream_truth_state`,
  `embedding_generation_acks`, `embedding_projection_changes`,
  `session_variables` (Decision 3's keys only), and read-only `sessions` and
  `tasks`. Review lessons are `memories` rows with lesson tags. No schema
  change.
- Source changes are built and tested before deployment. 8.2 is the one
  shipping commit and owns release bumps and promotion; earlier commits are
  unshipped source commits. Read the current `project_policy.release_guidance`
  at execution, honor the published and unpublished binary distinction, and
  use `src/gobby/install/bin_set_coherence.py::promote_workspace_binary_set`.
- Heavy Rust commands require the Release Manager's heavy-work admission and
  `CARGO_BUILD_JOBS=3`. Rust tests run through `cargo nextest run -p <package>`.
  No full pytest run.
- Python checks use `uv run`. Tests that touch the database use isolated
  temporary state and ports with
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
  and `GOBBY_TEST_PROTECT=1`, never the operator's daemon database. Qdrant
  and FalkorDB tests use the test stack's `GOBBY_TEST_QDRANT_URL` and
  `GOBBY_TEST_FALKOR_HOST`, `GOBBY_TEST_FALKOR_PORT` and
  `GOBBY_TEST_FALKOR_PASSWORD`, with collection and graph names unique to the
  run. Embedding and text generation use a local OpenAI-compatible stub; no
  test spends provider credit.
- macOS and Linux only. No latency gate is set; the only timing checks are
  the existing timeouts and bounded waits the corpus pins.
- #24968 does not apply: no row reads or writes `files_home`. Backups use
  `~/.gobby/backups/<project-uuid>/memories.jsonl` on the hub machine, and
  dream reports use the project root, as Python does.

Consumer evidence: the ledger shard predicate (notes citing #21563) returned
exactly the 74 plus 6 keys above. Wire-shape consumers that must keep working
unchanged are `web/src/hooks/useMemory.ts` (list, create, update, delete,
restore, promote, search, stats, graph, entities and neighbors), the dream,
graph and index commands in `src/gobby/cli/memory/` until 8.3 forwards them,
the rule templates under
`src/gobby/install/shared/workflows/rules/memory-lifecycle/` and
`.../review-learning/`, and the delivery tables in
`src/gobby/workflows/engine/delivery_formatting.py` and `mcp_injections.py`,
which key on `gobby-review-learning` tool names. No Rust crate calls
`/api/memories` or `/memory/dream`. The agent-token table
`src/gobby/servers/auth_service.py::_AGENT_CAPABILITY_MATRIX` admits only
`GET /api/memories/graph/counts` and `GET /api/memories/graph/rebuild/status`.
`src/gobby/servers/browser_sessions.py::browser_route_allowed` limits managed
browser cookies to GET, HEAD and OPTIONS. No memory row is a runtime-grant
route. The Python route, tool and service modules stay as the proxy backend
until the global Python removal; this plan edits none of them except the CLI
package that 8.3 replaces.

## P0: Freeze the family contract
`kind: framing`

### 0.1 Executable 80-row characterization corpus [category: test] (depends: #24963)
`kind: deliverable`

Targets:
- `scripts/memory_parity_corpus.py`
- `crates/gmemory/tests/fixtures/parity.json`
- `tests/contracts/test_memory_parity_corpus.py`

**Research context:** Entry points are
`src/gobby/servers/routes/memory.py::create_memory_router`,
`src/gobby/servers/routes/memory_dream.py::create_memory_dream_router`,
`src/gobby/mcp_proxy/tools/memory.py::create_memory_registry` (with
`memory_write.py::register_memory_write_tools`,
`memory_review.py::register_memory_review_tools`,
`memory_surface.py::register_memory_surface_tools` and
`memory_dream.py::register_memory_dream_tools`),
`src/gobby/mcp_proxy/tools/review_learning.py::create_review_learning_registry`
and `src/gobby/cli/memory/main.py::memory`. Existing Python fixtures live in
`tests/memory/`, `tests/review_learning/`, `tests/cli/test_memory_cli.py`,
`tests/cli/test_memory_coverage.py` and `tests/servers/routes/`. Generate a
committed, versioned JSON corpus from isolated Python fixtures: method and
path, tool and arguments, or command and argv; headers; seeded `memories`,
`memory_crossrefs`, `prompts`, dream, session and task rows; seeded Qdrant points and
FalkorDB nodes; stub embedding and generation behavior; expected response,
exit code and effects. Only explicitly enumerated volatile fields are
normalized: generated UUIDs, timestamps, job IDs and checkout paths.

The embedding and generation stub is a local OpenAI-compatible server that
returns deterministic vectors and canned extraction and dream-plan JSON. The
corpus records Qdrant points and FalkorDB nodes and edges after each effectful
case. For every HTTP row, record the admission matrix: no credential, API key,
local operator, ordinary and managed browser cookies, and agent tokens with
matching and mismatched session identity, using Python's
`_AGENT_CAPABILITY_MATRIX` and `browser_route_allowed`. For every MCP row,
record the exact input schema, description, brief projection and definition
hash. For every CLI row, record help text, stdout, stderr and exit codes.

Consume #24963's one `docs/contracts/parity-waivers.json` loader. The 64
changed behaviors in the waiver inventory keep their Python baseline and get
case-specific `Waived` assertions with a required native outcome, never masks
or `Skipped`. Each redaction case injects an unexpected fault into the Python
fixture to record the raw-text baseline. Each unsupported-prompt-syntax case
seeds a `prompts` row whose template uses a `{% if %}` block: a global
override of `memory/extract_relations` for the four graph-rebuild surfaces
and a project override of `memory/dream` for the four dream-status surfaces.
Python's baseline renders it and completes against the stub; the native
assertion requires the validation-class error naming the template path and
no stub generation request. This leaf uses test-only waiver
entries and native-result doubles to check loader matching; the owning leaves
add the production entries and replay against real handlers. No dependency
cycle is introduced.

**Acceptance:**

- 0.1.1 - The corpus inventory equals the exact 80-key ledger predicate, every
  key has a success case and a representative failure case, and a key outside
  the family is rejected.
  test: `tests/contracts/test_memory_parity_corpus.py::test_inventory_matches_memory_ledger`.
- 0.1.2 - Running the collector twice against reset isolated state gives the
  same normalized fixtures, ordering, stdout, stderr, exit codes, vectors,
  graph effects and stub requests.
  test: `tests/contracts/test_memory_parity_corpus.py::test_collection_is_reproducible`.
- 0.1.3 - Each of the 25 HTTP rows records the full admission matrix,
  including agent-token admission on the two graph rows only and managed
  cookies refused on every mutation.
  test: `tests/contracts/test_memory_parity_corpus.py::test_http_admission_matrix`.
- 0.1.4 - Each of the 64 changed behaviors resolves to one exact-surface test
  waiver with explicit native assertions, while untouched cases on the same
  surface stay `Passed`.
  test: `tests/contracts/test_memory_parity_corpus.py::test_debt_waivers_are_behavior_scoped`.
- 0.1.5 - Skipped-only reports, unwaived `Skipped`, stale or missing waiver
  decisions and an unrelated injected regression fail readiness even beside a
  valid `Waived` case.
  test: `tests/contracts/test_memory_parity_corpus.py::test_waiver_does_not_hide_unrelated_diff`.

Planned verification: the five named tests with the protected Python test
prefix; collector runs use temporary homes, the isolated test database and
run-unique Qdrant collections and FalkorDB graphs. No characterization test
has run during drafting.

## P1: Native family shell and reads
`kind: framing`

### 1.1 Crate, memory family shell, configuration and record reads [category: code] (depends: 0.1, #24443, #24448, #24963, #24970)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gmemory/Cargo.toml`
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/config.rs`
- `crates/gmemory/src/model.rs`
- `crates/gmemory/src/store.rs`
- `crates/gmemory/src/admission.rs`
- `crates/gmemory/src/http/mod.rs`
- `crates/gmemory/src/http/records.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: add one family-push call inside the pool block of both cfg-gated `run` functions
- `crates/gmemory/tests/records.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append this leaf's waiver entries to the shared manifest array

**Research context:** `crates/gruntime/src/lib.rs::RuntimeFamily` is the
pattern: a `NATIVE_ROUTES` table of method, path and handler rows, `claims`
matching method, path and caller, and `check_proxied` for routes left to
Python. `crates/gdaemon/src/front_door/routes.rs::RouteTable::new` selects
each family's backend from `routes.get(family.name())` or the family default,
and `RouteTable::lookup` takes the longest prefix. Create `MemoryFamily`
(name `memory`, prefixes `/api/memories` and `/memory/dream`, default Proxy).
`crates/gmemory/src/lib.rs` exposes one function that pushes it, called from
serve.rs inside `if let Some(pool) = &pool`; the service holds the pool by
value (Decision 5).

`crates/gmemory/src/config.rs` builds a per-request snapshot from
`crates/gruntime/src/config.rs::active_state`: the `memory.*` section
(`src/gobby/config/persistence.py::MemoryConfig`, including `dream` and `kg`),
`memory_backup.*`, and Qdrant, FalkorDB and embedding settings through
`crates/gcore/src/config/resolve.rs::resolve_qdrant_config`,
`::resolve_falkordb_config` and `::resolve_embedding_config`. Secret
references open through #24448's store and stay in hub memory. A change
applies on the next request, as Python's config subscribers do.

`crates/gmemory/src/model.rs` ports the `memories` row and its JSON
projection from `src/gobby/storage/memories_models.py`. `store.rs` ports the
list, get, stats and scope queries in `src/gobby/storage/memories_query.py`,
`memories_scope.py` and `memories_crud.py` (reads only): project and global
scope, `deleted_at` and `dream_action` visibility, tag filters (`tags_all`,
`tags_any`, `tags_none`), ordering, limit and offset, prefix resolution
through `resolve_memory_id`, and `src/gobby/servers/routes/memory.py::_current_project_id`
and `::_ensure_memory_in_current_project`. Handlers are `list_memories`,
`get_memory` and `memory_stats` in the same file.

`crates/gmemory/src/admission.rs` holds `AdmissionClass`:
`AgentToken { bind_identity: false }` for `GET /api/memories/graph/counts`
and `GET /api/memories/graph/rebuild/status`, and `OperatorOrBrowser` for
every other row, with managed browser cookies limited to GET. Its
`admitted(parts, class)` check returns false in this leaf, so every claim
fails closed and traffic stays proxied; 8.1 binds it to #25046 and #25068.
That is the stated remainder, not a stub awaiting a later choice.

Consume #24970's classification and HTTP adapter. Unexpected faults return
`Internal server error` in the existing `detail` field. This leaf adds the
three `memory.http-list`, `memory.http-get` and `memory.http-stats` redaction
entries to the shared waiver file with decision
`.gobby/plans/memory-family.md#1.1`.

**Granularity:** The crate, configuration snapshot, repository, admission
declaration, composition and three read endpoints form one usable
record-read outcome. The production-file count exceeds six because
composition crosses the gdaemon boundary; every later behavior is its own
deliverable.

**Acceptance:**

- 1.1.1 - `HTTP GET /api/memories` matches list scope, filters, ordering,
  limit, offset and payload.
  test: `crates/gmemory/tests/records.rs::list_matches_python`.
- 1.1.2 - `HTTP GET /api/memories/{memory_id}` matches identity, prefix
  resolution, foreign-project refusal and not-found outcomes.
  test: `crates/gmemory/tests/records.rs::get_matches_python`.
- 1.1.3 - `HTTP GET /api/memories/stats` matches totals, per-type counts and
  project filtering. test: `crates/gmemory/tests/records.rs::stats_matches_python`.
- 1.1.4 - The family claims only implemented rows, admission fails closed,
  a node composes no memory family, and a configuration change applies on the
  next request. test: `crates/gmemory/tests/records.rs::family_shell_and_placement`.
- 1.1.5 - Injected unexpected faults on the three rows return safe detail
  under their exact Waived citations and change no state.
  test: `crates/gmemory/tests/records.rs::unexpected_read_errors_are_safe_and_waived`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test records`;
`cargo check -p gobby-daemon`. No implementation check has run during drafting.

### 1.2 Knowledge-graph reads [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/graph/mod.rs`
- `crates/gmemory/src/graph/reader.rs`
- `crates/gmemory/src/http/mod.rs`
- `crates/gmemory/src/http/graph.rs`
- `crates/gmemory/tests/graph_reads.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append this leaf's waiver entries to the shared manifest array

**Research context:** Handlers are `memory_graph`, `graph_counts`,
`entity_graph` and `entity_neighbors` in
`src/gobby/servers/routes/memory.py::create_memory_router`, with
`_bounded_graph_limit` capping limits by the UI configuration and
`_require_falkordb_memory_manager` returning 502 when FalkorDB is absent or
unreachable. The reader is
`src/gobby/memory/services/knowledge_graph/reader.py` (entity listing,
neighbor expansion, counts, cluster expansion at line 552 behind
`memory.cluster_recall_expansion` and `cluster_expansion_per_entity`, and
co-occurrence and edge-weight scoring with `edge_half_life_days`). The graph
name and per-project scoping come from
`src/gobby/memory/falkor_client.py` and
`src/gobby/memory/services/knowledge_graph/normalization.py`. Use gcore's
FalkorDB `GraphClient` through `spawn_blocking`; do not edit that client
module, which is 958 lines at baseline. Graph reads return the
`MemoryGraphReader` results that 1.3's search graph leg also consumes. This
leaf adds the four 1.2 redaction entries.

**Acceptance:**

- 1.2.1 - `HTTP GET /api/memories/graph` matches nodes, crossref edges,
  limits and project scoping. test: `crates/gmemory/tests/graph_reads.rs::memory_graph_matches_python`.
- 1.2.2 - `HTTP GET /api/memories/graph/counts` matches counts and the
  FalkorDB-unavailable 502. test: `crates/gmemory/tests/graph_reads.rs::graph_counts_matches_python`.
- 1.2.3 - `HTTP GET /api/memories/graph/entities` matches entity listing,
  ordering and limit bounds. test: `crates/gmemory/tests/graph_reads.rs::entities_match_python`.
- 1.2.4 - `HTTP GET /api/memories/graph/entities/{entity_key}/neighbors`
  matches neighbor expansion, encoded keys, weights and unknown-entity
  results. test: `crates/gmemory/tests/graph_reads.rs::neighbors_match_python`.
- 1.2.5 - Cluster expansion matches Python with the flag on and off, and the
  four rows return safe detail under their exact Waived citations.
  test: `crates/gmemory/tests/graph_reads.rs::cluster_expansion_and_safe_errors`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test graph_reads`
against a run-unique FalkorDB graph on the test stack.

### 1.3 Hybrid search and surfaced statistics [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/search/mod.rs`
- `crates/gmemory/src/search/keyword.rs`
- `crates/gmemory/src/search/vector.rs`
- `crates/gmemory/src/search/results.rs`
- `crates/gmemory/src/search/access.rs`
- `crates/gcore/src/qdrant.rs::*` — scope-reason: add one point-retrieval function that returns stored vectors by id, beside the existing search
- `crates/gdaemon/src/serve.rs::*` — scope-reason: pass the memory route's resolved backend into the memory family push at both cfg-gated serve entry points
- `crates/gmemory/src/http/mod.rs`
- `crates/gmemory/src/http/search.rs`
- `crates/gmemory/tests/search.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append this leaf's waiver entries to the shared manifest array

**Research context:** Port
`src/gobby/memory/services/search.py::SearchService.search` and its helpers:
`_search_paths.py::search_with_graph` and `::search_qdrant_keyword`
(candidate vectors, Qdrant hits, unwindowed rescoring), `_search_keyword.py`
(pg_search BM25 ranking and keyword fallback, using
`crates/gcore/src/search.rs::sanitize_pg_search_query` and `bm25_score_expr`),
`_search_graph.py::search_graph_scored` over 1.2's reader,
`_search_results.py::build_results` and `::collapse_near_duplicates`,
`_search_ranking.py::order_results`, `_search_rrf.py` over
`crates/gcore/src/search.rs::rrf_merge`, the temporal decay in
`src/gobby/memory/scoring.py`, and `_search_access.py::update_surfaced_stats`
with its `SURFACED_CALLERS` set (`memory.surface`,
`mcp_proxy.memory.search_memories`, `mcp_proxy.memory.review_task_memories`,
`http.memory.search`, `cli.memory.recall`). Diagnostics and the
undecayed-similarity threshold axis match the corpus. The query embedding uses
`crates/gcore/src/ai/embeddings.rs::embed_one` with the configured query
prefix. Collapsing near duplicates needs stored vectors, which gcore's Qdrant
module cannot read today, so this leaf adds one retrieval function there. The
route handler is `search_memories` in `create_memory_router`. A missing
vector store or embedder falls back to keyword search exactly as Python does.
This leaf adds the `memory.http-search` redaction entry.

Compare isolation follows the terminals family's precedent,
`terminal_route_latch`: the memory family push receives the `memory` route's
resolved backend from `bootstrap.front_door.routes` (the family default when
absent), which gdaemon reads once at startup. The front door's compare
dispatch runs the native router on a copied `GET` with no shadow marker, so
under `Compare` every native search is the shadow leg. `update_surfaced_stats`
takes a caller-owned transaction; the search handler opens exactly one for
its surfaced writes and always rolls it back, on error paths too, and no
separate committing transaction exists for those writes. Under `Native` the
same transaction commits, as Python's writes do. Python's debounce reads
`last_surfaced_at`, so no in-process state needs isolating. 8.2 passes the
`gobby-memory` server's backend to the tool family the same way for
`search_memories`.

**Acceptance:**

- 1.3.1 - `HTTP GET /api/memories/search` matches ranking, scores,
  diagnostics, filters, limit and min-score across semantic, keyword and
  graph legs. test: `crates/gmemory/tests/search.rs::search_matches_python`.
- 1.3.2 - Keyword-only fallback, an unavailable vector store, a pg_search
  parse error and a graph-leg timeout match Python's degraded results.
  test: `crates/gmemory/tests/search.rs::degraded_search_matches_python`.
- 1.3.3 - Surfaced statistics change only for the five surfaced callers, with
  Python's debounce. test: `crates/gmemory/tests/search.rs::surfaced_stats_match_python`.
- 1.3.4 - The gcore retrieval function returns stored vectors by id and a
  typed error for a missing collection.
  test: `crates/gmemory/tests/search.rs::qdrant_retrieval_returns_vectors`.
- 1.3.5 - An injected unexpected fault returns safe detail under
  `memory.http-search.internal-error-redaction`.
  test: `crates/gmemory/tests/search.rs::unexpected_search_error_is_safe_and_waived`.
- 1.3.6 - With the family built for `Compare`, search returns its result and
  every surfaced-stat write rolls back, including after an error injected
  mid-update; built for `Native`, the writes commit with Python's debounce.
  test: `crates/gmemory/tests/search.rs::compare_backend_rolls_back_surfaced_stats`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test search`
against run-unique test-stack collections and the embedding stub;
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core -E 'test(qdrant)'`.

## P2: Native writers
`kind: framing`

### 2.1 Shared embedding serving lease and projection-change ledger [category: code] (depends: 0.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/embedding_generation.rs`
- `crates/gcore/src/embedding_generation/managed.rs`
- `crates/gcore/src/embedding_generation/tests.rs`
- `crates/gcore/src/lib.rs::*` — scope-reason: export the shared embedding-generation module beside the existing qdrant export
- `crates/gcore/tests/embedding_generation.rs`

**Research context:** Decision 4. Port
`src/gobby/storage/embedding_generation_state.py`:
`EmbeddingGenerationState.append_change`, `::watermark`, `::projection_targets`,
`::acknowledge`, `::renew`, `::prepare_serving_lease`, `::can_collect` and
`::incompatible_serving_acks`; `EmbeddingServingLease.activate`, `::fence`,
`::renew`, `::successor` and `::assert_serving`; the lease errors
`EmbeddingGenerationLeaseLost`, `EmbeddingGenerationLeaseExpired` and
`EmbeddingGenerationLeaseRenewTransient`; and the module function
`managed_projection_targets`. The managed lifecycle is
`src/gobby/runner_init/embedding_lease.py` (`_ManagedEmbeddingLease`,
`_renew_with_backoff`, `_reacquire_lease`, `_lease_matches_record`) and its
construction in `src/gobby/runner_init/services.py`
(`_serving_lease_identity`, `_read_completed_switch_record`,
`_managed_embedding_collection`). Tables are `embedding_generation_acks`
(primary key `daemon_instance_id`) and `embedding_projection_changes`
(`source_kind` in memory, tool or github_issue). The instance id is
`UUIDv5(machine_id, "gdaemon")` with gcore's existing `uuid` `v5` feature.
On a completed switch the holder re-reads the record, takes the successor
lease and re-targets the managed physical collection, as Python's
`_request_memory_services_rebuild` does; 2.2 supplies the memory re-target
callback. Projection targets read the managed physical names from the active
configuration key `ai.embeddings.switch_run`, as
`src/gobby/ai/embedding_switch.py::managed_embedding_projection` does. The
module is backend-neutral so #25071 reuses it for tool embeddings. Python's
lease stays unchanged and coexists under its own instance id.

`crates/gcore/src/embedding_generation/managed.rs` owns the managed lifecycle,
ported from `_ManagedEmbeddingLease`, `_renew_embedding_lease`,
`_renew_with_backoff` and `_reacquire_lease`; no other module renews or
reacquires. Renewal runs on a dedicated `std::thread`, as Python's
single-thread executor does, so a stalled tokio runtime cannot starve it. Every
10 seconds it renews; a transient renew failure retries after 1 second,
doubling to a 5-second cap, and fences once the remaining lease is no longer
than the next wait; an expired or lost lease fences at once. While fenced,
the same thread polls the completed-switch record every 5 seconds. A record
whose generation and revision match the held lease activates the successor,
resumes serving and calls the projection-repair callback once; a changed
record, or a successor that loses to a newer lease, calls the rebuild
callback with that record and stays fenced until it runs; a storage error
keeps polling. Python's `HostSleepTracker` only lowers log levels, so a host
sleep that outlives the lease takes the same expiry path. The holder supplies
three callbacks: re-target and rebuild from 2.2, and projection repair from
2.3, which runs its store reconciliation as Python's
`_request_memory_projection_repair` does.

**Acceptance:**

- 2.1.1 - `append_change`, `watermark` and `projection_targets` produce
  Python's rows and target sets for memory and tool sources, inside and
  outside a caller transaction.
  test: `crates/gcore/tests/embedding_generation.rs::ledger_matches_python`.
- 2.1.2 - Prepare, acknowledge, renew, fence, successor and `assert_serving`
  match Python's lease states, expiry and errors; a native and a Python lease
  on one machine hold separate rows, and `can_collect` honors both.
  test: `crates/gcore/tests/embedding_generation.rs::serving_lease_matches_python`.
- 2.1.3 - The instance id is stable across restarts and differs from
  `UUID(machine_id)`; a completed switch re-targets the holder through its
  callback. test: `crates/gcore/tests/embedding_generation.rs::instance_identity_and_retarget`.
- 2.1.4 - The managed lifecycle keeps renewing while the async runtime is
  blocked; transient renew failures retry at 1, 2, 4 and then 5 seconds and
  fence once the remaining lease is no longer than the next wait; an expired
  or lost lease, including one that expires across a simulated host sleep,
  fences at once.
  test: `crates/gcore/tests/embedding_generation.rs::managed_renewal_backoff_and_fence`.
- 2.1.5 - While fenced, a matching completed-switch record re-activates the
  successor, resumes serving and requests projection repair once; a changed
  record, or a successor that loses to a newer lease, requests a rebuild with
  that record and stays fenced; storage errors keep polling without resuming.
  test: `crates/gcore/tests/embedding_generation.rs::managed_reacquire_or_rebuild`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core --features postgres --test embedding_generation`
against the isolated test database.

### 2.2 Record lifecycle writes [category: code] (depends: 1.3, 2.1, #25059)
`kind: deliverable`

Targets:
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/write/mod.rs`
- `crates/gmemory/src/write/lifecycle.rs`
- `crates/gmemory/src/write/create.rs`
- `crates/gmemory/src/write/projection.rs`
- `crates/gmemory/src/write/crossref.rs`
- `crates/gmemory/src/http/mod.rs`
- `crates/gmemory/src/http/writes.rs`
- `crates/gmemory/tests/writes.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append this leaf's waiver entries to the shared manifest array

**Research context:** Port
`src/gobby/memory/services/lifecycle.py::MemoryLifecycleService`
(`create_memory`, `update_memory_scoped`, `delete_memory_scoped`,
`move_memory`, `set_memory_global`, `restore_memory_indices`,
`sync_memory_scope_indices`, `embed_and_upsert`, `enqueue_for_graph`,
`schedule_write_mark_due`, `mark_related_due` and
`rebuild_crossrefs_for_memory`), the create policy in
`src/gobby/mcp_proxy/tools/memory_write.py` (`AUTO_SUPERSEDE_SIMILARITY` 0.9,
`supersedes` up to 20 IDs through `src/gobby/storage/memories_crud.py::normalize_supersedes`,
`similar_existing`, `auto_superseded`, the ephemeral-note check from
`src/gobby/sync/memories.py::is_ephemeral_implementation_note`,
`normalize_memory_rationale` with `RATIONALE_MAX_CHARS` 500,
`content_cap_error` and `derive_memory_create_provenance`), the row writes in
`src/gobby/storage/memories_crud.py`, and
`src/gobby/memory/services/crossref.py::CrossrefService` (`create`,
`rebuild_for_memory`, `get_related`, with `crossref_threshold` and
`crossref_max_links`; `auto_crossref` is honored when set). The create policy
lives in `write/create.rs` so HTTP and 6.2's MCP tools share it; HTTP passes
the options its request model carries.

Each write follows Python's order: database transaction (with
`append_change` from 2.1 for memory create, update and delete), then vector
upsert or delete inside #25059's project fence after `assert_serving`, with
dual-writes to the managed projection targets, then graph enqueue
(`graph_status = 'pending'`) and dream due-marking
(`write_supersession_mark_due_enabled`). The vector writer holds the gdaemon
lease from 2.1 and supplies its re-target and rebuild callbacks.

Failure invariants follow Python's repair design, not a rollback. The row
write, its `vector_needs_reindex = TRUE` mark (set by create, content or type
change, move and scope change, as Python's row writes above do)
and its `append_change` row commit in one transaction. Vector work runs after commit; when the lease is
fenced, the fence is held, embedding fails or Qdrant fails, Python's
`embed_and_upsert` logs and returns false, the request still succeeds with
its committed row, and the flag and change row stay as durable repair intent.
No derived-store write happens outside that tracked path. 2.3's
reconciliation (`reconcile_stores` and
`secondary_reconciliation.reconcile_active_snapshot`) later writes the
vector, graph and crossref state and clears the flag only once they
converge. An unexpected fault before commit writes nothing and returns safe
detail.

Projection for one memory is serialized with Python's own protocol, so native
and the still-live Python writers cannot overwrite each other. Every
post-commit projection and reconciliation of a memory holds the session-level
`pg_advisory_lock` on Python's per-memory key (the UUID's low 64 bits read as
a signed integer, as `memories_crud.py::_memory_lock_key` computes it) across
its I/O without an idle transaction, as
`secondary_reconciliation.memory_row_session` does. Before any I/O it re-reads
the row `FOR SHARE` and stops when the row no longer matches the scheduled
snapshot (`_load_active_snapshot`). It clears `vector_needs_reindex` only
through the conditional update that matches content, project, global flag,
memory type and a live row. Create keeps Python's transaction-scoped advisory
locks over the superseded and final ids. Python runs every post-commit write
through `_reconcile_active_snapshot`, and native does the same. Handlers are
`create_memory`, `update_memory`, `delete_memory`, `restore_memory`,
`promote_memory`, `demote_memory` and `move_memory` in
`src/gobby/servers/routes/memory.py::create_memory_router`, with request
models `MemoryCreateRequest`, `MemoryUpdateRequest`, `MemoryMoveRequest` and
`MemoryPromoteRequest`. This leaf adds the seven 2.2 redaction entries.

**Granularity:** Seven HTTP rows share one write path: transaction, ledger,
fence, lease, vector projection, graph enqueue and due-marking. Splitting by
route would duplicate that path; maintenance, graph jobs and the seam are
separate deliverables.

**Acceptance:**

- 2.2.1 - `HTTP POST /api/memories` matches row, vector, crossref, graph
  enqueue and ledger effects, and validation failures write nothing.
  test: `crates/gmemory/tests/writes.rs::create_matches_python`.
- 2.2.2 - `HTTP PUT /api/memories/{memory_id}` matches content, tag and type
  updates with re-embedding and ledger rows.
  test: `crates/gmemory/tests/writes.rs::update_matches_python`.
- 2.2.3 - `HTTP DELETE /api/memories/{memory_id}` matches soft deletion,
  vector removal, tombstone change rows and foreign-project refusal.
  test: `crates/gmemory/tests/writes.rs::delete_matches_python`.
- 2.2.4 - `HTTP POST /api/memories/{memory_id}/restore` matches restoration
  and index re-projection. test: `crates/gmemory/tests/writes.rs::restore_matches_python`.
- 2.2.5 - `HTTP POST /api/memories/{memory_id}/promote` matches global
  promotion and scope re-projection. test: `crates/gmemory/tests/writes.rs::promote_matches_python`.
- 2.2.6 - `HTTP POST /api/memories/{memory_id}/demote` matches demotion to
  the current project. test: `crates/gmemory/tests/writes.rs::demote_matches_python`.
- 2.2.7 - `HTTP POST /api/memories/{memory_id}/move` matches the project move
  and its index effects. test: `crates/gmemory/tests/writes.rs::move_matches_python`.
- 2.2.8 - Supersedes, auto-supersede at 0.9, `similar_existing`, ephemeral
  notes, rationale and content caps and provenance match Python's create
  policy. test: `crates/gmemory/tests/writes.rs::create_policy_matches_python`.
- 2.2.9 - A held purge fence blocks vector writes, a lost lease refuses them,
  an active switch journal dual-writes, and a completed switch re-targets the
  writer. test: `crates/gmemory/tests/writes.rs::fence_lease_and_dual_write`.
- 2.2.10 - The seven rows return safe detail for injected faults under their
  exact Waived citations; a fault before commit leaves the row, ledger and
  derived stores unchanged, and a fault after commit leaves the committed row
  with `vector_needs_reindex` true and its change row, with no untracked
  derived-store write.
  test: `crates/gmemory/tests/writes.rs::unexpected_write_errors_are_safe_and_waived`.
- 2.2.11 - With the lease fenced or Qdrant failing, create and update commit
  the row with `vector_needs_reindex` true and its change row and return
  Python's success response without a vector write; no vector write happens
  until a successor lease serves, after which the next write projects
  normally and the earlier row keeps its flag for 2.3's repair.
  test: `crates/gmemory/tests/writes.rs::fenced_write_keeps_repair_intent_until_successor`.
- 2.2.12 - With projection I/O paused, an update, delete or move of the same
  memory by native and, separately, by a Python writer leaves the paused
  stale projection unable to overwrite the newer vector or scope, resurrect a
  deleted point or clear the newer flag; the final state is the newest
  row's projection or its durable repair intent.
  test: `crates/gmemory/tests/writes.rs::stale_projection_loses_to_newer_writes`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test writes`
against the isolated database, run-unique collections and graphs, the
embedding stub and #25059's fence.

### 2.3 Index maintenance and the shared job slot [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/maintenance.rs`
- `crates/gmemory/src/jobs.rs`
- `crates/gcore/src/qdrant.rs::*` — scope-reason: add one id-scrolling function for reconciliation beside the 1.3 retrieval function
- `crates/gmemory/src/http/mod.rs`
- `crates/gmemory/src/http/maintenance.rs`
- `crates/gmemory/tests/maintenance.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append this leaf's waiver entries to the shared manifest array

**Research context:** Port
`src/gobby/memory/services/indexing.py::IndexingService`
(`reconcile_stores`, `reindex_embeddings`, `clear_indices`,
`rebuild_indices`, `invalidate_all`, `fetch_all_project_memories`),
`src/gobby/memory/services/secondary_reconciliation.py`,
`src/gobby/memory/services/projection_repair.py::ProjectionScopeRepairService.repair`,
`src/gobby/memory/vectorstore_maintenance.py` and
`src/gobby/storage/memories_vector_reindex.py`. Reconciliation scrolls Qdrant
point IDs, which gcore cannot do today, so this leaf adds that one function.
Handlers are `invalidate_memories`, `reconcile_memory_stores`,
`rebuild_crossrefs` (with `_fetch_all_memories`, batch 500) and
`reindex_embeddings` in `create_memory_router`.

`crates/gmemory/src/jobs.rs` ports the single in-process job slot
(`_begin_rebuild`, `_update_rebuild_state`, `_get_rebuild_state`,
`_rebuild_snapshot`, guarded by one start lock). Invalidate's background
rebuild becomes a job of kind `invalidate` in that slot
(`memory.http-invalidate.tracked-rebuild`): while a job runs, invalidate
clears nothing and returns 202 with the running snapshot and
`already_running: true`; otherwise it clears, starts the job and returns the
clear report with `rebuild_status: "started"`. Crossref rebuild adds
`crossrefs_failed` (`memory.http-crossrefs-rebuild.reported-failures`). This
leaf adds those two entries and the four 2.3 redaction entries. It also
supplies 2.1's projection-repair callback, which runs the applied
`reconcile_stores` once after the lease is reacquired, as Python's
`_request_memory_projection_repair` does; a repair failure is logged and the
successor lease keeps serving.

**Acceptance:**

- 2.3.1 - `HTTP POST /api/memories/invalidate` matches the clear report and
  starts a tracked rebuild; a running job refuses to clear with the
  coalesced 202. test: `crates/gmemory/tests/maintenance.rs::invalidate_is_tracked_and_waived`.
- 2.3.2 - `HTTP POST /api/memories/reconcile` matches dry-run and applied
  reconciliation reports and effects.
  test: `crates/gmemory/tests/maintenance.rs::reconcile_matches_python`.
- 2.3.3 - `HTTP POST /api/memories/crossrefs/rebuild` matches processed and
  created counts and reports per-memory failures in `crossrefs_failed`.
  test: `crates/gmemory/tests/maintenance.rs::crossref_rebuild_reports_failures`.
- 2.3.4 - `HTTP POST /api/memories/embeddings/reindex` matches the reindex
  report, flag clearing and vector effects.
  test: `crates/gmemory/tests/maintenance.rs::reindex_matches_python`.
- 2.3.5 - The four rows return safe detail for injected faults under their
  exact Waived citations. test: `crates/gmemory/tests/maintenance.rs::unexpected_maintenance_errors_are_safe_and_waived`.
- 2.3.6 - After reacquisition, the projection-repair callback writes vectors,
  graph state and crossrefs for rows left with `vector_needs_reindex` true
  and clears each flag only once that row converges; a repair failure leaves
  the flags set and serving continues.
  test: `crates/gmemory/tests/maintenance.rs::projection_repair_clears_flags_after_reacquire`.
- 2.3.7 - A reconciliation paused after its snapshot check, then raced by a
  native or Python update, delete or move, neither clears the newer row's
  flag nor writes a stale vector, graph or crossref state; the newer row
  stays flagged until its own reconciliation converges.
  test: `crates/gmemory/tests/maintenance.rs::stale_reconciliation_keeps_newer_repair_intent`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test maintenance`;
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core -E 'test(qdrant)'`.

### 2.4 Knowledge-graph writes, extraction and rebuild jobs [category: code] (depends: 2.3, #25097)
`kind: deliverable`

Targets:
- `crates/gmemory/Cargo.toml`
- `Cargo.lock`
- `crates/gai/src/lib.rs`
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/graph/mod.rs`
- `crates/gmemory/src/graph/writer.rs`
- `crates/gmemory/src/graph/extraction.rs`
- `crates/gmemory/src/graph/densify.rs`
- `crates/gmemory/src/prompts.rs`
- `crates/gmemory/src/jobs.rs`
- `crates/gmemory/src/http/mod.rs`
- `crates/gmemory/src/http/graph_jobs.rs`
- `crates/gmemory/tests/graph_writes.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append this leaf's waiver entries to the shared manifest array

**Research context:** Decision 6: add `gobby-ai` as a path dependency and one
re-export of #25097's text-generation service in `crates/gai/src/lib.rs`.
Port `src/gobby/memory/services/knowledge_graph/service.py::KnowledgeGraphService`,
`writer.py`, `extraction.py` (structured output through
`src/gobby/memory/generation_schemas.py`), `rebuild.py`, `maintenance.py`,
`densify.py` (co-occurrence densification from stored entity embeddings,
`materialize_cooccurrence`), `code_linker.py` (`code_link_min_score`) and
`normalization.py`. Extraction resolves the `memory.kg` profile and
candidates through the AI service; `memory.kg.max_rebuild_concurrency`
bounds a rebuild. `crates/gmemory/src/prompts.rs` implements Decision 12:
it loads the `prompts` record for a path with
`src/gobby/prompts/loader.py::PromptLoader.load`'s scope precedence and
renders it with strict undefined variables, as `PromptLoader.render` does,
for the `memory/extract_entities`, `memory/extract_relations`,
`memory/delete_relations` and `memory/dream` templates. Graph extraction
loads with no project, so only global and bundled records apply, as
`src/gobby/memory/manager.py` builds its loader; dream planning passes the
run's project, so project, global and bundled records apply in that order.
The template's metadata defaults
(`src/gobby/prompts/models.py::PromptTemplate.get_default_context`, `None`
for a declared variable without one) merge first, and call variables replace
them. Values render as Python's `str()` does, including `None`, `True`,
`False` and floats such as `1.0`; a default of another type is refused as
unsupported. `default('literal')` applies only to a variable absent from the
merged context, and one trailing newline is dropped, as Jinja's defaults do.
A rebuild resolves and parses all three graph templates before it marks any
memory pending and refuses the whole job on an unsupported one (Debt not to
port). `extract_memory_graph` parses them before its first model call; the
sessions family (#21562) owns waiving its queue loop's matching per-memory
failure. Writes run inside #25059's fence, per project or under the
global writer for global memories, as `service.py` lines 157-160 and 662-703
do. Handlers are `rebuild_knowledge_graph` (foreground, or background with
`background=true`), `rebuild_knowledge_graph_status` and
`clear_knowledge_graph` in `create_memory_router`, with
`_execute_graph_rebuild` progress callbacks recorded into 2.3's job slot.
Expose `extract_memory_graph(memory)` for the sessions family's queue loop
(Overview non-goal). This leaf adds the three 2.4 redaction entries and the
two 2.4 unsupported-prompt-syntax entries. The status redaction replaces an
internal-class `error` with `Internal server error` and logs the diagnostic;
the validation-class prompt refusal keeps its text.

**Granularity:** Three HTTP rows and the densify service share one graph
writer, the extraction client and the job slot. Reclustering adds a new
dependency and is split into 2.5.

**Acceptance:**

- 2.4.1 - `HTTP POST /api/memories/graph/rebuild` matches foreground and
  background rebuilds, coalescing onto a running job, and FalkorDB effects.
  test: `crates/gmemory/tests/graph_writes.rs::graph_rebuild_matches_python`.
- 2.4.2 - `HTTP GET /api/memories/graph/rebuild/status` matches idle,
  running, completed and failed snapshots, with failure text redacted under
  its exact waiver. test: `crates/gmemory/tests/graph_writes.rs::rebuild_status_matches_python`.
- 2.4.3 - `HTTP POST /api/memories/graph/clear` matches the clear report and
  graph effects per project. test: `crates/gmemory/tests/graph_writes.rs::graph_clear_matches_python`.
- 2.4.4 - Extraction, writer, densify and code-link effects match Python for
  a fixture project, using the stubbed `memory.kg` candidates.
  test: `crates/gmemory/tests/graph_writes.rs::extraction_and_densify_match_python`.
- 2.4.5 - The four bundled memory prompts, a global override of a graph
  template and a project override of `memory/dream` render byte-identically
  to Python, including metadata defaults that call variables replace, a
  `None` default, a `1.0` float and the dropped trailing newline. A project
  override of a graph template is ignored and the global or bundled record
  renders. An undefined variable and an override using unsupported template
  syntax fail with a typed error naming the path and call no model.
  test: `crates/gmemory/tests/graph_writes.rs::prompt_rendering_matches_python`.
- 2.4.6 - Injected faults return safe detail on the three rows under their
  exact Waived citations. test: `crates/gmemory/tests/graph_writes.rs::unexpected_graph_errors_are_safe_and_waived`.
- 2.4.7 - With a global `{% if %}` override of `memory/extract_relations`,
  a foreground `HTTP POST /api/memories/graph/rebuild` returns the
  validation-class error naming the path, and a background job's
  `HTTP GET /api/memories/graph/rebuild/status` ends `failed` with that text
  unredacted, under their exact waivers. No memory's graph state changes and
  the generation stub receives no request.
  test: `crates/gmemory/tests/graph_writes.rs::unsupported_graph_prompt_refuses_rebuild`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test graph_writes`
against a run-unique FalkorDB graph and the generation stub;
`cargo check -p gobby-ai`.

### 2.5 HDBSCAN entity reclustering [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gmemory/Cargo.toml`
- `Cargo.lock`
- `crates/gmemory/src/graph/mod.rs`
- `crates/gmemory/src/graph/clustering.rs`
- `crates/gmemory/tests/clustering.rs`

**Research context:** Decision 2. Port
`src/gobby/memory/services/knowledge_graph/clustering.py`: load entity
embeddings, run HDBSCAN with `HDBSCAN_METRIC` euclidean,
`HDBSCAN_CLUSTER_SELECTION_METHOD` eom, `memory.cluster_min_cluster_size`
(default 5) and `memory.cluster_min_samples` (default 2, where none delegates
to the minimum cluster size), relabel clusters in Python's stable order,
compute the silhouette score over clustered points by hand, and persist
`_Entity.cluster_id` (noise -1). Add `hdbscan = "=0.12.0"` with default
features to `crates/gmemory/Cargo.toml` and commit the resolution in
`Cargo.lock`, which adds exactly `hdbscan` 0.12.0 and `kdtree` 0.7.0. Build
the parameters with `HdbscanHyperParams::builder()`, `.min_cluster_size`,
`.min_samples` (omitted when the setting is none, so the crate's default of
the minimum cluster size applies) and the default euclidean metric, then call
`Hdbscan::new(&data, params).cluster()`. The crate sorts distances with a
panicking float comparison, so keep Python's `_partition_valid_vectors` and
`_coerce_unit_vector` steps: empty, non-numeric, non-finite or zero-norm
vectors and vectors whose dimension differs from the most common one are
labelled noise and listed as invalid, and the rest are unit-normalised before
they reach the crate. Run the call in `spawn_blocking`. Rust-version evidence against the
workspace's `rust-version = "1.88"`: neither `hdbscan` 0.12.0 nor `kdtree`
0.7.0 declares a `rust-version`, both use edition 2021, and `hdbscan` 0.12.0
was published 2025-11-24, after Rust 1.88, so 2.5.3 builds both crates with
the 1.88.0 toolchain rather than trusting the missing field. Writes run inside
the project fence. 1.2's reader consumes the labels.

**Acceptance:**

- 2.5.1 - On fixture embeddings the labels, noise set, cluster sizes and
  silhouette score match scikit-learn's output, up to the relabelling Python
  applies. test: `crates/gmemory/tests/clustering.rs::hdbscan_matches_sklearn_fixture`.
- 2.5.2 - Persisted `cluster_id` values and the result summary match Python,
  and 1.2's cluster expansion reads them.
  test: `crates/gmemory/tests/clustering.rs::recluster_persists_labels`.
- 2.5.3 - `hdbscan` is pinned to `=0.12.0`, the lockfile gains exactly
  `hdbscan` 0.12.0 and `kdtree` 0.7.0, and both crates compile with the Rust
  1.88.0 toolchain.
  behavior: "hdbscan pinned to =0.12.0 and building on Rust 1.88" in `crates/gmemory/Cargo.toml`.
- 2.5.4 - Entities with empty, non-numeric, non-finite or zero-norm vectors,
  or a dimension other than the most common one, are labelled noise and
  listed as invalid without reaching the crate, as in Python.
  test: `crates/gmemory/tests/clustering.rs::invalid_vectors_are_noise`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test clustering`;
`cargo +1.88.0 check -p hdbscan -p kdtree` (install the toolchain with
`rustup toolchain install 1.88.0` when absent);
the fixture embeddings and expected labels are generated once from Python's
scikit-learn run and committed with the test.

## P3: Memories collection rebuild seam (#25070)
`kind: framing`

### 3.1 Native memories rebuild and project_change seam [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gcore/src/embedding_generation.rs`
- `crates/gcore/src/embedding_generation/seam.rs`
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/rebuild_seam.rs`
- `crates/gmemory/tests/rebuild_seam.rs`

**Research context:** #25070's contract, as the AI plan's 7.4 consumes it.
`rebuild(collection_kind = memories, physical_name, embedder)` fills the named
physical collection with one vector per live memory from the target
embedder, under the per-project write fence, as Python does with
`LocalMemoryManager.list_memories(100)` pages and `VectorStore.batch_upsert`
into `memories@<run_id>` (`src/gobby/memory/collection_names.py`).
`project_change(collection_kind = memories, source_id, physical_name,
embedder)` re-reads the source memory authoritatively and upserts its point,
or deletes the point for a tombstone. The switch, not this seam, owns the
ledger read, watermark and replay
(`src/gobby/ai/embedding_switch_runner.py::EmbeddingSwitchRunner.build` and
`::_replay_projection_changes`). Producers keep appending ledger rows and
dual-writing through 2.2.

Composition (Orchestrator ruling on MEM-ADV-01, option A): gmemory depends on
gai (2.4), so gai must never depend on gmemory. This leaf adds
`crates/gcore/src/embedding_generation/seam.rs`, declared from
`crates/gcore/src/embedding_generation.rs`, with
`pub trait CollectionRebuildSeam: Send + Sync` carrying
`rebuild(physical_name, &EmbeddingConfig)` and
`project_change(source_id, physical_name, &EmbeddingConfig)`; the embedder is
gcore's existing `config::EmbeddingConfig`, the switch's target binding.
gmemory exports `MemoriesRebuildSeam`, built from the hub pool, implementing
it for the `memories` kind, and #25071 implements the same trait for tool
embeddings. The AI plan's 7.4 runner holds one `Arc<dyn
CollectionRebuildSeam>` per kind, its test stubs implement the same trait,
and its 7.6 composes the real seams in gdaemon's serve path; the Orchestrator
records those AI 7.4 and 7.6 amendments as it did Decision 4's. The crate
graph stays acyclic: gai and gmemory depend on gcore, gmemory depends on gai,
and gdaemon depends on all three.

**Acceptance:**

- 3.1.1 - `rebuild` fills the named collection with one point per live
  memory from the target embedder, under the fence, and Python's
  `batch_upsert` rebuild yields the same point set for a fixture project.
  test: `crates/gmemory/tests/rebuild_seam.rs::rebuild_matches_python_point_set`.
- 3.1.2 - `project_change` upserts a changed memory's point from the
  authoritative row and deletes the point for a tombstone.
  test: `crates/gmemory/tests/rebuild_seam.rs::project_change_upserts_and_deletes`.
- 3.1.3 - Native create, update and delete during an armed switch append
  change rows and dual-write to the named physical target.
  test: `crates/gmemory/tests/rebuild_seam.rs::producers_dual_write_while_armed`.
- 3.1.4 - `rebuild` and `project_change` called through an
  `Arc<dyn CollectionRebuildSeam>` reach the real `MemoriesRebuildSeam`, and
  `cargo tree -p gobby-ai --edges normal` lists no `gobby-memory`.
  test: `crates/gmemory/tests/rebuild_seam.rs::seam_trait_object_reaches_memories_and_stays_acyclic`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test rebuild_seam`
against run-unique physical collections and the embedding stub.

## P4: Hub-only dream consolidation
`kind: framing`

### 4.1 Dream store, journal, apply and revert [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/dream/mod.rs`
- `crates/gmemory/src/dream/store.rs`
- `crates/gmemory/src/dream/journal.rs`
- `crates/gmemory/src/dream/apply.rs`
- `crates/gmemory/tests/dream_apply.rs`

**Research context:** Port `src/gobby/memory/dream/storage_runs.py`
(`DreamAdmission`, run creation, status transitions over
`memory_dream_runs.status`: started, running, completed, failed, reverted,
revert_failed, revert_forfeited, interrupted, partial), `storage_journal.py`,
`storage_actions.py` (snapshots in `memory_dream_snapshots`, with
`append_change` for each memory mutation),
`src/gobby/storage/memories_dreams.py`, `truth_digest.py`
(`memory_dream_truth_state`), `apply.py::apply_dream_plan` and
`::revert_dream_run` (keep, delete, refresh, review and promote actions;
fenced actions; cursor advance; reconcile after apply and revert), `plan.py`
validation, `decisions.py`, `models.py` and `options.py`. Expose
`purge_dream_hidden_memories` with `purge_delete_after_days` and
`purge_review_after_days` for the sessions family's loop (Overview non-goal).
Every memory mutation goes through 2.2's write path, so ledger, fence and lease
rules hold.

Port the stale-plan fences exactly. `apply_candidate_action` updates a
candidate only while its `dream_due_version`, `updated_at`, `project_id` and
`is_global` still equal the values captured at selection and the row is
live; otherwise the action is recorded as skipped with zero mutation.
`revert_snapshot` locks the row, reports `missing` for an absent row and
`conflict` when any fence column (`deleted_at`, `project_id`, `is_global`
and the action-owned columns) differs from the snapshot's after-state. It
restores only the action-owned columns (refresh: `content` and `tags`;
promote: `is_global`; review and delete: `deleted_at` and `dream_action`),
preserves every other later change, clears `last_dreamed_at`, increments
`dream_due_version`, sets `vector_needs_reindex`, restores crossrefs and
appends a projection change in that transaction, and reconciles projections
only after commit.

**Acceptance:**

- 4.1.1 - Run creation, admission conflicts, status transitions and
  checkpoints match Python's rows.
  test: `crates/gmemory/tests/dream_apply.rs::run_store_matches_python`.
- 4.1.2 - Applying a validated plan matches Python's memory, snapshot,
  ledger and reconcile effects for each action, and an invalid plan writes
  nothing. test: `crates/gmemory/tests/dream_apply.rs::apply_matches_python`.
- 4.1.3 - Revert matches Python for reverted, already reverted, forfeited and
  failed runs, including conflict and missing-row outcomes.
  test: `crates/gmemory/tests/dream_apply.rs::revert_matches_python`.
- 4.1.4 - The purge function deletes only hidden memories past their
  retention. test: `crates/gmemory/tests/dream_apply.rs::purge_matches_python`.
- 4.1.5 - A candidate changed after selection is skipped with zero mutation;
  revert reports a conflict when an action-owned or fence column changed,
  otherwise restores only action-owned columns, keeps unrelated later
  changes, increments `dream_due_version`, and reconciles projections only
  after the row transition commits.
  test: `crates/gmemory/tests/dream_apply.rs::stale_plan_and_revert_fences`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test dream_apply`
against the isolated database.

### 4.2 Dream planning, related evidence and sweep orchestration [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gmemory/src/dream/mod.rs`
- `crates/gmemory/src/dream/planner.rs`
- `crates/gmemory/src/dream/related.rs`
- `crates/gmemory/src/dream/orchestrator.rs`
- `crates/gmemory/tests/dream_sweep.rs`

**Research context:** Port `src/gobby/memory/dream/planner.py::build_raw_plan`
(LLM planning through Decision 6's service with the `memory.dream` profile,
`planner_batch_size`, `planner_batch_max_chars`, `max_tokens`, the prompt at
`prompt_path` `memory/dream`, and the confidence floors
`min_action_confidence`, `min_delete_confidence` and
`min_rescope_confidence`), `candidates.py::list_sweep_candidates`
(`redream_after_hours`, `include_global_memories`,
`dry_run_max_candidates`), `related.py::RelatedEvidenceSession` (vector and
keyword evidence with `related_evidence_top_k`,
`related_evidence_fetch_limit`, `evidence_channel_timeout_seconds`,
`evidence_phase_timeout_seconds` and `evidence_retry_attempts`) and
`orchestrator.py::DreamSweepOrchestrator` (`run_sweep`, `run_unit`,
`finalize_sweep`, `run_dry_run`, `run_inventory`, `SweepTotals`,
`work_unit_timeout_seconds`, `max_runtime_seconds`). The planner renders
`memory/dream` through 2.4's `crates/gmemory/src/prompts.rs` with the
variables `truth_digest`, `candidates`, `previous_summary` and the three
confidence floors, as `planner.py` passes them. Each page renders before its
model call, and the first page renders before any action applies. A missing
record stays isolated to its page, as Python's `FileNotFoundError` is an
`OSError` in `_EXPECTED_PLANNER_ERRORS`. An undefined variable or an
unsupported override fails the run, as Python's Jinja errors escape that
tuple (Debt not to port).

**Acceptance:**

- 4.2.1 - Candidate selection, batching and the plan request match Python for
  a fixture scope, and the stubbed plan is validated with Python's floors.
  test: `crates/gmemory/tests/dream_sweep.rs::planning_matches_python`.
- 4.2.2 - Related evidence matches Python's hits, timeouts and retries.
  test: `crates/gmemory/tests/dream_sweep.rs::related_evidence_matches_python`.
- 4.2.3 - Sweeps, work units, dry runs, inventory runs, checkpoints and
  runtime limits match Python's totals and stored plan.
  test: `crates/gmemory/tests/dream_sweep.rs::sweep_orchestration_matches_python`.
- 4.2.4 - A missing `memory/dream` record is isolated to its page as in
  Python, while an undefined variable or an unsupported project override
  fails the run with no generation request and no applied action.
  test: `crates/gmemory/tests/dream_sweep.rs::prompt_errors_match_python_isolation`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test dream_sweep`
against the generation stub and the isolated database.

### 4.3 Dream service, aggregate runs, reports and routes [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gmemory/src/dream/mod.rs`
- `crates/gmemory/src/dream/service.rs`
- `crates/gmemory/src/dream/aggregate.rs`
- `crates/gmemory/src/dream/report.rs`
- `crates/gmemory/src/http/mod.rs`
- `crates/gmemory/src/http/dream.rs`
- `crates/gmemory/tests/dream_routes.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append this leaf's waiver entries to the shared manifest array

**Research context:** Port `src/gobby/memory/dream/service.py::MemoryDreamService`
(`run`, `start`, `start_async`, `run_all_due_projects`,
`start_all_due_projects_async`, `execute_run`, `status`, `revert`,
`record_run_failure`, the per-scope `_execution_lock`, `run_memory_dream` and
`_admission_payload`), `aggregate.py` (round-robin over due projects for an
unscoped trigger), `coordinator.py::MemoryDreamCoordinator` (`trigger`,
`trigger_all_due_projects`, background task ownership, `aclose`) and
`report.py` (`report_path`, `render_report`, `previous_narrative`). Routes are
`memory_dream`, `memory_dream_status` and `memory_dream_revert` in
`src/gobby/servers/routes/memory_dream.py::create_memory_dream_router`, with
`_http_status` kept (Debt KEEP). Revert keeps 404 for an unknown run and
returns 409 with the same `detail` for `revert_forfeited` and `revert_failed`
(`memory.http-dream-revert.state-conflict-status`). Run status reports a run
that 4.2 failed on an unsupported prompt override as `failed` with the
validation-class error (`memory.http-dream-status.unsupported-prompt-syntax`).

`MemoryDreamService::new` takes the hub `Pool` by value (Decision 5) and is
the constructor #25024 binds; it also exposes
`reconcile_interrupted_dream_runs`, as
`src/gobby/memory/dream/cron.py::reconcile_interrupted_dream_runs` does for
the cron registration. This plan registers no schedule.

**Acceptance:**

- 4.3.1 - `HTTP POST /memory/dream` matches 202 admission, 200 coalescing,
  409 conflict, 400 invalid options, 500 failed launch and 503 unavailable
  coordinator, for scoped and unscoped triggers.
  test: `crates/gmemory/tests/dream_routes.rs::dream_trigger_matches_python`.
- 4.3.2 - `HTTP GET /memory/dream/{run_id}` matches run payloads and the
  unknown-run 404. test: `crates/gmemory/tests/dream_routes.rs::dream_status_matches_python`.
- 4.3.3 - `HTTP POST /memory/dream/{run_id}/revert` matches successful and
  already-reverted results and returns 409 for forfeited and failed reverts
  under its exact waiver. test: `crates/gmemory/tests/dream_routes.rs::dream_revert_status_is_waived`.
- 4.3.4 - Aggregate runs, the per-scope execution lock, reports and
  interrupted-run reconciliation match Python; the service cannot be built
  without a hub pool.
  test: `crates/gmemory/tests/dream_routes.rs::aggregate_reports_and_hub_only_construction`.
- 4.3.5 - With a project `{% if %}` override of `memory/dream`,
  `HTTP GET /memory/dream/{run_id}` reports the run `failed` with the error
  naming the path under its exact waiver, where Python's baseline completes.
  test: `crates/gmemory/tests/dream_routes.rs::unsupported_dream_prompt_status_is_waived`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test dream_routes`
against the generation stub, the isolated database and a temporary project
root for reports.

## P5: Review-learning service
`kind: framing`

### 5.1 Native review-learning service over memory rows [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/review/mod.rs`
- `crates/gmemory/src/review/lessons.rs`
- `crates/gmemory/src/review/recall.rs`
- `crates/gmemory/tests/review_learning.rs`

**Research context:** Port
`src/gobby/review_learning/service.py::ReviewLearningService`
(`recall_context`, `recall_review_lessons_for_files`,
`recall_review_lessons_by_class`, `list_check_keys`,
`retire_review_lesson`, `record`, `_search_recall_matches`,
`_candidate_lesson_memories` and the scope resolvers `_resolve_scope_sync`
and `_resolve_explicit_scope_sync`, which read the session row) and its
module functions (`build_recall_queries`, `rank_recall_matches`,
`_validate_recorded_finding`, `_missing_qa_rejection_fields`,
`_build_file_lesson`, `_parse_lesson_content`), plus `lessons.py`
(`derive_lesson_identity`, `normalize_lesson`, `build_tags`,
`render_lesson_content`, the source-kind, decision, guardrail and risk
vocabularies, `UNSCOPED_SCOPE_TAG`), `class_recall.py`, `file_paths.py`,
`fingerprint.py` and `guidance.py`. Lessons are `memories` rows carrying
lesson tags; recording and retirement go through 2.2's write path and 1.3's
search. `record` serializes per pattern under
`ReviewLearningPatternMutation`'s key
`review_learning_pattern:{project_id}:{pattern_key}` (Decision 7), taken as a
transaction advisory lock exactly as
`src/gobby/storage/hub/postgres_pool.py::advisory_lock_keys` derives it, so
Python and native writers exclude each other.

**Acceptance:**

- 5.1.1 - Recording matches Python's identity, normalization, tags,
  rendered content, deduplication and validation refusals, and a concurrent
  Python writer on the same pattern serializes with the native one.
  test: `crates/gmemory/tests/review_learning.rs::record_matches_python`.
- 5.1.2 - Context, file and class recall match Python's queries, ranking,
  tiers and scope resolution. test: `crates/gmemory/tests/review_learning.rs::recall_matches_python`.
- 5.1.3 - Check-key listing and retirement match Python's results and
  effects. test: `crates/gmemory/tests/review_learning.rs::check_keys_and_retire_match_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test review_learning`
against the isolated database, run-unique collections and the embedding stub.

## P6: Native internal-tool families
`kind: framing`

### 6.1 Memory session-variable writer [category: code] (depends: 5.1, #25262)
`kind: deliverable`

Targets:
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/session_vars.rs`
- `crates/gmemory/tests/session_vars.rs`

**Research context:** Decision 3. The writer touches exactly three keys of
the `session_variables` row: `accessed_memory_ids`
(`src/gobby/mcp_proxy/tools/memory_session.py::ACCESSED_MEMORY_IDS_VARIABLE`,
bounded by `src/gobby/mcp_proxy/tools/memory.py`'s `_ACCESSED_MEMORY_IDS_MAX`
of 1000, identity the whole `{memory_id, task_id}` record as
`_record_accessed_memory` passes it), `_memory_task_review_records`
(`REVIEW_RECORDS_VARIABLE`, bounded at 50, identity `closure_id`) and
`_memory_review_stop_delivered` (`REVIEW_DELIVERED_VARIABLE`, set true).
Semantics match `src/gobby/workflows/state_manager.py::SessionVariableManager.upsert_bounded_list_variable`
(drop a matching item, append, keep the newest `max_items`) and
`::set_variable`, key-scoped so concurrent writers of other keys are
untouched. Each write runs under `SessionVariableMutation`'s explicit key
from #25262, at priority 950, so Python and native writers of one session
serialize. Reads of `_memory_pending_task_reviews` reuse the same row read.
No other key is writable through this module.

**Acceptance:**

- 6.1.1 - Bounded-list upserts and the boolean set produce Python's rows for
  all three keys, including identity replacement and trimming.
  test: `crates/gmemory/tests/session_vars.rs::writes_match_python`.
- 6.1.2 - Concurrent Python and native writers on one session serialize
  under the shared key without losing either update, and writes to other
  keys are preserved. test: `crates/gmemory/tests/session_vars.rs::shared_lock_serializes_writers`.
- 6.1.3 - Any key outside the three is refused before a write.
  test: `crates/gmemory/tests/session_vars.rs::only_owned_keys_are_writable`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test session_vars`
against the isolated database, with a Python writer started by the test.

### 6.2 gobby-memory descriptor, schemas and MCP behavior [category: code] (depends: 6.1, #24883)
`kind: deliverable`

Targets:
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/mcp/mod.rs`
- `crates/gmemory/src/mcp/memory_tools.rs`
- `crates/gmemory/src/mcp/memory_requests.rs`
- `crates/gmemory/src/backup.rs`
- `crates/gmemory/tests/mcp_memory.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append this leaf's waiver entries to the shared manifest array

**Research context:** `src/gobby/mcp_proxy/tools/memory.py::create_memory_registry`
registers the 26 tools from `memory.py`, `memory_write.py`,
`memory_review.py`, `memory_surface.py` and `memory_dream.py`. Their exact
input schema, description, brief projection and hash come from 0.1's corpus.
Publish one `ToolFamily` returning the `gobby-memory` `ToolServer` with
#24883's landed `crates/gcore/src/mcp.rs` contract; request types derive
schemas normalized to JSON-object equality with Python. Handlers call the
services the earlier leaves built and take caller project and session from
the handler context the shared gateway projects, as
`src/gobby/mcp_proxy/tools/memory_scope.py::get_current_project_id`,
`::memory_owned_by_current_project` and `::resolve_current_memory_id` and
`memory_session.py::resolve_session` and `::resolve_claimed_task_id` resolve
them; a missing project falls back to the personal project as
`list_memories` does.

Tool specifics. `search_memories` uses caller
`mcp_proxy.memory.search_memories`. `surface_memories` uses
`SURFACE_MIN_SCORE` 0.65, `SURFACE_LIMIT` 5, `MAX_QUERY_CHARS` 2000,
`SURFACE_CALLER` `memory.surface`, excludes
`src/gobby/memory/push_exclusion.py::PUSH_EXCLUDED_TAGS`, falls back to the
session's last assistant message for a `turn` trigger whose text fails
`src/gobby/memory/prompt_triage.py::is_substantive_prompt`, returns
`reshow_after_injections`, and returns the empty result on any failure.
`get_memory` records access (`access_debounce_seconds`) and appends to
`accessed_memory_ids` through 6.1. `review_task_memories` ports
`src/gobby/mcp_proxy/tools/memory_review.py` (`_resolve_task`,
`_accessed_ids`, `_record_review`, `_CANDIDATE_LIMIT` 5, caller
`mcp_proxy.memory.review_task_memories`) and writes through 6.1. The dream
tools call 4.3's service; `memory_dream_decisions` reads 4.1's decisions.
`recluster_knowledge_graph_entities` calls 2.5, `densify_knowledge_graph_cooccurrence`
and `rebuild_knowledge_graph` call 2.4, and `rebuild_crossrefs` (with its
`limit`, default 500) and `reindex_embeddings` call 2.3.

`crates/gmemory/src/backup.rs` ports `src/gobby/sync/memories.py`
(`MemoryBackupManager.backup`, `::restore` with timestamp-wins outcomes,
`_validate_memory_record`, `_load_memory_backup`, `_jsonl_memory_record`,
`is_ephemeral_implementation_note`) with two write modes: the daemon mode
reconciles restored memories through 2.2 and 2.3, and the database-only mode
7.1 uses writes rows only, as Python's CLI manager does. Paths follow the
DROP above: the current project's
`~/.gobby/backups/<project-uuid>/memories.jsonl`, or an explicit path from
the CLI.

Consume #24970's classification and MCP adapter. Unexpected text becomes
`Internal server error`; deliberate domain errors keep their text. This leaf
adds the 21 gobby-memory redaction entries and
`memory.mcp-rebuild-crossrefs.reported-failures`,
`memory.mcp-backup-memories.project-backup-path`,
`memory.mcp-restore-memories.project-backup-path`,
`memory.mcp-rebuild-knowledge-graph.unsupported-prompt-syntax` and
`memory.mcp-memory-dream-status.unsupported-prompt-syntax`, each with decision
`.gobby/plans/memory-family.md#6.2`. Registering and activating the
descriptor waits for 8.2.

The landed descriptor integration suite is `crates/gcore/tests/mcp_contract.rs`.
As a #24883 consumer, this leaf runs the entire `--test mcp_contract` target;
a name filter is insufficient.

**Granularity:** Twenty-six public-row items, a catalog item and a safe-error
item exercise one descriptor and dispatch adapter over services the earlier
leaves own and test. The backup module is the one service not owned earlier,
because only the two backup tools and the CLI use it. Splitting the adapter
by tool would create per-file wrappers, not independent outcomes.

**Acceptance:**

- 6.2.1 - `MCP gobby-memory:backup_memories` writes the current project's
  backup, or the personal project's when none is current, under its exact
  waiver. test: `crates/gmemory/tests/mcp_memory.rs::backup_memories_uses_project_backup_path`.
- 6.2.2 - `MCP gobby-memory:create_memory` matches the create policy,
  provenance and result fields. test: `crates/gmemory/tests/mcp_memory.rs::create_memory_matches_python`.
- 6.2.3 - `MCP gobby-memory:delete_memory` matches deletion and ownership
  refusals. test: `crates/gmemory/tests/mcp_memory.rs::delete_memory_matches_python`.
- 6.2.4 - `MCP gobby-memory:demote_memory_from_global` matches demotion.
  test: `crates/gmemory/tests/mcp_memory.rs::demote_matches_python`.
- 6.2.5 - `MCP gobby-memory:densify_knowledge_graph_cooccurrence` matches
  densification results. test: `crates/gmemory/tests/mcp_memory.rs::densify_matches_python`.
- 6.2.6 - `MCP gobby-memory:get_memory` matches the payload, access
  recording and the `accessed_memory_ids` record with the claimed task id.
  test: `crates/gmemory/tests/mcp_memory.rs::get_memory_records_access`.
- 6.2.7 - `MCP gobby-memory:get_related_memories` matches crossref results.
  test: `crates/gmemory/tests/mcp_memory.rs::get_related_matches_python`.
- 6.2.8 - `MCP gobby-memory:list_memories` matches filters and the personal
  fallback. test: `crates/gmemory/tests/mcp_memory.rs::list_memories_matches_python`.
- 6.2.9 - `MCP gobby-memory:memory_dream` matches admission and coalescing
  results. test: `crates/gmemory/tests/mcp_memory.rs::memory_dream_matches_python`.
- 6.2.10 - `MCP gobby-memory:memory_dream_decisions` matches decision
  listing. test: `crates/gmemory/tests/mcp_memory.rs::dream_decisions_match_python`.
- 6.2.11 - `MCP gobby-memory:memory_dream_revert` matches revert results.
  test: `crates/gmemory/tests/mcp_memory.rs::dream_revert_matches_python`.
- 6.2.12 - `MCP gobby-memory:memory_dream_status` matches run status, and
  reports a run refused by an unsupported prompt override as `failed` with
  the error naming the path under its exact waiver.
  test: `crates/gmemory/tests/mcp_memory.rs::dream_status_matches_python`.
- 6.2.13 - `MCP gobby-memory:memory_stats` matches statistics.
  test: `crates/gmemory/tests/mcp_memory.rs::memory_stats_matches_python`.
- 6.2.14 - `MCP gobby-memory:move_memory` matches the move and ownership
  checks. test: `crates/gmemory/tests/mcp_memory.rs::move_memory_matches_python`.
- 6.2.15 - `MCP gobby-memory:promote_memory_to_global` matches promotion.
  test: `crates/gmemory/tests/mcp_memory.rs::promote_matches_python`.
- 6.2.16 - `MCP gobby-memory:rebuild_crossrefs` matches the limit and counts
  and reports `crossrefs_failed` under its exact waiver.
  test: `crates/gmemory/tests/mcp_memory.rs::rebuild_crossrefs_reports_failures`.
- 6.2.17 - `MCP gobby-memory:rebuild_knowledge_graph` matches rebuild
  results, and returns the validation-class error naming the path for an
  unsupported graph prompt override under its exact waiver.
  test: `crates/gmemory/tests/mcp_memory.rs::rebuild_knowledge_graph_matches_python`.
- 6.2.18 - `MCP gobby-memory:recluster_knowledge_graph_entities` matches the
  clustering summary. test: `crates/gmemory/tests/mcp_memory.rs::recluster_matches_python`.
- 6.2.19 - `MCP gobby-memory:reindex_embeddings` matches the reindex report.
  test: `crates/gmemory/tests/mcp_memory.rs::reindex_matches_python`.
- 6.2.20 - `MCP gobby-memory:restore_memories` restores from the current
  project's backup file with timestamp-wins outcomes and index
  reconciliation, under its exact waiver.
  test: `crates/gmemory/tests/mcp_memory.rs::restore_memories_uses_project_backup_path`.
- 6.2.21 - `MCP gobby-memory:restore_memory` matches single restoration.
  test: `crates/gmemory/tests/mcp_memory.rs::restore_memory_matches_python`.
- 6.2.22 - `MCP gobby-memory:review_task_memories` matches task resolution,
  candidate search, records and the delivered flag.
  test: `crates/gmemory/tests/mcp_memory.rs::review_task_memories_matches_python`.
- 6.2.23 - `MCP gobby-memory:search_knowledge_graph` matches graph search.
  test: `crates/gmemory/tests/mcp_memory.rs::search_knowledge_graph_matches_python`.
- 6.2.24 - `MCP gobby-memory:search_memories` matches ranking, diagnostics
  and surfaced statistics. test: `crates/gmemory/tests/mcp_memory.rs::search_memories_matches_python`.
- 6.2.25 - `MCP gobby-memory:surface_memories` matches triggers, the
  assistant-message fallback, exclusions and the fail-soft empty result.
  test: `crates/gmemory/tests/mcp_memory.rs::surface_memories_matches_python`.
- 6.2.26 - `MCP gobby-memory:update_memory` matches updates and validation.
  test: `crates/gmemory/tests/mcp_memory.rs::update_memory_matches_python`.
- 6.2.27 - Tool inventory, descriptions, schemas, definition hashes and
  read-only classifications match the frozen Python contract; malformed
  arguments call no service.
  test: `crates/gmemory/tests/mcp_memory.rs::catalog_and_validation_match_python`.
- 6.2.28 - Each of the 21 redaction tools returns safe text for an injected
  fault under its own exact waiver, and deliberate domain errors keep their
  text. A fault before commit leaves the row, ledger and derived stores
  unchanged; a fault after commit leaves the committed row with
  `vector_needs_reindex` true and its change row, with no untracked
  derived-store write, as 2.2 defines.
  test: `crates/gmemory/tests/mcp_memory.rs::tool_internal_errors_are_safe`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test mcp_memory`;
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core --test mcp_contract`.

### 6.3 gobby-review-learning descriptor [category: code] (depends: 6.2)
`kind: deliverable`

Targets:
- `crates/gmemory/src/mcp/mod.rs`
- `crates/gmemory/src/mcp/review_tools.rs`
- `crates/gmemory/tests/mcp_review.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append this leaf's waiver entries to the shared manifest array

**Research context:**
`src/gobby/mcp_proxy/tools/review_learning.py::create_review_learning_registry`
registers six tools over 5.1's service: `recall_review_context`,
`recall_review_lessons_for_files` (with `file_paths` and `file_paths_json`
coercion), `recall_review_lessons_by_class`, `list_check_keys`,
`retire_review_lesson` and `record_review_lesson`. Publish a second
`ToolFamily` returning the `gobby-review-learning` `ToolServer`. The tool
names stay exact, because
`src/gobby/workflows/engine/delivery_formatting.py` and `mcp_injections.py`
key on `("gobby-review-learning", "recall_review_lessons_for_files")` and
`("gobby-review-learning", "recall_review_lessons_by_class")`. Each tool's
`{"success": False, "error": str(exc)}` catch becomes the safe adapter result;
this leaf adds the six review-learning redaction entries.

**Granularity:** Six public-tool items and a catalog item describe one
descriptor and one adapter over 5.1's already-tested service. Splitting by
tool would create per-tool wrappers, not independent outcomes.

**Acceptance:**

- 6.3.1 - `MCP gobby-review-learning:list_check_keys` matches results.
  test: `crates/gmemory/tests/mcp_review.rs::list_check_keys_matches_python`.
- 6.3.2 - `MCP gobby-review-learning:recall_review_context` matches recall.
  test: `crates/gmemory/tests/mcp_review.rs::recall_context_matches_python`.
- 6.3.3 - `MCP gobby-review-learning:recall_review_lessons_by_class` matches
  class recall. test: `crates/gmemory/tests/mcp_review.rs::recall_by_class_matches_python`.
- 6.3.4 - `MCP gobby-review-learning:recall_review_lessons_for_files` matches
  file recall and path coercion. test: `crates/gmemory/tests/mcp_review.rs::recall_for_files_matches_python`.
- 6.3.5 - `MCP gobby-review-learning:record_review_lesson` matches recording
  and refusals. test: `crates/gmemory/tests/mcp_review.rs::record_lesson_matches_python`.
- 6.3.6 - `MCP gobby-review-learning:retire_review_lesson` matches
  retirement. test: `crates/gmemory/tests/mcp_review.rs::retire_lesson_matches_python`.
- 6.3.7 - The catalog matches the frozen contract, and all six tools return
  safe text for injected faults under their exact waivers.
  test: `crates/gmemory/tests/mcp_review.rs::catalog_and_safe_errors_match`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test mcp_review`;
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core --test mcp_contract`.

## P7: Native CLI commands
`kind: framing`

### 7.1 Native command tree and record commands [category: code] (depends: 6.3)
`kind: deliverable`

Targets:
- `crates/gmemory/src/lib.rs`
- `crates/gmemory/src/cli/mod.rs`
- `crates/gmemory/src/cli/records.rs`
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `crates/gmemory/tests/cli_records.rs`

**Research context:** `src/gobby/cli/memory/main.py::memory` assembles the
group. Record commands are `src/gobby/cli/memory/crud.py` (`create`,
`recall`, `delete`, `list_memories`, `show_memory`, `update_memory`,
`memory_stats`, `backfill_unscoped_lessons_command` over
`backfill_unscoped_lessons`), `export.py` (`export_memories` as markdown,
`backup_memories`, `restore_memories`, `_default_backup_path`) and
`maintenance.py` (`dedupe_memories`, `_list_all_memories`), with formatting in
`_formatting.py`. Decision 9: they run 1.1's store, 2.2's database writes
without the vector path, and 6.2's backup module in database-only mode over a
CLI pool built from the bootstrap `database_url`, as
`src/gobby/cli/memory/__init__.py::get_memory_manager` does; a machine without
a database credential fails as `require_cli_database` does. `recall` is
keyword-only with caller `cli.memory.recall`, and `resolve_memory_id`'s
prefix and ambiguity messages are kept. Extend the native `Command` enum and
dispatcher with `memory`; clap parsing and output formatting live in the
family crate. `gdaemon memory` is the internal dispatcher for the existing
public `gobby memory` contract and adds no public surface. Map errors through
#24970's CLI adapter for exit codes and stderr. This leaf edits
`crates/gdaemon/src/main.rs` after the skills plan's 5.1 and the AI plan's
7.5 if those land first (Overview).

**Granularity:** Thirteen ledger rows share one command tree, one CLI pool
and the database-only service mode. The HTTP-client commands are 7.2.

**Acceptance:**

- 7.1.1 - `CLI gobby memory` matches group help and dispatch.
  test: `crates/gmemory/tests/cli_records.rs::memory_group_matches_python`.
- 7.1.2 - `CLI gobby memory create` matches flags, output and the
  database-only effect. test: `crates/gmemory/tests/cli_records.rs::create_matches_python`.
- 7.1.3 - `CLI gobby memory recall` matches keyword-only results and surfaced
  statistics. test: `crates/gmemory/tests/cli_records.rs::recall_matches_python`.
- 7.1.4 - `CLI gobby memory delete` matches prefix resolution and output.
  test: `crates/gmemory/tests/cli_records.rs::delete_matches_python`.
- 7.1.5 - `CLI gobby memory list` matches filters and formatting.
  test: `crates/gmemory/tests/cli_records.rs::list_matches_python`.
- 7.1.6 - `CLI gobby memory show` matches output and errors.
  test: `crates/gmemory/tests/cli_records.rs::show_matches_python`.
- 7.1.7 - `CLI gobby memory update` matches updates and output.
  test: `crates/gmemory/tests/cli_records.rs::update_matches_python`.
- 7.1.8 - `CLI gobby memory stats` matches output.
  test: `crates/gmemory/tests/cli_records.rs::stats_matches_python`.
- 7.1.9 - `CLI gobby memory backfill-unscoped-lessons` stamps the same
  lessons and prints the same count. test: `crates/gmemory/tests/cli_records.rs::backfill_matches_python`.
- 7.1.10 - `CLI gobby memory dedupe` matches dry-run, confirmation and
  deletions. test: `crates/gmemory/tests/cli_records.rs::dedupe_matches_python`.
- 7.1.11 - `CLI gobby memory export` matches the markdown document and the
  metadata and stats flags. test: `crates/gmemory/tests/cli_records.rs::export_matches_python`.
- 7.1.12 - `CLI gobby memory backup` matches default and explicit paths and
  the JSONL bytes. test: `crates/gmemory/tests/cli_records.rs::backup_matches_python`.
- 7.1.13 - `CLI gobby memory restore` matches timestamp-wins restoration,
  missing-file messages and exit codes.
  test: `crates/gmemory/tests/cli_records.rs::restore_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test cli_records`
against the isolated database and temporary homes.

### 7.2 Native index, graph and dream commands [category: code] (depends: 7.1)
`kind: deliverable`

Targets:
- `crates/gmemory/src/cli/mod.rs`
- `crates/gmemory/src/cli/daemon.rs`
- `crates/gmemory/tests/cli_daemon.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append this leaf's waiver entries to the shared manifest array

**Research context:** `src/gobby/cli/memory/graph.py` (`clear_graph` with
its confirmation, `graph_counts` with `--json`, `rebuild_graph` with `--wait`
and `--timeout` polling the status route, `invalidate`), `indices.py`
(`reindex_embeddings`, `reconcile` with `--dry-run`, `rebuild_crossrefs` with
a 600-second timeout) and `dream.py` (`memory_dream` with its polling
`_poll_run`, resume hint, checkpoint, inventory, aggregate and dry-run
renderers; `memory_dream_status`; `memory_dream_revert` with a 300-second
timeout; `_request`'s error mapping). They call the daemon over HTTP with the
local operator credential, as `src/gobby/cli/memory/common.py::_get_daemon_client`
does. Reuse gcore's daemon URL and local token helpers
(`crates/gcore/src/daemon_url.rs`, `crates/gcore/src/local_token.rs`) with
blocking reqwest. A health failure prints Python's `Daemon not running`
message. `invalidate` prints that nothing was cleared and a rebuild is
already running when the response carries `already_running`
(`memory.cli-invalidate.tracked-rebuild`). `rebuild-graph`, `dream` and
`dream status` print the refusal or failed run that 2.4 and 4.3 report for
an unsupported prompt override through Python's existing error and
failed-run renderers. This leaf adds those four entries. Other output,
including `rebuild-crossrefs`'s, is unchanged.

**Granularity:** Ten ledger rows are thin HTTP clients sharing one client,
health check and error mapping. Their behavior lives in 2.3, 2.4 and 4.3.

**Acceptance:**

- 7.2.1 - `CLI gobby memory clear-graph` matches confirmation, output and
  errors. test: `crates/gmemory/tests/cli_daemon.rs::clear_graph_matches_python`.
- 7.2.2 - `CLI gobby memory graph-counts` matches text and JSON output.
  test: `crates/gmemory/tests/cli_daemon.rs::graph_counts_matches_python`.
- 7.2.3 - `CLI gobby memory rebuild-graph` matches foreground, `--wait`
  polling and timeout behavior, and prints an unsupported prompt override's
  refusal under its exact waiver.
  test: `crates/gmemory/tests/cli_daemon.rs::rebuild_graph_matches_python`.
- 7.2.4 - `CLI gobby memory invalidate` matches confirmation and the clear
  report, and reports a running rebuild under its exact waiver.
  test: `crates/gmemory/tests/cli_daemon.rs::invalidate_reports_running_rebuild`.
- 7.2.5 - `CLI gobby memory rebuild-crossrefs` matches output.
  test: `crates/gmemory/tests/cli_daemon.rs::rebuild_crossrefs_matches_python`.
- 7.2.6 - `CLI gobby memory reconcile` matches dry-run and applied output.
  test: `crates/gmemory/tests/cli_daemon.rs::reconcile_matches_python`.
- 7.2.7 - `CLI gobby memory reindex-embeddings` matches output.
  test: `crates/gmemory/tests/cli_daemon.rs::reindex_matches_python`.
- 7.2.8 - `CLI gobby memory dream` matches trigger, polling, dry-run,
  aggregate and resume-hint output, and prints a run failed by an
  unsupported prompt override under its exact waiver.
  test: `crates/gmemory/tests/cli_daemon.rs::dream_matches_python`.
- 7.2.9 - `CLI gobby memory dream status` matches status rendering, including
  that failed run under its exact waiver.
  test: `crates/gmemory/tests/cli_daemon.rs::dream_status_matches_python`.
- 7.2.10 - `CLI gobby memory dream revert` matches output and error detail.
  test: `crates/gmemory/tests/cli_daemon.rs::dream_revert_matches_python`.
- 7.2.11 - An unreachable daemon, an error status and invalid JSON produce
  Python's messages and exit codes. test: `crates/gmemory/tests/cli_daemon.rs::daemon_errors_match_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test cli_daemon`
against a local HTTP stub that replays 0.1's recorded responses.

## P8: Admission, activation and ledger
`kind: framing`

### 8.1 Native admission binding for memory rows [category: code] (depends: 7.2, #25046, #25068)
`kind: deliverable`

Targets:
- `crates/gmemory/src/admission.rs`
- `crates/gmemory/tests/admission.rs`
- `docs/contracts/memory-native.md`

**Research context:** The 25 HTTP rows fall into two classes. Agent-token
rows with `bind_identity` false, also open to operators and browsers: `GET
/api/memories/graph/counts` and `GET /api/memories/graph/rebuild/status`
(`src/gobby/servers/auth_service.py::_AGENT_CAPABILITY_MATRIX`). Operator
and browser rows: the other 23. API keys, the local operator and ordinary
`gobby_session` cookies keep Python's per-route permissions. Managed
`browser-testing.` cookies keep
`src/gobby/servers/browser_sessions.py::browser_route_allowed`: GET, HEAD and
OPTIONS only, so every memory and dream mutation refuses them before any
effect. No row is a runtime-grant route. The family consumes the Admitted
value #25046 and #25068 bind before dispatch; it never verifies a token or
cookie itself. This leaf replaces 1.1's fail-closed `admitted` with a match
of that value against each row's declared `AdmissionClass`. Unavailable
authority returns a typed 503. Shared CORS and preflight stay with #24964
through #25046. Until both seams land and Security approves this leaf's exact
SHA, every row stays proxied in all switch states; the activation record
states that remainder. MCP calls are admitted by the shared gateway, and CLI
calls by the local operator credential; neither passes through this leaf.

**Expansion obligation:** Immediately after expansion, the Orchestrator applies
`security-review` to the leaf for source section 8.1 and checks the persisted
label before dispatch. A missing label holds dispatch. Native admission stays
held until a Security Reviewer records an approved step for the exact
implementation SHA; a later implementation change needs renewed approval.
Server-derived M1 emits coverage labels only, so this obligation needs no
emitter change.

**Acceptance:**

- 8.1.1 - Agent tokens with matching, mismatched, revoked and expired
  identity get Python's outcomes on the two graph rows and are refused on
  the other 23. test: `crates/gmemory/tests/admission.rs::agent_token_rows_match_python`.
- 8.1.2 - API keys, the local operator and ordinary cookies keep Python's
  permissions on all 25 rows, and managed cookies are refused on every
  mutation before any effect.
  test: `crates/gmemory/tests/admission.rs::operator_and_browser_classes_match_python`.
- 8.1.3 - Without a bound Admitted value the row stays proxied, unavailable
  authority returns 503, and no second verifier exists in the crate.
  test: `crates/gmemory/tests/admission.rs::admission_fails_closed_without_shared_seam`.
- 8.1.4 - The admission record names the persisted `security-review` label,
  the pre-dispatch check and the approved step for the exact SHA; absent or
  stale approval holds native admission.
  behavior: "memory admission security gate" in `docs/contracts/memory-native.md`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-memory --test admission`
composing the landed #25046 and #25068 seams with isolated credential state;
0.1's admission-matrix test with the protected Python prefix.

### 8.2 Memory family activation and coherent release [category: code] (depends: 8.1, #24885, #24963)
`kind: deliverable`

Targets:
- `crates/gmemory/src/lib.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: register the two memory tool families with the shared gateway at both cfg-gated serve entry points, passing the gobby-memory server's resolved backend
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gdaemon/tests/memory_cutover.rs`
- `tests/e2e/test_memory_native.py`
- `docs/contracts/memory-native.md`

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: dynamically reads its bumped native version pin.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: dynamically reads the bumped gdaemon pin.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: dynamically reads its bumped native version pin.
- `src/gobby/cli/install_setup_gmcp.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: dynamically reads unchanged native version pins.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: resolves every pin dynamically by binary name.
- `src/gobby/code_index/gcode_versions.py` — no-edit-reason: both gcode maintenance floors derive from the bumped gcode pin.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: the hook compatibility floor derives from the bumped ghook pin.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: enumerates the map dynamically.
- `src/gobby/install/distribution.py` — no-edit-reason: resolves minimum versions dynamically.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: derives expected ghook versions from the map.
- `tests/cli/test_install_ghook.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gmcp.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: derives expected versions from the map.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: derives gcode expectations from the bumped pin.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: derives ghook expectations from the bumped pin.
- `tests/install/test_distribution.py` — no-edit-reason: dynamically parameterizes managed version expectations.
- `tests/install/test_version_pins.py` — no-edit-reason: already verifies each managed pin against its Cargo manifest.

**Research context:** #24885 owns the per-server MCP strangler: the public
gateway reads each internal server's `front_door.routes` entry (`proxy`,
`compare` or `native`). This leaf registers the `gobby-memory` and
`gobby-review-learning` descriptors through #24885's registration interface,
and selects `front_door.routes.memory`, `front_door.routes.gobby-memory` and
`front_door.routes.gobby-review-learning` through the existing bootstrap
mechanism, with Proxy selectable for rollback. Compare runs only the S3 read
rows and read-only tools. Effectful rows and tools are proven by the owning
leaves' isolated tests and the e2e run, never by running an effect twice.
Consume #24963's readiness result: every waiver in the inventory needs its
exact entry and passing native assertion, and at least one ordinary `Passed`
case is mandatory. The rule templates and delivery formatters keep calling
the same tool names, so injection needs no change. Native dream runs only on
hub and standalone (Decision 5).

This is the first shipping commit since 1.1 added the `crates/gmemory`
member to the root `Cargo.toml`.
`src/gobby/install/bin_release_identity.py::workspace_source_identity` hashes
the root manifest for every managed binary, and 1.3, 2.1 and 2.3 changed
gcore, so gdaemon, gcode and ghook each take a 0.0.1 bump above their
preceding released version, with manifests, lock and pins updated. Rebuild
gclient, gmcp and gterm alongside; unpublished binaries keep their versions
and use the existing component promotion paths. Promote the coherent trio
through `promote_workspace_binary_set` and read installed hashes after
signing. Only the Orchestrator delegates cutover, with a global announcement,
a quiet window, a passing source, schema and installed-set preflight, and a
health report. No push is authorized here.

**Granularity:** The targets and acceptance items belong to one family
activation and release state machine. 8.3 forwards the CLI and 8.4 records
ledger evidence.

**Acceptance:**

- 8.2.1 - Proxy, compare and native states affect only the memory HTTP rows
  and the two MCP servers; compare runs read rows only, effectful rows run
  once, and rollback to Proxy preserves state. For
  `GET /api/memories/search` both implementations compute a result, the
  native surfaced-statistics mutation is discarded with its isolated
  transaction, and Python's surfaced counter and debounce effect happens
  exactly once. This runs through the real front-door router against a
  Python stub: native shadow counters roll back even when the native leg
  errors, and in `Native` the native counters persist.
  test: `crates/gdaemon/tests/memory_cutover.rs::memory_cutover_is_scoped_and_single_effect`.
- 8.2.2 - With the Python backend unavailable, all 25 HTTP rows and 32 tools
  run natively for every admitted caller class on hub and standalone, rule
  surfacing still injects memories, and a node keeps its current backend.
  test: `tests/e2e/test_memory_native.py::test_memory_rows_work_without_python_backend`.
- 8.2.3 - Readiness rejects zero-Passed, all-Skipped and unwaived-Skipped
  reports and unknown or stale waivers, and requires every inventory entry
  beside ordinary `Passed` cases.
  test: `crates/gdaemon/tests/memory_cutover.rs::waiver_readiness_gates_memory_activation`.
- 8.2.4 - Shipping manifests, lock and pins agree at the required patch
  increment. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 8.2.5 - The runbook records build and installed hashes, compared cases,
  rollback command, announcement, health, the Security approval for 8.1 and
  native smoke evidence. behavior: "memory activation evidence" in `docs/contracts/memory-native.md`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test memory_cutover`;
the isolated e2e test with the protected Python prefix; scoped Ruff and mypy
with a changed-test type audit; the targeted version-pin check; an admitted
release build and Orchestrator-directed cutover. These are execution gates,
not results observed while drafting.

### 8.3 Forward the public memory CLI and retire Python callbacks [category: code] (depends: 8.2)
`kind: deliverable`

Targets:
- `src/gobby/cli/memory/__init__.py::*` — scope-reason: replace the package entry with a literal argument-forwarding Click command named `memory`
- `src/gobby/cli/memory/main.py::*` — operation: delete — scope-reason: retire the Python memory group assembly
- `src/gobby/cli/memory/crud.py::*` — operation: delete — scope-reason: retire the Python record command callbacks
- `src/gobby/cli/memory/dream.py::*` — operation: delete — scope-reason: retire the Python dream command callbacks
- `src/gobby/cli/memory/export.py::*` — operation: delete — scope-reason: retire the Python export, backup and restore callbacks
- `src/gobby/cli/memory/graph.py::*` — operation: delete — scope-reason: retire the Python graph command callbacks
- `src/gobby/cli/memory/indices.py::*` — operation: delete — scope-reason: retire the Python index command callbacks
- `src/gobby/cli/memory/maintenance.py::*` — operation: delete — scope-reason: retire the Python dedupe callback
- `src/gobby/cli/memory/common.py::*` — operation: delete — scope-reason: retire the Python daemon-client helper used only by the retired callbacks
- `src/gobby/cli/memory/_formatting.py::*` — operation: delete — scope-reason: retire the Python formatting helper used only by the retired callbacks
- `tests/cli/test_memory_cli.py::*` — scope-reason: replace callback expectations with forwarding checks and corpus ownership
- `tests/cli/test_memory_coverage.py::*` — scope-reason: remove callback-coverage cases now owned by the native CLI tests
- `tests/cli/test_memory_dream_publication.py::*` — scope-reason: move the publication expectation to the corpus and native dream CLI test
- `tests/memory/test_dream.py::*` — scope-reason: drop the CLI-callback cases that import the retired dream module
- `tests/review_learning/test_lessons.py::*` — scope-reason: replace the retired crud import with the corpus-backed backfill expectation
- `tests/sync/test_local_backup_paths.py::*` — scope-reason: replace the retired `_default_backup_path` import with the native backup path expectation
- `tests/cli/test_memory_native_forwarding.py`

Consumers unchanged:
- `src/gobby/cli/__init__.py` — no-edit-reason: its lazy map entry `"memory": ("memory", "memory")` still resolves `gobby.cli.memory.memory`.
- `src/gobby/memory/services/_search_access.py` — no-edit-reason: the `cli.memory.recall` caller tag stays in `SURFACED_CALLERS`; 7.1 sends it.

**Research context:** Literal sweeps for `gobby.cli.memory` found the six
test importers listed above and no production importer outside the package.
Once 7.1 and 7.2 implement every command and 8.2 has released the binary
that carries them, replace the package entry with a forwarding Click command
that disables option interpretation and calls the installed `gdaemon memory`
with literal arguments, cwd, global configuration and inherited streams,
reusing `src/gobby/utils/native_bin.py::resolve_native_bin`. Preserve
missing-binary diagnostics and the exact native exit status. Rehome business
expectations into 0.1's corpus and the native tests before deleting the
callback modules.

**Granularity:** Ten production targets implement one ownership switch: one
forwarding launcher replaces the callback implementation, and the nine
deletions are bounded cleanup in that switch. The test edits migrate imports
of the deleted modules.

**Acceptance:**

- 8.3.1 - The public launcher forwards literal arguments, cwd, configuration
  and exit status, including unknown options, Unicode, help and errors, to
  the resolved binary, and imports no memory business, storage or service
  module. test: `tests/cli/test_memory_native_forwarding.py::test_launcher_only_forwards`.
- 8.3.2 - Every retired callback expectation maps to the frozen corpus and a
  native row test, and import and patch-string sweeps find no reference to a
  deleted module. test: `tests/cli/test_memory_native_forwarding.py::test_retired_callbacks_have_native_coverage`.

Planned verification: the forwarding and migrated tests with the protected
Python prefix, both native CLI test files, scoped Ruff and mypy and a
changed-test type audit.

### 8.4 Publish memory family parity ownership [category: docs] (depends: 8.3)
`kind: deliverable`

Targets:
- `docs/contracts/parity-ledger-status.d/memory-skills.json::*` — scope-reason: update only the 74 memory status records and retain every other record
- `docs/contracts/parity-ledger-status.d/remainder.json::*` — scope-reason: update only the six review-learning status records and retain every other record
- `docs/contracts/parity-ledger.md`
- `docs/contracts/memory-native.md`
- `docs/guides/memory.md`

**Research context:** All 80 rows are `proxied` today.
`scripts/generate_parity_ledger.py` accepts `proxied`, `delegated` and
`dropped by decision #N`; objects carry exactly `status` and `note`. After
8.2 and 8.3, every row is served natively. Set each of the 80 to `delegated`
with a note citing its owning leaf, its native tests and any waiver ID,
following `.gobby/plans/completed/native-ws-transport.md`'s ledger step.
Regenerate `docs/contracts/parity-ledger.md` with `uv run python
scripts/generate_parity_ledger.py`; a second run leaves it unchanged.
`docs/contracts/memory-native.md` gains the final row ownership map, the
serving-lease identity and the session-variable seam. `docs/guides/memory.md`
says that gdaemon serves memory, dream and review learning natively and that
`gobby memory` forwards to `gdaemon memory`, with its last-verified date
updated.

**Acceptance:**

- 8.4.1 - The 74 memory rows are `delegated` with notes citing their leaf,
  tests and waiver IDs, and every other record is unchanged.
  behavior: "74 delegated memory parity rows" in `docs/contracts/parity-ledger-status.d/memory-skills.json`.
- 8.4.2 - The six review-learning rows are `delegated` with notes citing 6.3
  and its tests, and every other record is unchanged.
  behavior: "6 delegated review-learning parity rows" in `docs/contracts/parity-ledger-status.d/remainder.json`.
- 8.4.3 - The regenerated ledger matches the shards and the public inventory.
  test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.
- 8.4.4 - The guide describes native memory operation and the forwarding
  CLI. behavior: "native memory operation" in `docs/guides/memory.md`.

Planned verification: `uv run python scripts/generate_parity_ledger.py` run
twice with no second-run diff; `tests/test_parity_ledger.py` with the
protected Python prefix.

## E1 End-to-end verification
`kind: verification`

The acceptance items own all 80 rows. 1.1 owns the list, get and stats HTTP
rows; 1.2 the four graph read rows; 1.3 search; 2.2 the seven write rows; 2.3
invalidate, reconcile, crossref rebuild and reindex; 2.4 graph rebuild,
rebuild status and clear; 4.3 the three dream rows; 6.2 the 26 `gobby-memory`
tools; 6.3 the six `gobby-review-learning` tools; 7.1 the group and twelve
record commands; and 7.2 the ten index, graph and dream commands, with 8.3
forwarding the public entry to them. 0.1 rejects missing or extra keys. 2.1
supplies the shared serving lease and projection ledger, 2.5 the reclustering
service, 3.1 the #25070 seam, 4.1 and 4.2 the dream internals, 5.1 the
review-learning service and 6.1 the session-variable writer. 8.1 owns
admission and the Security approval gate, 8.2 activates the HTTP family and
both MCP servers, and 8.4 records the delegated ledger state.

The #24963 readiness gate requires ordinary `Passed` cases and exactly cited,
native-asserted `Waived` cases for the 64 waivers, and fails skipped-only and
unwaived reports. #24970 supplies the shared error classes and adapters.
Shared target files are dependency-ordered throughout, and no section leaves
a product choice to its executor. Josh reviews the flagged items with the
stamped plan: the HDBSCAN dependency (Decision 2), the AI 7.4 amendment
(Decision 4) and the AI 7.4 and 7.6 rebuild-seam amendments (3.1).

Draft validation command:
`uv run gobby plans validate .gobby/plans/memory-family.md -p /Users/josh/Projects/gobby`.
Expansion validation is unrun for the narrative draft and becomes mandatory
after the Adversary derives and applies M1. No implementation tests,
production cutover or task expansion have been performed during planning.

## V1 Plan Changelog
`kind: framing`

- 2026-10-28 — Initial narrative by Plan Writer gobby#18291 for #25254,
  carrying the Orchestrator's MEM-D1, MEM-D2 and MEM-D3 rulings and the
  #25262 edge. The Orchestrator accepted enhancer gobby#18364's MEM-ENH-01
  to 05: the managed lease lifecycle (2.1, 2.2, 2.3), explicit
  repair-intent failure invariants (2.2, 2.3, 6.2.28), stale-plan and
  revert fences (4.1), the search compare effect (8.2.1), and the pinned
  `hdbscan` 0.12.0 with `kdtree` 0.7.0 and Rust 1.88 evidence (Decision 2,
  2.5). Awaiting adversarial review by gobby#18294; no consensus claimed.

# Plan: `gobby memory dream` — unified memory consolidation

## Context

Gobby's memory system accumulates raw, redundant, and increasingly stale records over time. Today there is only a *deletion* pass: the bundled `nightly-memory-cleanup.yaml` pipeline runs `audit_memories` → `cleanup_memories` (wrapping `execute_cleanup` in `src/gobby/memory/services/maintenance.py`) to drop stale/duplicate/code-derivable/orphaned entries. There is no *consolidation* layer — nothing merges near-duplicates into a better canonical memory, supersedes a stale fact with its latest value, or synthesizes recurring cross-session patterns into higher-level memories. That consolidation is exactly what Claude Code's `/dream` does.

The user runs `gobby-memory` instead of Claude's built-in memory, so this capability has to live in Gobby. This plan adds `gobby memory dream`: a single async job that runs the existing cleanup pass first, then a new LLM consolidation pass, under one snapshot so the whole thing is revertible, with a human-readable changelog written to the project's git-tracked `.gobby/dream-log/`. It runs on demand and as an on-by-default nightly system cron that skips projects with no new memories.

This **replaces** the existing cleanup entry point: `nightly-memory-cleanup.yaml` is retired; `execute_cleanup` and the `audit_memories`/`cleanup_memories` tools are kept and reused as dream's phase 1.

### Decisions locked with the user

1. **Apply immediately, revertible.** No dry-run-first default. Each run consolidates and writes in one go, fully revertible from a per-run snapshot, and writes a markdown synopsis of what changed to `.gobby/dream-log/`. (`--dry-run` still exists as an explicit opt-in for preview.)
2. **Dream does both, and replaces cleanup.** One job: cleanup pass first (reuse `execute_cleanup`), then consolidation pass, one run record, one snapshot spanning both phases so a single revert undoes both. The old `nightly-memory-cleanup.yaml` pipeline is retired.
3. **Auto-dream on by default as a system cron.** A single nightly system cron (not per-project opt-in), disable/re-enable via the system cron row. It scans all projects and skips any project with zero new memories created since its last successful dream.

## Architecture

Mirror the `gobby build` shared-service shape: one core async service exposed identically via CLI, MCP, and HTTP, plus an async run record (mirroring `expansion_runs`) for status polling and a per-run snapshot for revert.

Pipeline per project, in order, all inside one dream run:

```
Stage 0  lock + scope (per-project active-run guard, load memories)
Stage 1  CLEANUP phase   → reuse execute_cleanup() (stale/dup/code-derivable/orphaned delete)
Stage 2  cluster         → deterministic embedding clustering (reuse find_duplicate_memories + dedup richness)
Stage 3  LLM consolidate → windowed structured pass over ambiguous clusters + session digests
Stage 4  snapshot+apply  → one transaction: snapshot affected rows, then apply cleanup deletes + consolidation
Stage 5  reconcile       → embeddings / crossrefs / knowledge-graph for changed ids only
Stage 6  changelog       → write .gobby/dream-log/<ts>-<run_id>.md, finalize run
```

The snapshot is captured for **both** the cleanup deletions and the consolidation changes inside the single apply transaction, so `gobby memory dream revert <run_id>` restores the pre-dream state of the entire job.

## New modules (`src/gobby/memory/dream/`)

All new code goes in a fresh package. `maintenance.py` (~890 lines) and `mcp_proxy/tools/memory.py` (~970) and `cli/memory.py` (~974) are near the 1,000-line limit — reuse them, do not extend them.

| File | Responsibility |
| --- | --- |
| `dream/__init__.py` | Exports `dream`, `revert_dream`, `DreamResult`, `DreamOptions`. |
| `dream/service.py` | Core orchestrator `dream(...)` + `revert_dream(...)`. Pure orchestration; delegates to stage modules. |
| `dream/options.py` | `DreamOptions` dataclass (mirrors `src/gobby/build/options.py`). |
| `dream/results.py` | `DreamResult`, `DreamPlan`, `ClusterDecision`, `CleanupSummary` dataclasses with `to_dict()`. |
| `dream/cluster.py` | Stage 2: deterministic clustering. Reuses `find_duplicate_memories` (`maintenance.py:548`) + `_memory_richness_score`/`_is_richer_memory_content` (`memory/services/dedup.py:47,60`). Classifies clusters `auto_merge` / `needs_llm` / `singleton`. |
| `dream/llm_pass.py` | Stage 3: windowed LLM adjudication; prompt assembly, structured-output parse/validate. |
| `dream/apply.py` | Stage 4: single-transaction snapshot + apply (cleanup deletes + consolidation writes). |
| `dream/reconcile.py` | Stage 5: post-commit embedding/crossref/graph reconcile for changed ids only. |
| `dream/snapshot.py` | Snapshot capture + verbatim restore of affected memories + crossrefs, keyed by `run_id`. |
| `dream/changelog.py` | Stage 6: render `.gobby/dream-log/<ts>-<run_id>.md` synopsis. |
| `dream/trigger.py` | Deterministic nightly-cron trigger policy (no LLM). |
| `dream/cron.py` | `register_memory_dream_cron(...)` system-cron registration (mirrors `src/gobby/github_triage/cron.py`). |
| `storage/memory_dream_runs.py` | `LocalMemoryDreamRunManager` + `MemoryDreamRun` model (mirrors `src/gobby/storage/expansion_runs.py`). |
| `mcp_proxy/tools/memory_dream.py` | `create_memory_dream_registry(ctx)` (mirrors `mcp_proxy/tools/build.py`). |
| `servers/routes/memory_dream.py` | `create_memory_dream_router()` (mirrors `servers/routes/build.py`). |
| `cli/memory_dream.py` | `dream`, `dream status`, `dream revert` subcommands, attached to the existing `memory` Click group via `group.add_command(...)`. |
| `install/shared/prompts/memory/dream_consolidate.md` | Consolidation LLM prompt template (modeled on `stale_audit.md`). |

### Core service signature

```python
# src/gobby/memory/dream/service.py
async def dream(
    *,
    project_id: str,
    memory_manager: MemoryManager,
    llm_service: LLMService,
    config: MemoryConfig,
    options: DreamOptions,
    run_manager: LocalMemoryDreamRunManager,
    run_id: str | None = None,
) -> DreamResult: ...

async def revert_dream(
    *,
    run_id: str,
    memory_manager: MemoryManager,
    run_manager: LocalMemoryDreamRunManager,
) -> DreamResult: ...
```

```python
# dream/options.py
@dataclass
class DreamOptions:
    dry_run: bool = False
    skip_cleanup: bool = False           # run only the consolidation phase
    skip_consolidation: bool = False     # run only the cleanup phase
    memory_types: tuple[str, ...] | None = None
    session_lookback: int = 50
    near_exact_threshold: float = 0.95
    similar_threshold: float = 0.85
    llm_cluster_window: int = 12
    max_llm_calls: int = 40
    max_memories: int = 5000
    # cleanup-phase passthroughs (defaults match nightly-memory-cleanup.yaml)
    max_stale_age_days: int = 30
    max_stale_access_count: int = 1
    stale_confidence_threshold: float = 0.85
    limit_per_category: int = 500
    use_stale_classifier: bool = True
```

```python
# dream/results.py
@dataclass
class ClusterDecision:
    action: Literal["keep", "merge", "supersede", "new"]
    surviving_id: str | None
    source_ids: list[str]
    content: str | None
    rationale: str
    origin: Literal["deterministic", "llm"]

@dataclass
class CleanupSummary:
    stale_deleted: int
    duplicate_deleted: int
    code_derivable_deleted: int
    orphaned_deleted: int
    review: int
    deleted_ids: list[str]

@dataclass
class DreamResult:
    run_id: str
    status: Literal["completed", "failed", "dry_run", "reverted"]
    project_id: str
    cleanup: CleanupSummary
    memories_scanned: int
    clusters_total: int
    clusters_auto: int
    clusters_llm: int
    kept: int
    merged: int
    superseded: int
    created: int
    deleted_ids: list[str]
    llm_calls: int
    snapshot_id: str | None
    changelog_path: str | None
    decisions: list[ClusterDecision]
    error: str | None = None
    def to_dict(self) -> dict[str, Any]: ...
```

## DB schema & migration

Append one migration to the `MIGRATIONS` list in `src/gobby/storage/migrations.py` (next sequential version after the current max, tuple form `(<n>, "Add memory dream runs and snapshots", _apply_memory_dream_schema)`), and add the same `CREATE TABLE` statements to `src/gobby/storage/baseline_schema.sql` for fresh DBs.

**`memory_dream_runs`** (mirrors `expansion_runs` idioms):

| Column | Type | Notes |
| --- | --- | --- |
| `id` | TEXT PK | `dream-<uuid12>` |
| `project_id` | TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE | |
| `trigger` | TEXT CHECK in (`manual`,`cron`) | |
| `status` | TEXT NOT NULL DEFAULT 'pending' CHECK in (`pending`,`running`,`cleaning`,`planning`,`applying`,`completed`,`failed`,`cancelled`,`reverted`) | |
| `options_json` | TEXT | serialized `DreamOptions` |
| `plan_json` | TEXT | full `list[ClusterDecision]` + `CleanupSummary` |
| `result_json` | TEXT | serialized `DreamResult` |
| `snapshot_id` | TEXT | groups snapshot rows for this run |
| `changelog_path` | TEXT | `.gobby/dream-log/...` path |
| `memories_scanned` | INTEGER DEFAULT 0 | |
| `llm_calls` | INTEGER DEFAULT 0 | |
| `error` | TEXT | |
| `logs_json` | TEXT | append-only log lines |
| `checkpoints_json` | TEXT | stage progress for crash recovery |
| `created_at` / `updated_at` / `started_at` / `completed_at` | TEXT | ISO8601 |

Indexes: `idx_memory_dream_runs_project (project_id)`, `idx_memory_dream_runs_status (status)`.

**`memory_dream_snapshots`** — row-level pre-apply snapshot of every memory and crossref the run will delete or modify (covers cleanup deletions and consolidation changes):

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER PK AUTOINCREMENT | |
| `snapshot_id` | TEXT NOT NULL | |
| `run_id` | TEXT NOT NULL REFERENCES memory_dream_runs(id) ON DELETE CASCADE | |
| `kind` | TEXT NOT NULL CHECK in (`memory`,`crossref`) | |
| `memory_id` | TEXT | for kind=memory |
| `payload_json` | TEXT NOT NULL | full serialized row incl. `tags`, `media`, `graph_processed`, timestamps |
| `created_at` | TEXT NOT NULL | |

Indexes: `idx_memory_dream_snapshots_snapshot (snapshot_id)`, `idx_memory_dream_snapshots_run (run_id)`.

A dedicated row-level per-run snapshot (rather than reusing whole-store `gobby memory backup`) makes revert surgical, transactional, and concurrency-safe.

`LocalMemoryDreamRunManager` copies `LocalExpansionRunManager`'s method shape (`create`, `get`, `start`, status setters, `save_plan`, `save_result`, `append_log`, `update_checkpoints`, `fail`, `cancel`, `mark_reverted`) plus dream-specific `get_latest_successful_for_project(project_id)` and `get_active_for_project(project_id)`.

## Pipeline stages (exact behavior)

A checkpoint string is written after each stage (`scoped`, `cleaned`, `clustered`, `planned`, `applied`, `reconciled`, `logged`) via `run_manager.update_checkpoints` for crash recovery.

**Stage 0 — lock & scope.** `get_active_for_project` guard (section *Concurrency*); reject a second concurrent run for the project, returning the active run id. `run_manager.start` → `running`. Load memories via `MemoryManager.list_memories(project_id=..., memory_type=...)`, capped at `options.max_memories`. Zero memories → short-circuit to a `completed` result, all counts 0, no snapshot, no LLM, no changelog.

**Stage 1 — cleanup phase.** Unless `options.skip_cleanup`, call the existing `execute_cleanup(...)` (`src/gobby/memory/services/maintenance.py:714`) with the cleanup passthrough options, **but in capture mode**: cleanup's deletions must be funneled through the dream apply transaction so they are snapshotted and revertible together with consolidation. Concretely, run `execute_cleanup` with its own dry-run/plan path to obtain the delete id set + `CleanupSummary` without committing deletes here; the actual deletes execute in Stage 4. (If `execute_cleanup` cannot cleanly separate plan from apply, add a `collect_only: bool` parameter to it — a small, additive change to `maintenance.py`, not a rewrite.) `run_manager` → `cleaning` checkpoint.

**Stage 2 — deterministic clustering (zero LLM cost).** Over the post-cleanup memory set, reuse `find_duplicate_memories` (`maintenance.py:548`, already does Qdrant vector-similarity duplicate detection) to get candidate groups, then union-find at `similar_threshold` (0.85). Classify each cluster with `_memory_richness_score`/`_is_richer_memory_content` (`memory/services/dedup.py`):
- all pairwise sims ≥ `near_exact_threshold` (0.95) → **auto `merge`** (keep richest), no LLM;
- size 1 → **`keep` singleton**, no LLM;
- mixed 0.85–0.95, or tag/`memory_type` conflict, or contradiction signal → **`needs_llm`**.

**Stage 3 — LLM consolidation.** Only `needs_llm` clusters, windowed `llm_cluster_window` per call, hard-capped at `max_llm_calls`; clusters beyond the cap default to safe `keep` (`rationale="llm_budget_exhausted"`). Session context: most recent `session_lookback` sessions via `SessionManager._QueryMixin.list(project_id=..., limit=...)`, reading only `digest_markdown`/`summary_markdown` columns (never raw transcripts in v1), chunked and truncated to a token budget, attached as cross-session pattern context. Prompt via `PromptLoader.load("memory/dream_consolidate.md").render(ctx)` (DB-prompt-override aware), called through `LLMService.call_feature(config.dream, prompt, system_prompt=..., caller="memory_dream")` with a new `MemoryDreamConfig(FeatureDefaultConfig)` modeled on `MemoryStaleAuditConfig` (`src/gobby/config/persistence.py`). Parse + validate (below), merge with deterministic decisions into one `DreamPlan`. `planning` checkpoint.

**Stage 4 — snapshot + apply (one transaction).** If `options.dry_run`: persist plan via `run_manager.save_plan`, write a dry-run changelog, status `completed` (result.status `dry_run`), no writes/snapshot/reconcile. Else open one `db.transaction_immediate()` (`src/gobby/storage/database.py`, write lock):
1. snapshot every affected memory row + every crossref touching those ids (cleanup-deleted ids ∪ consolidation source/surviving ids) into `memory_dream_snapshots`;
2. apply cleanup deletes;
3. apply consolidation: `keep` = noop; `merge`/`supersede` = UPDATE survivor content/tags + DELETE superseded (FK `ON DELETE CASCADE` clears their crossrefs, already snapshotted); `new` = INSERT;
4. persist `snapshot_id` + `result_json` on the run row.
Single transaction → atomic commit/rollback; a crash mid-apply auto-rolls back, leaving the DB untouched. `applying` checkpoint.

**Stage 5 — reconcile (post-commit, changed ids only).** Deleted ids → `VectorStore.delete_many` + `KnowledgeGraphService.remove_memory_from_graph` (graph best-effort). Updated/created ids → re-embed + `VectorStore.upsert`, `MemoryManager.rebuild_crossrefs_for_memory(...)`, set `graph_processed=0` for KG reprocessing. Final idempotent `IndexingService.reconcile_stores(dry_run=False)` sweep. Reconcile failures are logged to the run as `reconcile_warnings`; they do not roll back the committed, snapshot-protected apply (reconcile is re-runnable via `gobby memory reconcile`). `reconciled` checkpoint.

**Stage 6 — changelog & finalize.** Render `.gobby/dream-log/<ISO-ts>-<run_id>.md` (section below), store its path on the run, `run_manager.save_result`, status `completed`, release lock.

## LLM prompt & structured output

New `src/gobby/install/shared/prompts/memory/dream_consolidate.md`, same frontmatter/Jinja style as `stale_audit.md`. `required_variables`: `clusters`, `session_patterns`, `memory_types`, `merge_confidence_threshold`. Instruct: one verdict per numbered cluster — `keep` (all distinct), `merge` (semantic duplicates → one canonical, compose best content), `supersede` (later fact contradicts earlier → keep newest value, cite stale ids), `new` (synthesize a higher-level pattern; originals preserved unless also merged). Every decision cites `source_ids` (subset of that cluster's input ids) + one-sentence `rationale`; `merge`/`supersede` require `confidence ≥ merge_confidence_threshold`.

Strict JSON output:

```json
{"decisions":[{"cluster_index":0,"action":"merge","source_ids":["mem-a","mem-b"],"surviving_id":"mem-a","content":"Canonical merged content...","confidence":0.93,"rationale":"mem-b is a less detailed restatement of mem-a."}]}
```

Validation (`dream/llm_pass.py`): strip fences, `json.loads`; on `JSONDecodeError` one bounded "strict JSON only" retry. Validate with a Pydantic `DreamLLMResponse`. Any decision is rejected → safe `keep` when: `cluster_index` out of range; `source_ids` not a subset of that cluster's inputs; `merge`/`supersede` with `surviving_id` not in `source_ids`; missing/low `confidence`; `action=new` with empty `content`. Omitted clusters default to `keep`. Persistent malformed output after retry → that window degrades to all-`keep`, logged. The LLM can only delete/rewrite what it explicitly and validly names — the failure mode is "consolidated less," never "lost data."

## `.gobby/dream-log/` changelog

`dream/changelog.py` writes one markdown file per run to `<project_root>/.gobby/dream-log/<ISO-ts>-<run_id>.md`. `.gobby/` is git-tracked (source of truth), so the log is reviewable in version control. Contents: run id, project, trigger, timestamps, durations; **cleanup phase** synopsis (counts per category + bullet list of deleted memory ids with the classifier reason); **consolidation phase** synopsis (kept/merged/superseded/created counts + per-decision block: action, source ids, surviving id, rationale, before/after content excerpt); reconcile warnings if any; and a footer line `Revert: gobby memory dream revert <run_id>`. Directory created if absent; old logs are left in place (git history is the retention policy).

## Revert

`gobby memory dream revert <run_id>` → `revert_dream()`. Require `status == 'completed'` and non-null `snapshot_id` (reject `dry_run`/`reverted`/pre-apply-`failed` with a clear message). One `db.transaction_immediate()`: delete all project memories whose ids are in the snapshot's memory set or were created by the run, then re-INSERT every snapshotted memory + crossref row verbatim from `payload_json` (full column fidelity incl. timestamps, `graph_processed`). Post-commit reconcile (Stage 5 logic) over `(ids the run touched) ∪ (ids the run created)`. Mark run `reverted`; append a "reverted" note to the run's dream-log file. Idempotent — a second revert restores identical rows.

## Auto-dream system cron

- New config `MemoryDreamConfig(FeatureDefaultConfig)` on `MemoryConfig` (`src/gobby/config/persistence.py`), copying `MemoryStaleAuditConfig` fields (`model`, `tier`, `prompt_path`, `max_tokens`) plus: `enabled: bool = True` (on by default), `schedule_cron: str = "0 3 * * *"`, `min_new_memories: int = 1`, the cleanup passthrough defaults.
- `register_memory_dream_cron(...)` in `dream/cron.py`, modeled on `register_github_triage_cron` but as a **single system cron** (not per-project): ensure one system cron row `gobby:memory-dream` (`schedule_type="cron"`, `cron_expr=config.memory.dream.schedule_cron`, `action_type="handler"`, `action_config={"handler":"memory_dream"}`), and register handler `memory_dream` via `CronExecutor.register_handler`. If `config.memory.dream.enabled` is false, disable the existing system cron row (so it can be re-enabled later). Wire the call into `src/gobby/runner_init/orchestration.py` beside the existing `register_github_triage_cron` block, in the same try/except.
- The handler (`dream/trigger.py`, no LLM) on each tick enumerates all projects; for each, `count(memories WHERE project_id=? AND created_at > last_successful_dream.completed_at)` (fallback: created in the trailing 24h if no prior dream). If `>= min_new_memories` and no active dream run for that project → run `dream(trigger="cron")`; else record `skipped: no new memories` for that project. Returns a per-project summary string the `CronRun` records. Deterministic, cheap, no surprise LLM spend for idle projects.

## Retire the existing cleanup pipeline

Per the established retired-template tombstone pattern (`src/gobby/install/shared/CLAUDE.md`): move `src/gobby/install/shared/workflows/pipelines/nightly-memory-cleanup.yaml` into a `pipelines/deprecated/` subdirectory (excluded from sync), and ensure the bundled-sync soft-deletes the installed `nightly-memory-cleanup` row (same mechanism that retires `orchestrator.yaml` et al.). Keep `execute_cleanup`, `audit_memories`, and `cleanup_memories` — they are now invoked internally by dream's Stage 1, not by a pipeline. Add a one-line note to any docs that reference `nightly-memory-cleanup` pointing at `gobby memory dream`.

## CLI / MCP / HTTP surface (all call the one core service)

- **CLI** (`src/gobby/cli/memory_dream.py`, attached to the `memory` group):
  - `gobby memory dream [--dry-run] [--skip-cleanup] [--skip-consolidation] [--memory-type T] [--session-lookback N] [--wait] [--timeout S]` → POSTs to daemon, prints `run_id`; async by default, `--wait` polls.
  - `gobby memory dream status <run_id>` → status, stage checkpoint, counts, changelog path; for `--dry-run` runs prints the plan diff.
  - `gobby memory dream revert <run_id>` → restored counts.
  - Reuse `_get_daemon_client(ctx)` (`src/gobby/cli/memory.py:611`).
- **MCP** (`src/gobby/mcp_proxy/tools/memory_dream.py`): `create_memory_dream_registry(ctx)` registering `memory-dream`, `memory-dream-status`, `memory-dream-revert`; resolve `project_id` via `ctx.get_current_project_id()`, return `result.to_dict()` (as `create_build_registry` does). Register alongside `create_memory_registry` in the proxy registry assembly.
- **HTTP** (`src/gobby/servers/routes/memory_dream.py`): `POST /memory/dream` (body=`DreamOptions`, spawns detached `asyncio.create_task` running `dream()`, returns run id immediately — same idiom as `_execute_run_background` in `mcp_proxy/tools/tasks/_expansion.py`), `GET /memory/dream/{run_id}`, `POST /memory/dream/{run_id}/revert`. Mount where `create_build_router` is mounted.

## Concurrency, recovery, edge cases

- **Per-project lock:** `get_active_for_project` (statuses `pending/running/cleaning/planning/applying`) + `transaction_immediate()` give application- and storage-level mutual exclusion. The cron handler also checks this before firing. A manual run while the nightly cron holds the project is rejected with the active run id.
- **Crash during apply:** single `transaction_immediate` → SQLite auto-rolls back. On daemon restart, an `init_orchestration` reconciliation step (added beside the cron wiring) finds runs stuck in `cleaning/planning/applying` with no persisted result: if no snapshot rows → mark `failed` (DB already consistent via rollback); if snapshot rows exist but result missing → auto-revert from snapshot, then mark `failed` (safety-first).
- **Empty / all-singleton project:** Stage 0/2 short-circuit; zero LLM calls; no snapshot; a minimal changelog noting "nothing to consolidate."
- **LLM failure:** total provider failure before apply → `run_manager.fail`, status `failed`, no snapshot, DB untouched, lock released. Per-window failure → that window degrades to `keep`.
- **Partial reconcile failure:** apply already committed + snapshot-protected; run `completed` with `reconcile_warnings`; recoverable via `gobby memory reconcile` or revert.
- **Revert of dry-run / already-reverted:** rejected with a clear message, no state change.

## Size / refactor notes (1,000-line rule)

All new logic is in the fresh `src/gobby/memory/dream/` package and new CLI/MCP/route modules. Existing files get only small additive hooks: `migrations.py` (one tuple + function), `baseline_schema.sql` (two tables), `config/persistence.py` (one nested config), `runner_init/orchestration.py` (one registration call + restart-recovery step), proxy/server registry assembly points, and a possible `collect_only` param on `execute_cleanup` in `maintenance.py` (additive). `maintenance.py` (~890), `cli/memory.py` (~974), `mcp_proxy/tools/memory.py` (~970) are deliberately not extended. Before editing any of these three for an unrelated reason later, a refactor task should exist per the monolith rule — none is needed for this plan since the additive change to `maintenance.py` keeps it well under 1,000.

## Biggest risk & mitigation

**Irreversible memory loss from a bad consolidation pass** — no native versioning, the op deletes/overwrites rows, an LLM is in the loop, and the user chose apply-immediately. Mitigation, defense-in-depth: (1) mandatory pre-apply row-level snapshot of every affected memory *and* crossref, captured inside the *same* transaction as the mutation, so a committed change can never lack a snapshot; (2) one-command verbatim revert with post-revert reconcile, idempotent; (3) the LLM can only delete/rewrite ids it explicitly and validly names — omitted/malformed/over-budget/low-confidence all degrade to safe `keep`; (4) deterministic-first staging collapses exact duplicates without the LLM, shrinking its blast radius to genuinely ambiguous clusters; (5) every run writes a git-tracked `.gobby/dream-log/` synopsis with the exact revert command, so a bad pass is always visible and trivially undone.

## Critical files to modify / create

- create `src/gobby/memory/dream/*` (package above)
- create `src/gobby/storage/memory_dream_runs.py`
- create `src/gobby/mcp_proxy/tools/memory_dream.py`, `src/gobby/servers/routes/memory_dream.py`, `src/gobby/cli/memory_dream.py`
- create `src/gobby/install/shared/prompts/memory/dream_consolidate.md`
- modify `src/gobby/storage/migrations.py` (+migration), `src/gobby/storage/baseline_schema.sql` (+2 tables)
- modify `src/gobby/config/persistence.py` (+`MemoryDreamConfig`)
- modify `src/gobby/runner_init/orchestration.py` (+cron registration, +restart recovery)
- modify `src/gobby/cli/memory.py` (attach dream subcommands), proxy + server registry assembly (register dream registry/router)
- modify `src/gobby/memory/services/maintenance.py` (additive `collect_only` on `execute_cleanup` if needed)
- move `src/gobby/install/shared/workflows/pipelines/nightly-memory-cleanup.yaml` → `pipelines/deprecated/` (retire)
- reuse (no change): `find_duplicate_memories` & `execute_cleanup` (`maintenance.py`), `_memory_richness_score`/`_is_richer_memory_content` (`memory/services/dedup.py`), `LocalExpansionRunManager` shape (`storage/expansion_runs.py`), `register_github_triage_cron` shape (`github_triage/cron.py`), `build` shared-service shape

## Verification plan

All tests run against an isolated test daemon with `GOBBY_TEST_PROTECT=1` — never the user's daemon/DB.

Unit:
- `tests/memory/dream/test_cluster.py` — union-find, 0.85/0.95 boundaries, richness tie-break, conflict → `needs_llm`.
- `tests/memory/dream/test_llm_pass.py` — malformed-JSON retry, out-of-range index, non-subset `source_ids`, low confidence, omitted cluster → all degrade to `keep`.
- `tests/memory/dream/test_snapshot.py` — snapshot captures memories+crossrefs; restore byte-identical incl. timestamps/`graph_processed`.
- `tests/memory/dream/test_changelog.py` — changelog written to `.gobby/dream-log/`, contains revert command, cleanup+consolidation sections.
- `tests/memory/dream/test_trigger.py` — fires only with new memories, skips idle projects, respects active-run guard.
- `tests/storage/test_memory_dream_runs.py` — status transitions, `get_active_for_project`, `get_latest_successful_for_project`.
- `tests/storage/test_migrations.py` — new migration creates both tables+indexes, idempotent re-run.

Integration (in-memory `LocalDatabase`, fake embed fn, mocked `LLMService.call_feature`):
- End-to-end `dream()` with cleanup+consolidation: seed stale + near-dup + contradictory + singleton memories + session digests → assert cleanup deletions and merge/supersede/keep counts, Qdrant/crossref reconcile, single snapshot covering both phases.
- `--dry-run`: zero writes, plan persisted, dry-run changelog, no snapshot.
- `revert_dream()` round-trip: dream then revert → DB + vector store byte-identical to pre-dream (both phases undone).
- Concurrency: second `dream()` while one active → rejected with active run id.
- Crash recovery: abort mid-`applying` → DB unchanged; restart recovery marks run `failed` / auto-reverts if snapshot present.
- Idle project: cron handler skips, no LLM call asserted.
- Retirement: `nightly-memory-cleanup` no longer syncs as active; its installed row soft-deleted.
- Surfaces: `tests/cli/test_memory_dream.py`, `tests/mcp_proxy/tools/test_memory_dream.py`, `tests/servers/routes/test_memory_dream.py` assert each calls the shared service and returns `to_dict()` (mirroring `test_build.py`).

Manual end-to-end:
1. `uv run gobby start --verbose` (isolated), seed a project with redundant/stale memories + a few sessions.
2. `uv run gobby memory dream --wait` → inspect `.gobby/dream-log/<...>.md`, verify cleanup + consolidation synopsis.
3. `uv run gobby memory dream status <run_id>` and `uv run gobby memory recall` to confirm consolidated set.
4. `uv run gobby memory dream revert <run_id>` → confirm memories restored exactly.
5. Verify the `gobby:memory-dream` system cron exists and is enabled; toggle it off/on; confirm idle-project skip in `CronRun` output.

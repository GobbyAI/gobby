# Memory Rationale and Provenance Schema

> **Plan ID:** memory-rationale-provenance
> **Root task:** #20455 (planning sub-epic under #20442)

## Overview
`kind: framing`

The `memories` table records what an agent chose to remember but never why: the
only provenance is `source_type` (`user`/`agent`) plus `source_session_id`.
Junk memories — frozen review-run logs full of hex identifiers, one-time status
snapshots — pass unfiltered into durable facts and get re-served across
unrelated sessions, while dream's planner, which must cite a concrete
obsolescence signal before every `delete`, has no creation claim to judge
staleness against. This epic adds three columns — `rationale`,
`source_task_id`, `created_by_agent` — as schema migration 396 with the full
gcore identity-chain hop, makes `rationale` a required argument of the
`create_memory` contract, and surfaces the rationale everywhere the memory is
later judged: the dream planner's candidate payload and verdict reasons, the
recall injection payload, and the `recall_signal` events consumed by the
shadow judge. Research on memory-augmented agents shows recall quality
improves when writers must record creation rationale; this plan operationalizes
that finding. It pairs with the already-landed write-time auto-supersede from
#20447 (near-duplicates at similarity >= 0.9 are superseded at create time):
that change deduplicates content, this one makes every surviving row carry a
citable claim about its own durability.

## Constraints
`kind: framing`

- 0.5.0 has not shipped: no backward compatibility, no dual-read paths.
  Existing rows keep `NULL` in all three new columns; every consumer treats
  `NULL` as "no recorded claim", never as an error.
- The checksum-pinned schema baseline (`baseline.sql`, version 375) is
  immutable — new columns, constraints, and indexes go in a numbered migration
  only. The local branch's migration chain currently ends at 394
  (`394_sessions_status_last_activity_index.sql`, assets_root_hash
  `6fa33ac9c62b3e8f829d96089fcba7c764a83193cbcadaa46f4af04cd123e50f`), so this
  epic's migration is number 396. All identity pins — `GOLDEN_LATEST_CHECKSUM`,
  `GOLDEN_ASSETS_ROOT_HASH`, catalog `latest_version`,
  `schema_expected_identity.json`, and the four signed grant golden vectors
  under `tests/runtime_grants/golden/` — are derived from the post-396
  identity; never rewrite them backward.
- A gcore migration is live only after the full identity hop: embedded-asset
  registration with sha256 checksum, catalog-manifest regeneration against the
  protected test PostgreSQL instance, golden identity pins in gcore/gdaemon
  contract tests, the packaged Python identity projection, and a rebuild plus
  new-inode reinstall of all four gcore binaries (macOS kills processes that
  exec an in-place-overwritten signed binary: `cp` to a dotfile, then `mv -f`
  over the name). The daemon must restart afterward so freshly issued runtime
  grants carry the new schema identity; Rust binaries reject grants whose
  identity does not match their embedded chain.
- Postgres-gated Rust contract tests read `GOBBY_SCHEMA_TEST_DATABASE_URL`;
  point it only at the protected test DSN, never the live hub database. The
  regeneration order is the commit-bound release-artifact sequence in
  `docs/guides/account-identity-cutover.md`.
- Agent pytest runs are prefixed with `GOBBY_TEST_PROTECT=1` and scoped to the
  relevant files; the full suite is never run.
- The MCP `create_memory` tool is the enforcement surface for required
  rationale. Storage keeps the column nullable so restore/import paths and
  pre-existing rows load unchanged.

## P1: Schema Migration and Identity Chain
`kind: framing`

**Goal**: `memories` carries `rationale`, `source_task_id`, and
`created_by_agent`, and every element of the gcore schema-identity chain —
embedded assets, catalog manifest, golden pins, packaged Python projection,
installed binaries, runtime grants — agrees on migration 396.

### 1.1 Add migration 396 and complete the gcore identity hop [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/396_memory_rationale_and_provenance.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: the change appends the migration-396 entry to the `MIGRATIONS` const, which is not an indexed symbol, so file-wide scope is the only honest reference
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: the manifest is regenerated wholesale by the `UPDATE_GCORE_SCHEMA_MANIFEST=1` freshness test, not hand-edited
- `crates/gcore/src/grant/bundle.rs::expected_schema_identity`
- `crates/gcore/src/grant/tests.rs::expected_schema_identity_tracks_catalog_head`
- `crates/gcore/src/schema/runner_tests.rs::migrations_directory_exists_and_copy_agent_entry_is_registered`
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: the file is regenerated wholesale from the release gdaemon binary by `scripts/generate_schema_expected_identity.py`, not hand-edited
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: signed golden vector rewritten wholesale (schema_identity, payload_checksum, GOLDEN_SECRET signature) after the identity change
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: signed golden vector rewritten wholesale after the identity change
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: signed golden vector rewritten wholesale after the identity change
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: signed golden vector rewritten wholesale after the identity change

New migration file (`396_memory_rationale_and_provenance.sql`), following the
comment-first style of migration 391:

```sql
-- Memories record what an agent chose to remember but never why. Recall
-- audits show junk rows (frozen review-run logs with hex IDs, one-time
-- status snapshots) being re-served across unrelated sessions, and dream's
-- planner must cite a concrete obsolescence signal for every delete yet has
-- no creation claim to judge staleness against. rationale stores the
-- writer's durable-value claim; source_task_id and created_by_agent extend
-- provenance beyond source_type + source_session_id so verdicts can cite
-- which task and agent produced a memory. All three are NULL on
-- pre-existing rows: NULL means "no recorded claim", never an error.
ALTER TABLE memories ADD COLUMN rationale text;
ALTER TABLE memories ADD COLUMN source_task_id uuid;
ALTER TABLE memories ADD COLUMN created_by_agent text;

ALTER TABLE ONLY memories
    ADD CONSTRAINT memories_source_task_id_fkey
    FOREIGN KEY (source_task_id) REFERENCES tasks(id)
    ON DELETE SET NULL DEFERRABLE;

CREATE INDEX idx_memories_source_task ON memories USING btree (source_task_id);
```

The foreign key mirrors the existing `memories_source_session_id_fkey`
(`REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE`), and the index
mirrors `idx_memories_source_session`, which exists for the same
referencing-side reason.

Identity hop, in the release-artifact order documented in
`docs/guides/account-identity-cutover.md`:

1. Append the `EmbeddedMigration` entry for version 396 to the `MIGRATIONS`
   const in `assets.rs` — `version: 396`, `filename:
   "396_memory_rationale_and_provenance.sql"`, `checksum:` the sha256 of the
   file (compute with `shasum -a 256`), `sql: include_str!(...)` — exactly
   like the version-391 entry.
2. Regenerate the catalog manifest against the protected test database:
   `UPDATE_GCORE_SCHEMA_MANIFEST=1 GOBBY_SCHEMA_TEST_DATABASE_URL="$TEST_DSN"
   cargo test -p gobby-core --features postgres --test
   catalog_manifest_freshness catalog_manifest_is_fresh_for_embedded_assets --
   --exact`.
3. Pin the new chain in the four contract sites: `GOLDEN_LATEST_CHECKSUM` and
   `GOLDEN_ASSETS_ROOT_HASH` next to `expected_schema_identity` in
   `bundle.rs`; `latest_version` 396 in
   `expected_schema_identity_tracks_catalog_head`; `MIGRATIONS.len()` 21 plus
   the `MIGRATIONS[20]` version/filename asserts in
   `migrations_directory_exists_and_copy_agent_entry_is_registered`;
   `latest_asset` version/filename/checksum and `root_hash` in
   `embedded_assets_publish_a_complete_schema_identity`; `latest_version` and
   `latest_checksum` in `version_json_reports_exact_schema_identity_contract`.
4. Rebuild all four release binaries (`cargo build --release -p gobby-daemon
   -p gobby-code -p gobby-hooks -p gobby-wiki`), then regenerate the packaged
   Python projection with `uv run python
   scripts/generate_schema_expected_identity.py --gdaemon
   target/release/gdaemon` so `schema_expected_identity.json` reports
   `latest_version` 396 and the new `latest_checksum`/`assets_root_hash`.
5. Regenerate the four signed grant golden vectors under
   `tests/runtime_grants/golden/` (`brokered_datastores.json`,
   `direct_datastores.json`, `old_client_new_grant.json`,
   `unavailable_datastores.json`): rewrite each `schema_identity` to the
   post-396 identity, recompute `payload_checksum`, and re-sign with
   `GOLDEN_SECRET`, following the signed-vector suite's module docstring. The
   suite asserts every vector's identity equals the packaged Python
   projection, so it stays red between step 4 and this step.
6. Reinstall the four binaries into `~/.gobby/bin/` via new inodes (`cp` to a
   dotfile, `mv -f` over the name), then restart the daemon. The restarted
   daemon applies migration 396 and re-issues runtime grants stamped with the
   new schema identity; gcode/gdaemon/ghook/gwiki accept them again.

**Acceptance:**

- 1.1.1 - Migration 396 adds the three columns, the task foreign key with `ON DELETE SET NULL DEFERRABLE`, and the `idx_memories_source_task` index. file: `crates/gcore/assets/schema/migrations/396_memory_rationale_and_provenance.sql`.
- 1.1.2 - The embedded `MIGRATIONS` const registers version 396 with the file's sha256 checksum, and the runner contract test pins length 21 and the `MIGRATIONS[20]` entry. test: `crates/gcore/src/schema/runner_tests.rs::migrations_directory_exists_and_copy_agent_entry_is_registered`.
- 1.1.3 - The regenerated catalog manifest lists the three new `memories` columns, the foreign-key constraint, and the new index. file: `crates/gcore/assets/schema/catalog.manifest.json`.
- 1.1.4 - Grant and schema identity pins report version 396 with the new checksum and root hash, and both contract tests pass. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
- 1.1.5 - The gdaemon CLI identity contract pins version 396. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.
- 1.1.6 - The packaged Python identity projection reports `latest_version` 396 with matching `latest_checksum` and `assets_root_hash`, regenerated from the release gdaemon. file: `src/gobby/storage/schema_expected_identity.json`.
- 1.1.7 - All four binaries are rebuilt from this commit and reinstalled via new inodes, and after daemon restart the re-issued runtime grants carry the 396 identity accepted by `expected_schema_identity`. symbol: `expected_schema_identity`.
- 1.1.8 - The four signed grant golden vectors carry the post-396 `schema_identity` with recomputed `payload_checksum` and `GOLDEN_SECRET` signatures, and the signed-vector suite passes. test: `tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed`.

## P2: Write Path and Creation Contract
`kind: framing`

**Goal**: every path that writes a memory can persist `rationale`,
`source_task_id`, and `created_by_agent`; the agent-facing `create_memory`
contract requires a rationale and fills task/agent provenance automatically.

### 2.1 Thread the three columns through the Memory model and storage write path [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/memories_models.py::Memory`
- `src/gobby/storage/memories_models.py::Memory.from_row`
- `src/gobby/storage/memories_models.py::Memory.to_dict`
- `src/gobby/storage/memories_crud.py::MemoryCrudMixin.create_memory_with_outcome`
- `src/gobby/storage/memories_crud.py::MemoryCrudMixin.create_memory`
- `src/gobby/memory/services/lifecycle.py::MemoryLifecycleService.create_memory`
- `src/gobby/memory/backends/storage_adapter.py::StorageAdapter.create`
- `src/gobby/memory/backends/null.py::NullBackend.create`
- `src/gobby/memory/facade.py::MemoryManagerFacadeMethods.create_memory`
- `src/gobby/memory/services/dedup.py::DedupService._fallback_store`
- `src/gobby/memory/dream/protocols.py::MemoryDreamManagerProtocol.create_memory`
- `src/gobby/memory/protocol.py::MemoryRecord`
- `src/gobby/memory/protocol.py::MemoryRecord.to_dict`
- `src/gobby/memory/protocol.py::MemoryRecord.from_dict`
- `src/gobby/memory/protocol.py::MemoryBackendProtocol.create`
- `src/gobby/memory/backends/storage_adapter.py::StorageAdapter._to_record`
- `src/gobby/memory/services/repository.py::MemoryRepository.record_to_memory`
- `src/gobby/sync/memories.py::MemoryBackupManager._backup_memories_sync`
- `src/gobby/sync/memories.py::MemoryBackupManager._restore_memories_with_outcomes_sync`
- `src/gobby/memory/dream/storage_journal.py::*` — scope-reason: `_MEMORY_COLUMNS` is a module-level column tuple (it derives `restore_memory_row`'s INSERT and `ON CONFLICT ... SET` lists), not an indexed symbol

Model changes in the `Memory` dataclass:

```python
rationale: str | None = None          # writer's durable-value claim; NULL on legacy rows
source_task_id: str | None = None     # tasks.id UUID; FK ON DELETE SET NULL
created_by_agent: str | None = None   # agent definition name or interactive CLI source
```

`Memory.from_row` hydrates all three (casting `source_task_id` to `str` when
non-NULL, mirroring the `source_session_id` handling), and `Memory.to_dict`
emits them unconditionally so dream snapshots' `before_data`/`after_data`
carry the creation claim.

Storage: `MemoryCrudMixin.create_memory_with_outcome` and the
`MemoryCrudMixin.create_memory` wrapper accept keyword arguments
`rationale: str | None = None`, `source_task_id: str | None = None`, and
`created_by_agent: str | None = None`, and both INSERT statements (the
restore-metadata branch and the fresh-create branch) include the three columns
and bind their values. The duplicate-resolution branches (visible-content
duplicate, 60-second same-session proximity duplicate, ON CONFLICT revival)
keep the existing row's values — write-time dedup never rewrites a surviving
row's creation claim. Restore paths pass whatever the backup row carried.

Plumbing with the same three optional keywords, defaulting to `None`, through
every intermediate signature: `MemoryLifecycleService.create_memory` (which
forwards to `self.backend.create`), `StorageAdapter.create`,
`NullBackend.create` (echoes the fields on its in-memory record),
`MemoryManagerFacadeMethods.create_memory`, the `DedupService._fallback_store`
pass-through, and the `MemoryDreamManagerProtocol.create_memory` protocol
signature so the protocol stays aligned with the facade.

The backend-hop record model moves with the storage model: `MemoryRecord`
gains the same three optional fields, `MemoryRecord.to_dict` and
`MemoryRecord.from_dict` serialize them, the `MemoryBackendProtocol.create`
protocol signature carries the three keywords, `StorageAdapter._to_record`
copies them from the stored `Memory`, `NullBackend.create` sets them on the
record it constructs, and `MemoryRepository.record_to_memory` copies them back
onto `Memory`. Without this hop the lifecycle create path (`create_memory` →
`backend.create` → `_record_to_memory`) returns `Memory` rows whose new fields
are `None` even though the INSERT persisted them, and 2.2's echoed create
payload would misreport what was recorded.

Backup, restore, and dream-journal write paths move in the same leaf:
`MemoryBackupManager._backup_memories_sync` adds `rationale`,
`source_task_id`, and `created_by_agent` to its JSONL record dict;
`_restore_memories_with_outcomes_sync` forwards them to
`create_memory_with_outcome`, nulling `source_task_id` when the referenced
task id does not exist in the target database (mirroring the existing
`source_session_id` nulling) so the new foreign key can never abort a restore;
the restore-metadata ON CONFLICT branch in `create_memory_with_outcome`
updates the three columns when the incoming row wins; and the dream journal's
`_MEMORY_COLUMNS` tuple gains the three columns so `restore_memory_row`'s
derived INSERT and `ON CONFLICT ... SET` lists round-trip them during snapshot
rollback (`get_memory_row` already uses `SELECT *`).

**Acceptance:**

- 2.1.1 - `Memory` carries the three new optional fields and round-trips them through row hydration and dict serialization. symbol: `Memory.from_row`.
- 2.1.2 - Both INSERT branches — fresh-create and restore-metadata, including the restore-metadata ON CONFLICT update when the incoming row wins — persist rationale, source_task_id, and created_by_agent, and a created row reads them back. test: `tests/storage/test_storage_memories.py::test_create_memory_persists_rationale_and_provenance`.
- 2.1.3 - Duplicate resolution (content duplicate and same-session proximity duplicate) preserves the surviving row's original rationale and provenance. test: `tests/storage/test_storage_memories.py::test_duplicate_create_preserves_original_rationale`.
- 2.1.4 - A facade-level `create_memory` round-trip returns a `Memory` whose rationale, source_task_id, and created_by_agent echo what was passed — proving the `MemoryRecord` hop (`_to_record`, `NullBackend.create`, `record_to_memory`) carries the fields, not just the intermediate signatures. test: `tests/memory/test_backends.py::test_create_memory_round_trips_rationale_and_provenance`.
- 2.1.5 - A backup/restore cycle preserves the three fields; restoring a row whose source_task_id is absent from the target database nulls it instead of aborting. test: `tests/sync/test_memory_backup.py::test_backup_restore_preserves_rationale_and_provenance`.
- 2.1.6 - Dream snapshot rollback via `restore_memory_row` round-trips the three columns. test: `tests/storage/test_storage_memories.py::test_restore_memory_row_round_trips_rationale_and_provenance`.

### 2.2 Require rationale at the create surfaces and derive task/agent provenance [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/memory.py::create_memory_registry`
- `src/gobby/mcp_proxy/tools/memory.py::create_memory`
- `src/gobby/servers/routes/memory.py::MemoryCreateRequest`
- `src/gobby/servers/routes/memory.py::create_memory_router`
- `src/gobby/cli/memory/crud.py::create`
- `src/gobby/review_learning/service.py::ReviewLearningService.record`

MCP tool contract (`create_memory` inside `create_memory_registry`):

```python
async def create_memory(
    content: str,
    rationale: str,                      # NEW, required
    memory_type: ... = MemoryType.FACT,
    tags: list[str] | None = None,
    supersedes: list[str] | None = None,
    session_id: str | None = None,
    source_task_id: str | None = None,   # NEW, optional override (#N or UUID)
    created_by_agent: str | None = None, # NEW, optional override
    is_global: bool = False,
) -> dict[str, Any]:
```

Rationale validation runs first, before the ephemeral-implementation-note
skip: strip whitespace; reject empty or whitespace-only values and values
longer than 500 characters with `{"success": False, "error":
"rationale_required: one or two sentences on why this memory should be
re-served to future sessions (max 500 chars)"}`. The docstring defines a good
rationale as the durable-value claim — why a future, unrelated session should
see this — and warns that run logs, status snapshots, and one-time results do
not qualify.

Provenance derivation (new module-level helper in the tool module, using the
resolved session UUID):

- `source_task_id`: when the argument is provided, resolve `#N`/UUID
  references against the tasks table and use the task UUID. When omitted,
  look up the session's open claimed tasks via the existing task-storage
  query surface (`list_tasks` filtered by `claimed_by_session_id`, open
  only): exactly one claim uses that task's id; several claims use the most
  recently updated; none leaves it `NULL`.
- `created_by_agent`: when omitted, use the agent-run record for the session
  (the agents storage `get_by_session` lookup) and its `agent_name`; sessions
  without an agent run fall back to the sessions row's `source` value
  (`claude`, `codex`, ...). Derivation failures degrade to `NULL` with a
  debug log, mirroring the existing session-resolution fallback — creation
  never fails because provenance lookup failed.

The result dict echoes `rationale`, `source_task_id`, and `created_by_agent`
inside the `memory` payload so callers can verify what was recorded, and the
tool registration description tells agents rationale is mandatory.

HTTP surface: `MemoryCreateRequest` gains `rationale: str` (required,
`min_length=1`, `max_length=500`) plus optional `source_task_id` and
`created_by_agent` fields, and the create handler inside
`create_memory_router` forwards all three to the manager. No derivation on
the HTTP path — it is an operator/UI surface and passes explicit values or
`None`.

CLI surface: the `create` command gains a required `--rationale` option and
passes it through with `source_type="user"`; task/agent provenance stays
`NULL` on this operator path.

Review-learning writer: `ReviewLearningService.record` passes a deterministic
rationale for each promoted lesson —
`f"Confirmed review finding ({check_key}): recurring pattern worth re-serving
when similar code is reviewed"` built from the normalized finding's check
key — plus `created_by_agent="review-learning"`, and forwards the recording
session's claimed-task derivation exactly like the MCP tool (reusing the same
helper via the shared storage queries, or passing `None` when no session is
supplied).

**Acceptance:**

- 2.2.1 - `create_memory` rejects a missing, empty, or over-length rationale with the `rationale_required` error and persists a valid one. test: `tests/mcp_proxy/tools/test_memory.py::test_create_memory_requires_rationale`.
- 2.2.2 - With no explicit arguments, a session holding one open claimed task gets that task's UUID as `source_task_id`, and an agent-run session records its `agent_name` while an interactive session records its CLI `source`. test: `tests/mcp_proxy/tools/test_memory.py::test_create_memory_derives_task_and_agent_provenance`.
- 2.2.3 - The HTTP create request requires rationale and the route forwards all three fields. symbol: `MemoryCreateRequest`.
- 2.2.4 - The memory CLI create command requires `--rationale`. symbol: `create`.
- 2.2.5 - Review-learning lesson memories carry the deterministic rationale and `created_by_agent="review-learning"`. symbol: `ReviewLearningService.record`.

## P3: Read-Side Surfacing
`kind: framing`

**Goal**: the creation claim reaches every judge: dream's planner sees and
cites it, recall injection displays it, and `recall_signal` events log it for
the shadow judge.

### 3.1 Surface rationale and provenance to the dream planner and its verdicts [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/memory/dream/models.py::DreamCandidate`
- `src/gobby/memory/dream/models.py::DreamCandidate.to_prompt_dict`
- `src/gobby/memory/dream/candidates.py::memory_to_candidate`
- `src/gobby/install/shared/prompts/memory/dream.md`

`DreamCandidate` gains `rationale: str | None`, `source_task_id: str | None`,
and `created_by_agent: str | None` (defaulting to `None`);
`memory_to_candidate` copies them from the memory row with `getattr(...,
None)` like the existing `source_type` handling. `DreamCandidate.
to_prompt_dict` includes all three keys unconditionally so the planner sees
`"rationale": null` on legacy rows rather than a missing key.

Prompt template (`dream.md`) changes, keeping the existing decision procedure
intact:

- In the candidate-metadata explanation: `rationale` is the writer's own
  claim about why the memory deserved to persist; `source_task_id` and
  `created_by_agent` say which task and agent produced it.
- New staleness rule in the decision procedure: judge each candidate against
  its `rationale`. A rationale that names a one-time event — a specific
  review run, test run, task, or dated status — is a concrete, citable
  time-bound-state signal once that event is over; `source_task_id` referring
  to a completed or closed task corroborates it. A `delete` or `refresh` on a
  candidate with a non-null rationale must quote or paraphrase that rationale
  in its `reason` and say why the claim no longer holds.
- Explicit guard: a `NULL` rationale is a legacy row, not evidence — absence
  of a rationale may corroborate other signals but never justifies `delete`
  on its own.

The planner response schema (`DREAM_ACTIONS_SCHEMA`) is unchanged: the
citation lives in the existing free-text `reason` field, which downstream
verdict records and snapshots already persist.

**Acceptance:**

- 3.1.1 - Dream candidates carry rationale, source_task_id, and created_by_agent, and the prompt payload includes all three keys for both populated and legacy rows. symbol: `DreamCandidate.to_prompt_dict`.
- 3.1.2 - Candidate construction copies the three fields from the memory row. test: `tests/memory/test_dream.py::test_candidate_carries_rationale_and_provenance`.
- 3.1.3 - The planner prompt instructs judging staleness against the rationale, requires delete/refresh reasons to cite a non-null rationale, and forbids treating a null rationale as sufficient delete evidence. file: `src/gobby/install/shared/prompts/memory/dream.md`.

### 3.2 Display rationale in recall injection and log it in recall_signal [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/memory/recall.py::_memory_to_payload`
- `src/gobby/hooks/memory_recall_delivery.py::_memory_bodies`
- `src/gobby/mcp_proxy/tools/memory_recall.py::_next_chunk`
- `src/gobby/memory/context.py::format_memory_metadata_suffix`
- `src/gobby/memory/context.py::build_memory_context`
- `src/gobby/memory/services/_search_models.py::SearchDebugHit`
- `src/gobby/memory/services/_search_debug.py::emit_search_debug`
- `src/gobby/memory/recall_signal_log.py::_hit_to_event`

Injection display:

- `_memory_to_payload` adds `"rationale": memory.rationale` to the delivered
  payload (body field, always present; `None` for legacy rows).
- `_memory_bodies` copies `rationale` into each queued delivery body when it
  is a non-empty string, alongside `memory_type`.
- `_next_chunk` emits the body's rationale on each chunk payload's metadata
  (same placement as `memory_type`), so agents reading chunked recalls see
  the claim with the content.
- `format_memory_metadata_suffix` gains a keyword-only `rationale: str | None
  = None` field rendered as `why: <rationale>` after `memory_id`, and
  `build_memory_context` passes each memory's rationale so `<project-memory>`
  blocks show the claim inline.

Shadow-judge signal:

- `SearchDebugHit` gains `rationale: str | None` and `emit_search_debug`
  populates it from the returned memory objects when building hits.
- `_hit_to_event` logs `"rationale": hit.rationale` on every hit event, so
  `recall_signal.jsonl` rows give the shadow judge the creation claim next to
  the ranking features it audits. `build_recall_signal_event` needs no
  change — it already serializes whatever `_hit_to_event` returns.

**Acceptance:**

- 3.2.1 - Recall payloads, queued delivery bodies, and chunked recall payloads carry the memory's rationale. test: `tests/mcp_proxy/tools/test_memory_recall.py::test_recall_chunks_include_rationale`.
- 3.2.2 - Injected `<project-memory>` context renders `why: <rationale>` in the per-memory metadata suffix. test: `tests/memory/test_context.py::test_memory_context_shows_rationale`.
- 3.2.3 - Every recall_signal hit event includes the rationale field, `None` for legacy rows. test: `tests/memory/test_recall_signal_log.py::test_hit_event_logs_rationale`.
- 3.2.4 - Search debug hits carry rationale from the returned memories. symbol: `emit_search_debug`.

### 3.3 Update memory-skill guidance for required rationale [category: docs] (depends: 2.2)
`kind: deliverable`

Target: `src/gobby/install/shared/skills/memory/SKILL.md`

The memory skill documents the new contract for agents:

- `create_memory` requires `rationale` — one or two sentences stating why a
  future, unrelated session should be served this memory (max 500
  characters). Show one good example (a durable convention with its reason)
  and one bad example (a review-run log with hex IDs, rejected because its
  rationale can only describe a one-time event).
- `source_task_id` and `created_by_agent` are derived automatically from the
  claimed task and the session's agent identity; only pass them to override.
- Dream deletes cite the rationale: a memory whose rationale describes
  one-time state will be reaped once that state passes, so rationale quality
  directly controls memory lifetime.

**Acceptance:**

- 3.3.1 - The skill documents the required rationale with a good and a bad example, the automatic provenance derivation, and the dream-citation consequence. file: `src/gobby/install/shared/skills/memory/SKILL.md`.

## V1 Plan Changelog
`kind: framing`

- Round 0 (initial draft): narrative sections authored; no review rounds yet.

**Round 1** `kind: verification`

- reviewer_run: 57c41dbb-05ee-4edd-99b8-6a1222ac32ff
- reviewer_session: 39b21e88-74ff-4810-8c9f-87d9b940fd98
- verdict: needs_review
- findings:
- F1/blocking/traceability: plan pinned migration 392, `MIGRATIONS.len()` 17, and `MIGRATIONS[16]` while the working tree already ships 392–394 (len 19, identity pins at 394) — implementing as written collides with landed migrations
- F2/blocking/unhandled-edge: `MemoryRecord` and the Memory↔MemoryRecord converters were untargeted, so a lifecycle create would return `Memory` with the three new fields `None` even after a successful INSERT
- F3/blocking/missing-requirement: backup JSONL, restore kwargs, and the dream journal's `_MEMORY_COLUMNS` stayed closed lists despite P2's every-write-path goal and the Constraints restore policy
- resolution_notes: All three accepted after coordinator verification (migrations dir tip 394; `runner_tests.rs:1143` asserts len 19; both identity pins already at 394; `MemoryRecord` closed field set at `protocol.py:176-194`; `sync/memories.py:319-333` restore call without new kwargs plus the source_session_id nulling pattern to mirror; `storage_journal._MEMORY_COLUMNS` closed tuple driving INSERT/ON CONFLICT). Repairs: epic retargeted to migration 395 (`395_memory_rationale_and_provenance.sql`, `MIGRATIONS.len()` 20, `MIGRATIONS[19]`; Constraints now name the 394 chain end and forbid rewriting identity pins backward). 2.1 gained targets and prose for `MemoryRecord`/`to_dict`/`from_dict`, `MemoryBackendProtocol.create`, `StorageAdapter._to_record`, `MemoryRepository.record_to_memory`, `MemoryBackupManager._backup_memories_sync`/`_restore_memories_with_outcomes_sync` (with source_task_id nulling mirroring source_session_id), and the dream journal's `_MEMORY_COLUMNS`. Acceptance: 2.1.2 now covers the restore-metadata ON CONFLICT branch, 2.1.4 requires a facade-level round-trip through the record hop, and new 2.1.5/2.1.6 pin backup/restore preservation and snapshot rollback.

```json plan-review-round
{"evidence_id":"c4ff0395-247d-44de-ab30-cda86a13140d","plan_hash":"380b1294c67235dc93af07593e1d8396362a8dab45f4bdd16372e9087386b8a7","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"68068c06bb3677ce7b686c16dafbbbf195eaa85aec013ce0c8406cee45c09399","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":10,"emitted_findings":3,"total":13},"evidence_id":"c4ff0395-247d-44de-ab30-cda86a13140d","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":6,"manifest_digest":"cdfa3f23dab3148cff0674b9d072d127eb29a5d1ef370ff39b326f3e1d89bbe6","status":"valid"},"source_digest":"d68c5e602ba9da310f4362ae0d618ad3d249df5f01983706398a6532d44eccb0","version":1},"findings":[{"category":"traceability","check_key":"schema-identity-head","description":"The working tree already ships 392_chat_attachments_deletion_lease, 393_interactive_principal_hardening, and 394_sessions_status_last_activity_index. MIGRATIONS.len() is 19 and MIGRATIONS[16] is the chat-attachments lease. expected_schema_identity.json and expected_schema_identity() already pin latest_version 394. Overview, Constraints, P1, and 1.1 still name migration 392, length 17, and MIGRATIONS[16]. Implementing 1.1 as written collides with a landed migration and would regress identity pins.","finding_id":"F1","fix":"Retarget the epic to migration 395 (395_memory_rationale_and_provenance.sql). Update Constraints to say the chain currently ends at 394. Pin MIGRATIONS.len() 20 and MIGRATIONS[19]. Drive GOLDEN checksums, catalog latest_version, and schema_expected_identity.json from the post-395 identity; never rewrite them back to 392.","location":"Constraints / P1 / § 1.1","prevention":"Re-read crates/gcore/assets/schema/migrations/, MIGRATIONS.len(), and schema_expected_identity.json latest_version immediately before locking a migration number.","principle":"A gcore migration number must be the next free slot on the working-tree identity chain.","root_cause":"The draft pinned 392 when the tree still ended at 391; 392–394 landed before review.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"backend-record-field-drop","description":"MemoryLifecycleService.create_memory calls backend.create, then _record_to_memory. StorageAdapter.create maps the stored Memory through _to_record into MemoryRecord; NullBackend.create constructs MemoryRecord directly. MemoryRecord, to_dict, from_dict, and record_to_memory have no rationale, source_task_id, or created_by_agent. 2.1 does not target those symbols. A successful INSERT can still return Memory with those fields None, so 2.2's echoed create payload is false even when the row persisted.","finding_id":"F2","fix":"Add the three fields to MemoryRecord (including to_dict/from_dict), MemoryBackendProtocol.create, StorageAdapter._to_record, NullBackend.create's MemoryRecord construction, and MemoryRepository.record_to_memory, and list them as 2.1 Targets. Acceptance 2.1.4 must assert a facade/lifecycle create round-trip, not only storage.create_memory_with_outcome.","location":"P2 / § 2.1 (affects 2.2 return payload)","prevention":"For every new memories column, grep MemoryRecord, _to_record, record_to_memory, and MemoryBackendProtocol.create before declaring the write path complete.","principle":"Fields added to Memory must be modeled on MemoryRecord and copied by every Memory↔MemoryRecord converter on the create return path.","root_cause":"2.1 threads kwargs through StorageAdapter.create and NullBackend.create but leaves MemoryRecord and the converters as a closed field set.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"persist-all-write-paths","description":"P2 claims every write path can persist rationale, source_task_id, and created_by_agent; Constraints say restore/import paths load unchanged. Backup _backup_memories_sync emits a closed JSONL dict; _restore_memories_with_outcomes_sync calls create_memory_with_outcome without the new kwargs. Dream restore_memory_row INSERTs only _MEMORY_COLUMNS, so snapshot rollback drops the fields even though get_memory_row uses SELECT *. The restore-metadata ON CONFLICT branch must also update the new columns when the incoming row wins, and restore must NULL source_task_id when the task UUID is missing or the new FK aborts the restore. digest.py and services/crossref.py do not insert memories; they are not this gap.","finding_id":"F3","fix":"Add Targets for MemoryBackupManager._backup_memories_sync, _restore_memories_with_outcomes_sync, and storage_journal._MEMORY_COLUMNS/restore_memory_row. Specify backup JSONL fields, restore kwargs, missing-task nulling (mirror source_session_id), both INSERT column lists, and the restore-metadata ON CONFLICT SET cases. Extend 2.1.2 to cover restore-metadata INSERT, not only fresh create.","location":"P2 goal / Constraints restore policy / § 2.1","prevention":"Inventory INSERT INTO memories, create_memory_with_outcome callers, and explicit column tuples whenever adding memories columns.","principle":"If restore/import must load unchanged and every write path must be able to persist the new columns, every INSERT/serialize caller needs those fields.","root_cause":"2.1 only retargets the lifecycle/storage create signatures; backup JSONL, restore kwargs, and dream-journal _MEMORY_COLUMNS stay closed lists.","section_id":"2.1","severity":"blocking"}],"round_number":1,"verdict":"needs_review"},"session_id":"15e5b0c6-7211-4308-ab2a-26720a8cb358"}
```

**Round 2** `kind: verification`

- reviewer_run: eb62a2bf-aff1-46b7-8d46-0c69cdd63bdf
- reviewer_session: 47b3b2d6-3353-4312-9611-9db4129e0747
- verdict: needs_review
- findings:
- F1/blocking/traceability: after the round-1 retarget to migration 395, Constraints and 1.1 still omitted the four signed grant golden vectors (`tests/runtime_grants/golden/*.json`, all pinning latest_version 394), so implementing 1.1.6 as written would fail `test_config_revision_signed`. Vote: accepted.
- resolution_notes: Coordinator verified independently before voting: all four golden files pin latest_version 394 / latest_checksum 449a7e2e...; `test_config_revision_signed` asserts each vector's schema_identity equals `expected_schema_identity()` plus a GOLDEN_SECRET signature; the module docstring mandates wholesale regeneration after identity changes; the plan had zero `tests/runtime_grants` references. Repairs: the four golden vectors added to 1.1 Targets with wholesale-regeneration scope-reasons; new identity-hop step 5 (rewrite schema_identity, recompute payload_checksum, re-sign with GOLDEN_SECRET; old step 5 renumbered to 6); new acceptance 1.1.8 gated by `test_config_revision_signed`; Constraints' identity-pin list now names the four signed golden vectors. Base validation passes.

```json plan-review-round
{"evidence_id":"b1d294a4-1cf7-495c-8f64-de6b84499d2f","plan_hash":"f8334d369dac58baf319a3c208daef133533330aebe33085b78724708a5f19c8","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"cc815c9349358a01ce6af7cc8635b9fa285cbf04d06a724a236bad1884bf2377","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":5,"emitted_findings":1,"total":6},"evidence_id":"b1d294a4-1cf7-495c-8f64-de6b84499d2f","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":1,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":6,"manifest_digest":"148cd2917176e995b9ce791bc115004f283e60653149cb2402fc61e7b6d1371c","status":"valid"},"source_digest":"4e2d38813de171163cb72730e314338a0210a3a1f6c6f881d357419c014f57f8","version":1},"findings":[{"category":"traceability","check_key":"schema-identity-golden-consumer-inventory","description":"After the round-1 retarget to migration 395, Constraints and 1.1 still omit the signed grant golden vectors. tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed asserts each tests/runtime_grants/golden/*.json schema_identity equals expected_schema_identity() from the packaged Python projection, and the module docstring requires regenerating every golden file (schema_identity, payload_checksum, GOLDEN_SECRET signature) after that identity changes. Those four files currently pin latest_version 394 and latest_checksum 449a7e2e482c086b063fb066e93019c043703ce970ed1a0c7c30f46e7050d097. Implementing 1.1.6 as written updates schema_expected_identity.json to 395 and then fails this suite. Adjacent pins in bundle.rs, grant/tests.rs, schema_contract.rs, and cli_contract.rs are already 1.1 Targets.","finding_id":"F1","fix":"Add Targets for tests/runtime_grants/golden/brokered_datastores.json, direct_datastores.json, old_client_new_grant.json, and unavailable_datastores.json (bare JSON paths or ::* with a regenerate-wholesale scope-reason). Extend the identity-hop steps so that after regenerating schema_expected_identity.json, those four files are rewritten with the post-395 schema_identity, recomputed payload_checksum, and GOLDEN_SECRET signature. Add acceptance 1.1.8 gated by tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed. Name the same four files in Constraints' identity-pin list.","location":"Constraints / P1 / § 1.1","participating_section_ids":["1.1","Constraints"],"prevention":"For every schema-version bump, search tests/runtime_grants/golden, test_golden_vectors.py, GOLDEN_LATEST_CHECKSUM, latest_version pins, payload checksums, and signatures before locking Targets.","principle":"A signed schema-identity migration must update every pinned version, checksum, signature, and golden vector that asserts equality with expected_schema_identity().","root_cause":"Round-1 retargeted 1.1 to migration 395 and the gcore/gdaemon/Python identity pins, but left the signed Python grant golden vectors (latest_version 394) outside Targets and acceptance.","section_id":"1.1","severity":"blocking"}],"round_number":2,"verdict":"needs_review"},"session_id":"15e5b0c6-7211-4308-ab2a-26720a8cb358"}
```

**Round 3** `kind: verification`

- reviewer_run: 7fbb47c6-e43e-4575-8a76-d44388863c04
- reviewer_session: 3cf3f07f-7624-4e43-b4ce-37333dd59b87
- verdict: approved
- findings:
- none — zero findings; all seven candidate leads dismissed after verification
- resolution_notes: Final round (cap 3). Adversary verified the round-2 repairs (golden-vector regeneration step, Constraints pin list, 1.1 targets and acceptance 1.1.8) against the working tree and emitted no findings. Coverage attestation completed all three lanes (requirements_traceability, repository_blast_radius, runtime_invariants) with a valid 6-entry shadow manifest. M1 manifest applied via apply_plan_review_manifest (digest 1945a83ec144c20820fcec8441edf777b4e8cdc70121d8a015a57924f337d7fc); no plan edits this round.

```json plan-review-round
{"evidence_id":"c9eec6ab-7048-4f4f-b4fc-6f1f1bb6a823","plan_hash":"8d9a76cae7d929a95b5b627b4bf11d04e4d3d92f1d4521874528a326250d440c","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f05d2636529bf3b795e7a22a2a3fa9fd0c8e6ffd91d9e454bbc8262f65bc052f","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":0,"total":7},"evidence_id":"c9eec6ab-7048-4f4f-b4fc-6f1f1bb6a823","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":6,"manifest_digest":"e756e69afa88925e49fb8d80d1542215c78a14a7709533948dc6b97f2cb2d401","status":"valid"},"source_digest":"e1390512d51f9c013c008319ee6290be5e6f118e6c62465dc29120bbff1bc8d9","version":1},"findings":[],"manifest_entries":[{"category":"code","depends_on":[],"implementation_domain":"backend","labels":["covers:memory-rationale-provenance:1.1:1.1.1","covers:memory-rationale-provenance:1.1:1.1.2","covers:memory-rationale-provenance:1.1:1.1.3","covers:memory-rationale-provenance:1.1:1.1.4","covers:memory-rationale-provenance:1.1:1.1.5","covers:memory-rationale-provenance:1.1:1.1.6","covers:memory-rationale-provenance:1.1:1.1.7","covers:memory-rationale-provenance:1.1:1.1.8"],"source_section":"1.1","task_type":"feature","tdd":true,"title":"Add migration 395 and complete the gcore identity hop","validation_criteria":"1.1.1: Migration 395 adds the three columns, the task foreign key with `ON DELETE SET NULL DEFERRABLE`, and the `idx_memories_source_task` index. file: `crates/gcore/assets/schema/migrations/395_memory_rationale_and_provenance.sql`.\n1.1.2: The embedded `MIGRATIONS` const registers version 395 with the file's sha256 checksum, and the runner contract test pins length 20 and the `MIGRATIONS[19]` entry. test: `crates/gcore/src/schema/runner_tests.rs::migrations_directory_exists_and_copy_agent_entry_is_registered`.\n1.1.3: The regenerated catalog manifest lists the three new `memories` columns, the foreign-key constraint, and the new index. file: `crates/gcore/assets/schema/catalog.manifest.json`.\n1.1.4: Grant and schema identity pins report version 395 with the new checksum and root hash, and both contract tests pass. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.\n1.1.5: The gdaemon CLI identity contract pins version 395. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.\n1.1.6: The packaged Python identity projection reports `latest_version` 395 with matching `latest_checksum` and `assets_root_hash`, regenerated from the release gdaemon. file: `src/gobby/storage/schema_expected_identity.json`.\n1.1.7: All four binaries are rebuilt from this commit and reinstalled via new inodes, and after daemon restart the re-issued runtime grants carry the 395 identity accepted by `expected_schema_identity`. symbol: `expected_schema_identity`.\n1.1.8: The four signed grant golden vectors carry the post-395 `schema_identity` with recomputed `payload_checksum` and `GOLDEN_SECRET` signatures, and the signed-vector suite passes. test: `tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed`."},{"category":"code","depends_on":["1.1"],"implementation_domain":"backend","labels":["covers:memory-rationale-provenance:2.1:2.1.1","covers:memory-rationale-provenance:2.1:2.1.2","covers:memory-rationale-provenance:2.1:2.1.3","covers:memory-rationale-provenance:2.1:2.1.4","covers:memory-rationale-provenance:2.1:2.1.5","covers:memory-rationale-provenance:2.1:2.1.6"],"source_section":"2.1","task_type":"feature","tdd":true,"title":"Thread the three columns through the Memory model and storage write path","validation_criteria":"2.1.1: `Memory` carries the three new optional fields and round-trips them through row hydration and dict serialization. symbol: `Memory.from_row`.\n2.1.2: Both INSERT branches — fresh-create and restore-metadata, including the restore-metadata ON CONFLICT update when the incoming row wins — persist rationale, source_task_id, and created_by_agent, and a created row reads them back. test: `tests/storage/test_storage_memories.py::test_create_memory_persists_rationale_and_provenance`.\n2.1.3: Duplicate resolution (content duplicate and same-session proximity duplicate) preserves the surviving row's original rationale and provenance. test: `tests/storage/test_storage_memories.py::test_duplicate_create_preserves_original_rationale`.\n2.1.4: A facade-level `create_memory` round-trip returns a `Memory` whose rationale, source_task_id, and created_by_agent echo what was passed — proving the `MemoryRecord` hop (`_to_record`, `NullBackend.create`, `record_to_memory`) carries the fields, not just the intermediate signatures. test: `tests/memory/test_backends.py::test_create_memory_round_trips_rationale_and_provenance`.\n2.1.5: A backup/restore cycle preserves the three fields; restoring a row whose source_task_id is absent from the target database nulls it instead of aborting. test: `tests/sync/test_memory_backup.py::test_backup_restore_preserves_rationale_and_provenance`.\n2.1.6: Dream snapshot rollback via `restore_memory_row` round-trips the three columns. test: `tests/storage/test_storage_memories.py::test_restore_memory_row_round_trips_rationale_and_provenance`."},{"category":"code","depends_on":["2.1"],"implementation_domain":"backend","labels":["covers:memory-rationale-provenance:2.2:2.2.1","covers:memory-rationale-provenance:2.2:2.2.2","covers:memory-rationale-provenance:2.2:2.2.3","covers:memory-rationale-provenance:2.2:2.2.4","covers:memory-rationale-provenance:2.2:2.2.5"],"source_section":"2.2","task_type":"feature","tdd":true,"title":"Require rationale at the create surfaces and derive task/agent provenance","validation_criteria":"2.2.1: `create_memory` rejects a missing, empty, or over-length rationale with the `rationale_required` error and persists a valid one. test: `tests/mcp_proxy/tools/test_memory.py::test_create_memory_requires_rationale`.\n2.2.2: With no explicit arguments, a session holding one open claimed task gets that task's UUID as `source_task_id`, and an agent-run session records its `agent_name` while an interactive session records its CLI `source`. test: `tests/mcp_proxy/tools/test_memory.py::test_create_memory_derives_task_and_agent_provenance`.\n2.2.3: The HTTP create request requires rationale and the route forwards all three fields. symbol: `MemoryCreateRequest`.\n2.2.4: The memory CLI create command requires `--rationale`. symbol: `create`.\n2.2.5: Review-learning lesson memories carry the deterministic rationale and `created_by_agent=\"review-learning\"`. symbol: `ReviewLearningService.record`."},{"category":"code","depends_on":["2.1"],"implementation_domain":"backend","labels":["covers:memory-rationale-provenance:3.1:3.1.1","covers:memory-rationale-provenance:3.1:3.1.2","covers:memory-rationale-provenance:3.1:3.1.3"],"source_section":"3.1","task_type":"feature","tdd":true,"title":"Surface rationale and provenance to the dream planner and its verdicts","validation_criteria":"3.1.1: Dream candidates carry rationale, source_task_id, and created_by_agent, and the prompt payload includes all three keys for both populated and legacy rows. symbol: `DreamCandidate.to_prompt_dict`.\n3.1.2: Candidate construction copies the three fields from the memory row. test: `tests/memory/test_dream.py::test_candidate_carries_rationale_and_provenance`.\n3.1.3: The planner prompt instructs judging staleness against the rationale, requires delete/refresh reasons to cite a non-null rationale, and forbids treating a null rationale as sufficient delete evidence. file: `src/gobby/install/shared/prompts/memory/dream.md`."},{"category":"code","depends_on":["2.1"],"implementation_domain":"backend","labels":["covers:memory-rationale-provenance:3.2:3.2.1","covers:memory-rationale-provenance:3.2:3.2.2","covers:memory-rationale-provenance:3.2:3.2.3","covers:memory-rationale-provenance:3.2:3.2.4"],"source_section":"3.2","task_type":"feature","tdd":true,"title":"Display rationale in recall injection and log it in recall_signal","validation_criteria":"3.2.1: Recall payloads, queued delivery bodies, and chunked recall payloads carry the memory's rationale. test: `tests/mcp_proxy/tools/test_memory_recall.py::test_recall_chunks_include_rationale`.\n3.2.2: Injected `<project-memory>` context renders `why: <rationale>` in the per-memory metadata suffix. test: `tests/memory/test_context.py::test_memory_context_shows_rationale`.\n3.2.3: Every recall_signal hit event includes the rationale field, `None` for legacy rows. test: `tests/memory/test_recall_signal_log.py::test_hit_event_logs_rationale`.\n3.2.4: Search debug hits carry rationale from the returned memories. symbol: `emit_search_debug`."},{"assigned_agent":"tech-writer","category":"docs","depends_on":["2.2"],"labels":["covers:memory-rationale-provenance:3.3:3.3.1"],"source_section":"3.3","task_type":"feature","tdd":false,"title":"Update memory-skill guidance for required rationale","validation_criteria":"3.3.1: The skill documents the required rationale with a good and a bad example, the automatic provenance derivation, and the dream-citation consequence. file: `src/gobby/install/shared/skills/memory/SKILL.md`."}],"round_number":3,"routing_decisions":{"1.1":{"category":"code","implementation_domain":"backend","tdd":true},"2.1":{"category":"code","depends_on":["1.1"],"implementation_domain":"backend","tdd":true},"2.2":{"category":"code","depends_on":["2.1"],"implementation_domain":"backend","tdd":true},"3.1":{"category":"code","depends_on":["2.1"],"implementation_domain":"backend","tdd":true},"3.2":{"category":"code","depends_on":["2.1"],"implementation_domain":"backend","tdd":true},"3.3":{"category":"docs","depends_on":["2.2"],"tdd":false}},"verdict":"approved"},"session_id":"15e5b0c6-7211-4308-ab2a-26720a8cb358"}
```

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add migration 396 and complete the gcore identity hop
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Migration 396 adds the three columns, the task foreign
    key with `ON DELETE SET NULL DEFERRABLE`, and the `idx_memories_source_task` index.
    file: `crates/gcore/assets/schema/migrations/396_memory_rationale_and_provenance.sql`.

    1.1.2: The embedded `MIGRATIONS` const registers version 396 with the file''s
    sha256 checksum, and the runner contract test pins length 21 and the `MIGRATIONS[20]`
    entry. test: `crates/gcore/src/schema/runner_tests.rs::migrations_directory_exists_and_copy_agent_entry_is_registered`.

    1.1.3: The regenerated catalog manifest lists the three new `memories` columns,
    the foreign-key constraint, and the new index. file: `crates/gcore/assets/schema/catalog.manifest.json`.

    1.1.4: Grant and schema identity pins report version 396 with the new checksum
    and root hash, and both contract tests pass. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.

    1.1.5: The gdaemon CLI identity contract pins version 396. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.

    1.1.6: The packaged Python identity projection reports `latest_version` 396 with
    matching `latest_checksum` and `assets_root_hash`, regenerated from the release
    gdaemon. file: `src/gobby/storage/schema_expected_identity.json`.

    1.1.7: All four binaries are rebuilt from this commit and reinstalled via new
    inodes, and after daemon restart the re-issued runtime grants carry the 396 identity
    accepted by `expected_schema_identity`. symbol: `expected_schema_identity`.

    1.1.8: The four signed grant golden vectors carry the post-396 `schema_identity`
    with recomputed `payload_checksum` and `GOLDEN_SECRET` signatures, and the signed-vector
    suite passes. test: `tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed`.'
  labels:
  - covers:memory-rationale-provenance:1.1:1.1.1
  - covers:memory-rationale-provenance:1.1:1.1.2
  - covers:memory-rationale-provenance:1.1:1.1.3
  - covers:memory-rationale-provenance:1.1:1.1.4
  - covers:memory-rationale-provenance:1.1:1.1.5
  - covers:memory-rationale-provenance:1.1:1.1.6
  - covers:memory-rationale-provenance:1.1:1.1.7
  - covers:memory-rationale-provenance:1.1:1.1.8
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Thread the three columns through the Memory model and storage write path
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: "2.1.1: `Memory` carries the three new optional fields and\
    \ round-trips them through row hydration and dict serialization. symbol: `Memory.from_row`.\n\
    2.1.2: Both INSERT branches \u2014 fresh-create and restore-metadata, including\
    \ the restore-metadata ON CONFLICT update when the incoming row wins \u2014 persist\
    \ rationale, source_task_id, and created_by_agent, and a created row reads them\
    \ back. test: `tests/storage/test_storage_memories.py::test_create_memory_persists_rationale_and_provenance`.\n\
    2.1.3: Duplicate resolution (content duplicate and same-session proximity duplicate)\
    \ preserves the surviving row's original rationale and provenance. test: `tests/storage/test_storage_memories.py::test_duplicate_create_preserves_original_rationale`.\n\
    2.1.4: A facade-level `create_memory` round-trip returns a `Memory` whose rationale,\
    \ source_task_id, and created_by_agent echo what was passed \u2014 proving the\
    \ `MemoryRecord` hop (`_to_record`, `NullBackend.create`, `record_to_memory`)\
    \ carries the fields, not just the intermediate signatures. test: `tests/memory/test_backends.py::test_create_memory_round_trips_rationale_and_provenance`.\n\
    2.1.5: A backup/restore cycle preserves the three fields; restoring a row whose\
    \ source_task_id is absent from the target database nulls it instead of aborting.\
    \ test: `tests/sync/test_memory_backup.py::test_backup_restore_preserves_rationale_and_provenance`.\n\
    2.1.6: Dream snapshot rollback via `restore_memory_row` round-trips the three\
    \ columns. test: `tests/storage/test_storage_memories.py::test_restore_memory_row_round_trips_rationale_and_provenance`."
  labels:
  - covers:memory-rationale-provenance:2.1:2.1.1
  - covers:memory-rationale-provenance:2.1:2.1.2
  - covers:memory-rationale-provenance:2.1:2.1.3
  - covers:memory-rationale-provenance:2.1:2.1.4
  - covers:memory-rationale-provenance:2.1:2.1.5
  - covers:memory-rationale-provenance:2.1:2.1.6
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Require rationale at the create surfaces and derive task/agent provenance
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: `create_memory` rejects a missing, empty, or over-length
    rationale with the `rationale_required` error and persists a valid one. test:
    `tests/mcp_proxy/tools/test_memory.py::test_create_memory_requires_rationale`.

    2.2.2: With no explicit arguments, a session holding one open claimed task gets
    that task''s UUID as `source_task_id`, and an agent-run session records its `agent_name`
    while an interactive session records its CLI `source`. test: `tests/mcp_proxy/tools/test_memory.py::test_create_memory_derives_task_and_agent_provenance`.

    2.2.3: The HTTP create request requires rationale and the route forwards all three
    fields. symbol: `MemoryCreateRequest`.

    2.2.4: The memory CLI create command requires `--rationale`. symbol: `create`.

    2.2.5: Review-learning lesson memories carry the deterministic rationale and `created_by_agent="review-learning"`.
    symbol: `ReviewLearningService.record`.'
  labels:
  - covers:memory-rationale-provenance:2.2:2.2.1
  - covers:memory-rationale-provenance:2.2:2.2.2
  - covers:memory-rationale-provenance:2.2:2.2.3
  - covers:memory-rationale-provenance:2.2:2.2.4
  - covers:memory-rationale-provenance:2.2:2.2.5
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Surface rationale and provenance to the dream planner and its verdicts
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '3.1.1: Dream candidates carry rationale, source_task_id, and
    created_by_agent, and the prompt payload includes all three keys for both populated
    and legacy rows. symbol: `DreamCandidate.to_prompt_dict`.

    3.1.2: Candidate construction copies the three fields from the memory row. test:
    `tests/memory/test_dream.py::test_candidate_carries_rationale_and_provenance`.

    3.1.3: The planner prompt instructs judging staleness against the rationale, requires
    delete/refresh reasons to cite a non-null rationale, and forbids treating a null
    rationale as sufficient delete evidence. file: `src/gobby/install/shared/prompts/memory/dream.md`.'
  labels:
  - covers:memory-rationale-provenance:3.1:3.1.1
  - covers:memory-rationale-provenance:3.1:3.1.2
  - covers:memory-rationale-provenance:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Display rationale in recall injection and log it in recall_signal
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '3.2.1: Recall payloads, queued delivery bodies, and chunked
    recall payloads carry the memory''s rationale. test: `tests/mcp_proxy/tools/test_memory_recall.py::test_recall_chunks_include_rationale`.

    3.2.2: Injected `<project-memory>` context renders `why: <rationale>` in the per-memory
    metadata suffix. test: `tests/memory/test_context.py::test_memory_context_shows_rationale`.

    3.2.3: Every recall_signal hit event includes the rationale field, `None` for
    legacy rows. test: `tests/memory/test_recall_signal_log.py::test_hit_event_logs_rationale`.

    3.2.4: Search debug hits carry rationale from the returned memories. symbol: `emit_search_debug`.'
  labels:
  - covers:memory-rationale-provenance:3.2:3.2.1
  - covers:memory-rationale-provenance:3.2:3.2.2
  - covers:memory-rationale-provenance:3.2:3.2.3
  - covers:memory-rationale-provenance:3.2:3.2.4
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Update memory-skill guidance for required rationale
  category: docs
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '3.3.1: The skill documents the required rationale with a good
    and a bad example, the automatic provenance derivation, and the dream-citation
    consequence. file: `src/gobby/install/shared/skills/memory/SKILL.md`.'
  labels:
  - covers:memory-rationale-provenance:3.3:3.3.1
  tdd: false
  source_section: '3.3'
  assigned_agent: tech-writer
```

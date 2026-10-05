Plan artifact: `.gobby/plans/workspace-index-pin.md`

# Pin workspace indexes to the selected fork commit

**Plan ID:** workspace-index-pin
**Implementation root:** #23433 (workspace index pin and spawn cost)
**Status:** Accepted design; implementation proceeds in independently verifiable deliverables.

## R1 Decision record
`kind: framing`

A default worktree or local clone forks the caller checkout's local HEAD, including
unpushed commits, without fetching. An explicit local branch selects its local tip;
an explicit remote branch fetches and selects its latest tip. The selected commit is
resolved before creating or refreshing a workspace. An existing branch may not
silently substitute another commit. A reused agent worktree rebases its work onto
the selected commit and receives a new pin; an integration workspace refresh keeps
its merged commits. External URL clones have no live ancestor index and cold index.

The pin is a workspace-owned snapshot of effective file selectors, each naming a
source project, path, and content hash. Only clean indexed files whose recorded Git
blob ID equals the selected commit's tree entry qualify. Dirty, untracked, ignored,
or mismatched ancestor paths become explicit child indexing gaps. A selected commit
with no matching live indexed checkout has no inherited rows and uses the cold path.
Ask continues to read the caller's live index; its commit IDs do not select an index.

The pin governs every read lane, including graph and vector, and remains a
content-GC root until the child overlay is purged. When a source projection moves,
stale graph and vector rows are excluded immediately, and the existing
projection-sync lifecycle materializes only the missing pinned paths for the child.

## R2 Constraints and evidence
`kind: framing`

The 2026-10-04 observations were 34.9 s and 71.6 s in `code_index_index`, after a
15.3 s observation on 2026-09-28. The 2 s figure is a soft comparison target.
Memory 9644c698 requires a load-matched comparison before attributing a slowdown
to host load. Memory f1e1200c requires reused worktree conflicts to preserve
continuation ancestry and return a recoverable error.

Idle baseline, observed on #23433 at 2026-10-05 02:17 CDT (run
`d50ddf68-0066-4ce2-b39f-316bdbc36b41`): a Claude worktree spawn with base branch
`0.5.0`, forked at `084f759f15`, no other agents running, a healthy parent index at
that commit, and no content difference from the indexed parent commit. The
`spawn_agent` call took 33.9 s wall. `phase_timings_ms` recorded
`code_index_status` 979 ms, `code_index_index` 7,064 ms,
`code_index_search_content` 192 ms, `_preflight_srt` 1,234 ms, and every other
timed phase under 100 ms; the timed phases sum to 9.7 s, leaving about 24 s in no
timed phase. The daemon log timeline: 02:17:35 worktree start, 02:17:38 isolation
sidecar written, 02:17:58 MCP config written, 02:18:08 SRT verification,
02:18:09 Claude trust pre-approved and timings logged. The parent index's
`last_indexed_at` advanced to 07:18:54 UTC, after the spawn.

Attribution. The 7.064 s `code_index_index` on a zero-diff fork is this plan's
defect against Josh's 2026-10-05 contract that a new worktree should "copy, clone,
fork, or mirror" its fork commit's index: overlay reconcile runs full discovery,
compares against the parent's moving HEAD and status, and runs community
partitioning before its unchanged check (P3 and P4 remove each). The untimed 24 s
falls before `phase_timings_ms` exists: `spawn_agent` prepares isolation
before it creates the timing map (`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`,
observed by the enhancer with `gcode evidence`, excerpt hashes `77ee4ba3…` and
`e4911299…`). Between the sidecar write and the MCP config write, isolation repair
copies hooks, rewrites the marker, awaits the Python environment preseed, then
writes MCP config (`src/gobby/agents/isolation_repair.py`, excerpt `aea53db5…`);
the preseed runs `uv sync --offline --frozen --link-mode copy`
(`src/gobby/agents/python_env_seed.py`, excerpt `e612a216…`). The 20 s gap is
therefore most plausibly Python environment seeding, and worktree creation
explains most of the preceding 3 s. That attribution is inferred from source and
log order, not measured; this plan measures it. Attributing isolation cost is in
scope. Optimizing Python environment seeding is outside this index-pin plan.

The current checkout has migration 458 as its head. Observed 2026-10-05:
`git log --all --diff-filter=A --name-only --format= -- 'crates/gcore/assets/schema/migrations/459_*' 'crates/gcore/assets/schema/migrations/46*'`
returned no files, so no ref adds 459 or later. The Orchestrator assigned 459 to
#23439 (bigint session usage counters) and 460 to this plan:
`460_code_overlay_pins.sql`. If another migration lands first, the executor
repeats that all-ref sweep and takes the next free number.
The migration source requires its catalog, grant, schema-contract, CLI-contract,
and Python expected-identity carriers in the same deliverable.

The code index source is the PostgreSQL hub. The current overlay catalog resolves
inherited rows from the parent's current selectors, and overlay reconcile consults
the parent's HEAD and status. `refresh_project_communities` computes a partition
before its unchanged-result check. The 910-line clone Git module needs an extraction
before growth. Facts and entry points were checked with `gcode outline`, `gcode
grep`, and the supplied 2026-10-04 research note. All checks below are planned
unless explicitly marked observed.

## P1: Measure spawn isolation cost
`kind: framing`

### T1 Isolation subphase timing and wall residual
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `src/gobby/agents/spawn_timing.py::*` — scope-reason: add isolation phase keys, spawn wall time, and the unattributed residual
- `src/gobby/agents/isolation_models.py::*` — scope-reason: SpawnConfig carries the spawn timing map into prepare_environment
- `src/gobby/agents/isolation_repair.py::*` — scope-reason: time each repair step into the supplied timing map
- `src/gobby/agents/isolation_worktree.py::*` — scope-reason: pass the SpawnConfig timing map into repair
- `src/gobby/agents/isolation_clone.py::CloneIsolationHandler.prepare_environment`
- `src/gobby/mcp_proxy/tools/spawn_agent/_worktree_reuse.py::*` — scope-reason: reused-worktree repair records the same subphases
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: create the timing map at entry and move isolation preparation out
- `src/gobby/mcp_proxy/tools/spawn_agent/_isolation_prepare.py`
- `src/gobby/agents/spawn_models.py::*` — scope-reason: SpawnRequest carries the spawn start for the wall residual
- `src/gobby/mcp_proxy/tools/spawn_agent/_request.py::*` — scope-reason: pass the spawn start into SpawnRequest
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: the timing log computes wall time and residual
- `tests/agents/test_spawn_isolation_timing.py`

**Research context:** Observed with `gcode outline` and source reads on
2026-10-05: `spawn_agent_impl`
(`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`) calls
`handler.prepare_environment(spawn_config)` before it creates
`phase_timings_ms`, so worktree creation and isolation repair never reach the
spawn timing log. `repair_isolation_environment`
(`src/gobby/agents/isolation_repair.py`) runs `_copy_cli_hooks`,
`ensure_project_json_for_isolation`, `preseed_isolated_python_environment`
(which runs `uv sync --offline --frozen --link-mode copy`),
`_patch_mcp_config_for_isolation`, and `apply_isolation_git_hygiene`, in that
order. Its callers are `WorktreeIsolationHandler` and `CloneIsolationHandler`
(two calls each), `spawn_agent/_worktree_reuse.py`, and one reuse site inside
`spawn_agent_impl`; `src/gobby/agents/isolation.py` only re-exports it.
`complete_spawn_phase_timings` (`src/gobby/agents/spawn_timing.py`) emits only
`SPAWN_PHASES` keys, and `spawn_executor.py` logs them in its `finally` block,
timing only its own span. R2 records the idle baseline this deliverable
explains: 33.9 s wall, 9.7 s timed, about 24 s untimed, with a 20 s gap between
the isolation sidecar write and the MCP config write.

Approach: create the timing map and record the spawn start at
`spawn_agent_impl` entry. Carry the map on `SpawnConfig` as a new
`phase_timings_ms` field with an empty default, so `prepare_environment` and
every `repair_isolation_environment` call record into it through the existing
`phase_timings_ms: MutableMapping[str, float] | None = None` idiom that
`ensure_isolation_code_index` uses. New keys: `isolation_prepare` for the whole
`prepare_environment` call, and its nested `isolation_hook_copy`,
`isolation_project_marker`, `python_env_seed`, `isolation_mcp_config`, and
`isolation_git_hygiene`. `SpawnRequest` gains the spawn start, and
`complete_spawn_phase_timings` adds `spawn_wall` and `unattributed`:
`unattributed` is wall time minus top-level phases, with the nested isolation
subphases left out of the sum so no time counts twice. `_implementation.py` is
912 lines, so move the isolation handler selection, the `prepare_environment`
call, and the prepare-failure response out of `spawn_agent_impl` into the new
`src/gobby/mcp_proxy/tools/spawn_agent/_isolation_prepare.py`, which also times
`isolation_prepare`. This deliverable changes no isolation behavior and has no
dependencies; it lands first so every later deliverable is measured against an
attributed baseline. Rejected: deriving phases from log timestamps (neither
structured nor testable) and a context variable for the timing map (hidden
coupling where an explicit field already fits). Optimizing Python environment
seeding is outside this plan.

Planned checks: focused isolated pytest for
`tests/agents/test_spawn_isolation_timing.py` and `tests/agents/test_isolation.py`,
then one load-matched idle zero-diff Claude worktree spawn after landing whose
subphase breakdown and residual are recorded on the T1 leaf.

**Granularity:** Eleven production files change, but they carry one timing map
through one spawn path; splitting them would land phase keys with no writer or
writers with no log. The `_implementation.py` move is required by the
1,000-line ceiling.

**Acceptance:**

- T1.1 - Spawn timing reports `isolation_prepare` and the five repair subphases for worktree, clone, and reused-worktree spawns. test: `tests/agents/test_spawn_isolation_timing.py::test_spawn_timing_attributes_isolation_repair`.
- T1.2 - The timing log reports `spawn_wall` and an `unattributed` residual equal to wall time minus top-level phases, excluding nested subphases. test: `tests/agents/test_spawn_isolation_timing.py::test_idle_baseline_breakdown_is_complete`.
- T1.3 - Prepare-failure responses are unchanged after the move, and `_implementation.py` stays below 1,000 lines. test: `tests/agents/test_spawn_isolation_timing.py::test_prepare_failure_response_unchanged_after_move`.

## P2: Select and record the fork commit
`kind: framing`

### A1 Worktree ref selection and refresh (depends: T1)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `src/gobby/worktrees/git/_lifecycle.py::create_worktree`
- `src/gobby/worktrees/base_branch.py::*` — scope-reason: validate explicit ref forms and selected commit
- `src/gobby/worktrees/creation.py::*` — scope-reason: creation, cleanup, and result provenance share the selected commit
- `src/gobby/agents/isolation_worktree.py::*` — scope-reason: preparation and cleanup share fork provenance
- `src/gobby/agents/worktree_reuse.py::*` — scope-reason: refresh, conflict, and pin provenance share one continuation path
- `src/gobby/mcp_proxy/tools/worktrees/_create.py::*` — scope-reason: omitted base selects the caller's HEAD and remote-style refs are accepted
- `src/gobby/cli/worktrees.py::*` — scope-reason: CLI creation defaults to the caller's HEAD
- `src/gobby/servers/routes/source_control_worktrees.py::*` — scope-reason: client worktree creation forks the exact selected commit
- `src/gobby/hooks/event_handlers/_misc.py::*` — scope-reason: the worktree hook drops its clean-branch origin fallback
- `tests/worktrees/test_fork_commit.py`
- `tests/agents/test_worktree_fork_commit.py`
- `tests/mcp_proxy/tools/test_worktrees_create.py::*` — scope-reason: omitted versus explicit base cases
- `tests/hooks/test_misc_handlers.py::*` — scope-reason: hook exact-HEAD case
- `tests/servers/test_source_control_worktrees.py`

**Research context:** Agent spawns already select the caller's current branch
and preserve unpushed commits: `src/gobby/agents/isolation_worktree.py` replaces
a default `main` with the current branch and chooses the local ref when it has
unpushed commits (enhancer `gcode evidence`, excerpt `1067bfea…`). The remaining
clean-branch path in `src/gobby/worktrees/git/_lifecycle.py::create_worktree`
fetches and selects `origin/<branch>` (excerpt `bf8e75ed…`). Direct MCP, CLI,
and client-route creation still default to `main`, and the MCP surface in
`src/gobby/mcp_proxy/tools/worktrees/_create.py` rejects remote-style refs
(excerpt `beecb577…`). The worktree hook in
`src/gobby/hooks/event_handlers/_misc.py` uses the current branch but falls back
to origin when clean (excerpt `6b9c7c6c…`). `sync_reused_worktree_to_base`
already returns a recoverable conflict.

The default-path delta is to resolve the caller checkout's `HEAD^{commit}` once
and pass that exact SHA through creation, eliminating the clean-branch fetch and
its race. Bare explicit refs are local only; `origin/<name>` and
`refs/remotes/origin/<name>` are explicit remote refs and fetch only that ref.
Align the MCP, CLI, client-route, and hook surfaces with that rule while keeping
an explicitly supplied local `main` distinguishable from an omitted base. Reuse
the existing `get_local_commit` in `src/gobby/worktrees/git/_branch.py`
unchanged; add no second branch-resolution abstraction. Reject an existing
branch whose tip would change the selection. A reused agent worktree rebases
onto the selected commit. Integration workspace refresh keeps its current
merged-commit behavior and is re-pinned under B2. Planned check: isolated
focused pytest for local HEAD, unpushed commits, explicit local and remote refs,
mismatched existing branch, reuse conflict, and each public creation surface.

**Granularity:** These entry points share one selected-commit contract;
splitting them would leave a creation surface with different fork semantics.
The seven acceptance items are one rule checked at each surface that applies it.

**Acceptance:**

- A1.1 - Default worktrees fork the caller's local HEAD without fetch, including unpushed commits. test: `tests/worktrees/test_fork_commit.py::test_default_uses_caller_head_without_fetch`.
- A1.2 - Explicit local and remote refs resolve to one commit, with remote fetch only for remote selection. test: `tests/worktrees/test_fork_commit.py::test_explicit_refs_select_commit`.
- A1.3 - Existing branches cannot silently change the selected commit. test: `tests/worktrees/test_fork_commit.py::test_existing_branch_mismatch_is_rejected`.
- A1.4 - Reused worktrees rebase onto the selected commit and report recoverable conflicts. test: `tests/agents/test_worktree_fork_commit.py::test_refresh_preserves_workspace_ancestry`.
- A1.5 - Public creation surfaces distinguish an omitted base, which uses the caller's HEAD, from an explicit local `main`. test: `tests/mcp_proxy/tools/test_worktrees_create.py::test_omitted_base_uses_project_head_and_explicit_main_stays_main`.
- A1.6 - The worktree hook forks the exact local HEAD without an origin fallback. test: `tests/hooks/test_misc_handlers.py::TestWorktreeHandlers::test_worktree_create_uses_exact_local_head`.
- A1.7 - Client worktree creation forks the exact selected commit. test: `tests/servers/test_source_control_worktrees.py::test_create_client_worktree_uses_exact_selected_commit`.

### A2 Local clone selection and Git module extraction (depends: A1)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `src/gobby/clones/git.py::*` — scope-reason: extract merge implementation and route local/remote clone creation
- `src/gobby/clones/merge.py`
- `src/gobby/mcp_proxy/tools/_clones_creation.py::*` — scope-reason: clone tool arguments and result carry selected commit
- `src/gobby/agents/isolation_clone.py::CloneIsolationHandler.prepare_environment`
- `tests/clones/test_fork_commit.py`
- `tests/agents/test_clone_fork_commit.py`

**Research context:** `CloneGitManager.create_clone` currently follows the
remote shallow-clone path and the MCP clone tool defaults `base_branch` to main.
Use the caller's local repository as the clone source for default and local-ref
forks, preserve the exact selected commit, and use a fetched remote tip for an
explicit remote ref. An external URL has no matching local indexed checkout.
Split `CloneGitManager.merge_branch` and its private helpers from the 910-line
`git.py` into the new `merge.py`, preserving the manager's public method and
tests. Planned check: focused clone and agent pytest for all sources and the
extraction's merge behavior.

**Granularity:** The extraction keeps the touched production module below the
1,000-line ceiling and belongs in this clone lifecycle change.

**Acceptance:**

- A2.1 - Default clones include caller-local unpushed commits and record the selected commit. test: `tests/clones/test_fork_commit.py::test_local_clone_uses_caller_head`.
- A2.2 - Explicit local/remote clone refs select their resolved tips; external URL clones report a cold-index source. test: `tests/clones/test_fork_commit.py::test_clone_ref_sources`.
- A2.3 - The extracted merge path preserves merge behavior and keeps the Git module below 1,000 lines. test: `tests/clones/test_fork_commit.py::test_extracted_merge_path`.

## P3: Persist and read a pinned index
`kind: framing`

### B1 Pin schema, marker, and clean Git blob selectors (depends: A1, A2)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcore/assets/schema/migrations/460_code_overlay_pins.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: generated schema catalog entries change together
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: grant the exact pin-table read and write surface
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: verify migration, constraints, and grants
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: verify installed schema identity and CLI contract
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: generated schema identity fields change together
- `crates/gcore/src/project.rs::*` — scope-reason: parse and validate base commit and ancestor checkout identity
- `src/gobby/utils/project_context.py::*` — scope-reason: persist and read the isolation marker's pin provenance
- `crates/gcode/src/index/indexer/file.rs::*` — scope-reason: record blob IDs with clean selectors
- `crates/gcode/src/index/indexer/pipeline.rs::*` — scope-reason: propagate clean tracked state to selector writes
- `tests/utils/test_isolation_pin_marker.py`

**Research context:** `code_indexed_file_states` selects the current content
version and `code_indexed_files` owns immutable content facts. Add a nullable Git
blob ID to a clean tracked selector and an overlay-owned base table keyed by
machine, overlay project, and path with source project and content hash. Its FK
must keep the source content version valid. Store `base_commit` and the ancestor
checkout path/identity in the isolation marker, validating partial markers.
Dirty or untracked content never receives a qualifying blob ID. Update every
schema carrier with migration 460 (459 belongs to #23439; if another migration
lands first, repeat the R2 all-ref sweep and take the next free number). Planned checks: schema contracts, marker tests, and `cargo test -p
gobby-code` for selector writes.

**Granularity:** Schema, grants, generated catalog, expected identity, marker,
and selector storage form one atomic data contract. The carrier count is required
by the repository's schema contract, not independent implementation work.

**Acceptance:**

- B1.1 - Migration and all carriers expose an overlay-owned pin table with source-version FK and scoped grants. test: `crates/gcore/tests/schema_contract.rs::code_overlay_pin_schema_contract`.
- B1.2 - Clean tracked file selectors store Git blob IDs; dirty and untracked selectors do not qualify. test: `crates/gcode/src/index/indexer/tests/facts.rs::clean_selector_records_git_blob`.
- B1.3 - Isolation markers carry selected commit and ancestor checkout identity with validated completeness. test: `tests/utils/test_isolation_pin_marker.py::test_marker_requires_complete_pin_provenance`.

### B2 Create an effective pin and persist indexing gaps (depends: B1)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/commands/pin.rs`
- `crates/gcode/src/commands/mod.rs::*` — scope-reason: expose the pin command
- `crates/gcode/src/cli.rs::*` — scope-reason: parse pin command arguments
- `crates/gcode/src/dispatch.rs::*` — scope-reason: route the pin command
- `crates/gcode/src/visibility/catalog.rs::*` — scope-reason: pin derives the effective catalog view
- `src/gobby/agents/code_index.py::*` — scope-reason: preflight and pin invocation share one gcode runtime
- `src/gobby/worktrees/creation.py::*` — scope-reason: direct creation and cleanup share pin lifecycle
- `src/gobby/mcp_proxy/tools/_clones_creation.py::*` — scope-reason: pin after direct clone creation
- `src/gobby/build/workspace_services.py::*` — scope-reason: integration workspace creation and refresh re-pin their merged commit
- `crates/gcode/src/commands/pin/tests.rs`
- `tests/agents/test_code_index_pin.py`

**Research context:** A pin is copied from the effective selector view, not just
the ancestor's owned rows. For every candidate, compare its recorded Git blob
ID to `git ls-tree` at the selected commit and exclude ancestor dirty,
untracked, ignored, and mismatched paths. Copy qualifying selectors into
overlay-owned base rows and persist every tracked gap for child indexing. A
matching live indexed checkout is required; an explicit ref without one starts
with no inherited selectors. Pin replacement must be atomic.

Each path has one pin owner. Agent worktree and clone creation and reuse write
complete marker provenance under A1 and A2;
`src/gobby/agents/code_index.py::ensure_isolation_code_index` is the single
agent-path owner: it reads that marker and runs pin immediately before
`gcode index`, including after a reused-worktree rebase. Direct worktree
creation pins in `src/gobby/worktrees/creation.py`, direct clone creation pins
in `src/gobby/mcp_proxy/tools/_clones_creation.py`, and integration workspace
creation and refresh re-pin in `src/gobby/build/workspace_services.py`, keeping
the integration workspace's merged commits. Individual isolation handlers never
invoke pin. A failed pin never falls through to the moving parent's rows: it
records a cold pin, with no inherited selectors, and the child indexes locally.
Planned checks: Rust PostgreSQL tests for clean, stale, dirty, and nested-overlay
selectors, plus focused Python call-path and pin-failure tests.

**Granularity:** Pin selection and atomic replacement are one lifecycle
transaction; the Python callers exercise that same command at creation. Nine
production files change because each creation path gets exactly one pin call;
splitting callers from the command would ship a pin nothing invokes.

**Acceptance:**

- B2.1 - The pin copies clean effective selectors, including inherited rows from an author overlay, at the selected commit. test: `crates/gcode/src/commands/pin/tests.rs::pin_inherits_effective_selectors`.
- B2.2 - Dirty, untracked, ignored, and mismatched files are excluded and tracked gaps persist for indexing. test: `crates/gcode/src/commands/pin/tests.rs::pin_records_only_safe_selectors_and_gaps`.
- B2.3 - No matching live indexed checkout produces a cold pin; re-pin atomically replaces prior rows. test: `crates/gcode/src/commands/pin/tests.rs::pin_cold_and_replacement`.
- B2.4 - Agent spawns pin from the marker in `ensure_isolation_code_index` immediately before `gcode index`, and isolation handlers never invoke pin. test: `tests/agents/test_code_index_pin.py::test_agent_preflight_pins_marker_before_index`.
- B2.5 - A pin failure records a cold pin and indexes locally, never exposing moving-parent rows. test: `tests/agents/test_code_index_pin.py::test_pin_failure_forces_cold_index_without_parent_fallthrough`.
- B2.6 - Direct worktree creation, direct clone creation, and integration refresh each pin the selected commit before returning, and integration refresh keeps its merged commits. test: `tests/agents/test_code_index_pin.py::test_direct_paths_pin_before_return`.

### B3 Scoped PostgreSQL and BM25 reads (depends: B2)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/visibility.rs::*` — scope-reason: shared source-project, path, and hash visibility predicates
- `crates/gcode/src/visibility/catalog.rs::*` — scope-reason: effective tree and kind catalog read pinned selectors
- `crates/gcode/src/db/queries.rs::*` — scope-reason: scope BM25 and symbol SQL before limits
- `crates/gcode/src/commands/search.rs::*` — scope-reason: apply visibility before ranked search limits
- `crates/gcode/src/commands/search/scoped_fetch.rs`
- `crates/gcode/src/commands/symbols.rs::*` — scope-reason: symbol and tree results use effective selectors
- `crates/gcode/src/codewiki_facts/scope.rs::*` — scope-reason: codewiki facts use pinned visibility
- `crates/gcode/src/visibility/tests.rs::*` — scope-reason: add pinned read cases to existing fixtures
- `crates/gcode/src/cli/tests/search.rs::*` — scope-reason: verify scoped BM25 and symbol result limits

**Research context:** Facts are keyed by source project, file path, and content
hash. Overlay-owned file states shadow pinned base rows; tombstones hide paths.
Resolve each inherited fact by its exact pinned source version for BM25, symbol,
tree, import, and codewiki reads. Apply scope in SQL before `LIMIT`, so stale
parent rows cannot consume a result window. Split `crates/gcode/src/commands/search.rs`
by moving scoped fetch into `crates/gcode/src/commands/search/scoped_fetch.rs`
while changing its ranked retrieval path. Planned
check: isolated PostgreSQL Rust tests after parent re-index, plus search limit
and overlay-of-overlay cases.

**Granularity:** The SQL and shared visibility predicates are one read contract
used by all text and symbol lanes; projection reads are separately owned by B4.

**Acceptance:**

- B3.1 - Parent changes cannot alter pinned BM25, symbol, tree, import, or codewiki facts. test: `crates/gcode/src/visibility/tests.rs::pinned_reads_survive_parent_reindex`.
- B3.2 - Visibility is applied before search limits. test: `crates/gcode/src/cli/tests/search.rs::pinned_visibility_precedes_limit`.
- B3.3 - Overlay states and tombstones shadow pinned rows. test: `crates/gcode/src/visibility/tests.rs::overlay_shadowing_of_pins`.

### B4 Vector and graph pin scope with background recovery (depends: B3)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/commands/vector.rs::*` — scope-reason: constrain semantic results to pinned symbol versions
- `crates/gcode/src/commands/graph/reads.rs::*` — scope-reason: graph reads use the pinned effective graph
- `crates/gcode/src/graph/code_graph/read/relationships.rs::*` — scope-reason: relationship queries filter source versions
- `crates/gcode/src/projection/sync.rs::*` — scope-reason: reconcile moved source projections by pinned path in background
- `crates/gcode/src/commands/search/scoped_fetch.rs`
- `crates/gcode/src/projection/sync/tests.rs::*` — scope-reason: recovery tests use existing projection fixtures
- `crates/gcode/src/cli/tests/projection.rs::*` — scope-reason: verify degraded and recovered search

**Research context:** Graph and vector projections currently hold only a source
project's current per-path version. PostgreSQL facts become available before
projection completion. R1 makes graph and vector availability part of the pin
contract, so filtering alone is incomplete: it prevents stale answers but loses
graph and vector results once a parent projection moves. Filter existing source
projection results to pinned IDs; if a source projection moved, submit the
affected pinned paths through the existing idempotent projection-sync request
for the child overlay, and introduce no new worker or queue. Keep text and BM25
search available with an explicit degraded projection state during recovery.
Planned check: Rust projection tests that move one ancestor path, verify
immediate text reads, and observe eventual graph/vector recovery without
rebuilding unchanged paths.

**Granularity:** Projection reconciliation has one background lifecycle and two
projection backends; shared recovery state keeps their failure handling coherent.

**Acceptance:**

- B4.1 - Graph and vector results grant access only to source versions named by pins or child-owned selectors. test: `crates/gcode/src/cli/tests/projection.rs::projection_reads_respect_pin_versions`.
- B4.2 - A moved source projection reprojects only affected pinned paths in background. test: `crates/gcode/src/projection/sync/tests.rs::reproject_only_moved_pinned_paths`.
- B4.3 - PostgreSQL/BM25 remain usable while projections recover and search reports degraded state. test: `crates/gcode/src/cli/tests/projection.rs::text_search_survives_projection_recovery`.
- B4.4 - Repeated reads while recovery is pending deduplicate the same child, path, and target work and never admit stale projection rows. test: `crates/gcode/src/projection/sync/tests.rs::pinned_recovery_reuses_idempotent_sync`.

### B5 Pin-aware retention and overlay purge (depends: B3)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/commands/status/content_gc.rs::*` — scope-reason: pinned source versions are referenced for pruning
- `crates/gcode/src/commands/status/prune.rs::*` — scope-reason: preserve source facts while a child pin exists
- `crates/gcode/src/commands/status/prune/reconcile.rs::*` — scope-reason: purge overlay-owned pin rows with child removal
- `crates/gcode/src/commands/status/content_gc/tests.rs::*` — scope-reason: exercise pin retention with existing GC fixtures
- `crates/gcode/src/commands/status/prune/tests.rs::*` — scope-reason: exercise overlay purge and source retention

**Research context:** Old content versions currently age out after the normal
unreferenced-content period. R1 makes a live pin a content-GC root. Pins join the
existing reachability predicate as a content reference, including when the
source project is otherwise stale; add no new retention policy or TTL. Child
purge deletes the child's pin rows inside the existing purge transaction, and
normal pruning collects released versions on the next pass. In an
overlay-of-overlay chain, purging an intermediate overlay must not collect a
source version a live descendant still pins. Planned check: focused Rust
PostgreSQL GC/prune tests for retained, released, and overlay-of-overlay
versions.

**Acceptance:**

- B5.1 - Prune retains pinned versions and their source-project facts while any child pin exists. test: `crates/gcode/src/commands/status/content_gc/tests.rs::pinned_version_survives_prune`.
- B5.2 - Child purge removes its pins and later prune can collect unreferenced source facts. test: `crates/gcode/src/commands/status/prune/tests.rs::purged_overlay_releases_pins`.
- B5.3 - Purging an intermediate overlay cannot collect a source version still pinned by a live descendant. test: `crates/gcode/src/commands/status/prune/tests.rs::nested_pin_chain_preserves_source_version`.

## P4: Make unchanged overlay indexing cheap
`kind: framing`

### C1 Candidate-only reconciliation and import resolution (depends: B2, B3)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/index/indexer/overlay.rs::*` — scope-reason: candidate selection, gaps, discovery, and reconcile share one path
- `crates/gcode/src/index/indexer/local_imports.rs::*` — scope-reason: retain unchanged import providers during candidate resolution
- `crates/gcode/src/index/import_resolution/context.rs::*` — scope-reason: construct candidate-only import context
- `crates/gcode/src/freshness.rs::*` — scope-reason: remove parent moving-HEAD freshness condition
- `crates/gcode/src/index/indexer/tests/overlay.rs::*` — scope-reason: verify candidate sets and fallback
- `crates/gcode/src/index/indexer/tests/facts.rs::*` — scope-reason: verify import-provider retention

**Research context:** Current overlay reconcile performs full discovery and
compares with the parent's moving HEAD/status. Use child status, persisted pin
gaps, and `git diff --name-only base_commit HEAD` as the normal candidate set.
Route those files directly without a full discovery walk; use full discovery for
`--full` or Git failure. Build import resolution only when a candidate needs it,
while retaining unchanged providers so local imports still resolve. Drop the
parent timestamp freshness trigger. Planned check: isolated Rust tests for no
change, one file, source movement, Git failure, and import-provider continuity.

**Granularity:** Reconcile candidate selection and import providers must share
one candidate set; separating them would allow an incomplete index pass.

**Acceptance:**

- C1.1 - No-change and one-file overlays avoid full discovery and ignore parent HEAD movement. test: `crates/gcode/src/index/indexer/tests/overlay.rs::reconcile_uses_child_candidates_only`.
- C1.2 - Full mode and Git failure use complete discovery. test: `crates/gcode/src/index/indexer/tests/overlay.rs::reconcile_falls_back_to_full_discovery`.
- C1.3 - Candidate import resolution retains unchanged providers. test: `crates/gcode/src/index/indexer/tests/facts.rs::candidate_imports_keep_unchanged_providers`.

### C2 Pre-Leiden fingerprint, phase name, and documented contract (depends: C1, B4, B5)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/communities.rs::*` — scope-reason: fingerprint and refresh state share the community lifecycle
- `crates/gcode/src/communities/partition.rs::*` — scope-reason: expose stable pre-partition input fingerprint
- `crates/gcode/src/communities/refresh_tests.rs::*` — scope-reason: assert unchanged runs never invoke Leiden
- `src/gobby/agents/code_index.py::*` — scope-reason: phase timing and gcode preflight share one function path
- `src/gobby/agents/spawn_timing.py::*` — scope-reason: replace obsolete spawn phase key
- `tests/agents/test_code_overlay_index_timing.py`
- `docs/guides/code-index.md`
- `docs/guides/gcode-development-guide.md`

**Research context:** `refresh_project_communities`
(`crates/gcode/src/communities.rs`) loads imports and runs `build_partition`
before it compares the stored partition signature, so the existing signature is
computed after Leiden and an early skip needs a separate fingerprint of the
partition inputs. Persist and compare that fingerprint before `build_partition`;
refresh when inputs change. Rejected (enhancer E5, Orchestrator ruling
2026-10-05): seeding the child's communities from the ancestor at pin time and
skipping refresh when C1 reports zero change. The existing Overlay
`seed_from_parent` (`crates/gcode/src/db/communities.rs`) copies the parent's
current partition, which can include dirty or moved-HEAD content, so a skip would
serve communities that do not match the pinned inputs; the fingerprint is
computed from the child's effective inputs and stays correct for cold and first
runs. Rename `code_index_index` to `code_overlay_index` in the spawn writer
(`src/gobby/agents/code_index.py`) and the `SPAWN_PHASES` catalog
(`src/gobby/agents/spawn_timing.py`, which T1 already extends). Document pinned
workspace semantics, the cold fallback, projection recovery, and Ask's
live-index scope. Planned check: focused Rust community tests proving zero
Leiden calls for unchanged inputs; Python phase tests; then a load-matched idle
zero-diff spawn using T1's timing, reporting wall time, every named phase,
isolation subphases, and the residual against R2's 33.9 s and 7.064 s baseline,
plus measured pin, one-file overlay, and cold index passes against the 34.9 s and
71.6 s observations. The 2 s target is comparative. No spawn improvement is
claimed without that measured breakdown.

**Granularity:** This deliverable closes the end-to-end performance contract;
the fingerprint and named timing phase make the result independently measurable.

**Acceptance:**

- C2.1 - An unchanged index run performs no Leiden pass. test: `crates/gcode/src/communities/refresh_tests.rs::unchanged_inputs_skip_leiden`.
- C2.2 - Spawn timing reports `code_overlay_index`. test: `tests/agents/test_code_overlay_index_timing.py::test_spawn_phase_uses_overlay_name`.
- C2.3 - Guides state the workspace pin and Ask live-index contracts. file: `docs/guides/code-index.md`.
- C2.4 - A load-matched zero-diff spawn records wall time, every named phase, isolation subphases, and the residual against the 33.9 s and 7.064 s idle baseline, and pin, one-file, and cold measurements are recorded against the 34.9 s and 71.6 s observations. behavior: "benchmarked workspace indexing" in `docs/guides/gcode-development-guide.md`.

## V1 Plan Changelog
`kind: verification`

- 2026-10-04: Initial draft under #23433 (`9c8ef7912f`), from the 2026-10-04
  research note and Josh's 2026-10-05 design contract.
- 2026-10-05: Enhancer pass for #23443 (Writer gobby#15400; run `f2fd1c3f`,
  `plan-enhancer-taskless-old`). Five suggestions; the Orchestrator gobby#14972
  accepted all as the Writer recommended. E1: B2 names one pin owner per path
  (`ensure_isolation_code_index` for agent spawns, `creation.py`,
  `_clones_creation.py`, and `workspace_services.py`, moved from A1), and a pin
  failure forces a cold index (B2.4-B2.6). E2: A1 is rewritten against current
  code, since agent spawns already fork the current branch with unpushed commits;
  the delta is one exact `HEAD^{commit}` and aligned MCP, CLI, route, and hook
  surfaces (A1.4 narrowed, A1.5-A1.7 added, `_branch.py` dropped). E3: R2 records
  and attributes the #23433 idle baseline (7.064 s zero-diff `code_index_index`;
  about 24 s untimed, most plausibly Python environment seeding during isolation
  repair), and new first deliverable T1 measures isolation subphases and the wall
  residual; C2.4 requires the measured breakdown. E4: R1 makes the pin govern
  graph and vector reads and act as a GC root; B4 reuses the existing projection
  sync request (B4.4) and B5 covers nested pin chains (B5.3). E5: the core
  (pin-time community seeding with a zero-change skip) is rejected because
  `seed_from_parent` copies the parent's current, possibly dirty partition; its
  side edit drops `docs/plans/gcode-ask-fix.md` from C2. Migration renumbered
  459 to 460 on the Orchestrator's ruling (459 belongs to #23439), after an
  all-ref `git log --all --diff-filter=A` sweep found no 459 or later.

## V2: Verification
`kind: verification`

Each behavior deliverable runs its focused isolated-hub pytest with
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
and `GOBBY_TEST_PROTECT=1`, plus focused `cargo test -p gobby-code` and schema
contract tests where Rust or DDL changed. Final checks include Ruff, mypy on
`src/`, the changed-test type and quality audits, and suppression ratchet.
Never run the full pytest suite. After crate changes, rebuild and promote the
coherent Rust binary set with `promote_workspace_binary_set`; coordinate any
daemon restart with a global notice and a quiet window. Record exact commands,
results, and load-matched timings on the task deliverables. T1 lands first; its
idle zero-diff breakdown is the baseline, and no deliverable claims a spawn
improvement without a load-matched breakdown (wall, named phases, isolation
subphases, residual) against it.

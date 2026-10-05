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

## R2 Constraints and evidence
`kind: framing`

The 2026-10-04 observations were 34.9 s and 71.6 s in `code_index_index`, after a
15.3 s observation on 2026-09-28. The 2 s figure is a soft comparison target.
Memory 9644c698 requires a load-matched comparison before attributing a slowdown
to host load. Memory f1e1200c requires reused worktree conflicts to preserve
continuation ancestry and return a recoverable error.

The current checkout has migration 458 as its head. A read-only `git for-each-ref`
and `git ls-tree` sweep checked all 432 refs on 2026-10-04 UTC and found maximum
migration 458. Reserve `459_code_overlay_pins.sql` for this plan; if another
migration lands first, use the next free number after repeating the all-ref sweep.
The migration source requires its catalog, grant, schema-contract, CLI-contract,
and Python expected-identity carriers in the same deliverable.

The code index source is the PostgreSQL hub. The current overlay catalog resolves
inherited rows from the parent's current selectors, and overlay reconcile consults
the parent's HEAD and status. `refresh_project_communities` computes a partition
before its unchanged-result check. The 910-line clone Git module needs an extraction
before growth. Facts and entry points were checked with `gcode outline`, `gcode
grep`, and the supplied 2026-10-04 research note. All checks below are planned
unless explicitly marked observed.

## P1: Select and record the fork commit
`kind: framing`

### A1 Worktree ref selection and refresh
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `src/gobby/worktrees/git/_lifecycle.py::create_worktree`
- `src/gobby/worktrees/git/_branch.py::*` — scope-reason: consolidate branch and remote-tip resolution
- `src/gobby/worktrees/base_branch.py::*` — scope-reason: validate explicit ref forms and selected commit
- `src/gobby/worktrees/creation.py::*` — scope-reason: creation, cleanup, and result provenance share the selected commit
- `src/gobby/agents/isolation_worktree.py::*` — scope-reason: preparation and cleanup share fork provenance
- `src/gobby/agents/worktree_reuse.py::*` — scope-reason: refresh, conflict, and pin provenance share one continuation path
- `src/gobby/build/workspace_services.py::*` — scope-reason: integration workspace creation and refresh use the same resolved commit
- `tests/worktrees/test_fork_commit.py`
- `tests/agents/test_worktree_fork_commit.py`

**Research context:** `create_worktree` currently defaults `base_branch` to main
and may choose an origin tip; `WorktreeIsolationHandler.prepare_environment` has
an unpushed-commit switch; `sync_reused_worktree_to_base` already returns a
recoverable conflict. Resolve the selected commit once from the caller checkout,
carry it through creation and refresh, and reject an existing branch whose tip
would change the selection. The integration path must retain merged commits.
Treat explicit local and remote refs distinctly: fetch only the remote ref.
Planned check: isolated focused pytest for local HEAD, unpushed commits, explicit
local/remote refs, mismatched existing branch, reuse conflict, and integration
refresh.

**Granularity:** These entry points share one selected-commit contract and one
refresh outcome; splitting them would leave a creation path with different fork
semantics. The two new test files cover the public and agent entry points.

**Acceptance:**

- A1.1 - Default worktrees fork the caller's local HEAD without fetch, including unpushed commits. test: `tests/worktrees/test_fork_commit.py::test_default_uses_caller_head_without_fetch`.
- A1.2 - Explicit local and remote refs resolve to one commit, with remote fetch only for remote selection. test: `tests/worktrees/test_fork_commit.py::test_explicit_refs_select_commit`.
- A1.3 - Existing branches cannot silently change the selected commit. test: `tests/worktrees/test_fork_commit.py::test_existing_branch_mismatch_is_rejected`.
- A1.4 - Reused worktrees rebase and report recoverable conflicts while integration refresh keeps merged commits. test: `tests/agents/test_worktree_fork_commit.py::test_refresh_preserves_workspace_ancestry`.

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

## P2: Persist and read a pinned index
`kind: framing`

### B1 Pin schema, marker, and clean Git blob selectors (depends: A1, A2)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcore/assets/schema/migrations/459_code_overlay_pins.sql`
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
schema carrier with migration 459 (or the next free number after another all-ref
sweep). Planned checks: schema contracts, marker tests, and `cargo test -p
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
- `crates/gcode/src/commands/pin/tests.rs`
- `tests/agents/test_code_index_pin.py`

**Research context:** A pin is copied from the effective selector view, not just
the ancestor's owned rows. For every candidate, compare its recorded Git blob
ID to `git ls-tree` at the selected commit and exclude ancestor dirty,
untracked, ignored, and mismatched paths. Copy qualifying selectors into
overlay-owned base rows and persist every tracked gap for child indexing. A
matching live indexed checkout is required; an explicit ref without one starts
with no inherited selectors. Run this step at every worktree and clone creation
entry point and after reused-worktree rebase. Pin replacement must be atomic.
Planned checks: Rust PostgreSQL tests for clean, stale, dirty, and nested-overlay
selectors, plus focused Python call-path tests.

**Granularity:** Pin selection and atomic replacement are one lifecycle
transaction; the Python callers exercise that same command at creation.

**Acceptance:**

- B2.1 - The pin copies clean effective selectors, including inherited rows from an author overlay, at the selected commit. test: `crates/gcode/src/commands/pin/tests.rs::pin_inherits_effective_selectors`.
- B2.2 - Dirty, untracked, ignored, and mismatched files are excluded and tracked gaps persist for indexing. test: `crates/gcode/src/commands/pin/tests.rs::pin_records_only_safe_selectors_and_gaps`.
- B2.3 - No matching live indexed checkout produces a cold pin; re-pin atomically replaces prior rows. test: `crates/gcode/src/commands/pin/tests.rs::pin_cold_and_replacement`.
- B2.4 - All creation and reuse paths invoke pin with the resolved commit before child indexing. test: `tests/agents/test_code_index_pin.py::test_creation_paths_pin_selected_commit`.

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
projection completion. Filter existing source projection results to pinned IDs;
if a source projection moved, queue re-projection of only those pinned paths into
the child overlay. Keep text and BM25 search available with an explicit degraded
projection state during recovery. Planned check: Rust projection tests that move
one ancestor path, verify immediate text reads, and observe eventual graph/vector
recovery without rebuilding unchanged paths.

**Granularity:** Projection reconciliation has one background lifecycle and two
projection backends; shared recovery state keeps their failure handling coherent.

**Acceptance:**

- B4.1 - Graph and vector results grant access only to source versions named by pins or child-owned selectors. test: `crates/gcode/src/cli/tests/projection.rs::projection_reads_respect_pin_versions`.
- B4.2 - A moved source projection reprojects only affected pinned paths in background. test: `crates/gcode/src/projection/sync/tests.rs::reproject_only_moved_pinned_paths`.
- B4.3 - PostgreSQL/BM25 remain usable while projections recover and search reports degraded state. test: `crates/gcode/src/cli/tests/projection.rs::text_search_survives_projection_recovery`.

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
unreferenced-content period. Treat any live child pin as a content reference,
including when the source project is otherwise stale. Remove child pin rows when
the child overlay is purged, then allow normal pruning on the next pass. Planned
check: focused Rust PostgreSQL GC/prune tests for retained, released, and
overlay-of-overlay versions.

**Acceptance:**

- B5.1 - Prune retains pinned versions and their source-project facts while any child pin exists. test: `crates/gcode/src/commands/status/content_gc/tests.rs::pinned_version_survives_prune`.
- B5.2 - Child purge removes its pins and later prune can collect unreferenced source facts. test: `crates/gcode/src/commands/status/prune/tests.rs::purged_overlay_releases_pins`.

## P3: Make unchanged overlay indexing cheap
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
- `docs/plans/gcode-ask-fix.md`

**Research context:** The existing partition signature is computed after
Leiden, so an early skip needs a separate fingerprint of its inputs. Persist
and compare that fingerprint before `build_partition`; refresh when inputs
change. Rename `code_index_index` to `code_overlay_index` in the spawn writer
and phase catalog. Document pinned workspace semantics, the cold fallback,
projection recovery, and Ask's live-index scope. Planned check: focused Rust
community tests proving zero Leiden calls for unchanged inputs; Python phase
tests; a measured pin, no-change and one-file overlay pass, and cold index
against the 34.9 s and 71.6 s observations. The 2 s target is comparative.

**Granularity:** This deliverable closes the end-to-end performance contract;
the fingerprint and named timing phase make the result independently measurable.

**Acceptance:**

- C2.1 - An unchanged index run performs no Leiden pass. test: `crates/gcode/src/communities/refresh_tests.rs::unchanged_inputs_skip_leiden`.
- C2.2 - Spawn timing reports `code_overlay_index`. test: `tests/agents/test_code_overlay_index_timing.py::test_spawn_phase_uses_overlay_name`.
- C2.3 - Guides state the workspace pin and Ask live-index contracts. file: `docs/guides/code-index.md`.
- C2.4 - Pin, no-change, one-file, and cold measurements are recorded against the observed spawn timings. behavior: "benchmarked workspace indexing" in `docs/guides/gcode-development-guide.md`.

## V1 Verification and release
`kind: verification`

Each behavior deliverable runs its focused isolated-hub pytest with
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
and `GOBBY_TEST_PROTECT=1`, plus focused `cargo test -p gobby-code` and schema
contract tests where Rust or DDL changed. Final checks include Ruff, mypy on
`src/`, the changed-test type and quality audits, and suppression ratchet.
Never run the full pytest suite. After crate changes, rebuild and promote the
coherent Rust binary set with `promote_workspace_binary_set`; coordinate any
daemon restart with a global notice and a quiet window. Record exact commands,
results, and load-matched timings on the task deliverables.

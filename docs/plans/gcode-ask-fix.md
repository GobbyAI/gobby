# Make native Ask answer from the caller's index

**Plan ID:** gcode-ask-fix

## Overview
`kind: framing`

Complete the user-directed correction to epic #22010 under coordinator #22021.
Ask uses the ordinary index of its caller's checkout, including worktree overlay
visibility and dirty indexed files. Remove the snapshot build path and historical
commit flags, preserve managed evidence authority and independent claim review,
then close acceptance tasks on real service and frozen-cohort evidence.

This is the execution plan for existing tasks, not a request to expand another
task tree. The user supplied the implementation decisions and authorized execution.
The requested canonical artifact is `docs/plans/gcode-ask-fix.md`.

## Constraints
`kind: framing`

- HEAD commit and tree IDs are recorded provenance resolved at run start. They
  never select or construct an index. A request containing `commit_ref` is invalid.
- Keep all ten stages, independent claim review, claim validation and one bounded
  repair. Keep generation CAS and stale-generation fencing across recovery.
- Retain managed grants scoped to the real repository root, identity headers,
  isolated runtime home, scrubbed subprocess environment and sandbox denial of
  direct investigator access to the repository. Scratch and repository are disjoint.
- Bind probes retain the 5-second config and 10-second content-search caps.
- Frozen questions, source commits and gold criteria remain unchanged. The cohort
  runs serially with deterministic retrieval, a 600-second budget, no count cap,
  and one primary invocation per question. Preserve errors and interruptions;
  separately label every retry. Never place answer keys in the indexed corpus.
- Preserve unrelated changes and coordinate native installation/restart through
  project messages. Install release executables by copying to a new inode and
  renaming. No full Python test suite.
- Implementation checklist below substitutes for the unavailable provider-native
  tracker. Findings stay in this coordinator until fixed or handed to an active
  owner under the repository rules.

## P1: Restore incremental indexing
`kind: framing`

### 1.1 Flush valid files despite root-path notifications [category: code]
`kind: deliverable`

Targets:
- `src/gobby/code_index/trigger.py::*` — scope-reason: normalize and reject invalid file notifications before enqueue and retry.
- `tests/code_index/test_trigger.py::*` — scope-reason: cover invalid notifications mixed with valid and deleted files.
- `crates/gcode/src/index_lock.rs::*` — scope-reason: defensively skip non-file entries in batch lock acquisition.
- `crates/gcode/src/index_lock/tests.rs::*` — scope-reason: cover root aliases and retained outside-root rejection.

**Research context:** `CodeIndexTrigger._schedule_file` inserts every result of
`_normalize_file_path` into `_pending_by_root`. An empty path, dot, root absolute
path or root alias normalizes to `.`. `_resolve_repo_edit_paths` in the hook
handler can supply that path; the trigger owns the queue boundary for all callers.
`lock_project_files` currently collects a fallible normalization result for the
whole batch; `normalize_file_lock_path` rejects an empty root-relative path.
Inspection used gcode outlines and symbol-at retrievals plus the
`notify_file_changed` consumer sweep in source and tests. The hook caller remains
valid: enqueue must reject a directory regardless of notification origin.

Reject root/directory notifications before queue insertion, preserving missing
file paths so deletions still update the index. Normalize root aliases consistently.
Make native batch acquisition skip root/directory entries without hiding
outside-root or filesystem errors. Test valid siblings survive; an all-invalid
batch requires no database locks. Run focused trigger and index-lock tests.

**Acceptance:**

- 1.1.1 - Root aliases cannot poison pending batches; valid and deleted files still flush. file: `src/gobby/code_index/trigger.py`.
- 1.1.2 - Native batch locking skips non-file entries and still rejects outside-root paths. file: `crates/gcode/src/index_lock.rs`.

## P2: Bind Ask to live evidence
`kind: framing`

### 2.1 Resolve evidence through the caller's ordinary index [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/tests.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/commands/graph/view/mod.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/graph/code_graph/read/relationships.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/visibility/graph.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/evidence/provenance.rs::*` — scope-reason: resolve visible caller facts and verify working-tree evidence bytes.
- `crates/gcode/src/evidence/source.rs::*` — scope-reason: resolve visible caller facts and verify working-tree evidence bytes.
- `crates/gcode/src/search/fts/common.rs::*` — scope-reason: resolve visible caller facts and verify working-tree evidence bytes.
- `crates/gcode/src/search/fts/content.rs::*` — scope-reason: resolve visible caller facts and verify working-tree evidence bytes.
- `crates/gcode/src/vector/code_symbols/search.rs::*` — scope-reason: resolve visible caller facts and verify working-tree evidence bytes.
- `crates/gcode/src/visibility.rs::*` — scope-reason: resolve visible caller facts and verify working-tree evidence bytes.
- `crates/gcode/src/visibility/catalog.rs::*` — scope-reason: resolve visible caller facts and verify working-tree evidence bytes.
- `crates/gcode/src/evidence/mod.rs::*` — scope-reason: bind evidence to Context and ordinary freshness.
- `crates/gcode/src/evidence/read.rs::*` — scope-reason: read working-tree bytes with indexed hash verification.
- `crates/gcode/src/evidence/search.rs::*` — scope-reason: remove complete commit-inventory validation from every lane.
- `crates/gcode/src/evidence/graph.rs::*` — scope-reason: preserve graph lanes using ordinary visible projects.
- `crates/gcode/src/evidence/snapshot.rs` — operation: delete
- `crates/gcode/src/evidence/tests.rs::*` — scope-reason: replace snapshot tests with live-index provenance and working-tree evidence cases.
- `crates/gcode/src/evidence/contracts.rs::*` — scope-reason: remove snapshot request and inventory contracts.
- `crates/gcode/src/commands/evidence.rs::*` — scope-reason: remove snapshot-json handler and bind through caller context.
- `crates/gcode/src/index/security.rs::*` — scope-reason: remove snapshot-only credential fixture exclusion predicates.

**Research context:** The user measured 7,768 of 11,372 files indexed in 405 seconds,
with most backend samples in unnecessary code_calls deletes for a new project.
`EvidenceLibrary::new` and `validate_index_inventory` force that fresh inventory.
Ordinary `freshness` and `visibility::visible_project_ids` already provide the
caller/worktree graph. Reuse them. Excerpt reads must verify working-tree bytes
against indexed hashes rather than compare with HEAD blobs; retain stale-range
failures for genuinely changed bytes and path containment checks.

**Granularity:** This is one evidence protocol change across library construction,
six retrieval lanes and excerpt reads. They must agree on project and content
authority. Python run lifecycle and public CLI removal are separate sections.

Remove the snapshot inventory, materialization, verification and exclusion layer.
Ordinary gitignore-based discovery defines live index visibility, including
untracked nonignored files. Preserve ordinary overlay-parent resolution. Add
dirty-file and documentation evidence cases, including the CLI guide containing
sample PostgreSQL credentials. Delete tests solely covering removed blob behavior.

**Acceptance:**

- 2.1.1 - All evidence lanes use the caller's visible index and ordinary freshness. file: `crates/gcode/src/evidence/mod.rs`.
- 2.1.2 - Dirty indexed files are citable; subsequent content mismatch is refused. file: `crates/gcode/src/evidence/read.rs`.
- 2.1.3 - Documentation and untracked nonignored indexed files remain reachable. file: `crates/gcode/src/evidence/tests.rs`.

### 2.2 Bind and recover managed runs without building source trees [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/ask/service.py::*` — scope-reason: carry live bindings through shared service and bounded database operations.
- `src/gobby/mcp_proxy/registries.py::*` — scope-reason: carry live bindings through shared service and bounded database operations.
- `src/gobby/storage/hub/transaction_deadline.py::*` — scope-reason: carry live bindings through shared service and bounded database operations.
- `tests/ask/test_pipeline_definition.py::*` — scope-reason: carry live bindings through shared service and bounded database operations.
- `tests/ask/test_storage.py::*` — scope-reason: carry live bindings through shared service and bounded database operations.
- `tests/storage/hub/test_postgres_operation_deadline.py::*` — scope-reason: carry live bindings through shared service and bounded database operations.
- `src/gobby/ask/snapshots.py::*` — scope-reason: replace snapshot lifecycle with live binding and generation fencing.
- `src/gobby/ask/evidence.py::*` — scope-reason: resolve authority and evidence cwd against the real repository root.
- `src/gobby/ask/storage.py::*` — scope-reason: remove snapshot attachment/inventory storage while preserving provenance and generation CAS.
- `src/gobby/ask/validation.py::*` — scope-reason: remove self-comparing snapshot metadata validations.
- `src/gobby/ask/contracts.py::*` — scope-reason: reflect live binding provenance.
- `src/gobby/ask/evidence_authority.py::*` — scope-reason: move persisted managed grant and generation admission out of evidence execution.
- `src/gobby/ask/validation_models.py::*` — scope-reason: move immutable evidence and report models out of validation algorithms.
- `src/gobby/ask/errors.py::*` — scope-reason: own the shared evidence admission error.
- `src/gobby/ask/__init__.py::*` — scope-reason: update the public error import.
- `src/gobby/ask/evidence_runtime.py::*` — scope-reason: read live source bytes and migrate validation model imports.
- `src/gobby/ask/publication.py::*` — scope-reason: consume the relocated immutable validation models.
- `src/gobby/ask/stage_runtime.py::*` — scope-reason: bind the live root and consume relocated validation models.
- `tests/ask/test_snapshots.py::*` — scope-reason: replace historical snapshot preparation/recovery assertions with live binding tests.
- `tests/ask/test_evidence.py::*` — scope-reason: preserve managed authority and stale-generation failures with live roots.
- `tests/ask/test_native_integration.py::*` — scope-reason: migrate evidence error and immutable model consumers while preserving behavior.
- `tests/ask/test_export.py::*` — scope-reason: migrate evidence error and immutable model consumers while preserving behavior.
- `tests/ask/test_pipeline.py::*` — scope-reason: migrate evidence error and immutable model consumers while preserving behavior.
- `tests/ask/test_publication.py::*` — scope-reason: migrate evidence error and immutable model consumers while preserving behavior.
- `tests/ask/test_validation.py::*` — scope-reason: migrate evidence error and immutable model consumers while preserving behavior.
- `tests/ask/test_validation_native.py::*` — scope-reason: migrate evidence error and immutable model consumers while preserving behavior.

**Research context:** `AskSnapshotManager.prepare_async` currently inspects a
snapshot, attaches its identity, then creates a generation. `_prepare_index`
issues a managed grant and invokes `ensure_isolation_code_index` with the entire
remaining budget. The replacement reuses grant issuance, isolated runtime home,
config probe and content smoke against the caller root, without indexing.
The initial source sizes were 982 lines for snapshots, 974 for evidence and 982
for validation. After deleting snapshot machinery, move authority admission from
evidence.py into evidence_authority.py and immutable models from validation.py
into validation_models.py. Their final sizes are 756 and 717 lines respectively;
all consumers move directly without compatibility reexports.

**Granularity:** The lifecycle and evidence authority form one generation protocol;
publishing a replacement without updating the consumer would break recovery.

Delete worktree creation/materialization/sealing, rematerializing recovery and
cleanup, snapshot attachment and obsolete identity checks. Record the resolved
caller project/root and HEAD provenance, issue a grant for that root, then publish
the generation with CAS. Recovery renews authority and advances generation; old
authority is rejected. Add duration logs for every bind phase. Verify the sandbox
still denies direct source access and source/scratch are disjoint. Retain revoke
and cancellation ownership for managed credentials.

**Acceptance:**

- 2.2.1 - Bind uses the caller project and grant root, creates no source checkout and completes within existing probe caps. file: `tests/ask/test_snapshots.py`.
- 2.2.2 - Instrumented bind tests fail on index, snapshot-commit or snapshot-json argv. file: `tests/ask/test_snapshots.py`.
- 2.2.3 - Recovery CAS, stale-generation refusal, scrubbed environment and sandbox boundaries survive. file: `tests/ask/test_evidence.py`.

## P3: Remove the historical snapshot surface
`kind: framing`

### 3.1 Retire snapshot scope and commit selection [category: refactor] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/search_regression_tests.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/commands/status/content_gc/tests.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/commands/status/retire_files/tests/serial_db.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/db/queries_cas_tests.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/index/api_tests.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/index/indexer/overlay.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/index/indexer/sink.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/index/indexer/tests/api_contract.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/index/indexer/tests/facts.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/index/api/file_state.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/index/languages/tests.rs::*` — scope-reason: directly migrate graph visibility, file-state writes and extracted language tests.
- `crates/gcode/src/contract/schema.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/api.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/captured_sources.rs` — operation: delete
- `crates/gcode/src/index/import_resolution.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/import_resolution/context.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/import_resolution/context/apple.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/import_resolution/context/dotnet.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/import_resolution/context/elixir.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/import_resolution/context/jvm.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/import_resolution/context/package_metadata.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/import_resolution/context/python.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/import_resolution/context/scripting.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/import_resolution/tests/context_loading.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/indexer.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/indexer/tests/serial_db.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/languages.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/languages/captured_tests.rs` — operation: delete
- `crates/gcode/src/index/mod.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/parser.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/parser/tests.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/index/parser/tests/captured.rs` — operation: delete
- `crates/gcode/tests/contract.rs::*` — scope-reason: remove snapshot-only index machinery and regenerate public contract consumers.
- `crates/gcode/src/config/context.rs::*` — scope-reason: remove Snapshot project scope.
- `crates/gcode/src/config/tests.rs::*` — scope-reason: replace snapshot marker expectations with ordinary overlay behavior.
- `crates/gcode/src/index/indexer/pipeline.rs::*` — scope-reason: remove snapshot indexing route.
- `crates/gcode/src/index/indexer/snapshot.rs` — operation: delete
- `crates/gcode/src/index/indexer/snapshot/tests.rs` — operation: delete
- `crates/gcode/src/cli.rs::*` — scope-reason: remove snapshot index flag.
- `crates/gcode/src/dispatch.rs::*` — scope-reason: remove snapshot dispatch surfaces.
- `crates/gcode/src/commands/index.rs::*` — scope-reason: remove snapshot indexing parameter.
- `crates/gcode/src/freshness.rs::*` — scope-reason: collapse Single and Snapshot arms.
- `crates/gcode/src/commands/grep.rs::*` — scope-reason: collapse Single and Snapshot arms.
- `crates/gcode/src/codewiki_facts/text.rs::*` — scope-reason: collapse Single and Snapshot arms.
- `crates/gcore/src/project.rs::*` — scope-reason: remove snapshot_commit marker field and parsing.
- `src/gobby/utils/project_context.py::*` — scope-reason: remove snapshot marker argument and restore cargo target symlink behavior.
- `src/gobby/agents/code_index.py::*` — scope-reason: remove snapshot_commit while preserving ordinary spawned-agent indexing.
- `crates/gcode/src/cli/ask.rs::*` — scope-reason: remove commit flag.
- `crates/gcode/src/dispatch_ask.rs::*` — scope-reason: remove commit forwarding.
- `crates/gcode/src/ask_client.rs::*` — scope-reason: remove commit request field.
- `crates/gcode/src/cli/tests/top_level.rs::*` — scope-reason: remove snapshot option expectations.
- `crates/gcode/tests/ask.rs::*` — scope-reason: reflect removed commit option.
- `crates/gcode/src/contract.rs::*` — scope-reason: regenerate CLI contract.
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerate the complete CLI contract after removing commit flags.
- `tests/contracts/gcode.contract.json::*` — scope-reason: regenerate the complete CLI contract after removing commit flags.
- `src/gobby/mcp_proxy/tools/ask.py::*` — scope-reason: reject commit_ref at MCP boundary.
- `src/gobby/servers/routes/ask.py::*` — scope-reason: reject commit_ref at HTTP boundary.
- `docs/contracts/ask.md`
- `docs/guides/ask.md`

**Research context:** The user identified snapshot marker routing in Context and
the gcore isolation marker. Ordinary markers with parents must continue resolving
IsolatedOverlay. The frozen cohort gets historical provenance from two normal
worktrees, so no historical request flag remains necessary. Sweep every removed
field's constructors, fixtures and consumers before validation and include any
additional carrier edits here. Update #22019's stored CLI description.

**Granularity:** This is the removal of one cross-language public protocol and
its generated carriers; partial removal would leave advertised flags or wire
fields that cannot execute. Evidence and lifecycle behavior are prerequisites.

**Acceptance:**

- 3.1.1 - No Snapshot scope, snapshot index route or marker field remains; worktrees use ordinary overlays. file: `crates/gcode/src/config/context.rs`.
- 3.1.2 - CLI, HTTP and MCP reject commit selection and generated contracts agree. file: `tests/contracts/gcode.contract.json`.
- 3.1.3 - Guides explain caller-root binding and HEAD provenance. file: `docs/contracts/ask.md`.

## P4: Prove service behavior
`kind: framing`

### 4.1 Repair the DB-backed native regressions [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/config/layers.rs::*` — scope-reason: share process-global logger setup with thread-local test capture.
- `crates/gcode/src/test_env.rs::*` — scope-reason: share process-global logger setup with thread-local test capture.
- `crates/gcode/src/codewiki_facts/tests.rs::*` — scope-reason: verify registered checkout alias handling.
- `crates/gcode/src/index_lock/tests.rs::*` — scope-reason: repair the two newly exposed serial database failures from their diagnosed causes.

**Research context:** Schema lineage commit a2c4fd208f exposed five formerly
unrunnable failures: two codewiki checkout alias cases, two index-lock cases,
and one snapshot blob timeout case. The removed snapshot test must be confirmed
absent. Canonicalize registered and requested roots consistently. Characterize
lock failures with their exact diagnostics and fix production causes where found;
do not weaken assertions to obtain a green run.

**Acceptance:**

- 4.1.1 - The gcode library suite including DB-backed cases passes; removed snapshot coverage is accounted for. file: `crates/gcode/src/index_lock/tests.rs`.

### 4.2 Keep managed MCP authority valid through tool completion [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/ask/agents.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/ask/composition.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/ask/permissions.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/servers/_app_lifecycle.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/servers/app_factory.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/servers/auth_service.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/servers/routes/ask_mcp.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/mcp_proxy/services/result_handling.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/mcp_proxy/services/session_context.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/mcp_proxy/services/tool_execution.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/mcp_proxy/services/tool_proxy.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `src/gobby/mcp_proxy/services/tool_discovery.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `tests/ask/http_mcp_support.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `tests/ask/test_composition.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `tests/ask/test_permissions.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `tests/mcp_proxy/services/test_direct_tool_session_activation.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `tests/mcp_proxy/services/test_result_offload.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `tests/mcp_proxy/services/test_tool_proxy_coverage.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.
- `tests/mcp_proxy/services/test_tool_proxy_validation.py::*` — scope-reason: preserve authenticated Ask identity across the managed MCP transport and completion event.

**Research context:** Live Ask agents could not use the ordinary bridge while the
sandbox denied source files. `write_ask_mcp_config` now names the authenticated
Ask HTTP bridge. `resolve_tool_event_context` must attribute events to the bound
source checkout, not the private scratch directory. Capture the after-tool event
before executing a tool that can terminalize its agent; re-resolving live authority
after successful self-completion incorrectly rejects that completion. Keep all
project, session, stage and generation guards. Move discovery functions out of
tool_execution.py into tool_discovery.py; the former is now 833 lines.

**Granularity:** Transport registration and event attribution share one authenticated
request/completion protocol; the existing service fixtures exercise both boundaries.

**Acceptance:**

- 4.2.1 - Real signed requests reach evidence and self-completion, with correct event roots and rejection after terminalization. file: `tests/ask/test_permissions.py`.

### 4.3 Cancel owned agent processes without deleting terminal sessions [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `src/gobby/agents/runner_queries.py::*` — scope-reason: preserve terminal identity while terminating owned native agent processes.
- `src/gobby/ask/composition.py::*` — scope-reason: preserve terminal identity while terminating owned native agent processes.
- `tests/agents/test_runner.py::*` — scope-reason: preserve terminal identity while terminating owned native agent processes.
- `tests/agents/test_runner_cancel.py::*` — scope-reason: preserve terminal identity while terminating owned native agent processes.
- `tests/ask/test_composition.py::*` — scope-reason: preserve terminal identity while terminating owned native agent processes.

**Research context:** `AgentRunner` cancellation previously lost terminal session
identity needed for cleanup. The shared termination path must stop the real
process and update durable state. `composition.cancel_agent` invokes that path,
propagating failures instead of marking only the database row.

**Acceptance:**

- 4.3.1 - Cancellation preserves terminal ownership and terminates the agent through the shared lifecycle. file: `tests/agents/test_runner_cancel.py`.

### 4.4 Expose complete evidence and content-only submission schemas [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `src/gobby/ask/claims.py::*` — scope-reason: make the MCP adapter schema match trusted shared-service submissions.
- `src/gobby/ask/service.py::*` — scope-reason: enforce the original request question at shared-service submission before persistence.
- `tests/ask/test_pipeline.py::*` — scope-reason: reject rewritten questions before persistence and verify corrected submission, review, repair, recovery and publication.
- `src/gobby/ask/agents.py::*` — scope-reason: make the MCP adapter schema match trusted shared-service submissions.
- `src/gobby/mcp_proxy/tools/ask.py::*` — scope-reason: make the MCP adapter schema match trusted shared-service submissions.
- `tests/mcp_proxy/tools/test_ask.py::*` — scope-reason: make the MCP adapter schema match trusted shared-service submissions.
- `docs/contracts/ask.md::*` — scope-reason: make the MCP adapter schema match trusted shared-service submissions.

**Research context:** Live agents guessed native evidence selector shapes and
failed to submit answers because provenance and canonical hashes were required
inside otherwise opaque objects. `AnswerContent` and `ReviewContent` own the
content schema; the MCP adapter derives trusted caller identity and computes hashes.
The shared service retains strict `AnswerDraft` and `ReviewerResult` admission.
Reject a draft whose question differs from the recorded request at submission,
before writing an artifact, so the agent can correct it in the same attempt.
Document each query lane and exact flat selectors. Unknown identity fields fail
validation; they never override the authenticated principal.

**Acceptance:**

- 4.4.1 - Nested schemas reject forged identity and valid content reaches the real authenticated service. file: `tests/mcp_proxy/tools/test_ask.py`.

### 4.5 Give the bounded repair its rejected draft and diagnostics [category: code] (depends: 4.4)
`kind: deliverable`

Targets:
- `src/gobby/ask/agents.py::*` — scope-reason: carry immutable previous-attempt context into a fresh repair agent.
- `src/gobby/ask/stage_runtime.py::*` — scope-reason: carry immutable previous-attempt context into a fresh repair agent.
- `tests/ask/test_pipeline.py::*` — scope-reason: carry immutable previous-attempt context into a fresh repair agent.

**Research context:** `_run_agent` launched repair with no prior draft, forcing
research to restart. Load the durable initial submission and both validation
reports, verify their draft hashes, then supply them to `AskAgentSpec`.
A previously accepted durable repair submission returns before rebuilding context.
The repair narrows or removes unsupported side claims while preserving every
requested question part. Independent review and the single-repair limit remain.

**Acceptance:**

- 4.5.1 - The repair receives exactly the rejected draft and matching reports, and recovery reuses accepted submissions. file: `tests/ask/test_pipeline.py`.

### 4.6 Bind native startup to explicit provider and sandbox controls [category: code] (depends: 4.5)
`kind: deliverable`

Targets:
- `src/gobby/ask/runtime_controls.py::*` — scope-reason: preserve canonical runtime controls and sealed admission while fixing tool startup.
- `src/gobby/ask/runtime_validation.py::*` — scope-reason: preserve canonical runtime controls and sealed admission while fixing tool startup.
- `src/gobby/ask/runtime_profile.py::*` — scope-reason: preserve canonical runtime controls and sealed admission while fixing tool startup.
- `src/gobby/ask/runtime_derivation.py::*` — scope-reason: preserve canonical runtime controls and sealed admission while fixing tool startup.
- `tests/ask/native_probe_harness.py::*` — scope-reason: preserve canonical runtime controls and sealed admission while fixing tool startup.
- `tests/ask/test_native_probe_harness.py::*` — scope-reason: preserve canonical runtime controls and sealed admission while fixing tool startup.
- `tests/ask/test_native_probe_provenance.py::*` — scope-reason: preserve canonical runtime controls and sealed admission while fixing tool startup.
- `tests/ask/test_runtime_validation.py::*` — scope-reason: preserve canonical runtime controls and sealed admission while fixing tool startup.
- `tests/ask/test_runtime_derivation.py::*` — scope-reason: preserve canonical runtime controls and sealed admission while fixing tool startup.
- `tests/ask/test_permissions.py::*` — scope-reason: preserve canonical runtime controls and sealed admission while fixing tool startup.

**Research context:** Interactive native startup built its first prompt before MCP
connected. `ask_provider_args` uses print mode to wait for tools while retaining
restricted mode, the exact MCP allowlist and source-denying SRT policy. The control
digest changes with those arguments, so cohort admission must use fresh sealed
probe artifacts through the normal loader. Move arguments, sandbox construction
and policy normalization from runtime_validation.py into runtime_controls.py.
The original file reached 1,003 lines; the completed split leaves 808 and 217 lines,
with canonical hashes unchanged by the move. Migrate all direct imports and test
monkeypatch targets atomically.

**Acceptance:**

- 4.6.1 - Runtime control tests preserve policy and grant guards; contained fresh/resumed probes seal and load the exact current controls. file: `tests/ask/test_runtime_validation.py`.

### 4.7 Establish real authenticated shared-service acceptance [category: test] (depends: 4.6)
`kind: deliverable`

Targets:
- `tests/servers/routes/test_ask.py::*` — scope-reason: replace adapter acceptance fakes with authenticated real shared-service fixtures.
- `tests/mcp_proxy/tools/test_ask.py::*` — scope-reason: verify real guards and discovery.
- `crates/gcode/tests/ask.rs::*` — scope-reason: exercise rebuilt installed CLI against the real HTTP boundary.

**Research context:** #22019's live objection rejects adapter fakes. Use the real
shared service, signed tokens, matching identity headers and authenticated
middleware. Remove remaining SimpleNamespace claims stubs in acceptance paths.
Validate stable 408, 403 and 404 results and project/run/stage authorization.
Tests use an isolated daemon/state and never the production database.

**Acceptance:**

- 4.7.1 - Shared run and event-driven wait contract passes through authenticated HTTP. test: `tests/servers/routes/test_ask.py::test_shared_run_contract_and_event_driven_wait`.
- 4.7.2 - MCP authorization and discovery pass with real authority. test: `tests/mcp_proxy/tools/test_ask.py::test_ask_authorization_and_discovery`.
- 4.7.3 - CLI lifecycle uses real service and rebuilt native executable. test: `crates/gcode/tests/ask.rs::test_ask_cli_lifecycle_contract`.

### 4.8 Bind registered caller worktrees and prepare the runtime probe [category: code] (depends: 4.7)
`kind: deliverable`

Targets:
- `src/gobby/worktrees/creation.py::create_worktree`
- `tests/worktrees/test_creation.py::*` — scope-reason: verify canonical registration with real Git and database identity derivation.
- `src/gobby/storage/managed_credentials.py::ManagedCredentialManager.issue`
- `src/gobby/storage/managed_credentials.py::ManagedCredentialManager.issue_tool_request`
- `crates/gcore/assets/schema/migrations/435_bind_tool_grants_to_requested_checkout.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: register the immutable numbered migration and checksum.
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: reconstruct the historical five-argument issuer before testing migration 432.
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: generate the checkout identity from rebuilt gdaemon.
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: verify numbered migration changes against the baseline catalog carrier.
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: verify grant bundle schema identity follows the embedded migration identity.
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: validate the derived native schema contract.
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: validate the rebuilt schema CLI identity.
- `tests/storage/test_managed_credentials.py::*` — scope-reason: verify requested overlay authority and requested primary precedence over session workspace.
- `src/gobby/ask/runtime_controls.py::*` — scope-reason: preserve the shared policy normalization and sealed runtime controls.
- `tests/ask/test_runtime_validation.py::*` — scope-reason: normalize bound grant locks and redundant owned prompt reads while preserving foreign grants, write access and denial precedence.
- `tests/ask/native_probe_harness.py::*` — scope-reason: preserve fresh and resumed contained runtime preparation and receipts.
- `tests/ask/test_native_probe_harness.py::*` — scope-reason: cover fresh-session transcript recovery and completed-agent process identity without relaxing ownership checks.
- `src/gobby/storage/schema_divergence.py::binary_set_apply_refusal`
- `tests/cli/test_install_setup_gdaemon.py::*` — scope-reason: keep the refusal remedy aligned with the three schema-bearing binary members.
- `docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md`

**Research context:** Live worktree admission exposed raw `/tmp` registration
versus canonical `/private/tmp` index identity. Canonicalize shared creation.
The subsequent Ask grant omitted overlay authority because SQL inferred the
session workspace rather than the requested root. Pass the already-authorized
registered spelling into issuance; SQL validates primary checkout or registered
overlay against project and machine. Existing issuance without an operation root
continues to derive its session workspace. Use a numbered migration and refresh
the complete installed schema-bearing native set via new inodes.

The contained probe never called its existing ordinary-index preparation helper.
Call it once before fresh Ask admission, with a dedicated new private runtime
directory; resumed runs reuse the index. The actual managed SRT launch adds the
private grant lock. Normalize only that exact bound lock alongside the existing
grant-read normalization, rejecting duplicate lock entries and retaining every
other read/write path in the digest.

Long reviewer prompts use `GOBBY_PROMPT_FILE`, adding an explicit read for
`<run>/assets/prompt.md` beneath the already-readable assets directory. Ignore
that redundant read in the digest only when the directory grant exists and no
read denial at or beneath assets can alter precedence. Resolve symlinks first;
external prompt reads and all write grants remain significant.

Completion clears the current agent PID, while the captured launch receipt retains
its PID and OS start identity. Use that receipt only with matching terminal and
child-session identity; normalize PostgreSQL UUID values before comparing with
JSON strings. Fresh Claude launches use their child session as native session ID;
resume metadata is optional on that path. Resolve missing transcript paths through
the existing machine-bound transcript resolver, retaining all export file checks.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/worktrees/_create.py` — no-edit-reason: delegates creation to the shared function and receives its canonical path.
- `src/gobby/servers/routes/source_control_worktrees.py` — no-edit-reason: delegates creation to the same shared function without deriving overlay identity itself.
- `src/gobby/agents/spawn.py` — no-edit-reason: agent issuance keeps session-derived workspace authority with the optional requested path absent.
- `src/gobby/runner_init/servers.py` — no-edit-reason: existing issuance omits the new optional requested path.
- `tests/agents/test_spawn.py` — no-edit-reason: existing agent issuance contract is unchanged.
- `tests/runner_init/test_grant_issuance.py` — no-edit-reason: the default issuance path remains session-derived.
- `tests/runtime_grants/test_maintenance_launch.py` — no-edit-reason: maintenance launch does not request a caller checkout override.
- `tests/runtime_grants/test_revocation.py` — no-edit-reason: revocation is independent of optional checkout selection.
- `tests/storage/test_hub_auth_schema_isolation.py` — no-edit-reason: schema-isolated issuance retains its default authority derivation.
- `src/gobby/ai/_managed_tool_chat_lease.py` — no-edit-reason: already passes the requested project path to the shared tool issuer.
- `tests/ai/test_managed_tool_chat_lease.py` — no-edit-reason: existing lease tests exercise the unchanged public tool-issuance signature.
- `src/gobby/cli/daemon.py` — no-edit-reason: displays the shared refusal string without assuming the number of binary members.

**Acceptance:**

- 4.8.1 - Canonical worktree registration agrees with native/database overlay identity. test: `tests/worktrees/test_creation.py::test_creation_registers_canonical_root_for_overlay_grants`.
- 4.8.2 - A requested worktree receives overlay authority; an explicit primary root overrides a session overlay. test: `tests/storage/test_managed_credentials.py::test_issue_tool_request_accepts_registered_overlay_without_primary` and `tests/storage/test_managed_credentials.py::test_tool_request_primary_root_overrides_session_overlay`.
- 4.8.3 - Private grant locks and redundant owned prompt reads normalize without concealing foreign paths, write grants or denial precedence. test: `tests/ask/test_runtime_validation.py::test_policy_digest_normalizes_only_the_bound_grant_lock` and `tests/ask/test_runtime_validation.py::test_policy_digest_ignores_only_redundant_owned_prompt_reads`.
- 4.8.4 - Real worktree Ask cites its checkout; fresh/resumed contained probe records actual boundary receipts after ordinary index preparation. Record every failed diagnostic and cleanup outcome before normal-loader admission. file: `docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md`.

## P5: Record the frozen cohort and close acceptance
`kind: framing`

### 5.1 Evaluate all fourteen frozen questions [category: test] (depends: 4.8)
`kind: deliverable`

Targets:
- `docs/evidence/wiki-bakeoff-code-2026-09/ask_cohort.py::*` — scope-reason: prepare ordinary commit-pinned worktrees once and invoke flagless Ask.
- `scratchpad/run_cohort.py`
- `docs/evidence/wiki-bakeoff-code-2026-09/test_ask_cohort.py::*` — scope-reason: pin unchanged cohort and separate retrieval/answer scoring.
- `docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md`
- `docs/evidence/wiki-bakeoff-code-2026-09/ask_cohort_records.py::*` — scope-reason: own cohort provenance, contained runtime preparation, verified exports, separate scoring and report rendering.
- `docs/evidence/wiki-bakeoff-code-2026-09/ask_cohort_runtime.py::*` — scope-reason: own cohort provenance, contained runtime preparation, verified exports, separate scoring and report rendering.
- `docs/evidence/wiki-bakeoff-code-2026-09/ask_cohort_publication.py::*` — scope-reason: own cohort provenance, contained runtime preparation, verified exports, separate scoring and report rendering.
- `docs/evidence/wiki-bakeoff-code-2026-09/ask_scoring.py::*` — scope-reason: own cohort provenance, contained runtime preparation, verified exports, separate scoring and report rendering.
- `docs/evidence/wiki-bakeoff-code-2026-09/ask_scoring_retrieval.py::*` — scope-reason: own cohort provenance, contained runtime preparation, verified exports, separate scoring and report rendering.
- `docs/evidence/wiki-bakeoff-code-2026-09/ask_report.py::*` — scope-reason: own cohort provenance, contained runtime preparation, verified exports, separate scoring and report rendering.

**Research context:** #22020 freezes Q01–Q13 at
0216f1e33f05962d49467d95fe84609041c6dba8 and Q14 at
8b24ac26699aac8b24254a647aa70b208287b492. Create two game-goblins worktrees,
index each once with the ordinary indexer, then ask without commit flags. Preserve
all primary outputs, provenance, timings, errors and interruptions. Keep retries
separate and never replace a primary result. Gold keys remain outside each corpus.

Move cohort record identity, contained runtime preparation and publication archive
verification out of ask_cohort.py into ask_cohort_records.py,
ask_cohort_runtime.py and ask_cohort_publication.py. Move retrieval scoring and
report rendering out of ask_scoring.py into ask_scoring_retrieval.py and
ask_report.py. Keep the canonical runner below 850 lines.

Score retrieval and answers separately, compare gold-span overlap to the 6/14
baseline and name lost baseline hits. Explicitly verify Q14's nine changed paths,
case-sensitive longest-prefix precedence, Hobby Supplies=2 and Sleeves: =4.
A faithfully reported negative result is valid. Record evidence for original
acceptance 3.2.1–3.2.3 without changing prompts, source commits or gold criteria.

**Acceptance:**

- 5.1.1 - Frozen cohort/scoring contract passes unchanged prompt and source-commit checks. test: `docs/evidence/wiki-bakeoff-code-2026-09/test_ask_cohort.py::test_frozen_cohort_and_scoring_contract`.
- 5.1.2 - Fourteen primary answers or failures are preserved with separate retrieval and answer scores. file: `docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md`.
- 5.1.3 - Baseline comparison and all specified Q14 checks are reported faithfully. file: `docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md`.

## V1 Integration verification and completion
`kind: verification`

Planned checks, not yet passing evidence:

```bash
cargo fmt --check
cargo clippy -p gobby-code -p gobby-core --all-targets
GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo test -p gobby-code --lib
cargo test -p gobby-code --test contract --test ask
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/ask/ tests/servers/routes/test_ask.py tests/mcp_proxy/tools/test_ask.py -q
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run mypy src/
uv run gobby test-types audit tests/ask --baseline .gobby/test-types-baseline.json --fail-on-new
uv run gobby test-types suppressions . --baseline .gobby/python-suppressions-baseline.json
```

Format owned changed files before final verification; preserve foreign edits.
After coordinated rebuild/install/restart, run the requested repository question
with GOBBY_SESSION_ID=gobby#13038 and JSON output. Record bind duration, absence of
a source checkout/new index project, successful index flush, and evidence from an
ordinary Gobby worktree. Then execute the serial cohort and close #22019, #22020,
#22013, #22010 and #22021 as their real gates become satisfied, accounting for
automatic ancestor closure. Commit only owned paths with the user-specified task
tags; link every required commit and complete bounded criteria reviews.

## V2 Implementation checklist
`kind: verification`

- [x] Claim #22021, search memory, inspect repository ownership and flush entry.
- [x] Validate this execution plan and correct stale targets and consumer inventory.
- [x] Fix incremental flush and verify focused tests.
- [x] Replace native evidence snapshot behavior with live-index semantics.
- [x] Replace Python binding/recovery and preserve authority guards.
- [x] Remove historical flags/scope and regenerate public contracts.
- [x] Repair exposed native failures and validate authenticated service fixtures.
- [x] Fix managed transport, completion, submission schema and repair context.
- [ ] Re-prove installed CLI acceptance after the final source changes.
- [ ] Rebuild/install, coordinate restart and record repository/worktree smoke.
- [ ] Run fourteen primaries and publish honest cohort scoring.
- [ ] Finish checks, scoped commits, criteria reviews and task closures.

## V3 Execution checkpoint
`kind: verification`

Completed in session gobby#13038:

- Commit `74142c6`: root/directory notifications are rejected before enqueue;
  native file locking filters them after containment normalization. Python trigger
  tests: 25 passed (five new cases first failed). Rust index-lock tests: 22 passed.
  Clippy, Ruff and test-quality/type/suppression checks passed for that change.
- Commit `b8ebbbe`: codewiki fixture contexts now canonicalize macOS roots. Config
  and index-lock tests share one process-global logger with thread-local capture.
  Full gcode library baseline reproduced exactly five failures (1,050 passed,
  five failed, three ignored). After these fixes, the library run excluding only
  `evidence::tests::snapshot_blob_reads_use_bounded_git_processes` passed 1,054
  tests, three ignored. The excluded test belongs to the snapshot code being removed.
- Both scoped changes received a read-only code-review subagent review (required
  by that skill for four files), with 100% coverage and no material findings.

Current uncommitted refactor is incomplete and not acceptance evidence:

- Native EvidenceLibrary now holds caller-root, RepositoryBinding and visible
  FileFact rows. New evidence/source.rs reads working-tree bytes, verifies indexed
  hashes and retains containment/size/range checks. New evidence/provenance.rs
  retains Git commit/change metadata only. SnapshotBinding, blob_oid on source
  evidence, inventory/exclusions and snapshot-json request surface were removed.
- Removed Snapshot project scope, snapshot index implementation and dispatch.
  Additional scope consumers found in search/fts/common.rs, search/fts/content.rs,
  vector/code_symbols/search.rs and visibility.rs/catalog.rs were converted.
- `cargo check -p gobby-code -p gobby-core --lib` passes with 29 dead-code/import
  warnings from obsolete captured-source parsing/import helpers. Remove those
  snapshot-only helpers and their tests; do not suppress the warnings.
- Python snapshots.py was replaced with a roughly 300-line live binding manager.
  It issues grants for the real root, makes runtime assets under the Ask run's
  scratch directory, runs status/search smoke with unchanged caps, and publishes
  the generation CAS. Recovery refreshes grants; release only revokes authority.
  Existing class names remain for now, but no checkout/index construction remains.
- AskBinding now records repository_root; AskRequest no longer has commit_ref;
  EvidenceReference uses binding_digest; AskRunRecord exposes generation. Storage
  resolves HEAD, fingerprints caller root, drops attach_snapshot and the generation
  getter, and retains publish_snapshot_generation. Composition no longer needs
  worktree storage. Spawned-agent code_index loses snapshot_commit only.

Next: convert Python evidence authority, models, manifest and citation validation
from snapshot inventory/blob identities to binding plus content hashes; update
remaining API/CLI consumers and tests; remove native captured-source dead code;
complete guards/recovery tests and full checks. Native tests still reference removed
snapshot types and have not yet been converted. The new provenance Git helper
still uses its prior subprocess implementation and needs bounded execution review.
Plan validation currently reports production-size-growth on snapshots/evidence/
validation and the 1,612-line cohort runner. Snapshot reduction should clear its
warning; reduce or coherently split the others and update Targets to actual edits.
No native installation/restart, live Ask, cohort execution or task closure yet.


### V4. Live-index implementation checkpoint (session #13038, second epoch)
`kind: verification`

The live-index refactor remains uncommitted. The previous V3 checkpoint is historical;
this section supersedes its pending implementation details.

- Native EvidenceLibrary now uses caller context, ordinary freshness, indexed FileFact
  hashes, and working-tree bytes. Snapshot scope/commands/modules and snapshot-only
  CapturedSources parser/import helpers/tests are removed. Commit metadata remains in
  evidence/provenance.rs, with a 10-second Git process bound and bounded output drains.
  SourceEvidence has no blob_oid. Retired inventory/error/exclusion contracts are gone.
- Python binding preparation uses the real root and isolated runtime scratch. Evidence
  admission reads the current generation lifecycle and validates root, binding, runtime,
  managed grant and scrubbed environment. Validation and publication use repository_binding,
  auth project_id, binding_digest, and cited working bytes keyed by content_hash.
  Indexed source excerpts remain exact, including documentation fixture credentials;
  runtime/error credentials still reject or redact.
- Removed commit_ref from Ask/API/MCP/CLI. Found and fixed an additional root-propagation
  gap: CLI now sends project_path, auth project remains parent for overlay contexts, and
  HTTP/MCP resolve primary-or-authorized-overlay paths. Pipeline prepare reads the
  persisted run root instead of re-resolving the primary checkout. This adds targets
  src/gobby/mcp_proxy/registries.py and src/gobby/ask/service.py to the implementation.
- Added a small found-work fix: distant operation deadlines overflowed PostgreSQL's
  signed millisecond settings. transaction_deadline.py caps values at INT32_MAX; the
  isolated DB regression and its 22-test file pass. These two extra targets are
  src/gobby/storage/hub/transaction_deadline.py and
  tests/storage/hub/test_postgres_operation_deadline.py.
- Replaced snapshot manager tests with live-root/grant/probe-cap/recovery-CAS/commit-ref
  tests. Adapted validation, publication, export, storage, pipeline, evidence admission,
  and real managed native evidence tests. Native integration now explicitly indexes its
  caller fixture once and verifies live refresh, dirty/untracked files, managed identity,
  provenance, and credential fencing. Retired native snapshot fixture tests were deleted.
- Updated native_probe_harness.py private index provisioning to index the actual caller
  root once, require its HEAD to match the supplied source commit, preserve command
  receipts, and never materialize/rebind a checkout. Ask request no longer carries a ref.
- Regenerated both gcode contract JSON carriers from the rebuilt development binary.

Validation observed this epoch (not a final acceptance claim):

- DB-backed gcode library: 1036 passed, 3 ignored, no failures (after snapshot deletion,
  before the final error-name cleanup). Focused five native evidence tests passed.
- cargo clippy -q -p gobby-code -p gobby-core --all-targets passed before final error cleanup.
- cargo test -q -p gobby-code --test contract --test ask: 17 passed, two suites.
- Real managed native evidence and exact-emission validation: 2 passed with rebuilt binary.
- Python binding/admission: 13 passed. Validation/publication/export: 12 passed.
  Storage/pipeline: 11 passed. PostgreSQL deadline tests: 22 passed.
- Focused mypy for Ask and its HTTP/MCP/registry consumers passed before last tiny edits.
- Full tests/ask run: 256 passed, four obsolete fixture expectations failed; those
  expectations have now been updated. Combined Ask/routes/MCP rerun is in progress at
  /tmp/ask-live-pytest-final-epoch.log (exec session 43669). One project_root expectation
  was removed just after starting that run, so if it persists rerun that focused test.
- Prior HTTP/MCP run: 78 passed, three stale adapters failed; commit_ref request and
  root-resolver fixture signature edits are applied and included in the combined rerun.

Still required: cancellation/overlay regressions for new binding lifecycle; complete
broad checks/test-quality/test-types/suppression ratchets; verify primary/worktree HTTP
root authorization coverage; real installed CLI/service acceptance (the Rust ask lifecycle
still uses scripted HTTP, not yet the requested real service boundary); review and scoped
commits; docs and #22019 stored description; plan coverage/size validation and target
inventory expansion for deleted captured-source helpers; cohort runner rewrite/decomposition,
14 frozen primary invocations, scoring/report, installed native promotion, coordinated
restart, live Gobby/worktree answers, and task closures. No native install/restart/live
answer/cohort invocation/task closure occurred this epoch.


The combined epoch rerun finished with 340 passed and one stale project_root assertion
loaded before its edit. The final focused rerun of that corrected test follows in the
session transcript. All HTTP/MCP route tests passed in the combined run. Its real shared
service fixture already uses AuthService, signed agent tokens, matching headers and
AuthMiddleware; the remaining SimpleNamespace objects there are server containers,
not claims. The separate scripted Rust HTTP lifecycle still needs real installed-boundary
acceptance before #22019 can close.

## V5 Current execution checkpoint — 2026-09-12/13
`kind: verification`

This supersedes the pending-work statements in V3/V4; those remain historical records.

- `469ee29d23` committed the complete live-index binding, deleted snapshot machinery,
  removed historical flags, regenerated contracts and propagated authorized caller/
  worktree roots. Cancellation/recovery CAS and signed-token HTTP tests pass.
  The installed CLI lifecycle test now uses a real isolated authenticated service.
- `a86d67d` bounds file-navigation freshness waits during concurrent indexing.
  Eleven freshness tests pass, including a red/green busy-lock regression.
- `2f5a12b` removes the last obsolete snapshot-only isolation test. All 75
  project-context tests pass; the type audit reports no new errors.
- `4179312` replaces Ask's source-dependent stdio launcher with a stateless HTTP
  MCP bridge at /api/ask/mcp. Three wrappers reuse the existing authenticated tool
  handlers, identity context, schema leases and stage permissions. Foreign targets
  fail closed. Evidence stays inline because generic offloading produces references
  outside Ask's exact allowlist. A real middleware/permission-store test covers
  signed identities, run/stage/tool denials and a 20,000-character evidence response.
- Native library validation: 1,036 passed, three ignored. Native format/clippy pass;
  contract/Ask suites: 18 passed. The exact installed CLI lifecycle case passed.
- Latest Python Ask/routes/MCP run: 349 passed in 43.28 seconds. Authentication and
  MCP app-factory tests: 90 passed. Source mypy and Ruff pass. Touched-test quality
  and type audits pass. Suppression ratchet: 217 baseline, zero new and zero stale.
- Cohort preparation/scoring refactor: 47 tests pass and six-file review is clean.
  Two ordinary registered worktrees are prepared and indexed once by the runner.
  Frozen prompts, commits and gold semantics are unchanged. No live cohort yet.

Live runs are retained without substituting retries for primaries. Gobby smoke
e1971592-b7fe-40a5-a0d0-8b6d8f6c488a bound in about 2.8 seconds, then seed found an
old indexed chunk ending beyond its file. The chunker fix was already committed
in e447449f5e; one ordinary full refresh repaired old rows (7,444 files, 398 seconds).
Ask did not invoke that refresh. Separately identified retry
3fedb96e-b9a2-4be2-8edb-c19ff22a5929 bound in about 0.68 seconds and passed seed,
then its investigator was refused before startup: stdio MCP granted access inside
the denied real source root. Commit 4179312 fixes that diagnosed conflict.

The rebuilt native binary was installed through a new inode and the main daemon
was restarted after coordination; the latest native install includes a86d67d.
The HTTP MCP fix awaits another announced restart after session #12910 releases
its active close-validator hold. Next: real Gobby/worktree answers, complete
cohort runtime admission and fourteen serial primaries, honest retrieval/answer
scores and specified checks, final target inventory/validation, scoped evidence
commits and criteria-gated closure of #22019/#22020/#22013/#22010/#22021.

### V6 Live transport follow-up
`kind: verification`

After #12910 released its validator hold, the daemon restarted successfully on
4179312. Retry c463728a-46d3-44d5-8f16-9e3defe27729 bound in about 0.99 seconds
(grant 23.5 ms, config 512.3 ms, search 441.5 ms, publish 10.0 ms), passed seed,
and launched investigator 36413c8b-e4e0-4fd4-9ca6-8546e9668314. The source-read
conflict is gone. Claude's first turn nevertheless had zero MCP tools and refused
to guess. Its one-turn/no-tool output is retained; provider MCP connection/config
diagnosis remains open. All required identity environment names were present.
A read-only curl probe through the exact SRT policy reached daemon health with
HTTP 200, so the observed network-denial noise does not prove an MCP network block.

Cancellation exposed another concrete defect: runner_queries.cancel_run committed
the agent cancellation, then attempted to change its already expired session to
cancelled, producing HTTP 500. A second cancellation completed the Ask run as
cancelled. Fix the owner with the existing atomic update_status_if_non_terminal
helper and a terminal-session race regression; do not loosen terminal transitions.
Primary/retry/cancellation records remain distinct under /tmp/ask-live-gobby-13038-*.
No real answer or cohort primary has yet been accepted.

Plan validation now parses the checkpoint headings but reports the existing
850-line planning threshold on evidence.py (910), validation.py (896), and
ask_cohort.py (943). Coherent decomposition and final target inventory remain
owned work; the production 1,000-line ceiling is not crossed by these changes.

### V7 Managed tool startup, completion and submission contracts
`kind: verification`

The no-tool startup was reproduced with Claude 2.1.270: interactive startup
submitted the first question before MCP was ready. The same authenticated HTTP
bridge and exact SRT policy connected all three allowed wrappers in print mode.
Commit c1ee1da adds `--print` to Ask provider arguments; 379 focused tests passed.
Commit b20c8a8 makes runner cancellation preserve already-terminal sessions;
40 focused runner/storage tests passed. Old leaked child processes were removed
without replacing the running gterm host.

Retry 1a871546-070e-40da-aea9-7ddf522827f4 bound in approximately 1.04 seconds
(grant 31.9 ms, config 529.4 ms, search 463.4 ms, publish 12.8 ms), passed seed,
and made real MCP calls. Calls failed because the proxy treated private Ask
scratch as a registered checkout. Its error/refusal remains recorded separately;
no successful answer or cohort primary is claimed for that run.

Commit e637cfa resolves project tool-event attribution through the authenticated
Ask runtime source checkout and captures completion attribution before executing
a terminalizing tool. A real `end_agent_run` regression verifies the success row,
source-root after-tool event and rejection of subsequent tool authority. Ask
cancellation now invokes the existing shared process-termination path. Tool
inventory and schema discovery moved into `tool_discovery.py` to keep execution
below the production ceiling. Two existing activation tests now supply and clean
up their required workflow evaluation runtime. The owned native probe harness
formatting failure reported by #12967 was corrected. Validation: 650 Ask, proxy
service and API tests passed in 46.58 seconds; source mypy, scoped Ruff, test
quality and type ratchets passed; suppressions remain 217 with no new or stale
entries. Review covered all fourteen changed paths and resolved its completion
lifecycle finding.

Commit b5e8708 gives the investigator explicit native evidence selector guidance
and operation enums. Answer/review tool schemas derive nested content from the
shared validated models. The MCP adapter supplies authenticated managed-agent
identity and canonical submission hashes; strict service authorization, evidence
freshness and immutable submission checks remain unchanged. Reviewer prompts now
include the immutable draft hash. Validation: 354 Ask/API tests passed in 43.31
seconds; source mypy, scoped Ruff, test type/quality audits and suppression ratchet
passed. Four-path review found no remaining issue.

An ordinary main-checkout daemon restart was announced globally to deploy these
Python changes. No native binary promotion is part of this restart. Native
coordinator #12967 reports that installed binaries and the adopted gterm host
predate its #22089 protocol change; coordinate a synchronized promotion with that
owner before any future native rebuild/install. Real Gobby/worktree answers,
contained runtime admission, the frozen fourteen-question cohort, final plan
inventory/decomposition and acceptance closures remain required.

## V8 Structural cleanup and seventh live diagnostic
`kind: verification`

Commit `97c27ab320` gives repair the rejected immutable draft and the complete
claim/review diagnostics. Commit `8a8bc4d` directly separates evidence authority,
validation models and runtime controls, with all 24 consumers reviewed. The final
structural checks passed: 266 Ask tests (29.82s), source mypy (2,030 files), scoped
Ruff, zero test-type errors and the 217-entry suppression ratchet. Logs:
`/tmp/ask-controls-epoch7{,-mypy,-types,-suppressions}.log`.

Diagnostic retry 7, run `194000a2-d48d-409a-8dad-74bcd342cdac`, ended with
`deadline_exceeded` in review after its one repair. Investigator and repair
submissions were accepted, but the reviewer rejected unnecessary delegation and
recovery assertions. Concurrent import edits changed some retrieved source hashes;
subsequent live diagnostics must run after source edits finish. This failure is
preserved in `/tmp/ask-live-gobby-13038-retry7-status.json` and the run transcripts.
It is not a cohort primary and is not a successful answer. No cohort primary has
yet been executed.

Plan review reconciled every changed path in the core live-index commit
`469ee29d23` and test-fix commit `b8ebbbe60c` against Targets. Deleted helpers use
delete targets; live consumers use justified file scopes. The canonical plan
validator passes after these corrections. Commit `4956c19` narrows optional
answer detail without weakening claims or review, and documents MCP content
submission. Authenticated HTTP/MCP tests passed 88 cases using the installed CLI;
pipeline/recovery tests passed 28. Cargo formatting and Clippy passed.

The completed Rust split moves graph-result visibility from visibility.rs
(861 lines) into visibility/graph.rs, and file-state mutations from index/api.rs
(930 lines) into index/api/file_state.rs. Move index/languages.rs's inline tests
(855 total lines) into index/languages/tests.rs. Final production sizes are
710, 716 and 688 lines respectively. All 20 paths were reviewed; SQL and moved
function bodies are byte-identical. The repeated library run passed 1,036 tests
with three ignored; Clippy and formatting passed.

Commit `9fdffea` fixes the uncited-stale-retrieval failure from diagnostic retry 8:
all immutable manifest checks remain global, but current source freshness follows
citations and explicit evidence/invocation scopes. Two regression cases failed
before the change; 268 Ask tests passed afterward, with clean mypy, test type and
quality audits.

Diagnostic retry 9 (`cd510e5d-d466-4976-965f-9cf276adbef8`) completed successfully
with outcome `complete`, independent review and zero repairs. Its immutable
publication root hash is
`1f401fedab8577688dd682d4b29bfa6d35c307fc5002079d412fa50f84f65abd`.
The answer cites the caller's `src/gobby/ask/snapshots.py`. Export receipt:
`/tmp/ask-live-gobby-13038-retry9-export.json`. This is a diagnostic, not a cohort
primary. Worktree smoke, contained runtime admission, fourteen primaries and
criteria-gated task closure remain outstanding.

## V9 Worktree and contained-probe diagnostics — 2026-09-13
`kind: verification`

Commit `beeada6` fixes canonical worktree registration. The red integration test
reproduced the alias mismatch; 219 other worktree tests passed, and the corrected
regression passed against the real auth function. The recreated worktree
`214a1912-f0ff-4dde-8fd5-b157cd0c1b36` at
`/private/tmp/gobby-ask-live-13038` indexed its overlay in 1.587 seconds.
Ask diagnostic `c45d193f-231f-4613-9ae9-908375c362ce` then failed with a parent-row
hash against worktree bytes. Its grant lacked the overlay project ID.

Commit `5ce9929` repairs requested-root grant issuance with migration 435.
Validation: 34 credential tests plus the new primary-precedence case passed;
29 native schema cases and 356 Ask/HTTP/MCP cases passed. The historical migration
432 test explicitly reconstructs its original issuer before replaying 432.

Contained probe epoch 7 ended on a PostgreSQL backend termination. Concurrent
pytest was observed, but causality is unproven. The schema sweeper is ruled out:
its numeric six-part schema-name parser cannot accept the probe schema name.
Epoch 8 spent most of its time in cold freshness/index import resolution because
its preparation helper was orphaned, then failed on SRT policy identity at
investigator launch. No child session or PID was recorded. Export and cleanup
are incomplete, so the unique schema/runtime are retained for audit. Evidence:
`/tmp/ask-native-probe-13038-epoch8/{raw-probe.json,cleanup.json,failure.json}`.
Neither diagnostic is a cohort primary or a successful runtime admission.

The first schema-435 promotion attempt copied two binaries before the protected
memory-dream stop completed. Startup refused the mixed installed pin. Both
binaries were restored from the captured schema-434 set, and the pending stop
was interrupted. Main daemon and gterm remained running. The second protected
stop completed normally. The complete gcode/gdaemon/ghook set and identity stamp
were then promoted with the shared binary-set helper. Daemon health passed in
13.9 seconds and installed main-checkout symbol search succeeded. Gterm was unchanged.

The refusal's "all four" suffix was stale: the enforced set is gcode, gdaemon
and ghook. Reuse the canonical three-member remedy. Gterm is excluded; its active
owner will promote it separately after the terminal-control work lands. The
protected memory-dream run finished before the successful normal stop.

Task #22279 is now parented under #22010 at the user's direction. Commit
`8cfb3ac0ec` already materializes the BM25 match set once. Before closure, verify
the installed binary's fresh-process matching and no-match searches on both
repositories against the unchanged two-second limit, confirm one scan execution,
and rerun the scoped-role project-isolation regression. This remains an acceptance
obligation of the epic; the Ask smoke timeout remains ten seconds.

## V10 Installed worktree answer and content-scan acceptance — 2026-09-13
`kind: verification`

Commit `3e4835c` fixes contained-probe index preparation and exact private grant-lock
normalization, and corrects the binary-set refusal remedy. Final focused validation:
138 runtime/derivation/harness/schema-divergence/install tests passed in 4.80 seconds;
Ruff passed. The five-file staged review covered every file and found no issues.

Worktree diagnostic retry `3ed2e1d2-438c-40a4-945f-d89d790dce18` completed with
outcome `complete`, independent review, and zero repairs. It cites
`src/gobby/ask/snapshots.py:92-135` from the caller checkout at commit
`2f04d100ed3d751127061fd89c79c3ed8e4403a2`. The evidence binding names overlay
`69418746-dea0-5260-bccf-15d1c1d3fc39`; the run binding names its actual root
`/private/tmp/gobby-ask-live-13038-epoch9`. Ordinary overlay indexing took 3.294
seconds before Ask. Logged bind phases: grant 28.3 ms, config 392.6 ms, search
551.1 ms, publication 833.1 ms. No run `source` directory exists. This is slower
than the approximate 0.2-second target, but both fixed probe caps hold and Ask
performs no snapshot indexing.

Export: `/tmp/ask-worktree-epoch9-export/ask-3ed2e1d2-438c-40a4-945f-d89d790dce18.tar`.
Publication SHA256: `07bd2fc164e452e29974c78a06c327190e3cff1a53de98d7e44a98090e9a0230`.
Both epoch-8 and epoch-9 temporary managed worktrees were deleted through the
worktree service after the export. The last historical root-path lock error is
2026-09-12 19:24:56; no such error appears on September 13.

Task #22279's installed fresh-process searches passed the unchanged two-second
criterion: Gobby matching/no-match 0.31/0.24 seconds; game-goblins matching/no-match
0.35/0.22 seconds. Matching terms were `AskSnapshotManager` and `Sleeves`; the
no-match term was `zzzask22279nomatch9fdb62`. Each scoped-role EXPLAIN ANALYZE
contains exactly one ParadeDB scan with `Actual Loops=1`. Database execution
times were 61.860/32.191 and 52.140/30.763 ms respectively. Each role saw only its
own project rows. Raw plans and measurements are in `/tmp/ask-22279-*`.
The named scoped-content RLS regression passed in 0.63 seconds. These checks are
diagnostics and acceptance evidence, not frozen cohort primaries.

## V11 Acceptance and contained-probe recovery — 2026-09-13, session #13038
`kind: verification`

- #22279 passed background criteria review and closed with implementation commit
  `8cfb3ac0ec`. V10's four installed hit/miss timings, one-scan plans and scoped RLS
  evidence met the unchanged acceptance criteria. Post-close memory review is complete.
- `ea60889` updates stale native test identity pins to migration 435. The final
  nextest checks passed six gcore schema-contract and eight gdaemon CLI-contract tests;
  scoped formatting and Clippy passed. This was a test-only correction.
- Epoch9's contained probe failed before Ask because shared-checkout status changed
  during indexing. This is source drift, not evidence that indexing mutated source.
  Its owned schema/runtime were removed with no cleanup errors. Epoch10 failed
  preflight because the captured binaries were outside the stable source clone.
  Both failed attempts remain preserved under `/tmp/ask-native-probe-13038-epoch9`
  and the epoch10 driver log.
- Epoch11 used the stable managed clone. The quiet window had no unexplained backend
  termination. Ask prepared, investigated and reviewed, then publication correctly
  rejected a draft that rewrote the request question. `e025d4a` moves that rejection
  to submission, before artifacts/checkpoints, allowing correction in the same attempt.
  The regression failed before the fix; all four pipeline integration tests passed
  afterward (`/tmp/ask-question-epoch12-{red,green}.log`).
- Epoch11 export/cleanup also exposed assumptions about resume-only metadata, uncleared
  PIDs and UUID string representation. `1702dd1` fixes those assumptions using captured
  launch authority and the production transcript resolver. Independent review covered
  both files without findings; 115 harness/cleanup/provenance tests passed. Type and
  quality audits reported zero issues; Ruff and the suppression ratchet passed.
- The original failed epoch11 output is unchanged. Diagnostic re-export recovered all
  six raw receipts. Normal harness finalization then reported `errors=[]`, removed
  the owned runtime and dropped only its private schema. Evidence is in
  `/tmp/ask-native-probe-13038-epoch11-cleanup-epoch12`; raw export SHA-256 is
  `aa897bf4806ef3fae9c7d0e58d83552b48a448a642f5a551baebc12e804d7a6b`.
  This remains a failed attempt, not native admission evidence.
- HTTP/MCP acceptance: `DATABASE_URL=<isolated test hub> GOBBY_TEST_PROTECT=1
  GOBBY_GCODE_BIN=/Users/josh/.gobby/bin/gcode uv run pytest
  tests/servers/routes/test_ask.py tests/mcp_proxy/tools/test_ask.py -q` passed
  88 tests, including real authenticated boundaries. The same environment with
  `cargo nextest run -p gobby-code --test contract --test ask` passed all 18 tests,
  including the installed CLI lifecycle against the authenticated service.
- `338a063` commits the worktree-based frozen cohort runner. All 47 cohort contract
  tests passed; type/quality audits and Ruff passed. No primary cohort invocation
  has run. Frozen prompts, source commits, baseline scoring and answer-key exclusion
  remain unchanged.
- Epoch12 starts a new contained fresh/resumed probe from the stable clone with both
  fixes cherry-picked. It requests no exclusive test-hub window and makes no global
  installation or daemon restart. Output: `/tmp/ask-native-probe-13038-epoch12`.
  Native admission, the fourteen primaries and remaining task closures are pending.
- Final Python Ask package: 270 tests passed in 31.80 seconds. Native library:
  1,036 tests passed in 45.794 seconds through nextest using the separate
  `gobby_gcode_test` database on the isolated hub, as required by `crates/AGENTS.md`.
  Three configured skips remain explicitly reported by nextest.

## V12 Long-prompt policy identity and pending admission decision
`kind: verification`

- Epoch12 exited 1. Its investigator corrected the rewritten question after the
  new submission guard rejected it, then submitted successfully. Reviewer launch
  failed with `Ask SRT policy semantics changed after validation` before terminal
  creation. No resumed phase ran and this is not accepted runtime evidence.
- The reviewer prompt was 8,333 bytes versus the investigator's 1,923 bytes. Its
  metadata recorded the managed prompt file. Replacing only the run/workspace/temp
  identities in the captured investigator policy and adding that prompt read
  reproduced the exact recorded reviewer policy SHA-256:
  `eba8c1038d83ea6b432bbfa2eb63d7a751e05afb85cde7db325681578f65710e`.
- `75624b2` fixes the redundant prompt-read normalization. The new regression
  failed before the fix. With the fix, replay of both recorded policies gives
  `6104de7fcfa2ffd892e5b799f5906279df4e0e4728ec4f5020eff7c25e333a44`.
  This replay establishes the failure cause; it does not replace a native run.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
  GOBBY_TEST_PROTECT=1 uv run pytest tests/ask/test_runtime_validation.py
  tests/ask/test_permissions.py -q` passed 50 tests. Ruff check and format check
  passed for both changed files; `uv run mypy src/` passed all 2,031 sources.
  Test type/quality audits reported zero issues and the suppression ratchet passed.
  Logs: `/tmp/ask-prompt-policy-epoch13-{red,green,mypy,suppressions}.log`.
  Inline OCR review covered both staged files, with no remaining findings.
- Epoch12 cleanup proved the investigator, worker and owned terminal host absent,
  but could not establish launch authority for the rejected reviewer. It retained
  `/private/tmp/gobby-ap-37nszup8` and private schema
  `gobby_test_askprobe_3aa3b7c462d94bf1b3d9f2e2b7485aaa`. The incomplete raw export
  contains three receipts and three explicit exclusions, SHA-256
  `d9a7e511f558aab15b72fcbbfe465467f7c29b361ef1c13da70ad6f257c3d507`.
  Original failure and cleanup records remain in
  `/tmp/ask-native-probe-13038-epoch12`; nothing was fabricated or discarded.
- Cohort admission is awaiting the user's decision. #22020's stored prerequisite
  says "reviewed sealed native runtime admission through the normal loader";
  production startup also supports live provider/SRT derivation without a sealed
  artifact. The proposed amendment is to require that production admission plus
  accepted #22018/#22019 and installed CLI evidence, and explicitly report the
  unavailable native-tool denial receipts and safely ignored session override.
  It would preserve all sandbox/identity controls, frozen prompts/commits,
  fourteen serial primary invocations and scoring criteria. This proposal has
  not been applied to task criteria or cohort admission. No cohort primary ran.

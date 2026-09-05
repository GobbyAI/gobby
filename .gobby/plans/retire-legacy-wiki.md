# Retire the Legacy Gobby Wiki

**Plan ID:** retire-legacy-wiki

Approved execution intent supplied by the user in session #11777. Removal epic:
#21771. Worktree: `/Users/josh/.gobby/worktrees/gobby/wt-epic-21771`, branch
`wt-epic-21771`, based on `0.5.0` at
`53e862d53d40027173ba2d74d86858d44fbc88f4`.

## 1. Outcome and execution boundaries
`kind: framing`

Remove the existing wiki implementation, content, integrations, and obsolete task
backlog. Complete retirement before planning the replacement from the user's saved
outline. Perform implementation, isolated validation, and commits in the dedicated
epic worktree. Main-checkout daemon operations occur after removal changes land.

Preserve canonical session summaries and revisions, handoffs, transcript archives,
memories, project records, original files outside wiki storage, gcode functionality,
indexing, search, graph data, shared platform services, and explicit grant reservations.

Archive the old implementation with an annotated Git tag before removing it from
the active tree. Retain a scoped recovery backup outside repository indexing and
active wiki discovery. Future wiki development receives a new plan and task tree.

## 2. Retire the existing backlog
`kind: framing`

Close these 38 tasks through gobby-tasks, recording that the user superseded the
implementation with complete retirement: #19670, #19664, #19665, #19671, #19672,
#18779, #18790, #18869, #19766–#19793 inclusive, #21504, and #21539.

Use no-work closure reason `obsolete`, preserve history and linked commits, and
verify ancestor closures. Recheck ownership immediately before mutations. The
removal epic is outside the old task tree.

Narrow #21577 node configuration and acceptance to gcode. Narrow #21586 daemon
orchestration migration to code-index services and shared dependencies. Archive
the active wiki-output-design plan through the registry. Update roadmap references
and obsolete active guidance. Closing #21504 leaves reserved grants unchanged;
the replacement's datastore contract belongs to subsequent planning.

**Required outcome:** Every listed old task is closed; the two shared tasks retain gcode
obligations; completed work remains historically accessible; obsolete wiki work
cannot be dispatched.

## 3. Remove code and public surfaces
`kind: framing`

First relocate independently consumed functionality: terminal-output redaction
from the session-wiki module into shared utilities (updating agent spawn and health
checks); hub request forwarding from the wiki package into the shared files layer
(preserving authentication, streaming, response handling, and proxy-loop protection
for chat attachments); Mermaid utilities consumed by gcode.

Remove the entire crates/gwiki package, workspace membership, release/build/install
wiring, and Gobby-owned installed gwiki binaries. Remove Python wiki gateways,
services, watchers, scheduling, project-purge dependencies, and wiki runtime contracts.
Remove wiki MCP registration, HTTP routes, CLI/setup features, configuration,
generation profiles, and associated authorization entries. Remove wiki UI, settings,
navigation, shortcuts, subscriptions, and API clients. Saved Wiki-tab selections
return to the application's normal default view.

Remove automatic overview injection, recap generation, session-summary file mirrors,
and mirror-freshness requirements. Canonical summary generation and transcript
processing continue independently. Remove obsolete wiki tests; retain and update
tests covering surviving shared behavior.

Retire dedicated wiki schema objects through a new migration and update schema and
configuration contract carriers. Preserve historical migrations and shared datastore
objects.

**Required outcome:** Wiki tools and routes disappear from discovery; removed HTTP
endpoints return the application's normal missing-route response. Startup,
installation, project cleanup, and session processing succeed without the wiki.

## 4. Purge all legacy wiki state
`kind: framing`

Implement one bounded retirement procedure using existing datastore clients and
operational mechanisms. Default operation produces an inventory; apply uses that
explicit inventory.

Inventory this repository's wiki and legacy vaults associated with other recorded
checkouts; all personal, topic, project, and test vaults under hub wiki home; imported
source copies, generated pages, manifests, exports, session mirrors, discovery
registries; PostgreSQL gwiki_documents, gwiki_chunks, gwiki_links, gwiki_sources,
gwiki_ingestions; wiki-owned Qdrant collections; dedicated FalkorDB graph gobby_wiki;
installed wiki rules, variables, jobs, and remaining writer processes.

Execute in order:

1. Record Git archive tag, exact deletion inventory, ownership evidence, and scoped
   recovery backup.
2. Rehearse deletion and restoration against isolated state.
3. Disable legacy injection and writers, drain outstanding wiki operations, and
   deploy removal changes through the main checkout.
4. Verify installed runtime cannot recreate or serve wiki content.
5. Delete inventoried wiki files, projections, records, and registrations; apply
   dedicated schema retirement.
6. Reconcile stale code-index entries belonging to removed artifacts.
7. Record completion receipts and verify a fresh session receives no wiki injection.

Validate path containment and ownership before filesystem deletion. Remove symlink
entries without traversing targets. Preserve original sources outside wiki storage.
Use exact datastore targets; refuse blanket database, collection, or graph resets.
Failures record partial results and permit bounded retries. Recovery uses the
isolated backup and recorded baseline.

**Required outcome:** All inventoried legacy wiki content and derived state are gone;
remaining services cannot recreate it; unrelated files and platform state survive;
recovery has been demonstrated.

## 5. Verification and handoff
`kind: framing`

Use focused tests and isolated services to verify canonical sessions still
summarize, revise, hand off, and archive without wiki mirrors; diagnostic redaction
and hub chat-attachment forwarding retain behavior; startup, restart, installation,
project purge, and gcode indexing/search/graphs work without wiki package or binary;
MCP discovery, HTTP routing, UI navigation, and settings expose no legacy wiki.

Verify purge repeated and interrupted execution, unavailable backends, symlinks,
and out-of-scope paths. Verify unrelated datastore records and original files
remain intact, fresh sessions/restarted services neither inject nor regenerate wiki,
and task/plan registry matches retirement disposition.

Run targeted Python, Rust, frontend, schema-contract, lint, and type checks. Do not
run full pytest. Before formal Full-plan validation, fix the validator false
positive for explicit whole-file deletion of large files. Regression coverage must
distinguish complete deletion from edits still subject to size safeguards.

Before clearing context, persist a handoff containing epic/worktree identity, task
disposition, preservation boundaries, and outstanding validation. Replacement wiki
product outline remains input to separate planning after retirement.

## C1 Execution evidence and scope map
`kind: framing`

The narrative above is the approved scope. The deliverables below make its file
ownership, ordering, and validation explicit. Existing tasks stay in epic #21771;
do not create another removal epic or dispatch the retired backlog.

Archive tag: `legacy-wiki-before-retirement-21771`. Preparation commits are
`bcdb3ba7fa` (#21775 no-work review), `b372908c3d` (#21774 whole-file deletion
lint), `8ff5cb31bc` (#21778 schema 426), `b90c868a8f` (#21783 shared helpers),
and `e5681dbafd` (#21784 streaming JSON cleanup).

Consumer inventory used `gcode grep -l 'wiki|gwiki' src/gobby -m 200`, the same
sweep in tests and web, and `gcode grep -l 'codewiki_facts|codewiki_contract|vault::'
crates -m 100`. The Targets inventories below record the owned removal surfaces.
Shared Markdown wikilink syntax is independent rendering behavior; preserve it.
Code graph views consume CodewikiFacts/GraphAvailability: retain their query and
graph behavior while removing the wiki generator. Preserve the Mermaid renderer.
The project memory-dream truth digest currently reads a generated wiki sidecar;
remove that sidecar input and its change trigger while preserving ordinary project
and platform dream processing and all historical memory records.

All generated/configuration carriers are regenerated through their existing
source tools. Schema migration 426 is destructive and must not be applied live
before the scoped backup, restore rehearsal, and writer shutdown. Historical
core migrations, canonical summaries AND revisions, grant reservations, gcode
facts, external source files, and unrelated shared state remain preserved.

Rust removal precedes Python removal because their target sets are disjoint, and
removing the Rust package first allows an early focused build check. This changes
only execution order within the approved scope. Project-aware read-only validation
passed with zero warnings and 688 resolved targets before manifest emission.

## P1: Preserve independent behavior and prepare retirement contracts
`kind: framing`

### 6.1 Relocate shared diagnostics and file forwarding [category: refactor]
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor_support.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/files_home_proxy.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/mcp_proxy/tools/spawn_agent/_health.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/servers/routes/chat.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/servers/routes/chat_attachments.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/servers/routes/wiki.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/sessions/session_wiki_file.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/utils/terminal_output.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/wiki/owner_dispatch.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/mcp_proxy/tools/test_agent_capture_results.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/servers/routes/test_hub_files_proxy.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/sessions/test_session_wiki_file.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/utils/test_terminal_output.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file

Existing tasks #21783 and #21784 own this work. Move terminal-output redaction
into the shared utility and hub HTTP forwarding beside files_home_http. Update
all agent and chat consumers. Preserve secret/home redaction, auth, filtered
headers/status, request streaming, hop refusal, and transport cleanup. Read JSON
streams before parsing and close them on parse/read failure. Keep temporary wiki
consumers pointing inward until their deletion. Focused unit/API validation covers
redaction, agent failures, attachment operations, and upstream stream cleanup.

**Acceptance:**

- 6.1.1 - Independent agent diagnostics use shared redaction. file: `src/gobby/utils/terminal_output.py`.
- 6.1.2 - Chat attachment forwarding is independent of wiki and preserves transport behavior. file: `src/gobby/files_home_proxy.py`.

### 6.2 Prepare exact wiki schema retirement [category: code] (depends: 6.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: update the complete generated or declarative contract carrier
- `crates/gcore/assets/schema/migrations/426_retire_legacy_wiki.sql`
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/grant/tests.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: update the complete generated or declarative contract carrier
- `tests/storage/test_wiki_schema_retirement.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file

Existing task #21778 owns migration 426 and the identity carriers. A single
current-schema-qualified DROP targets exactly gwiki_documents, gwiki_chunks,
gwiki_links, gwiki_sources and gwiki_ingestions with RESTRICT. Unexpected consumers
must fail atomically. The catalog excludes these external projections and remains
unchanged after verification. Do not alter historical migrations. Isolated two-schema
validation proves repeatability, refusal on unknown dependencies, and survival of
shared records, summary revisions, grants, and outside same-name tables. Live
application is owned by 8.2 after backup and shutdown, never by this preparation.

**Acceptance:**

- 6.2.1 - Migration 426 removes only the five owned projection tables. file: `crates/gcore/assets/schema/migrations/426_retire_legacy_wiki.sql`.
- 6.2.2 - Identity carriers agree and isolated preservation/refusal tests pass. test: `tests/storage/test_wiki_schema_retirement.py`.

### 6.3 Correct retirement review prerequisites [category: code] (depends: 6.2)
`kind: deliverable`

Targets:
- `docs/contracts/plan-coverage.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: update the complete generated or declarative contract carrier
- `src/gobby/install/shared/workflows/agents/task-close-validator.yaml::*` — scope-reason: update the complete generated or declarative contract carrier
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_orchestration.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/plans/semantic_lint.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/tasks/agentic_close_review.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/plans/test_semantic_lint.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/tasks/test_agentic_close_review.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file

Existing tasks #21775 and #21774 own these fixes. Carry the no-work closure reason
through detached review and judge obsolete disposition without requiring retired
implementation; preserve completed-work validation. Exempt only explicit whole-file
operation: delete Targets from size-growth lint; partial edits and ambiguous deletion
prose retain safeguards. Regenerate the bundled manifest from the updated review
agent template. Focused task-review and semantic-lint tests cover both boundaries.

**Acceptance:**

- 6.3.1 - No-work reviews receive the canonical closure reason. file: `src/gobby/tasks/agentic_close_review.py`.
- 6.3.2 - Whole-file deletion passes size lint while partial edits stay guarded. test: `tests/plans/test_semantic_lint.py`.

## P2: Remove active implementation and public surfaces
`kind: framing`

### 7.1 Remove Python wiki services and session mirrors [category: code] (depends: 7.2)
`kind: deliverable`

Targets:
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: update the complete generated or declarative contract carrier
- `src/gobby/agents/sandbox_policy.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/ai/_tool_chat_contracts.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/ai/_tool_chat_tools.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/autonomous/progress_tracker.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/config/app.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/config/bootstrap.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/config/indexing.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/config/registry.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/config/wiki.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/files_migrate.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/gwiki_gateway.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/hooks/event_handlers/_session_start/agents.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: update the complete generated or declarative contract carrier
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-wiki-overview.yaml::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml::*` — scope-reason: update the complete generated or declarative contract carrier
- `src/gobby/mcp_proxy/registries.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/mcp_proxy/tools/wiki.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/mcp_proxy/wait_tools.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/memory/dream/service.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/memory/dream/storage_runs.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/memory/dream/truth_digest.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/projects/__init__.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/projects/gwiki_lock.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/projects/purge.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/runner.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/runner_init/config_subscribers.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/runner_init/servers.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/runner_lifecycle_periodic.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/runner_lifecycle_shutdown.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/servers/_app_routes.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/servers/app_factory.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/servers/auth_service.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/servers/grant_auth.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/servers/routes/llm.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/servers/routes/wiki.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/servers/routes/wiki_code.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/sessions/session_wiki_file.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/sessions/summarize.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/sessions/transcript_processing.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/storage/cron.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/storage/cron_display.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/storage/memories_dreams.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/tasks/criteria_contract.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/utils/wiki_vault.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/__init__.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/codewiki_dormant.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/owner_dispatch.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/owner_gateway.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/prune_job.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/scheduled_exports.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/scheduled_jobs.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/scheduled_jobs_history.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/scope_resolution.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/status.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/sync_container.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/update_coordinator.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/wiki/watcher.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/ai/test_tool_chat_service.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/ai/test_tool_chat_tools.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/test_files_migrate.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/test_hub_files_restore.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/config/test_app_config.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/config/test_config_registry.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/config/test_config_wiki.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/config/test_removed_config_migrations.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/contracts/gwiki.contract.json::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/hooks/event_handlers/test_session_variable_preservation.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/hooks/event_handlers/test_wiki_overview_seed.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/hooks/test_session_materialize.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/mcp_proxy/test_mcp_proxy_stdio.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/mcp_proxy/tools/test_mcp_proxy_tools_wiki.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/memory/test_dream.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/memory/test_dream_frozen_digest.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/meta/test_import_hygiene.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/projects/test_purge_components.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/projects/test_purge_service.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/scheduler/test_cron_storage.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/servers/routes/test_hub_files_proxy.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/servers/routes/test_llm_routes.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/servers/routes/test_wiki_code_routes.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/servers/routes/test_wiki_routes.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/servers/test_auth_service.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/servers/test_http_cron.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/sessions/test_session_wiki_file.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/sessions/test_sessions_lifecycle.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/storage/test_cron.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/storage/test_cron_display.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/test_gwiki_gateway.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/test_gwiki_gateway_reconciliation.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/test_runner_init.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/test_runner_lifecycle.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/test_runner_lifecycle_subsystems.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/test_runner_project_recovery.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/test_runner_shutdown.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/test_wiki_retirement_contract.py`
- `tests/utils/test_wiki_vault.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/wiki/test_codewiki_dormant.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/wiki/test_prune_job.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/wiki/test_recap_cron_timeout.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/wiki/test_scheduled_jobs.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/wiki/test_scope_resolution.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/wiki/test_update_coordinator.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/wiki/test_watcher.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/wiki/test_watcher_lifecycle.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/wiki/test_wiki_files_home.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/wiki/test_wiki_status.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/workflows/test_context_handoff_fencing.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/config/registry_key_encoding.py`
- `src/gobby/runner_init/project_purge.py`
- `src/gobby/runner_shutdown_storage.py`
- `src/gobby/runner_startup_code_index.py`
- `src/gobby/storage/cron_schedule.py`
- `src/gobby/config/_loading.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `src/gobby/config/values.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `src/gobby/servers/routes/configuration_generation_endpoints.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `src/gobby/storage/config_store.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `src/gobby/code_index/nightly_repair.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `src/gobby/scheduler/scheduler.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `tests/config/test_runtime_loop_consumers.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `tests/runner_init/test_config_runtime_startup.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `tests/scheduler/test_cron_integration.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `tests/storage/definitions/test_revisions.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `tests/test_bm25_startup.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement

Remove the dedicated wiki gateways, watchers, cron handlers, route/MCP registries,
config, authorization entries, and project-purge drain dependencies. Remove overview
seeding/injection and summary mirrors/freshness. Canonical valid summaries no longer
require a mirror; generation, revisions, transcript processing and handoffs remain.
Remove the generated project truth-digest reader and trigger; retain ordinary dream
sweeps and platform digest behavior. Remove wiki-owned AI tool families, scopes,
variables and runtime-config fields, regenerating config/template carriers. Shared
files-home migrations must no longer discover or move wiki storage. Remove dedicated
wiki tests; update surviving lifecycle, startup, config, route, auth and cleanup tests.
Existing exact retired-job protection may remain until 8.2 deletes inventoried rows;
no retired job may become dispatchable during cutover. No blanket shared-state reset.
Use isolated unit/API tests, source lint/types and config contract verification.

Current production sizes: registry.py 887, orchestration.py 891,
runner_lifecycle_shutdown.py 930, runner_lifecycle_subsystems.py 930, and cron.py 895.
Use direct, behavior-preserving extraction with existing focused characterization
checks and migrate all callers atomically; retain one owner for mutable state.

Move the dynamic key-segment encoding/decoding and its codec error from
`src/gobby/config/registry.py` into `src/gobby/config/registry_key_encoding.py`.
The registry and configuration consumers depend on that stateless codec.

Move project purge construction and datastore cleaner resolution from
`src/gobby/runner_init/orchestration.py` into `src/gobby/runner_init/project_purge.py`.
Orchestration calls the initializer; the initializer depends on project purge APIs.

Move executor and database concurrency shutdown from
`src/gobby/runner_lifecycle_shutdown.py` into `src/gobby/runner_shutdown_storage.py`.
Pass required shutdown policy explicitly, retaining cancellation-safe ordering.

Move code-index BM25 repair and task startup from
`src/gobby/runner_lifecycle_subsystems.py` into `src/gobby/runner_startup_code_index.py`.
The lifecycle coordinator invokes these code-index startup operations directly.

Move next-run computation from `src/gobby/storage/cron.py` into
`src/gobby/storage/cron_schedule.py`; storage and scheduler depend on this schedule
policy module, which depends only on cron models and time/cron libraries.

Consumer evidence: `gcode grep -l 'encode_dynamic_segment|decode_dynamic_segment|RegistryError'
src/ tests/ -m 100` found config/_loading.py, config/registry.py, config/values.py,
servers/routes/configuration_generation_endpoints.py, storage/config_store.py and
tests/config/test_config_registry.py. The corresponding literal sweep for
`_resolve_project_vector_cleaner|_resolve_project_graph_cleaner|_shutdown_database_executor|_shutdown_database_concurrency|_repair_code_index_bm25|_start_code_index_tasks|compute_next_run`
found code_index/nightly_repair.py, runner_init/orchestration.py,
runner_lifecycle_shutdown.py, runner_lifecycle_subsystems.py, scheduler/scheduler.py,
storage/cron.py, wiki/scheduled_jobs.py, and the targeted runtime/startup/scheduler/
revision/BM25/lifecycle/shutdown test files. All surviving consumers are Targets.

**Acceptance:**

- 7.1.1 - Startup and discovery expose no wiki services, tools, routes or config. test: `tests/test_wiki_retirement_contract.py`.
- 7.1.2 - Canonical summaries/revisions and transcript archives operate without wiki mirrors. file: `src/gobby/sessions/transcript_processing.py`.
- 7.1.3 - Project cleanup and memory dreams operate without wiki storage. file: `src/gobby/projects/purge.py`.

### 7.2 Remove gwiki crate and wiki-owned Rust contracts [category: code] (depends: 6.3)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `Cargo.toml`
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: update the complete generated or declarative contract carrier
- `crates/gcode/src/commands/graph/view/render.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcode/src/commands/graph/view/render_tests.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcode/src/index/walker/hidden.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcode/src/index/walker/tests/hidden.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcode/tests/contract.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcode/tests/facade_boundary.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/ai/daemon/operations.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/ai/daemon/tests/text.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/ai/generation/mod.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/ai/generation/tier.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/ai/mod.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/ai/tests.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/config/tests/standalone_removed.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/config/types.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/lib.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/mermaid.rs`
- `crates/gcore/src/qdrant/naming.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/qdrant/tests.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/schema/external.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/schema/mod.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/schema/runner.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/schema/runner_adoption.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/schema/verify.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/src/vault.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gcore/src/vault/links.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gcore/src/vault/lint.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gcore/src/vault/mermaid.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gcore/src/vault/reserved.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gcore/tests/public_boundary.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gwiki/Cargo.toml::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/README.md::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/assets/skills/audit.md::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/assets/skills/compile.md::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/assets/skills/query.md::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/contract/gwiki.contract.json::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ai/chunk.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ai/clients.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ai/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ai/translate.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/api.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/audit.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/audit/claims.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/audit/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/audit/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/benchmark.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/catalog.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/citations.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/cli.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/cli/code.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/cli/mapping.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/cli/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/cli/tests/code.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/cli/tests/ownership.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/cli/tests/routing.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/cli_runtime.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/code_graph.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/collect.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/collect/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/audit.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/backlinks.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/benchmark.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/citation_quality.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/citation_quality/contradictions.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/architecture_diagrams.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/architecture.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/audit.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/changes.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/concepts.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/concepts/plan.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/concepts/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/concepts/spans.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/concepts/support.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/concepts/types.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/curated_content.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/curated_content/page_content.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/curated_content/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/curated_content/tool_loop_dump.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/features.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/file.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/hotspots.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/infrastructure.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/modules.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/onboarding.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/build_parts/snapshot.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/cluster.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/command.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/compare.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/diagram_compose/candidates.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/diagram_compose/evidence.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/diagram_compose/generation.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/diagram_compose/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/doc_paths.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/frontmatter.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/generation.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/generation/aggregates.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/generation/files.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/graph.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/io.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/io/progress_journal.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/lock.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/ownership.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/ownership/analysis.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/ownership/codeowners.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/ownership/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/ownership/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/ownership_timeout_tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/paths.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/progress.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/prompts.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/prompts/builders.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/prompts/excerpts.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/prompts/systems.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/prompts/tables.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/prompts/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/prompts/types.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/publication.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/purge.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/relationship_facts.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/render/audit.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/render/common.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/render/diagrams.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/render/features.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/render/infrastructure.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/render/overview.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/render/pages.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/render/repo.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/repair.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/reuse.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/reuse_guard.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/run.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/run/ai.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/run/finalization.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/run/preparation.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/runtime.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/strict_markdown.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/stubs.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/system_model.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/ai.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/architecture.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/audit.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/changes.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/concepts.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/concurrency.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/contract.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/features.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/graph.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/hotspots.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/incremental.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/infrastructure.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/invalidation.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/io_safety.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/lock.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/modules.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/onboarding.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/progress.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/provenance.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/publication.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/purge.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/repair.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/reuse.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/support.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/tests/truth_digest.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/citations.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/frontmatter.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/generation.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/generation/aggregate.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/generation/one_shot.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/generation/outcome.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/generation/routing.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/generation/tool_loop.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/sanitize.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/structural.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/text/verify.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/truth_digest.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/types.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/code/types/ai.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/collect.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/compile.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/export.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/generation_routes.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/graph.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/graph_context.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/health.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/index.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/index/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/init.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/librarian.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/lint.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/normalize.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/page.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/pages.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/paths.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/project_admission.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/project_admission/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/prune.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/purge.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/read.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/recap.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/refresh/candidate.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/refresh/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/refresh/model.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/refresh/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/refresh/selection.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/refresh/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/refresh/vault.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/review_report.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/search.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/session_sync.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/sources.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/status.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/trust.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/upkeep.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/commands/vault_tools.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/compile/collect.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/compile/index.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/compile/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/compile/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/compile/select.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/compile/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/contract.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/credibility.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/document.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/error.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/explainer.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/exports.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/exports/assets.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/exports/graph.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/exports/pages.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/exports/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/exports/write.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/falkor_graph.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/falkor_graph/boost.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/falkor_graph/code_edges.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/falkor_graph/query.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/falkor_graph/sync.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/falkor_graph/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/falkor_graph/wiki_facts.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/frontmatter.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/graph/analytics.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/graph/context.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/graph/export.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/graph/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/health.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/health/citation_drift.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/health/citations.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/health/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/indexer.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/audio/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/audio/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/document/html.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/document/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/document/office.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/document/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/document/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/file.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/file/degradation.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/file/dispatch.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/file/generic.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/file/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/file/replay.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/file/source.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/file/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/git.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/image.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/immutable.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/pdf/ingest.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/pdf/markdown.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/pdf/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/pdf/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/pdf/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/pdf/text.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/pdf/types.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/codex.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/connections.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/daemon_wiki.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/derived.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/droid.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/grok.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/metadata.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/prompts/handoff_session_end.md::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/qwen.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/redaction.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/summarize.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session_archive.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session_archive/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/session_archive/tests/summary.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/url.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/url/cache.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/url/fetch.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/url/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/url/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/video/assets.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/video/metadata.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/video/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/video/processing.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/video/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/video/tests/degradation.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/video/tests/frame_assets.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/video/tests/processing.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/video/tests/storage.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/ingest/video/tests/translation.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/lib.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/librarian.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/librarian/proposals.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/librarian/semantic.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/librarian/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/lifecycle.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/links.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/lint.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/log.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/main.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/markdown.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/media.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/models.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/normalize.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/obsidian.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/output.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/owner_fs.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/page_version.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/paths.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/progress.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/project_lock.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/project_lock/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/provenance.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/recap.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/registry.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/runner.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/schema.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/scope.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/search/bm25.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/search/graph_boost.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/search/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/search/rrf.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/search/semantic.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/search/semantic_tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/session.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/singleton.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/sources/atomic.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/sources/manifest.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/sources/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/sources/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/sources/replay_repair.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/sources/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/sources/types.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/store.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/store/helpers.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/store/postgres.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/store/test_fake.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/store/types.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/config.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/counts.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/env.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/graph.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/postgres.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/scope.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/search.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/services.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/test_env.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/text.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/support/time.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/synthesis.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/synthesis/generate.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/synthesis/paths.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/synthesis/render.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/synthesis/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/synthesis/types.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/synthesis/write.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/test_http.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/transcribe.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/upkeep.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/upkeep/reserved_pages.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/upkeep/runner.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/upkeep/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/vault.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/vector.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/video.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/video/alignment.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/video/markdown.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/video/sampling.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/video/tests.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/video/timestamps.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/video/types.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/video/write.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/src/vision.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_collect.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_contract.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_export.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_init.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_output.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_parse.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_routing_surface.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_search.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_smoke/benchmark.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_smoke/config_postgres.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_smoke/ingest_refresh.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_smoke/main.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_smoke/public_smoke.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/cli_sources.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/code_engine_boundary.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/code_parity.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/common/mod.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/fixtures/codewiki_parity/README.md::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/fixtures/codewiki_parity/baseline.sha256::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/fixtures/codewiki_parity/project/Cargo.toml::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/fixtures/codewiki_parity/project/README.md::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/fixtures/codewiki_parity/project/src/lib.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/grant_errors.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `crates/gwiki/tests/status_grant_state.rs::*` — operation: delete — scope-reason: retire the entire legacy wiki file

Move the independently consumed Mermaid utilities into the gcore Mermaid module,
then delete the entire gwiki package and remaining dedicated vault code. Retain gcode
code-fact queries and graph views, including their current facts contract until an
independent rename is justified. Remove workspace membership and regenerate Cargo.lock.
Remove wiki generation profiles, schema creators/adoption, Qdrant naming and runtime
contracts that could recreate retired projections. Preserve gcode schema adoption,
search, vectors, graph data, and shared AI services. Do not repurpose reserved grants.
Run focused nextest targets for gcore/gcode/gdaemon, graph rendering and schema/config
contracts; cargo build, fmt and clippy on affected packages. No installation here.

**Acceptance:**

- 7.2.1 - The workspace has no gwiki package or wiki schema creator. file: `Cargo.toml`.
- 7.2.2 - gcode graph rendering retains shared Mermaid behavior. file: `crates/gcore/src/mermaid.rs`.
- 7.2.3 - Code indexing/search/graphs retain existing behavior and shared data. test: `crates/gcode/tests/contract.rs`.

### 7.3 Remove wiki UI and restore normal saved-tab behavior [category: code] (depends: 7.1)
`kind: deliverable`

Targets:
- `web/src/components/activity/ActivityPanel.tsx::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `web/src/components/activity/ActivityPanelTabs.tsx::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `web/src/components/activity/WikiSourceRemovalDialog.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/WikiTab.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/__tests__/WikiRetirement.test.tsx`
- `web/src/components/activity/wiki/WikiBacklinks.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiBrowse.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiCodewikiStatus.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiForceGraph.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiGraphScene.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiGraphView.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiPageEditor.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiPageReader.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiPageTree.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiQuickOpen.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiSourcesManager.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiTabActions.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiTabData.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiTabModel.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiTabState.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/WikiTabToolbar.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiA11y.test.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiBrowse.test.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiCodeMode.test.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiCodewikiStatus.test.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiGraph.test.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiPageEditor.conflict.test.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiPageEditor.test.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiTab.shell.test.tsx::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiTabActions.test.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiTabData.test.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiTabModel.test.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/WikiTabState.test.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/activity/wiki/__tests__/fixtures.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/components/app/__tests__/useAppKeyboardShortcuts.test.tsx::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `web/src/components/app/useAppKeyboardShortcuts.ts::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `web/src/components/settings/fields/TypedListField.tsx::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `web/src/components/settings/sections.ts::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `web/src/components/settings/sections/MemoryKnowledgeSection.tsx::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `web/src/components/settings/sections/__tests__/MemoryKnowledgeSection.test.tsx::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `web/src/components/shared/MermaidBlock.tsx::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `web/src/hooks/useWiki.ts::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `web/src/styles/tokens.css::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `web/tests/style-surfaces.spec.ts::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file

Load impeccable and the project design contract. Remove the Wiki tab, its tree,
editor, graph, API hook/client, subscriptions, keyboard paths, settings fields, graph
color tokens and dedicated tests. A persisted wiki selection must use the existing
normal default view; retain valid saved selections. Preserve shared Markdown and
Mermaid components and their independent consumers. Verify navigation and settings
with focused frontend tests and browser checks; run frontend types/lint/build.

**Acceptance:**

- 7.3.1 - Navigation/settings expose no wiki and saved wiki selections choose the normal default. test: `web/src/components/activity/__tests__/WikiRetirement.test.tsx`.
- 7.3.2 - Shared rendering and remaining activity views work after deletion. file: `web/src/components/activity/ActivityPanel.tsx`.

### 7.4 Remove installation wiring and active wiki guidance [category: code] (depends: 7.3)
`kind: deliverable`

Targets:
- `.github/workflows/ci.yml`
- `.github/workflows/release-gwiki.yml::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `.github/workflows/rust-ci.yml`
- `AGENTS.md`
- `ROADMAP.md`
- `crates/AGENTS.md`
- `crates/gcode/README.md`
- `crates/gcode/assets/SKILL.md`
- `crates/gcode/tests/release_workflows.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `crates/gcode/tests/workflow_security.rs::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `docs/architecture/gobby-v1.0.0-roadmap.md`
- `src/gobby/cli/_install_prompts.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/cli/cutover.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/cli/extensions.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/cli/init.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/cli/install_setup.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/cli/install_setup_gwiki.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/cli/installers/git_hooks.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/cli/installers/wiki_branch_setup.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `src/gobby/install/bin_set_coherence.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: update the complete generated or declarative contract carrier
- `src/gobby/install/distribution.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/install/shared/skills/code-index/SKILL.md`
- `src/gobby/install/shared/skills/impeccable/SKILL.md`
- `src/gobby/install/shared/skills/intro/SKILL.md`
- `src/gobby/install/shared/workflows/rules/AGENTS.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/utils/deps.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/utils/status.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/installers/test_git_hooks.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/installers/test_git_hooks_installer.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/installers/test_wiki_branch_setup.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/cli/test_cli_init_wiki_setup.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/cli/test_cli_install.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/test_cutover.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/test_daemon_set_coherence.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/test_install_coverage.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/test_install_setup.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/test_install_setup_gterm.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/cli/test_pack.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/docs/test_codewiki_docs.py::*` — operation: delete — scope-reason: retire the entire legacy wiki file
- `tests/install/test_bin_set_coherence.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/install/test_distribution.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/install/test_version_probe.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/skills/test_code_index_skill.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/test_cli_contracts.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/utils/test_deps.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/utils/test_utils_status.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `tests/utils/test_utils_status_1.py::*` — scope-reason: update wiki-related declarations, imports, behavior and contract assertions throughout this file
- `src/gobby/cli/install_release.py`
- `src/gobby/cli/install_setup_gclient.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `src/gobby/cli/install_setup_gcode.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `src/gobby/cli/install_setup_gdaemon.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `src/gobby/cli/install_setup_ghook.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `src/gobby/cli/install_setup_gterm.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `src/gobby/cli/install_setup_rtk.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `tests/cli/test_install_ghook.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement
- `tests/cli/test_install_setup_rtk.py::*` — scope-reason: migrate shared capability imports and characterization assertions during retirement

Remove gwiki build/release/install/version/coherence wiring and wiki-specific CLI
setup/hook commands, retaining other managed binaries and git hooks. Update roadmap
promises and active skills/guidance to reflect complete retirement; historical
changelogs, commits and completed task evidence remain. Regenerate the bundled
manifest from source. Installed binaries, rules and state are inventoried and handled
by 8.2 after landing. Focused installation/CLI/Rust workflow tests and manifest checks
must pass without gwiki available; do not start a daemon from the worktree.

Current install_setup.py size is 905 lines. Move release discovery, archive
extraction, checksum verification and binary download from
`src/gobby/cli/install_setup.py` into `src/gobby/cli/install_release.py`.
Surviving native-binary installers call this release transport module directly;
retain the existing authentication, checksum and new-inode installation behavior.
Use focused installer tests as characterization before and after the extraction.

Consumer evidence: `gcode grep -l '_build_release_download_url|_download_release_binary|_fetch_release_checksum|_verify_release_artifact|_extract_binary_from_release_archive|_resolve_latest_release_tag'
src/ tests/ -m 100` found install_setup.py, install_setup_gclient.py,
install_setup_gcode.py, install_setup_gdaemon.py, install_setup_ghook.py,
install_setup_gterm.py, install_setup_gwiki.py, install_setup_rtk.py, and
CLI tests test_install_ghook.py, test_install_setup.py, test_install_setup_gterm.py,
and test_install_setup_rtk.py. All consumers are targeted or deleted.

**Acceptance:**

- 7.4.1 - Installation, cutover and status succeed without gwiki. file: `src/gobby/cli/install_setup.py`.
- 7.4.2 - Active roadmap and bundled guidance no longer dispatch or recommend the legacy wiki. file: `ROADMAP.md`.

## P3: Purge legacy state and verify retirement
`kind: framing`

### 8.1 Implement bounded inventory, backup and retirement procedure [category: code] (depends: 7.4)
`kind: deliverable`

Targets:
- `scripts/retire_legacy_wiki.py`
- `scripts/wiki_retirement_inventory.py`
- `scripts/wiki_retirement_receipts.py`
- `scripts/wiki_retirement_storage.py`
- `tests/cli/test_wiki_retirement.py`

Provide one operator entrypoint with inventory as the default; backup, rehearsal,
apply and bounded retry require the exact recorded inventory and baseline. Use existing
PostgreSQL, Qdrant, FalkorDB, filesystem and operational clients. Inventory recorded
checkout vaults plus hub personal/topic/project/test vaults, imported copies, generated
pages/manifests/exports/mirrors/registries, exact five PostgreSQL tables, wiki-owned
Qdrant collections, exact gobby_wiki graph, installed rules/variables/jobs, binaries and
writer processes. Record identities, ownership evidence and content digests. Scope
backup and inventories outside repo indexing and wiki discovery, under the operator's
Gobby retirement directory with owner-only permissions. Back up exact targets and
record baseline/tag; never copy unrelated source content into this backup.

Before filesystem deletion, validate exact root containment, ownership and unchanged
inventory evidence; reject unowned/out-of-scope/changed paths. Unlink symlink entries
without walking targets or symlink parents. Use exact datastore names, not prefix-wide
resets; preserve shared database, grant reservations and gcode projections. Missing
backends fail visibly with partial receipts; successful items are recorded durably so
retry operates only on remaining inventory. A new artifact after inventory requires a
new reviewed inventory, never automatic widening. Rehearse deletion and restoration
against isolated equivalent targets before any live apply. The apply path requires
recorded writer shutdown and a runtime proof that the installed removal cannot serve
or recreate wiki. PostgreSQL schema retirement uses the canonical guarded migration
runner; never ad hoc production DDL. Test repeat/interruption/backend failure, symlinks,
containment/refusal and preservation using temporary files and isolated services.

**Acceptance:**

- 8.1.1 - Inventory is the safe default and apply consumes only explicit validated targets. file: `scripts/retire_legacy_wiki.py`.
- 8.1.2 - Backup/restore and interrupted retries preserve unrelated files and records. test: `tests/cli/test_wiki_retirement.py`.

### 8.2 Land retirement, purge inventoried state and close obsolete backlog [category: test] (depends: 8.1)
`kind: deliverable`

Targets:
- `.gobby/plans/completed/wiki-output-design.md`
- `.gobby/plans/retire-legacy-wiki.md`
- `.gobby/plans/retire-legacy-wiki.receipt.md`
- `.gobby/plans/wiki-output-design.md`

Task #21772 retains archival/backlog obligations. Record tag/inventory/ownership and
scoped recovery backup, then demonstrate isolated restoration. Disable installed
injection/writers through their registries and drain operations. Coordinate a quiet
restart window with active sessions. Land via managed worktree tools, deploy/rebuild
and install affected binaries through new inodes from main, and verify coherent schema
identity. Start only from main; verify the installed runtime lacks wiki discovery and
normal removed-route behavior. Apply exact inventory purge and migration 426 with the
verified backup, then reconcile only stale gcode entries for removed artifacts. Remove
Gobby-owned gwiki binaries/stamps and inventoried jobs/rules/variables/registrations.

Recheck ownership before each task mutation. Close all 38 user-listed obsolete tasks
with reason obsolete, retaining history/commits and verifying ancestors. Preserve
#21577 and #21586 narrowed gcode obligations and #21504 reserved grants. Archive the
old active plan through the plan registry at landing, maintaining branch/file/registry
coherence. Verify fresh sessions contain no wiki injection, restarted services cannot
regenerate it, and preserved summaries/revisions, handoffs, transcripts, memories,
projects, original sources and gcode index/search/graphs survive. Record sanitized
receipts, exact validation commands/results and backup references; no secret dump in
repo. Close leaves and epic only when complete, and delete the landed managed worktree.
Replacement product planning remains a separate subsequent session.

**Acceptance:**

- 8.2.1 - All inventoried wiki state is removed and recovery has been demonstrated. file: `.gobby/plans/retire-legacy-wiki.receipt.md`.
- 8.2.2 - All 38 old tasks and old plan are retired while shared obligations and grant reservations survive. file: `.gobby/plans/retire-legacy-wiki.receipt.md`.
- 8.2.3 - Main runtime/fresh sessions expose no wiki and retained services pass focused validation. file: `.gobby/plans/retire-legacy-wiki.receipt.md`.

## V1 Validation checkpoint
`kind: verification`

The explicit whole-file deletion validator prerequisite is complete. Run full
project-aware plan validation after the final target refinement, then emit the
approved manifest. Implementation and live retirement remain incomplete until
all listed acceptance is verified. Never run the full pytest suite.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Relocate shared diagnostics and file forwarding
  category: refactor
  task_type: feature
  depends_on: []
  validation_criteria: '6.1.1: Independent agent diagnostics use shared redaction.
    file: `src/gobby/utils/terminal_output.py`.

    6.1.2: Chat attachment forwarding is independent of wiki and preserves transport
    behavior. file: `src/gobby/files_home_proxy.py`.'
  labels:
  - covers:retire-legacy-wiki:6.1:6.1.1
  - covers:retire-legacy-wiki:6.1:6.1.2
  tdd: false
  source_section: '6.1'
  assigned_agent: backend-developer
- title: Prepare exact wiki schema retirement
  category: code
  task_type: feature
  depends_on:
  - '6.1'
  validation_criteria: '6.2.1: Migration 426 removes only the five owned projection
    tables. file: `crates/gcore/assets/schema/migrations/426_retire_legacy_wiki.sql`.

    6.2.2: Identity carriers agree and isolated preservation/refusal tests pass. test:
    `tests/storage/test_wiki_schema_retirement.py`.'
  labels:
  - covers:retire-legacy-wiki:6.2:6.2.1
  - covers:retire-legacy-wiki:6.2:6.2.2
  tdd: false
  source_section: '6.2'
  implementation_domain: backend
- title: Correct retirement review prerequisites
  category: code
  task_type: feature
  depends_on:
  - '6.2'
  validation_criteria: '6.3.1: No-work reviews receive the canonical closure reason.
    file: `src/gobby/tasks/agentic_close_review.py`.

    6.3.2: Whole-file deletion passes size lint while partial edits stay guarded.
    test: `tests/plans/test_semantic_lint.py`.'
  labels:
  - covers:retire-legacy-wiki:6.3:6.3.1
  - covers:retire-legacy-wiki:6.3:6.3.2
  tdd: false
  source_section: '6.3'
  implementation_domain: backend
- title: Remove Python wiki services and session mirrors
  category: code
  task_type: feature
  depends_on:
  - '7.2'
  validation_criteria: '7.1.1: Startup and discovery expose no wiki services, tools,
    routes or config. test: `tests/test_wiki_retirement_contract.py`.

    7.1.2: Canonical summaries/revisions and transcript archives operate without wiki
    mirrors. file: `src/gobby/sessions/transcript_processing.py`.

    7.1.3: Project cleanup and memory dreams operate without wiki storage. file: `src/gobby/projects/purge.py`.'
  labels:
  - covers:retire-legacy-wiki:7.1:7.1.1
  - covers:retire-legacy-wiki:7.1:7.1.2
  - covers:retire-legacy-wiki:7.1:7.1.3
  tdd: false
  source_section: '7.1'
  implementation_domain: backend
- title: Remove gwiki crate and wiki-owned Rust contracts
  category: code
  task_type: feature
  depends_on:
  - '6.3'
  validation_criteria: '7.2.1: The workspace has no gwiki package or wiki schema creator.
    file: `Cargo.toml`.

    7.2.2: gcode graph rendering retains shared Mermaid behavior. file: `crates/gcore/src/mermaid.rs`.

    7.2.3: Code indexing/search/graphs retain existing behavior and shared data. test:
    `crates/gcode/tests/contract.rs`.'
  labels:
  - covers:retire-legacy-wiki:7.2:7.2.1
  - covers:retire-legacy-wiki:7.2:7.2.2
  - covers:retire-legacy-wiki:7.2:7.2.3
  tdd: false
  source_section: '7.2'
  implementation_domain: backend
- title: Remove wiki UI and restore normal saved-tab behavior
  category: code
  task_type: feature
  depends_on:
  - '7.1'
  validation_criteria: '7.3.1: Navigation/settings expose no wiki and saved wiki selections
    choose the normal default. test: `web/src/components/activity/__tests__/WikiRetirement.test.tsx`.

    7.3.2: Shared rendering and remaining activity views work after deletion. file:
    `web/src/components/activity/ActivityPanel.tsx`.'
  labels:
  - covers:retire-legacy-wiki:7.3:7.3.1
  - covers:retire-legacy-wiki:7.3:7.3.2
  tdd: false
  source_section: '7.3'
  implementation_domain: frontend
- title: Remove installation wiring and active wiki guidance
  category: code
  task_type: feature
  depends_on:
  - '7.3'
  validation_criteria: '7.4.1: Installation, cutover and status succeed without gwiki.
    file: `src/gobby/cli/install_setup.py`.

    7.4.2: Active roadmap and bundled guidance no longer dispatch or recommend the
    legacy wiki. file: `ROADMAP.md`.'
  labels:
  - covers:retire-legacy-wiki:7.4:7.4.1
  - covers:retire-legacy-wiki:7.4:7.4.2
  tdd: false
  source_section: '7.4'
  implementation_domain: backend
- title: Implement bounded inventory, backup and retirement procedure
  category: code
  task_type: feature
  depends_on:
  - '7.4'
  validation_criteria: '8.1.1: Inventory is the safe default and apply consumes only
    explicit validated targets. file: `scripts/retire_legacy_wiki.py`.

    8.1.2: Backup/restore and interrupted retries preserve unrelated files and records.
    test: `tests/cli/test_wiki_retirement.py`.'
  labels:
  - covers:retire-legacy-wiki:8.1:8.1.1
  - covers:retire-legacy-wiki:8.1:8.1.2
  tdd: false
  source_section: '8.1'
  implementation_domain: backend
- title: Land retirement, purge inventoried state and close obsolete backlog
  category: test
  task_type: feature
  depends_on:
  - '8.1'
  validation_criteria: '8.2.1: All inventoried wiki state is removed and recovery
    has been demonstrated. file: `.gobby/plans/retire-legacy-wiki.receipt.md`.

    8.2.2: All 38 old tasks and old plan are retired while shared obligations and
    grant reservations survive. file: `.gobby/plans/retire-legacy-wiki.receipt.md`.

    8.2.3: Main runtime/fresh sessions expose no wiki and retained services pass focused
    validation. file: `.gobby/plans/retire-legacy-wiki.receipt.md`.'
  labels:
  - covers:retire-legacy-wiki:8.2:8.2.1
  - covers:retire-legacy-wiki:8.2:8.2.2
  - covers:retire-legacy-wiki:8.2:8.2.3
  tdd: false
  source_section: '8.2'
  assigned_agent: backend-developer
```

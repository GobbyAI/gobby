Plan artifact: `.gobby/plans/workspace-maintenance-and-log-hygiene.md`
# Workspace maintenance and log hygiene

**Plan ID:** workspace-maintenance-and-log-hygiene

## C1: Confirmed decisions and exclusions
`kind: framing`

**Decision Record:** Workspace backups run only at 03:30 America/Chicago through the existing cron, never from session start/end, handoff, compaction or team teardown. Preserve source, sanctioned repository copies and reviewed snapshots, scripts, notes, patches and evidence. Route build/cache data to existing managed external roots. Reclaim exclusively owned generated caches of expired sessions nightly before backup without extra retention grace. Protect live, revived, shared and unknown owners. Keep repository copying restricted to sanctioned Gobby clone/worktree tools. Exclude generated data from backups and source commits; retire tracked generated files from the index without deleting working files or rewriting history.

Reuse scheduling, liveness, resource locks and safe-deletion machinery. No new cache registry, schema, scheduler, watchdog or periodic diagnostic logger. Quiet expected outcomes; retain actionable failures and security enforcement.

**Ownership exclusions:** The audit inspected attached task scopes, dirty paths and branch diffs. Exclude #25645 startup warning flood, test-hub hangs, sandbox/managed-environment repairs; #25644 coordination waits, landing/base tracking and sender authority; #25640 bootstrap/routing/reuse; #25633/#25608 restart notices/quiet hours; #25565 definition security. #25639 and #25630 were already fixed. Scheduled model defaults remain owned by #25641. Recheck claims/branch diffs before edits. #25646 nightly-only backup landed at 044f775e0d; it removed lifecycle triggers without extracting the backup module. Reuse that delivered behavior.

**Additional confirmed decision:** Coordinator gobby#19309 relayed Josh's instruction to fold in the hook-trust finding from security auditor gobby#19399 on #25633. Section 1.4 owns that independent outcome and its activation.

**Granularity:** Twelve remaining independently closeable deliverables. Artifact eligibility, expired-owner deletion, hook trust and each runtime defect have distinct boundaries. Each owns regressions. Shared paths have explicit dependency ordering. Section 1.2 coordinates the large-module split with all existing backup test consumers to keep its behavior atomic. No filler planning/testing leaves. Keep production files below 1,000 lines. New test names are planned symbols, not passing evidence.

## P1: Workspace generated-data lifecycle and commit safety
`kind: framing`

Preserve existing nightly cron identity, serialized background Git work, 30-second timeout, lock recovery and disabled/parked settings. Process remaining projects after a failure, then fail the nightly result truthfully. Preserve register_agent_workspace_cron so runner orchestration needs no edit.

### 1.1: Landed nightly-only backup prerequisite
`kind: framing`

#25646 removed lifecycle backup triggers and scheduling APIs. Its landed caller sweep covered team_teardown.py, hooks/event_handlers/_agent.py, _session_end.py, _session_start/materialize.py and mcp_proxy/tools/sessions/_terminal.py. Do not recreate it. Remaining module split and truthful nightly failure aggregation belong to 1.2.

### 1.2: Exclude generated artifacts and reject cache commits (depends: #25646)
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/utils/generated_artifacts.py`
- `src/gobby/agents/agent_workspaces.py::*` — scope-reason: extract backup implementation and retain workspace binding plus public cron registration
- `src/gobby/agents/workspace_backup.py`
- `src/gobby/cli/extensions.py::*` — scope-reason: invoke staged-artifact verification through existing hook command
- `src/gobby/hooks/verification_runner.py::*` — scope-reason: enforce staged checks without optional commands
- `.gitignore`
- `tests/agents/test_agent_workspaces.py::*` — scope-reason: adapt backup imports and patch sites to the extracted module
- `tests/agents/test_agent_workspace_backup_bounds.py::*` — scope-reason: preserve backup bounds across extraction
- `tests/agents/test_agent_workspace_commit_lock.py::*` — scope-reason: preserve serialization and lock recovery across extraction
- `tests/agents/test_agent_workspace_epics.py::*` — scope-reason: preserve workspace source coverage across extraction
- `tests/agents/test_agent_workspace_lifecycle.py::*` — scope-reason: preserve nightly-only lifecycle behavior across extraction
- `tests/agents/test_agent_workspace_skipped_paths.py::*` — scope-reason: preserve exclusion regression patch sites across extraction
- `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py::*` — scope-reason: preserve session-end cleanup without backup triggers
- `tests/agents/test_workspace_generated_artifacts.py`
- `tests/hooks/test_generated_artifact_verification.py`

**Research context:** Existing exclusions cover target, node_modules and virtual environments but omit observed cargo-target and mypy-cache variants. Audit counted 91,531 tracked workspace files, mostly source snapshots; one cargo-target/debug/deps held 2,990 files and about 1.76 GiB. Pathspec exclusions did not reliably prune traversal: install Git ignores before enumeration. hooks_run and VerificationRunner.run_stage supply pre-commit integration; installer already invokes that command. Source snapshots remain eligible. agent_workspaces.py is 874 lines after #25646. Existing backup tests above import or patch its internals; orchestration calls register_agent_workspace_cron and remains unchanged.

Split src/gobby/agents/agent_workspaces.py and move backup Git operations, worker ownership and nightly execution into new src/gobby/agents/workspace_backup.py. Keep workspace binding and public cron-registration entrypoint in the original. Adapt all affected existing test imports/patches. Preserve nightly-only scheduling, serialization, timeout, lock recovery and parked/disabled semantics. Attempt other projects after failures and then fail the nightly job truthfully.

Extract a shared generated-directory classifier. Preserve current exclusions; add cargo-target, cargo-target-* variants, mypy-cache, .zig-cache and zig-out. Match full directory components rather than substrings/extensions. Apply Git ignores before untracked traversal and classifier before staging. Retire tracked generated paths by index-only removal, retaining disk/history. Read staged index with NUL-safe paths. Pre-commit refuses generated additions/modifications including force-adds, permits deletion and never restages/deletes. Preserve legitimate similarly named source files and source fixtures.

**Acceptance:**

- 1.2.1 - Generated data is excluded before traversal/staging; source snapshots remain backed up. file: `src/gobby/agents/workspace_backup.py`.
- 1.2.2 - Staged checks run without optional commands and preserve index/worktree contents. symbol: `VerificationRunner.run_stage`.
- 1.2.3 - Tracked generated files are retired only from index. file: `src/gobby/utils/generated_artifacts.py`.
- 1.2.4 - Split preserves nightly-only invocation, locks, timeout and source coverage; project failures fail nightly result after other projects run. file: `src/gobby/agents/workspace_backup.py`.
- 1.2.5 - Force-add, deletion, unusual paths and source boundaries have regressions. test: `tests/hooks/test_generated_artifact_verification.py::test_precommit_rejects_force_added_cache_files`.

**Verification:** Planned isolated Git fixtures in both new files and all existing backup/hook regressions above. No live artifact removal in tests.

### 1.3: Nightly cleanup of expired-session caches (depends: 1.2, #25645)
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/agents/workspace_cache_cleanup.py`
- `src/gobby/agents/workspace_backup.py`
- `src/gobby/agents/cargo_target.py::*` — scope-reason: reuse target ownership and safe removal for expired exclusive consumers
- `docs/guides/agents.md`
- `tests/agents/test_workspace_cache_cleanup.py`
- `tests/agents/test_workspace_cache_routing.py`

**Research context:** SandboxRunPaths.environment redirects caches; Cargo homes/checkout targets intentionally share build inputs. cargo_target helpers guard roots; sandbox reaper patterns supply liveness/safe deletion. Expired session status is authoritative, not age/mtime. #25645 owns environment repairs: verify integration without duplicating them. Preserve common Cargo homes, sccache and live consumers' targets. workspace_backup.py is introduced by prerequisite 1.2; refresh its symbol-qualified inventory after that leaf lands.

Use existing external cache roots and checkout routing; verify sanctioned copies/reviewed snapshots inherit paths after #25645. Add off-event-loop cleanup before nightly backup, without hooks/new scheduler/schema. Resolve verified local session/project ownership of legacy folders and managed per-session/per-checkout caches. Require expired owner, no live CLI/process/run/checkout writer and no other live consumer. Recheck under existing resource locks immediately before deletion. Legacy directories require classifier plus tool cache markers. Skip unknown/shared/live/revived owners, symlinks and out-of-root paths. Preserve code, notes, patches, evidence and unrecognized directories. Continue per-owner failures and report genuine failures in nightly result. Idempotent cleanup, no extra grace.

**Acceptance:**

- 1.3.1 - Exclusively owned expired generated caches including identified legacy caches are removed before backup. file: `src/gobby/agents/workspace_cache_cleanup.py`.
- 1.3.2 - Live/revived/shared/unknown/symlink/out-of-root cases survive with lock-protected rechecks. file: `src/gobby/agents/workspace_cache_cleanup.py`.
- 1.3.3 - Sanctioned copying preserves code/evidence and uses external targets without relaxed restrictions. file: `tests/agents/test_workspace_cache_routing.py`.
- 1.3.4 - Expiration/preservation boundaries pass isolated regressions. test: `tests/agents/test_workspace_cache_cleanup.py::test_expired_session_cleanup_preserves_code_and_live_caches`.

**Verification:** Planned expired/live/shared/revived fixtures, repeated cleanup, failure, symlink escape and cache routing tests; existing Cargo target tests remain green.

### 1.4: Preserve protected-branch hook trust
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/cli/installers/git_hooks.py::*` — scope-reason: integrate extracted trusted hook rendering into installation
- `src/gobby/cli/installers/git_hook_guard.py`
- `src/gobby/install/shared/workflows/rules/git-hook-trust.yaml`
- `tests/cli/installers/test_landing_guard_hook.py::*` — scope-reason: update existing renderer consumer after extraction
- `tests/cli/installers/test_git_hook_trust.py`
- `tests/workflows/test_git_hook_trust_commands.py`

**Research context:** Security auditor gobby#19399 reported _landing_guard_hook resolving python3 through PATH and ghook through HOME. Coordinator gobby#19309 relayed Josh's fold-in instruction. A seat can prefix PATH/HOME or git -c core.hooksPath to swap/skip protected-branch reference-transaction enforcement. Existing gobby install regenerates that hook. The new rule is a planned definition with file-level version 1.0.0; Python tests must never mention bundled definition names. git_hooks.py has 870 lines. The literal consumer sweep gcode grep -w _landing_guard_hook found only its installer definition/call and tests/cli/installers/test_landing_guard_hook.py; both edited consumers are listed above.

Split src/gobby/cli/installers/git_hooks.py and move the protected-branch hook renderer into new src/gobby/cli/installers/git_hook_guard.py. Update the installer import/call and existing renderer test consumer, preserving the generated hook contract.

Resolve absolute python3 and ghook paths at install time and embed safely quoted literal absolute paths in the rendered hook. Eliminate runtime PATH/HOME lookup for these programs. Add a before_tool rule refusing git invocations with core.hooksPath configuration overrides, including -c spacing/quoting and --config-env, and PATH/HOME/GIT_CONFIG_* environment prefixes, including env and bash -c wrappers. Use existing shell command parsing/enforcement machinery; preserve plain git invocation. Test the bundled definition generically with YAML validation/engine dry run and synthetic blocked/allowed tables. Reinstall through gobby install for activation.

**Acceptance:**

- 1.4.1 - Synthetic installation produces absolute interpreter/ghook literals without runtime PATH/HOME lookup. test: `tests/cli/installers/test_git_hook_trust.py::test_guard_embeds_absolute_install_paths`.
- 1.4.2 - Config overrides and environment/wrapper bypasses are refused while plain git remains allowed. test: `tests/workflows/test_git_hook_trust_commands.py::test_git_hook_override_forms_are_refused`.
- 1.4.3 - The new definition is versioned, loaded generically and effective after hook regeneration. file: `src/gobby/install/shared/workflows/rules/git-hook-trust.yaml`.

**Verification:** Planned focused rendering and engine command-table tests with synthetic paths, plus existing renderer tests. Inspect installed rendered hook after gobby install; do not move a live protected branch during testing.

## P2: Runtime correctness and useful diagnostics
`kind: framing`

Independent leaves subject to active ownership. Expected quiet outcomes retain enforcement/state. Genuine failures remain visible.

### 2.1: Recognize routine Claude transcript metadata
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/sessions/transcripts/claude.py::*` — scope-reason: recognize metadata without changing primary or incremental parsing
- `tests/sessions/test_claude_metadata_records.py`

**Research context:** ClaudeTranscriptParser._SKIPPED_RECORD_TYPES recognizes metadata. Rotations repeatedly logged api-request, api-request-blob, api-request-shape, session-owner and tools-used. Normal records separately carry conversation/usage. Unknown types still require diagnostics.

Add five observed types to recognized metadata. Preserve messages, usage, positions and incremental indices. Metadata must not render/dump payloads/repeat parser errors. Preserve genuinely unknown synthetic-type diagnostics; no global unknown-type suppression.

**Acceptance:**

- 2.1.1 - Five metadata types are quiet and absent from conversation including repeated parses. symbol: `ClaudeTranscriptParser`.
- 2.1.2 - Messages/usage/offsets remain correct; unknown types remain diagnostic. test: `tests/sessions/test_claude_metadata_records.py::test_known_metadata_does_not_log_or_render`.

**Verification:** Planned named file plus existing Claude incremental/usage tests.

### 2.2: Quiet expected Python lifecycle diagnostics
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/events/wake.py::*` — operation: delete-lines — base-blob: 06dc483f37855cd0cde0d07e51b68434909f3fed — lines: 845-847 — scope-reason: remove only routine draft warning and preserve refusal
- `src/gobby/adapters/codex_impl/hooks_adapter.py::*` — scope-reason: classify expected attribution refusal at debug
- `src/gobby/hooks/terminal_context.py::*` — scope-reason: quiet expected native identity absence
- `src/gobby/sessions/mailbox_delivery.py::*` — scope-reason: quiet ordinary bookkeeping while retaining failures
- `tests/adapters/test_codex_pending_shell_refusal_log.py::*` — scope-reason: retain proof refusal coverage at intended level
- `tests/events/test_expected_lifecycle_logging.py`

**Research context:** Draft protection and pending-shell cwd proofs intentionally refuse unsafe actions. Audit saw repeated expected warnings, shared app-server native identity absence and mailbox INFO bookkeeping. wake.py has 991 lines; its bound deletion proof removes only the three-line warning. No unrelated split is needed.

Delete routine draft warning, preserving refusal/state. Expected unattributed pending-call proof refusal and native identity absence become DEBUG. Preserve unreadable/malformed rollout and unexpected errors. Routine mailbox delivery/skip/retry becomes DEBUG; actual failures remain visible. Do not change cwd security, wake routing or message state.

**Acceptance:**

- 2.2.1 - Draft/cwd protections and mailbox state are unchanged; expected outcomes are quiet at INFO/WARNING. file: `src/gobby/events/wake.py`.
- 2.2.2 - Malformed input and actual delivery failures retain diagnostics. test: `tests/events/test_expected_lifecycle_logging.py::test_expected_deferrals_remain_safe_and_quiet`.

**Verification:** Planned named files and existing wake/terminal/mailbox regressions with captured logs, synthetic sessions and no live delivery.

### 2.3: Quiet expected native client reconnect and exit noise
`kind: deliverable`

Category: code; implementation_domain: fullstack; tdd: true.

Targets:
- `crates/gclient/src/app/live_sidebar.rs::*` — scope-reason: quiet optional row failures during known unavailability
- `crates/gclient/src/app/live_attach/recovery.rs::*` — scope-reason: classify expected exit stale and tombstone outcomes without state changes
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed client version coherent

**Research context:** Optional sidebar rows repeat unavailable warnings; recovery warns for terminal_exited/stale/tombstone outcomes. Existing connection transitions/visible status communicate outages. Unpublished managed client source changes still require patch bump, rebuild/promotion. No new outage tracker.

Downgrade known-unavailable optional row failures and expected exit/stale/tombstone outcomes to DEBUG. Preserve WARN for authorization/protocol/persistent unexpected recovery failures and existing transition reporting. Keep UI/state unchanged. Add focused Rust regressions in modules. Bump client patch, lockfile/pin in shipping commit; rebuild/promote through guard. No unrelated crate release.

**Acceptance:**

- 2.3.1 - Expected outcomes are quiet; meaningful failures/state/visible errors remain correct. file: `crates/gclient/src/app/live_attach/recovery.rs`.
- 2.3.2 - Focused native tests and coherent installation pass. test: `crates/gclient/src/app/live_attach/recovery.rs::tests::expected_terminal_exit_is_not_a_warning`.

**Verification:** Planned targeted Cargo tests/build and installed identity checks. Load Rust skill/release guidance before editing.

### 2.4: Handle pinned index contention like normal sync
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/code_index/sync_worker.py::*` — scope-reason: apply typed busy outcomes to pinned vector and graph work
- `tests/code_index/test_pinned_sync_contention.py`

**Research context:** _sync_file catches GcodeBusyError; _sync_pinned_file emits generic failure tracebacks. Pin stamps must stay pending during contention. Existing worker passes/bounded retries suffice.

Catch typed busy outcomes for vector/graph pins, mirror ordinary breaker accounting, leave pending for later pass. Never mark busy work complete or fail breakers incorrectly. Preserve exhausted transport/unexpected projection ERROR. No new timer.

**Acceptance:**

- 2.4.1 - Busy pins remain pending and later succeed without incorrect breaker failure. symbol: `_sync_pinned_file`.
- 2.4.2 - Vector/graph contention and actual failures have regressions. test: `tests/code_index/test_pinned_sync_contention.py::test_busy_pin_remains_pending_until_success`.

**Verification:** Planned named file and existing sync-worker tests with typed errors/stamp assertions.

### 2.5: Detach mutable detector payloads from frozen events
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/workflows/engine/run_command.py::*` — scope-reason: detach payload before normalization while preserving frozen input
- `tests/workflows/test_run_command_frozen_payload.py`

**Research context:** Offline reproduction: build_run_command_payload(freeze_evaluation_value(event)) raises TypeError, Workflow evaluation inputs are read-only. deepcopy retains frozen dict. Existing mutable_evaluation_result recursively detaches mutable data; retain evaluation security.

Use mutable_evaluation_result before normalization/metadata writes. Preserve original event and nested inputs. Verify serialization and actual detector dispatch.

**Acceptance:**

- 2.5.1 - Frozen events yield normalized serializable payloads without mutation/encoding warning. symbol: `build_run_command_payload`.
- 2.5.2 - Nested immutability and dispatch have regressions. test: `tests/workflows/test_run_command_frozen_payload.py::test_detector_payload_accepts_frozen_event`.

**Verification:** Planned named file and existing run-command tests. Observed failure above is separate from planned passing checks.

### 2.6: Fail closed normally on malformed shell input
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/workflows/workspace_read_targets.py::*` — scope-reason: translate expected scanner failure to existing unprovable-read outcome
- `tests/workflows/test_workspace_shell_parse_refusal.py`

**Research context:** Offline _workspace_read_targets with cat plus unterminated quote raises ValueError: Unclosed shell quote or escape. _mask_regex_operands scans before fallback. None already means unprovable read; permissive splitting weakens enforcement.

Catch expected scanner ValueError at boundary, return fail-closed sentinel. Preserve valid quoting/heredocs/regex masking/path restrictions. No permissive split. Unexpected programmer errors still raise.

**Acceptance:**

- 2.6.1 - Malformed quotes give ordinary fail-closed decisions without evaluator exceptions; valid behavior remains. symbol: `_workspace_read_targets`.
- 2.6.2 - Malformed/valid boundaries have regressions. test: `tests/workflows/test_workspace_shell_parse_refusal.py::test_malformed_quote_is_normal_fail_closed_result`.

**Verification:** Planned named file and existing workspace read enforcement tests.

### 2.7: Resolve short spawn parents in caller project
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/workflows/condition_helpers_spawn.py::*` — scope-reason: bind local parent lookup to verified project
- `tests/workflows/test_spawn_parent_project_resolution.py`

**Research context:** _target_project_id omits verified caller project resolving parent_session_id, causing observed project-context-required errors for #N. Project identity already reaches helper. Explicit project-path precedence/authorization remain authoritative.

Pass caller project for local parent resolution. Preserve qualified cross-project refs/UUIDs, explicit path precedence and missing/unauthorized refusal. No arbitrary cwd authority inference.

**Acceptance:**

- 2.7.1 - Local refs resolve in caller project; qualified/UUID semantics and authorization remain. symbol: `_target_project_id`.
- 2.7.2 - Local lookup/precedence have regressions. test: `tests/workflows/test_spawn_parent_project_resolution.py::test_short_parent_ref_uses_verified_caller_project`.

**Verification:** Planned named file and existing spawn-condition tests with separate projects.

### 2.8: Avoid copying provider hooks onto themselves
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/agents/isolation_repair.py::*` — scope-reason: recognize identical roots without weakening tracked-file protection
- `tests/agents/test_same_directory_hook_repair.py`

**Research context:** _copy_cli_hooks reaches shutil.copytree on identical directories; audit warnings name identical paths. Existing tracked-provider exclusions protect repository hooks.

Quiet no-op for same filesystem object including aliases. Preserve distinct-directory copying, tracked exclusions and actual filesystem errors.

**Acceptance:**

- 2.8.1 - Same-root/alias repair is quiet; distinct eligible hooks copy and tracked files remain protected. symbol: `_copy_cli_hooks`.
- 2.8.2 - Identity/distinct-copy boundaries have regressions. test: `tests/agents/test_same_directory_hook_repair.py::test_same_directory_hooks_are_not_copied`.

**Verification:** Planned named file and isolation-repair tests using temporary directories/aliases.

### 2.9: Report overflow according to actual recoverability
`kind: deliverable`

Category: code; implementation_domain: backend; tdd: true.

Targets:
- `src/gobby/adapters/degradation.py::*` — scope-reason: expose carry persistence outcomes for truthful classification
- `src/gobby/llm/sdk_utils.py::*` — scope-reason: classify overflow after recoverability is known
- `tests/adapters/test_context_overflow_reporting.py`

**Research context:** fit_additional_context warns before fitting. truncate_context_for_adapter persists overflow/stages whole contributors. _stage_carried_context skips oversized/excess entries. Blanket downgrading hides loss; a second queue is unnecessary.

Classify after fitting/persistence/staging. Successful carry is DEBUG. Uncarriable/unrecoverable omissions and persistence/staging failures remain WARNING. Minimal internal outcome metadata; preserve public adapter strings, whole-contributor fitting, caps and bounded queue. Avoid duplicate warnings.

**Acceptance:**

- 2.9.1 - Every omission is carried/recoverable or causes warning; successful carry is DEBUG. symbol: `truncate_context_for_adapter`.
- 2.9.2 - Whole-contributor fitting/caps and single-warning behavior remain. symbol: `fit_additional_context`.
- 2.9.3 - Oversized/excess parts, absent storage and failed persistence/receipt staging have regressions. test: `tests/adapters/test_context_overflow_reporting.py::test_carried_overflow_is_quiet_and_loss_warns`.

**Verification:** Planned named file and fitting/degradation regressions with exact retained content/logs.

## V1: Validation and activation
`kind: verification`

Each code leaf records failing-before, passing-after, final validation and bounded review against full candidate SHA. Python tests use DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest with focused files/neighbors. Run Ruff check, format --check, mypy src/ and changed-test quality/type/suppression audits. Never full pytest. Native work runs focused tests, versioned rebuild/guarded promotion. Follow installed restart policy/quiet hours. Run gobby install to regenerate trusted Git hooks for 1.4. Use isolated fixtures; no live deletion or protected-branch moves during planning/testing.

Run uv run gobby plans validate .gobby/plans/workspace-maintenance-and-log-hygiene.md -p /Users/josh/Projects/gobby after saving. Narrative draft has no M1; expansion validation is not applicable yet. Registration waits for real implementation root; no synthetic planning tasks. Review/manifest/expansion are separate stages. Implementation has not begun under this plan.

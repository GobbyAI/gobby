# Herdr Terminal Client QA Fixes

**Plan ID:** herdr-terminal-client-qa-fixes

## Overview
`kind: framing`

Epic #20255 (`.gobby/plans/herdr-terminal-client.md`) closed on branch
`wt-task-20255-m4` (HEAD `518cec5c41`, merge-base with `0.5.0` = `b89f371a15`) with
the daemon-side terminal runtime largely real and the client side largely not: ten red
tests at HEAD, a `gclient` binary that probes health and exits, fourteen placebo
acceptance tests in `crates/gterminal/tests`, a fabricated native-backend-flip evidence
artifact that flipped the shipped default, and a set of merge-drift breakages against
the 69 commits `0.5.0` gained since the merge-base. This plan fixes every finding of the
2026-08-21 QA review inside an isolation worktree that starts from `0.5.0`, absorbs
`wt-task-20255-m4` as its first leaf, and lands on `0.5.0` only when the accumulated
guard set is green and herdr UI parity evidence is committed.

The review, its finding IDs (`A-*` CI/packaging, `B-*` storage/runtime, `C-*` WS/REST/web,
`D-*` Rust host/client, `E-*` native/flip, `F-*` merge drift), and the confirmed
Decision Record live in the review record; every deliverable below cites the IDs it
closes so the adversary and the close-time judge can trace each fix to its finding. The
review record is not checked in, so P1 cites the `F-*` class as a whole: 1.1 closes every
merge-drift finding and 1.2 the schema-lineage finding; every other class is cited by ID.

One further class, `Q-*`, carries defects reported from the running UI while this plan
was under adversarial review, with the user's decision on each recorded here. `Q-1`
(daemon, § 4.3) and `Q-2` (web client, § 4.6) are the two halves of one defect: terminal
history is unreadable in the web UI because output can precede the attach
acknowledgement and the renderer remounts under it. The user chose the bounded
recent-history contract over full tmux retention: the host's existing attach-history
window stands (500 lines by default, never above 2,000 lines or 256 KiB), truncation is
shown rather than implied, and scrolling must be reliable inside that window — on touch
as well as pointer devices, which is why the history has to reach the mounted renderer
before the first keyframe. The user also fixed the proof: § 4.6 closes only after live
tier-preview validation in all three canonical modes, not on unit and Playwright
evidence alone.

## Constraints
`kind: framing`

- **Build invocation.** From the `0.5.0` checkout at `/Users/josh/Projects/gobby`:
  `uv run gobby build .gobby/plans/herdr-terminal-client-qa-fixes.md --planning-seed-state approved --completed-plan-review-rounds <N>`.
  `<N>` is not a placeholder to be guessed: handoff substitutes the number of
  finalized adversarial rounds recorded in `## V1 Plan Changelog` at the moment of
  handoff, and Task Mapping's copy of this command is the authoritative one. A literal
  count baked in here goes stale the moment another round finalizes and would seed
  build state with review history the plan does not have.
  The target branch is `0.5.0` (the checkout's current branch), so the epic's shared
  worktree is created from `0.5.0` and the epic's `merge` stage lands on `0.5.0`.
  Leaf 1.1 merges `wt-task-20255-m4` into that worktree, which yields exactly the
  "0.5.0 merged into the epic work, fixes on top, then land" ordering the Decision
  Record fixed; the branch carrying the work is the epic branch rather than
  `wt-task-20255-m4`, which stays untouched as the audit trail.
- **Targets and the code index.** gcode indexes the `0.5.0` checkout; the worktree
  overlay index cannot be built today (`gcode index` in a worktree fails with
  `new row violates row-level security policy for table "code_indexed_projects"`, filed
  separately). Targets therefore follow the index the validator actually consults:
  files that exist only on `wt-task-20255-m4` are listed as bare paths with the changed
  symbols named in the body; files present on `0.5.0` use exact `qualified_name`s that
  exist on `0.5.0` or a justified `::*`. No target mixes the two forms for one file.
  The bare form for a branch-only file is forced, not preferred, and that matters
  because the file *does* exist and does carry symbols once 1.1 lands. Project-aware
  validation resolves Targets against the project root it is invoked on; a file absent
  from that root is classified as new, which is the one case the contract permits a
  bare path — and the same absence means neither an exact `qualified_name` nor a `::*`
  scope can resolve, so qualifying these Targets before the merge would fail the
  expansion-mode gate this plan must pass to hand off. 181 of the plan's unique source
  Targets are bare and 142 of those exist on the branch but not at the root, so the
  compensating obligation is load-bearing rather than incidental: every leaf whose
  Targets include a branch-only file names that file's changed symbols in its body,
  and the leaf's own post-merge sweep below is what upgrades that prose to an
  index-checked fact. A leaf that lists a branch-only file without naming what changes
  in it is incomplete.
  Because the index cannot see the branch, the planning-time consumer sweep ran against
  the branch ref itself (`git show wt-task-20255-m4:<path>` over every `src/` and
  `tests/` file) for each symbol this plan removes or re-signs: `SpawnResult(` and its
  three tmux fields, `.attach(` / `spawn_web_terminal(`, `grant_lease` /
  `takeover_lease`, `TmuxSessionManager(`, `_check_tmux_session_alive`,
  `_frame_host_epoch`, `write_handler_faulted`, and `bin_freshness_loop`; every hit is
  either in the owning leaf's Targets or named in that leaf's body as needing no change.
  Each leaf re-runs its own sweep on the epic worktree (where the index is live:
  `gcode grep -F "<needle>" src/ tests/`) before its guard run and records the hit list
  in the leaf's evidence, never in this plan.
- **Guard set G.** Every leaf's close gate runs the whole accumulated guard set from
  the epic worktree root, not just the leaf's own tests. G is:
  1. `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_runner_lifecycle_restart_replay.py tests/terminals/test_no_direct_tmux_spawn.py tests/terminals/test_no_direct_tmux_consumers.py tests/agents/test_resume_executor.py tests/config/test_runtime_config_contract.py tests/terminals/test_backend_selection.py tests/cli/test_install_setup_gterm.py tests/gterminal/test_vendor_layer.py tests/mcp_proxy/tools/sessions/test_terminal_clear.py tests/sessions/test_clear_continuation.py tests/servers/test_admin_health.py tests/install/test_version_pins.py tests/install/test_distribution.py`
  2. `GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals tests/storage/test_terminals.py tests/servers/test_terminal_ws_create.py tests/servers/test_terminal_ws_golden.py tests/servers/test_terminal_ws_lease.py tests/servers/test_terminal_ws_rename.py tests/servers/test_terminal_ws_viewport.py tests/servers/test_tmux_bridge_authority.py tests/servers/test_native_web_proxy.py tests/servers/test_attention_respond.py tests/mcp_proxy/test_sessions_terminal_tools.py`
     with `GOBBY_POSTGRES_TEST_DSN` exported (DB-backed suites).
     `test_tmux_bridge_authority.py` is in this group because 4.1 re-signs the lease
     mutations its fixtures call directly (4.1.13): an acceptance whose proof lives in
     a file the close gate never runs proves nothing.
  3. `cargo clippy -p gobby-terminal -p gobby-client --all-targets -- -D warnings`
  4. `cargo nextest run -p gobby-terminal -p gobby-client`
  5. `uv run ruff check src/ && uv run ruff format --check src/ && uv run mypy src/`
  6. `cd web && npx vitest run src/hooks src/components/activity`
  7. Test-owned host leak check: the set of `gterm host` PIDs after groups 2 and 4
     equals the set recorded before they started, and no surviving `gterm host` command
     line references a state directory created by the run (`GOBBY_TEST_STATE_ROOT`, the
     per-test tempdirs). Pre-existing hosts on the machine (the leaked review-machine
     hosts, explicitly out of scope) never fail the gate; only hosts the run started do.
  A leaf whose diff cannot affect a group may record "unaffected: <group>" with the
  reason instead of running it; any leaf touching `src/gobby/terminals`,
  `src/gobby/agents`, `src/gobby/servers`, `crates/gterminal`, or `crates/gclient` runs
  groups 1–5 and 7 in full.
  **G is checked in, not plan-local.** An expanded leaf receives only its own `### N.N`
  section, never this framing, so leaf 1.1 mirrors this bullet and the known-red
  carve-out below verbatim into `docs/guides/gterminal-development-guide.md` §
  "Guard set G" (acceptance 1.1.9), and every deliverable's Close gate names that file
  and section rather than the bare shorthand. This plan and the guide state the same
  policy; the guide is the copy leaves execute against, and 9.2 keeps it current with
  whatever the epic adds.
- **Known-red carve-out.** Group 1 run at `518cec5c41` fails exactly ten tests. One,
  `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`,
  is 1.1's own (the regeneration in 1.1.8 makes it green). The other nine are owned by
  later leaves and stay red on the merged tree until their owner closes:
  `tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation::{test_provisional_resolution_skips_reconciliation_pending_run, test_reconcile_live_tmux_run_refreshes_pid_and_reader, test_reconcile_uses_configured_tmux_socket_for_live_agent, test_reconcile_missing_tmux_session_parks_and_resumes_run, test_reconcile_dead_tmux_pane_parks_and_resumes_run}`
  and `tests/agents/test_resume_executor.py::test_codex_resume_delivers_prompt_via_composer_not_argv`
  (owner 2.1);
  `tests/terminals/test_no_direct_tmux_spawn.py::{test_spawn_paths_use_runtime, test_identity_generation_absent_from_runtimes}`
  and `tests/terminals/test_no_direct_tmux_consumers.py::test_owned_consumers_are_backend_neutral`
  (owner 2.2). "G green" for a leaf that closes while an owner leaf is still open
  means: every G test passes except the carved-out tests whose owner has not closed,
  and each of those fails for the same behavioural reason recorded at `518cec5c41`
  (an assertion or mock-call failure, never a collection or import error); the leaf's
  evidence lists the carved-out ids it observed. The carve-out for a test ends the
  moment its owner leaf closes, after which that test is a hard failure for every
  leaf. Group 1's four `GOBBY_POSTGRES_TEST_DSN` errors at `518cec5c41` are
  environment, not code: the gate runs with the DSN exported.
- **Leaf ordering is enforced by manifest edges, never by prose.** Two leaves that
  edit the same production file or the same test module carry an explicit dependency
  edge (the `(depends: …)` annotation on the later one); P2's 2.4/2.5 and 2.6–2.9 chains
  join at 2.7, P4 runs 4.1 → 4.2/4.3 → 4.4 → 4.5 → 4.6, 3.4 follows 3.3, 6.2 follows 4.5,
  6.3 follows 4.6, and 9.2 follows every leaf whose output it documents.
- **Acceptance shape.** Acceptance items describe observable behaviour and name the
  artifact that proves it. "Test X passes" is never sufficient on its own: the named
  test must exercise the behaviour against the real component (running host, real
  coordinator, real hook), and the criteria judge is expected to read the test body.
- **TDD.** Every `tdd: true` leaf records a real red run (the named test fails for the
  behavioural reason, not at collection), the minimal green run, and the final run,
  with exact commands. A collection `ImportError` is not red evidence.
- **Builders.** Leaves route through `implementation_domain` to the default codex
  `backend-developer` / `frontend-developer` / `fullstack-developer` agents. No leaf
  sets `assigned_agent` to a Grok-backed definition until #20635 lands.
- **No aliases, no compatibility.** `0.5.0` is unshipped. Replaced symbols are
  deleted, not re-exported; no shim keeps old names alive (source plan C1).
- **Lint policy.** No crate-, module-, or function-level blanket `allow` attributes in
  hand-maintained source. Item-level `#[allow(...)]` carries a `// reason:` comment.
  The one exemption is bindgen output under `crates/gterminal/src/ghostty/bindings.rs`
  and `crates/gterminal/src/ghostty/bindings/generated_*.rs`, which carry a
  `// generated: bindgen — do not edit` header line and keep their file-level allows;
  the carve guard exempts exactly the files with that header.
- **Monolith ceiling.** Every hand-maintained production source file stays under
  1,000 lines at every leaf close, measured by the leaf's close gate, not only at
  landing. A leaf that grows a file past 999 lines splits it in the same leaf
  (`crates/gterminal/src/host/state.rs` starts at 977 lines and is split in 2.6 before
  2.7, 2.9, and 8.1 add to it); the herdr import splits files before they land.
- **Schema rule (supersedes source plan C1 for this epic).** Schema changes ship as
  `baseline.sql` edits for fresh installs plus a numbered migration (the next free
  version above `0.5.0`'s registry, 403 at review time) for existing hubs;
  prior-receipt acceptance never substitutes for a missing migration.
  `baseline.sql` remains the fresh-install truth and `403_terminals.sql` mirrors its DDL
  exactly. The closed source plan `.gobby/plans/herdr-terminal-client.md` is a registered
  artifact with a managed coverage hash and is not edited by this epic; this bullet is
  the governing statement of the rule.
- **Out of scope.** Publishing `gterm-v0.1.0` / `gclient-v0.1.0` tags and the Homebrew
  formulae (operator release step after landing); killing the leaked `gterm host`
  processes on the review machine; the native default flip itself (D1); a real-`gclient`
  end-to-end run against an externally discovered tmux terminal — the source plan routes
  tmux rows through the same host frame path as native rows (no tmux-specific client
  branch exists to prove), and source 5.2's `tests/e2e/test_external_terminal_attach.py`
  already proves discovery, attach, control, detach, and expiry at the daemon boundary.
- **Named defaults.** Host respawn backoff: 1 s doubling to 30 s, reset after 60 s of
  health. Control-socket line cap: 2 MiB, typed close `control_overflow`. Delta lag
  timeout 5 s, control deadline 2 s (per `docs/contracts/gterm-protocols.md`
  "Backpressure"). Parity PTY geometry 120×40. gclient log path
  `~/.gobby/logs/gclient.log`.

## P1: Merge and schema
`kind: framing`

**Goal**: The epic worktree contains `0.5.0` ∪ `wt-task-20255-m4` with every
merge-drift breakage resolved and a migration that brings existing hubs to the
`terminals` schema.

### 1.1 Merge `wt-task-20255-m4` into the epic worktree and resolve drift [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: conflict resolution of `BASELINE_CHECKSUM`, `MIGRATIONS`, and `PRIOR_RECEIPT_CHECKSUMS` constants
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: conflict resolution of the golden schema-identity constants
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: conflict resolution of the pinned identity literals
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: pinned identity literals auto-merge to stale values
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated wholesale
- `tests/agents/test_resume_executor.py::*` — scope-reason: conflict resolution; the two vLLM tests move to the fake-runtime seam
- `tests/mcp_proxy/tools/sessions/test_terminal_clear.py::*` — scope-reason: every `_call_clear_self` test patches a symbol the branch renamed
- `tests/sessions/test_clear_continuation.py::*` — scope-reason: patches the renamed tmux seam
- `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py::*` — scope-reason: the `capture_pane` call moves to the runtime-neutral seam
- `crates/gclient/src/startup.rs`
- `docs/guides/gterminal-development-guide.md`
- `tests/servers/test_admin_health.py`
- `tests/e2e/test_external_terminal_attach.py`
- `tests/terminals/test_runtime_contract.py`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated wholesale; the catalog inventory feeds the root hash
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated wholesale
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated wholesale
- `.gobby/test-types-baseline.json`
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: re-signed wholesale
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: re-signed wholesale
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: re-signed wholesale
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: re-signed wholesale
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: re-signed wholesale
- `Cargo.lock`
- `src/gobby/agents/idle_check_handler.py::IdleCheckHandler.__init__`
- `src/gobby/agents/lifecycle_monitor.py::AgentLifecycleMonitor.__init__`
- `src/gobby/agents/lifecycle_monitor.py::AgentLifecycleMonitor.check_autonomous_stuck_agents`
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: conflict resolution; the branch's `prepare_codex_spawn` / `_runtime_spawn` codex path replaces `0.5.0`'s inline `_spawn_codex_terminal`
- `src/gobby/agents/spawn_executor_providers.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: conflict resolution; both sides added adjacent validation
- `src/gobby/tasks/validation_evidence.py::*` — scope-reason: conflict resolution; the branch's prefix-collapse helpers are kept next to `0.5.0`'s edits
- `tests/agents/test_verified_review_regressions.py::*` — scope-reason: conflict resolution; the branch's `terminal_backend="tmux"` request fixture is kept
- `tests/mcp_proxy/tools/test_spawn_agent_speed.py::*` — scope-reason: conflict resolution; the branch's `AsyncMock` `execute_spawn` patch and `terminal_backend` fixture are kept
- `tests/tasks/test_validation_evidence.py::*` — scope-reason: conflict resolution; both sides' tests are kept

Closes every `F-*` merge-drift finding of the review record (the conflict set, the
`/api/health` move, the renamed tmux seams, and the stale identity literals below).

In the epic worktree (branched from `0.5.0`) run `git merge wt-task-20255-m4`. Twelve
paths conflict against the `0.5.0` tip this plan was reviewed on (read-only probe
`git merge-tree --write-tree 0.5.0 wt-task-20255-m4` → tree `62a906fbfc`; re-run the
probe first and treat any further path it reports as a conflict to resolve by the same
rule: keep both sides' additions, keep the branch's terminal-runtime shape, and port
`0.5.0`'s renames onto it); resolve them as follows, then make the semantic-drift edits,
regenerate the derived files, and commit everything as one merge commit
`[gobby-#<leaf>] chore: merge wt-task-20255-m4 into the QA-fix epic worktree`.

Conflict resolutions:

1. `crates/gcore/src/schema/assets.rs` — keep `0.5.0`'s `MIGRATIONS` entries 399–402
   (`399_drain_orphan_binding_alias.sql`, `400_drop_vision_extract_config_rows.sql`,
   `401_model_metadata_reasoning.sql`, `402_task_close_reviews.sql`; the branch's
   inventory stops at 398) and its `PRIOR_RECEIPT_CHECKSUMS` hunk (including
   `(375, "8467fc42…")`). Set `BASELINE_CHECKSUM` to
   `shasum -a 256 crates/gcore/assets/schema/baseline.sql` of the merged file (the
   branch added `terminals` + `agent_runs.terminal_id`; `0.5.0` deleted the
   `indexer_version` line; `baseline.sql` itself auto-merges). Recompute it; no literal
   recorded at planning time is trusted because `0.5.0` keeps moving.
2. `crates/gcore/src/grant/bundle.rs` — `GOLDEN_BASELINE_CHECKSUM` = the value from 1;
   keep `0.5.0`'s latest-asset checksum `10593cb9…` / `latest_version: 402`;
   `GOLDEN_ASSETS_ROOT_HASH` = regenerated root hash (below).
3. `crates/gcore/tests/schema_contract.rs` — same three values; keep `0.5.0`'s
   `latest_asset` block (`402_task_close_reviews.sql`).
4. `src/gobby/storage/schema_expected_identity.json` — take either side, then
   regenerate (never hand-edit).
5. `tests/agents/test_resume_executor.py` — keep the branch's imports and helpers
   (`bind_spawn_runtime`, `_runner` wiring `terminal_manager` /
   `terminal_runtime_registry` / `_test_runtime`; `_patch_common` no longer patches
   `_tmux_spawner`). Keep `0.5.0`'s `CODEX_ENDPOINT_API_KEY_ENV` import and both vLLM
   tests (`test_resume_vllm_endpoint_uses_config_override`,
   `test_resume_vllm_endpoint_reports_unresolved_secret`), rewritten to the fake runtime
   seam: drop `spawner.spawn.return_value`; read the environment from
   `runner._test_runtime.last_request.env`; assert
   `runner._test_runtime.create_calls == 0` where the old test asserted the spawner was
   not called. `src/gobby/agents/resume_executor.py` auto-merges correctly (the
   config-override branch returns before `_runtime_spawn`).
6. `src/gobby/agents/idle_check_handler.py` (`IdleCheckHandler.__init__`) — keep both
   keyword parameters: `0.5.0` #20713's `is_parked` and the branch's
   `terminal_services`.
7. `src/gobby/agents/lifecycle_monitor.py` — `AgentLifecycleMonitor.__init__` passes
   both `is_parked=(completion_registry.is_awaiting if … else None)` and
   `terminal_services=self._terminal_services` to the handler;
   `check_autonomous_stuck_agents` keeps `0.5.0` #20710's
   `self._draft_grace_observations.pop(run.id, None)` line and the branch's
   `elif run.terminal_id and self._terminal_services is not None:` write branch in place
   of the deleted `elif run.tmux_session_name:` / `self._tmux.send_keys(...)` branch.
8. `src/gobby/agents/spawn_executor.py` — first hunk: keep only the branch's
   `_spawn_in_doubt_seconds`; `0.5.0` #20697's `_agent_prompt_prefix` and
   `_append_code_index_warning` are dropped here because the branch already hosts both
   in `src/gobby/agents/spawn_executor_providers.py`. Second hunk: take the branch's
   `_spawn_codex_terminal` (`prepare_codex_spawn` → `_runtime_spawn` →
   `plan.inject_persona` → `schedule_codex_prompt_delivery` through the coordinator)
   verbatim; `0.5.0`'s inline composer path goes away. Then port #20697 onto the
   branch: in `spawn_executor_providers.py` rename `_persona_prompt_prefix` to
   `_agent_prompt_prefix` and return `agent_body.prompt_for("agent") or ""`
   (`AgentBody.build_prompt_preamble` no longer exists on `0.5.0`, so the merged tree
   fails mypy and every codex spawn test until this port lands); `prepare_codex_spawn`
   prepends that value exactly as it prepended the persona. The
   `tests/agents/test_spawn_executor.py` cases #20697 changed run unchanged against
   the ported helper.
9. `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` — keep both: `0.5.0`'s
   `agent_body.prompt_for("agent")` validation (returns the `ValueError` as the tool
   error) followed by the branch's `resolve_terminal_backend(...)` call and its
   `ValueError` mapping.
10. `src/gobby/tasks/validation_evidence.py` — keep the branch's `_build_file_manifest`,
    `_largest_remaining_prefix`, `_collapse_overflow_prefixes`, and
    `_format_collapsed_manifest` (the `0.5.0` side of the hunk is empty; its
    `_format_file_manifest` edits auto-merge) and keep `build_close_diff_evidence`
    calling `_build_file_manifest`.
11. `tests/agents/test_verified_review_regressions.py` and
    `tests/mcp_proxy/tools/test_spawn_agent_speed.py` — take the branch side of every
    hunk (the `terminal_backend="tmux"` request fixture, the `AsyncMock` patch of
    `execute_spawn`, and the branch's patch ordering); `0.5.0`'s side is whitespace
    and ordering only.
12. `tests/tasks/test_validation_evidence.py` — keep both sides' tests
    (`test_oversized_shared_prefix_collapses_to_directory_summary`,
    `test_manifest_still_fails_when_exact_paths_cannot_fit`, and `0.5.0`'s
    `test_explicit_evidence_bound_fails_without_truncating`).

Semantic-drift edits in the same commit:

- `0.5.0` #20641 moved `/api/admin/health` to `/api/health` and removed the auth
  exemption. Change `crates/gclient/src/startup.rs` `HEALTH_PATH` to `"/api/health"`,
  and the three test probes: `tests/servers/test_admin_health.py` (the `gterm_host`
  payload test), `tests/e2e/test_external_terminal_attach.py` `_health()`, and
  `tests/terminals/test_runtime_contract.py` `_health()`.
- `0.5.0`'s `tests/mcp_proxy/tools/sessions/test_terminal_clear.py` patches
  `gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context` and
  `tests/sessions/test_clear_continuation.py` patches
  `gobby.sessions.compact_continuation.get_tmux_manager_for_context`; the branch
  replaced both with `gobby.terminals.lookup.manager_for_terminal_context` imported
  into each module. Patch `…_terminal.manager_for_terminal_context` /
  `…compact_continuation.manager_for_terminal_context`. The `test_terminal_clear.py`
  fixtures set `tmux_manager.send_keys` / `.capture_pane` AsyncMocks while the branch's
  `_send_tmux_keys` awaits `tmux.dispatch_keys(...)` and reads via `snapshot_lines`;
  mirror the fakes in the branch's
  `tests/mcp_proxy/tools/sessions/test_compact_self.py` and update the wrong-method
  asserts. In `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py` replace
  `tmux.capture_pane(target, lines=1)` with the runtime-neutral `snapshot_lines` call
  the branch uses elsewhere.
- `crates/gdaemon/tests/cli_contract.rs` auto-merges to `0.5.0`'s identity literals;
  set them to the merged checksum / root hash / latest-asset values.

Regeneration after the conflicts are resolved:

```bash
shasum -a 256 crates/gcore/assets/schema/baseline.sql                       # X
UPDATE_GCORE_SCHEMA_MANIFEST=1 GOBBY_SCHEMA_TEST_DATABASE_URL="$GOBBY_POSTGRES_TEST_DSN" \
  cargo test -p gobby-core --features postgres --test catalog_manifest_freshness \
  catalog_manifest_is_fresh_for_embedded_assets -- --exact                  # catalog.manifest.json
cargo build --release -p gobby-daemon -p gobby-code
uv run python scripts/generate_schema_expected_identity.py --gdaemon target/release/gdaemon   # R
uv run python scripts/generate_runtime_config_contract.py
uv run python -c "from pathlib import Path; from gobby.install.manifest import write_bundled_content_manifest; write_bundled_content_manifest(Path('src/gobby/install'))"
uv sync && cargo check --workspace
uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new   # --write-baseline when only new files are reported
```

Paste R into `bundle.rs`, `schema_contract.rs`, and `cli_contract.rs`; re-sign every
file in `tests/runtime_grants/golden/` (`schema_identity.*`, `payload_checksum`,
`signature`) with `GOLDEN_SECRET` from `tests/runtime_grants/support.py` per the header
of `tests/runtime_grants/test_golden_vectors.py`. Take the branch's `Cargo.lock`
and `0.5.0`'s `uv.lock` / `pyproject.toml` (one side each, no conflict).

Note that leaf 1.1 of the source epic (`ab76390fc2`) also changed
`src/gobby/tasks/validation_evidence.py` (prefix-collapse for oversized close-review
manifests, covered by `tests/tasks/test_validation_evidence.py`); that change is kept and
called out in the landing PR.

Close gate: guard set G green — this leaf writes the definition every later leaf reads
(`docs/guides/gterminal-development-guide.md` § "Guard set G", acceptance 1.1.9) — under
the known-red carve-out (the nine tests owned by
2.1 and 2.2 are expected red here; `test_checked_in_contract_matches_registry` must be
green), plus
`cargo test -p gobby-core --features postgres --test schema_contract --test catalog_manifest_freshness`,
`cargo test -p gobby-daemon --test cli_contract --test schema_cli`,
`cargo test -p gobby-hooks --test contract`, and
`GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_golden_vectors.py tests/config/ tests/install/test_bundled_content_manifest.py tests/sessions/test_compact_continuation.py tests/servers/routes/test_admin.py tests/servers/test_auth_middleware.py tests/workflows/test_step_snapshot_semantics.py tests/test_cli_contracts.py`.

**Acceptance:**

- 1.1.1 - The epic worktree's HEAD is a merge commit whose parents are the `0.5.0` tip and `518cec5c41`, and `git diff HEAD -- Cargo.lock uv.lock` is empty after `uv sync && cargo check --workspace`. behavior: "merge commit with both parents" in `.gobby/plans/herdr-terminal-client-qa-fixes.md`.
- 1.1.2 - `BASELINE_CHECKSUM`, `GOLDEN_BASELINE_CHECKSUM`, the `schema_contract.rs` literal, and the `cli_contract.rs` literal all equal `sha256(baseline.sql)` of the merged file, and the embedded root hash equals the regenerated value. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
- 1.1.3 - `gdaemon version --json` reports the same schema identity as `src/gobby/storage/schema_expected_identity.json`. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.
- 1.1.4 - Every golden grant vector verifies against the regenerated identity. file: `tests/runtime_grants/golden/direct_datastores.json`.
- 1.1.5 - The gclient startup probe and every test probe use `/api/health`; no source or test under `crates/gclient`, `tests/servers`, `tests/e2e`, or `tests/terminals` references `/api/admin/health`. file: `crates/gclient/src/startup.rs`.
- 1.1.6 - `test_terminal_clear.py` and `test_clear_continuation.py` patch `manager_for_terminal_context` and drive `dispatch_keys` / `snapshot_lines` fakes; all of their tests pass on the merged tree. file: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.
- 1.1.7 - Both vLLM resume tests assert through `runner._test_runtime` and pass with no `spawner.spawn` seam. test: `tests/agents/test_resume_executor.py::test_resume_vllm_endpoint_uses_config_override`.
- 1.1.8 - `runtime_config_contract.json` and `bundled_content_manifest.json` are regenerated on the merged tree and their freshness tests pass. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 1.1.9 - `docs/guides/gterminal-development-guide.md` carries a "Guard set G" section holding the seven guard groups verbatim (exact commands), the unaffected-group rule, the known-red carve-out with each carved test's owning leaf and its end condition, and the test-owned host-leak rule, so a leaf that sees only its own section can run and interpret its close gate from the checked-in file. file: `docs/guides/gterminal-development-guide.md`.

### 1.2 Add `403_terminals.sql` and amend the schema identity carriers [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/403_terminals.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: register the 403 entry and the two branch-lineage prior receipts
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated wholesale; registering 403 changes the embedded catalog inventory that the freshness and root-hash gates compare
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: migration-count and lineage fixtures move to 403; backfill cases added
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: latest-asset constants move to 403
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: latest-asset block moves to 403
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: identity literals change with the root hash
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated wholesale
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: re-signed wholesale
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: re-signed wholesale
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: re-signed wholesale
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: re-signed wholesale
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: re-signed wholesale

Closes the `F-*` schema-lineage finding (existing hubs never receive the `terminals`
DDL).

Existing hubs (including the developer hub this machine's daemon uses) carry a
`baseline@375` receipt of `8467fc42…` (pre-branch) or `d1981000…` (`0.5.0`) or
`30eb60a3…` (branch HEAD). `PRIOR_RECEIPT_CHECKSUMS` makes all three resolve to
`BaselineState::AlreadyBaselined`, so the baseline is never re-applied and such a hub
fails at runtime with `relation "terminals" does not exist`. Fix by shipping the DDL as a
numbered migration that runs for any hub whose receipts stop at 402 (`0.5.0`'s
registry ends at `402_task_close_reviews.sql`; 403 is the next free version — confirm
against the merged `MIGRATIONS` before creating the file).

Create `crates/gcore/assets/schema/migrations/403_terminals.sql` mirroring the merged
`baseline.sql` exactly (column order, defaults, every CHECK constraint, the six indexes,
the five foreign keys, the GRANT). It is transactional and preserves live identity: it
carries no `-- gobby:destructive` directive, so `apply_pending_migrations` executes it
on `AlreadyBaselined` lineages and a fresh baseline receipt-stamps nothing (the table is
already in the baseline; guard every statement with `IF NOT EXISTS` / `DO $$ … $$`
existence checks so a fresh install that somehow replays it is a no-op). Between
`ADD COLUMN terminal_id` and `DROP COLUMN tmux_session_name` the migration backfills a
tmux terminal row for every agent run that still carries a legacy identity, and refuses
to drop the column while any unmapped identity remains:

```sql
-- 403: existing hubs were baselined before the terminals table existed. Mirrors
-- baseline.sql exactly; every statement is idempotent.
CREATE TABLE IF NOT EXISTS terminals (
    id uuid NOT NULL,
    backend text NOT NULL,
    ownership text NOT NULL,
    state text NOT NULL,
    spawn_key text,
    machine_id uuid NOT NULL,
    locator jsonb,
    locator_key text,
    session_name text,
    window_id text,
    title text,
    host_epoch text,
    unresolved_writes jsonb DEFAULT '{}'::jsonb NOT NULL,
    automatic_write_quarantined_at timestamp with time zone,
    automatic_write_quarantine_action_key text,
    attempt_generation integer DEFAULT 1 NOT NULL,
    attempt_started_at timestamp with time zone DEFAULT now() NOT NULL,
    process jsonb,
    rows integer,
    cols integer,
    project_id uuid NOT NULL,
    session_id uuid,
    agent_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    liveness_at timestamp with time zone,
    CONSTRAINT terminals_pkey PRIMARY KEY (id),
    CONSTRAINT terminals_backend_check CHECK ((backend = ANY (ARRAY['tmux'::text, 'native'::text]))),
    CONSTRAINT terminals_ownership_check CHECK ((ownership = ANY (ARRAY['gobby'::text, 'external'::text]))),
    CONSTRAINT terminals_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'live'::text, 'exited'::text, 'orphaned'::text]))),
    CONSTRAINT terminals_title_byte_limit CHECK ((title IS NULL OR octet_length(title) <= 1024)),
    CONSTRAINT terminals_locator_present_when_attachable CHECK (
        (state = ANY (ARRAY['pending'::text, 'exited'::text]))
        OR ((locator IS NOT NULL) AND (locator_key IS NOT NULL))),
    CONSTRAINT terminals_locator_pair_consistent CHECK ((locator IS NULL) = (locator_key IS NULL)),
    CONSTRAINT terminals_spawn_key_matches_ownership CHECK (
        ((ownership = 'gobby'::text) AND (spawn_key IS NOT NULL))
        OR ((ownership = 'external'::text) AND (spawn_key IS NULL))),
    CONSTRAINT terminals_external_is_never_pending CHECK ((ownership = 'gobby'::text) OR (state <> 'pending'::text)),
    CONSTRAINT terminals_host_epoch_is_native_only CHECK ((host_epoch IS NULL) OR (backend = 'native'::text)),
    CONSTRAINT terminals_native_attachable_has_epoch CHECK (
        (backend <> 'native'::text) OR (state <> ALL (ARRAY['live'::text, 'orphaned'::text])) OR (host_epoch IS NOT NULL)),
    CONSTRAINT terminals_process_is_native_only CHECK ((process IS NULL) OR (backend = 'native'::text)),
    CONSTRAINT terminals_pending_has_no_identity CHECK (
        (state <> 'pending'::text) OR ((locator IS NULL) AND (locator_key IS NULL) AND (host_epoch IS NULL))),
    CONSTRAINT terminals_external_always_has_locator CHECK (
        (ownership = 'gobby'::text) OR ((locator IS NOT NULL) AND (locator_key IS NOT NULL))),
    CONSTRAINT terminals_quarantine_pair_consistent CHECK (
        (automatic_write_quarantined_at IS NULL) = (automatic_write_quarantine_action_key IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_terminals_run ON terminals USING btree (agent_run_id) WHERE (agent_run_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_terminals_machine ON terminals USING btree (machine_id);
CREATE INDEX IF NOT EXISTS idx_terminals_project ON terminals USING btree (project_id);
CREATE INDEX IF NOT EXISTS idx_terminals_live ON terminals USING btree (state) WHERE (state = ANY (ARRAY['pending'::text, 'live'::text]));
CREATE UNIQUE INDEX IF NOT EXISTS idx_terminals_locator_key_active ON terminals USING btree (locator_key)
    WHERE ((locator_key IS NOT NULL) AND (state = ANY (ARRAY['pending'::text, 'live'::text])));
CREATE UNIQUE INDEX IF NOT EXISTS idx_terminals_spawn_key ON terminals USING btree (spawn_key) WHERE (spawn_key IS NOT NULL);
-- foreign keys, guarded by pg_constraint lookups inside DO blocks:
--   terminals_machine_id_fkey   (machine_id)  REFERENCES machines(id)
--   terminals_project_id_fkey   (project_id)  REFERENCES projects(id)
--   terminals_session_id_fkey   (session_id)  REFERENCES sessions(id) ON DELETE SET NULL DEFERRABLE
--   terminals_agent_run_id_fkey (agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL DEFERRABLE
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS terminal_id uuid;
--   agent_runs_terminal_id_fkey (terminal_id) REFERENCES terminals(id)   (guarded DO block)
-- Backfill: one tmux terminal row per legacy identity still attached to a run that is
-- not in a terminal state. Runs already carrying terminal_id are left alone. Exited
-- runs keep no identity (the column goes away; their history lives in sessions).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'agent_runs' AND column_name = 'tmux_session_name') THEN
    -- agent_runs has no project_id / session_id columns: the terminal row binds to the
    -- run's child session (the same binding spawn_executor writes for new rows) and
    -- takes project_id from that session, falling back to the parent session (NOT NULL)
    -- when the run never minted a child. A run whose session row resolves no project
    -- is left out of `legacy` and therefore trips the unmapped-identity check below.
    WITH legacy AS (
      SELECT r.id AS run_id, r.tmux_session_name, s.project_id, r.child_session_id AS session_id,
             r.machine_id, r.created_at
      FROM agent_runs r
      JOIN sessions s ON s.id = COALESCE(r.child_session_id, r.parent_session_id)
      WHERE r.tmux_session_name IS NOT NULL AND r.terminal_id IS NULL
        AND r.status IN ('pending', 'running')
    ), inserted AS (
      INSERT INTO terminals (id, backend, ownership, state, spawn_key, machine_id,
                             locator, locator_key, session_name, project_id, session_id,
                             agent_run_id, created_at, updated_at)
      SELECT gen_random_uuid(), 'tmux', 'gobby', 'orphaned', l.tmux_session_name,
             l.machine_id,
             jsonb_build_object('backend', 'tmux', 'session_name', l.tmux_session_name),
             'tmux:' || l.tmux_session_name, l.tmux_session_name, l.project_id,
             l.session_id, l.run_id, l.created_at, now()
      FROM legacy l
      ON CONFLICT (spawn_key) WHERE spawn_key IS NOT NULL DO NOTHING
      RETURNING id, agent_run_id
    )
    UPDATE agent_runs r SET terminal_id = i.id FROM inserted i WHERE r.id = i.agent_run_id;
    -- A duplicate spawn_key (two live runs claiming one session) maps the later run
    -- onto the row the earlier run created rather than leaving it unmapped.
    UPDATE agent_runs r SET terminal_id = t.id
    FROM terminals t
    WHERE r.terminal_id IS NULL AND r.tmux_session_name IS NOT NULL
      AND t.spawn_key = r.tmux_session_name
      AND r.status IN ('pending', 'running');
    IF EXISTS (SELECT 1 FROM agent_runs
               WHERE tmux_session_name IS NOT NULL AND terminal_id IS NULL
                 AND status IN ('pending', 'running')) THEN
      RAISE EXCEPTION 'migration 403: unmapped legacy tmux identities remain';
    END IF;
  END IF;
END $$;
ALTER TABLE agent_runs DROP COLUMN IF EXISTS tmux_session_name;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE terminals TO gobby_daemon_runtime;
```

The backfill classifies runs with the positive active predicate
`status IN ('pending', 'running')` — the `agent_runs` status vocabulary is
`pending | running | success | error | timeout | cancelled` (the baseline's own
`idx_agent_runs_pending_termination` predicate names the active pair), so a terminal
run of any kind is never backfilled as an active orphaned terminal and an unknown
status fails closed as "not active". Copy the exact index predicate text
(`idx_terminals_live` state list, the `idx_terminals_locator_key_active` state list)
from the merged `baseline.sql` rather than from this excerpt. Source lineage is fixed
by the baseline's `agent_runs` definition: `machine_id` (NOT NULL) comes from the run,
`session_id` is the run's `child_session_id` (nullable — `terminals.session_id` is
nullable and spawn_executor binds new rows to the child session the same way), and
`project_id` comes from the `sessions` row joined on
`COALESCE(child_session_id, parent_session_id)` (`parent_session_id` is NOT NULL, so
an active run whose session row is missing is the only way the join drops a run, and
that run then fails the unmapped-identity check and rolls the migration back).
Backfilled rows are `orphaned` (locator present, no
live attachment) so the restart reconcile of 2.1 adopts or exits them on the next daemon
boot exactly as it treats any orphaned tmux row; the daemon never reads the dropped
column. The whole migration is one transaction, so a raised exception leaves the
column, the table, and every receipt untouched.

Register the file in `MIGRATIONS` with `version: 403`, `filename: "403_terminals.sql"`,
its sha256, and `include_str!`, after the `402_task_close_reviews.sql` entry 1.1 kept.
Regenerate `catalog.manifest.json` (the
`UPDATE_GCORE_SCHEMA_MANIFEST=1` command from 1.1) before the root hash, identity JSON,
and grant signatures, in that order — the catalog inventory is an input to the root
hash. Add `(375, "d19810005e6c931219781941ab1c63ecc057973dfe60e2d4a8b6a69f460c6dd0")`
and `(375, "<sha256 of baseline.sql at 518cec5c41>")` to `PRIOR_RECEIPT_CHECKSUMS`
(keep `8467fc42…`). Update `runner_tests.rs` wherever `MIGRATIONS.len()` or the latest
version is pinned, and add tests that install a scratch database at the `0.5.0`
lineage (baseline receipt `d1981000…`, receipts through 402 — the fixture seeds the
399–402 receipts with the checksums registered in the merged `MIGRATIONS` — and no
`terminals` table):

- seed no legacy runs, run `SchemaRunner::apply`, assert `terminals` exists with
  `agent_runs.terminal_id` present and `agent_runs.tmux_session_name` absent; apply
  again and assert a receipt no-op;
- seed one `running` run with `tmux_session_name = 'gobby-1'`, one `pending` run with
  `'gobby-p'`, one named run in each terminal status (`success`, `error`, `timeout`,
  `cancelled`), one `running` run with `NULL`, and two `running` runs sharing
  `'gobby-dup'`; after `apply` the active named runs each carry a `terminal_id`
  pointing at an `orphaned` tmux row with `spawn_key` = the old name, the two
  duplicates share one row, the four terminal-status and the `NULL` runs have
  `terminal_id IS NULL`, and exactly three terminal rows exist; the `running` run with
  `'gobby-1'` has a child session and its row carries that `session_id` and the
  child session's `project_id`; the `pending` run with `'gobby-p'` has
  `child_session_id IS NULL` and its row carries `session_id IS NULL` and the parent
  session's `project_id`; every row's `machine_id` equals the run's;
- seed a live named run whose `parent_session_id` references no `sessions` row (insert
  with the FK disabled in the fixture) and assert the migration raises, the
  transaction rolls back, `tmux_session_name` still exists, and no receipt for 403
  was written.

Move `latest_asset` to 403 in `bundle.rs` and `schema_contract.rs`, regenerate the root
hash and `schema_expected_identity.json` with the 1.1 commands, re-sign the golden
vectors, and update `cli_contract.rs`.

The schema rule that supersedes source plan C1 is stated in this plan's
`## Constraints` ("Schema rule"); the closed source plan is not edited.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) under that carve-out (2.1 and 2.2 have not closed yet) plus `cargo test -p gobby-core --features postgres --lib schema::runner_tests --test schema_contract --test catalog_manifest_freshness`, `cargo test -p gobby-daemon --test cli_contract --test schema_cli`, `GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_golden_vectors.py tests/storage/test_terminals.py`.

**Acceptance:**

- 1.2.1 - A hub baselined at `d1981000…` with receipts through 402 gains the `terminals` table, its six indexes, five foreign keys, GRANT, `agent_runs.terminal_id`, and loses `agent_runs.tmux_session_name` after one `apply`; a second `apply` applies zero migrations. test: `crates/gcore/src/schema/runner_tests.rs::terminals_migration_upgrades_pre_terminals_lineage`.
- 1.2.2 - A fresh baseline install stamps receipt 403 without executing it and has an identical `terminals` definition (columns, constraints, indexes compared through `pg_catalog`). test: `crates/gcore/src/schema/runner_tests.rs::terminals_migration_matches_baseline_definition`.
- 1.2.3 - Receipts `8467fc42…`, `d1981000…`, and the branch-HEAD checksum all resolve to `AlreadyBaselined`. symbol: `prior_baseline_receipt`.
- 1.2.4 - `latest_asset` is `403_terminals.sql` in the grant bundle, the schema contract test, the daemon CLI contract, and `schema_expected_identity.json`; `catalog.manifest.json` is fresh for the 403 inventory; and every golden grant vector re-verifies. test: `tests/runtime_grants/test_golden_vectors.py`.
- 1.2.5 - A hub with live legacy runs keeps their identity: every `pending` or `running` run that had a `tmux_session_name` carries a `terminal_id` pointing at an `orphaned` tmux row whose `spawn_key` is the old name, whose `machine_id` is the run's, whose `session_id` is the run's `child_session_id` (NULL when the run has none), and whose `project_id` is that of the child session or, without one, the parent session; duplicates share one row; runs in `success`, `error`, `timeout`, or `cancelled` and unnamed runs get no row. test: `crates/gcore/src/schema/runner_tests.rs::terminals_migration_backfills_live_legacy_identities`.
- 1.2.6 - When a legacy identity cannot be mapped (an active named run whose session row resolves no project) the migration raises, the transaction rolls back, `tmux_session_name` survives, and no 403 receipt is written. test: `crates/gcore/src/schema/runner_tests.rs::terminals_migration_refuses_to_drop_unmapped_identity`.
- 1.2.7 - `catalog_manifest_freshness` passes on the tree with 403 registered. test: `crates/gcore/tests/catalog_manifest_freshness.rs::catalog_manifest_is_fresh_for_embedded_assets`.

## P2: Red tests and spawn/host safety
`kind: framing`

**Goal**: Every test on the merged tree is green, every spawn failure is a typed refusal
with no leaked row, and the host cannot be wedged, spun, or killed by a control client.

### 2.1 Restore restart reconciliation and the resume composer path [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/runner_lifecycle_agents.py::*` — scope-reason: the restart reconcile helpers move out and the startup hooks import them from the new module
- `src/gobby/runner_lifecycle_restart.py`
- `src/gobby/agents/resume_executor.py::*` — scope-reason: the composer delivery path lost its call under the terminal-runtime migration
- `tests/test_runner_lifecycle_restart_replay.py::*` — scope-reason: five reconcile tests are red at HEAD and gain a native case
- `tests/agents/test_resume_executor.py::*` — scope-reason: `test_codex_resume_delivers_prompt_via_composer_not_argv` is red at HEAD

Closes E-3 and the resume composer red test.

`src/gobby/runner_lifecycle_agents.py` is 942 lines, so split it before the rewrite:
move `_reconcile_agent_runs_after_restart`, `_find_live_tmux_by_planned_name`,
`_resolve_provisional_daemon_resume_row`, `_resolve_provisional_daemon_resumes`,
`_refresh_active_run_dispatch_mutex`, and `_cleanup_missing_tmux_agent_run` into the new
`src/gobby/runner_lifecycle_restart.py`; `runner_lifecycle_agents.py` keeps the startup
hooks and imports those helpers. The rewrites below land in the new module, and
`tests/test_runner_lifecycle_restart_replay.py` patches the new module path.

`_reconcile_agent_runs_after_restart` (rewritten in `518cec5c41`) has three defects:
(a) `if not live_sessions: return reconciled` skips parking/resuming every run whose tmux
session is missing when the tmux server is dead or empty; (b) `_run_backend()` returns
`None` when `runner.terminal_manager` is absent or the terminal row is missing, so such
runs are neither tmux-handled nor native-handled; (c) native runs get no
`_refresh_active_run_dispatch_mutex`, no `notify_parent_of_recovery`, and are never
added to `resolved_run_ids`. Fix: delete the empty-list early return (an empty live set
means every tmux run is missing and must be parked/resumed); treat a `None` backend as
the legacy tmux path; add a native branch that verifies liveness through
`runner.terminal_runtime_registry.resolve("native").is_live(row)`, refreshes the mutex,
notifies recovery, and records the run id. Look up tmux sessions through
`runner.terminal_runtime_registry.resolve("tmux")` rather than constructing
`TmuxSessionManager()` (this is also the first half of 2.2's guard failure).

`test_codex_resume_delivers_prompt_via_composer_not_argv` fails with the composer mock
"Called 0 times": trace `resume_agent_run` on the merged tree, find where the codex
composer delivery (`mock_codex_prompt_delivery`) stopped being invoked after the
`_runtime_spawn` migration, and restore the delivery on the codex branch before the
runtime request is issued. Root-cause, do not patch the test.

Add native reconcile cases mirroring the four tmux cases (live refresh, missing parks and
resumes, dead pane parks and resumes, configured socket) using the fake runtime from
`tests/terminals/fakes.py`.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out).

**Acceptance:**

- 2.1.1 - With an empty or dead tmux server every tmux-backed run with a missing session is parked and resumed (not skipped). test: `tests/test_runner_lifecycle_restart_replay.py::test_reconcile_missing_tmux_session_parks_and_resumes_run`.
- 2.1.2 - A run whose terminal row is missing is reconciled through the legacy tmux path instead of being ignored. test: `tests/test_runner_lifecycle_restart_replay.py::test_reconcile_run_without_terminal_row_uses_tmux_path`.
- 2.1.3 - A live native run has its dispatch mutex refreshed and recovery notified; a dead one is parked and resumed. test: `tests/test_runner_lifecycle_restart_replay.py::test_reconcile_native_run_refreshes_mutex_and_parks_dead`.
- 2.1.4 - The codex resume path delivers the prompt through the composer exactly once and never in argv. test: `tests/agents/test_resume_executor.py::test_codex_resume_delivers_prompt_via_composer_not_argv`.
- 2.1.5 - All five reconcile tests that were red at `518cec5c41` pass. file: `tests/test_runner_lifecycle_restart_replay.py`.

### 2.2 Restore the no-direct-tmux guards and move identity minting out of `gobby.terminals` [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/terminals/leases.py`
- `src/gobby/terminals/web_spawn.py`
- `src/gobby/servers/websocket/terminal_ws.py`
- `src/gobby/servers/routes/terminals.py`
- `src/gobby/runner_lifecycle_agents.py::*` — scope-reason: remaining `TmuxSessionManager(` construction sites
- `src/gobby/runner_lifecycle_restart.py`
- `tests/terminals/test_no_direct_tmux_spawn.py`
- `tests/terminals/test_no_direct_tmux_consumers.py`
- `tests/terminals/test_lease_authority.py`
- `tests/terminals/test_backend_selection.py`
- `tests/servers/test_terminal_ws_create.py`
- `tests/servers/test_terminal_ws_lease.py`
- `tests/servers/test_terminal_ws_viewport.py`
- `tests/servers/test_tmux_bridge_authority.py`

Closes B-1 (the C-8 allowlist narrowing and reader migration are both 2.3).

The 2.1 split of `src/gobby/runner_lifecycle_agents.py` puts the restart reconcile
helpers in `src/gobby/runner_lifecycle_restart.py`, so the `TmuxSessionManager(` sweep below
covers both files; neither may construct the manager directly after this leaf, and
`src/gobby/runner_lifecycle_restart.py` is added to the guard's `_SPAWN_PATHS` scan set
in `tests/terminals/test_no_direct_tmux_spawn.py`. The guard scans that fixed set
(spawn executors, resume executor, the spawn-agent tool modules, dispatch spawn actions,
the spawn route, dry run, the daemon-resume keys, and the two lifecycle modules); the
other constructors the branch-wide sweep finds (`agents/lifecycle_monitor.py`,
`mcp_proxy/tools/agents_termination.py`, `mcp_proxy/tools/sessions/_terminal_tmux.py`,
`runner_lifecycle_processes.py`, `sessions/tmux_context.py`, the runtime registration
in `runner_init/orchestration.py`, and `servers/websocket/tmux.py`, which 4.5 deletes)
are outside the QA findings and this leaf leaves them alone.

`TerminalLeaseRegistry.attach` and `spawn_web_terminal` gain required id parameters,
so every direct caller moves in this leaf: the production callers above and the test
callers in guard set G that construct registries or call `attach(...)` /
`spawn_web_terminal(...)` today without ids (`test_lease_authority.py`,
`test_terminal_ws_create.py`, `test_terminal_ws_lease.py`, `test_terminal_ws_viewport.py`,
`test_tmux_bridge_authority.py`, `test_backend_selection.py`). Before the guard run,
sweep with `gcode grep -F ".attach(" tests/ src/` and `gcode grep -F "spawn_web_terminal(" tests/ src/`
on the epic worktree and migrate every hit; a caller left passing no id is a red test,
not a follow-up.

`tests/terminals/test_no_direct_tmux_spawn.py::test_spawn_paths_use_runtime` finds
`TmuxSessionManager(` in `src/gobby/runner_lifecycle_agents.py` (introduced by the 2.4
leaf after the 2.3 guard); `test_identity_generation_absent_from_runtimes` finds
`uuid4()` in `src/gobby/terminals/leases.py` (`TerminalLeaseRegistry.attach`) and
`src/gobby/terminals/web_spawn.py` (`spawn_web_terminal`, four sites);
`tests/terminals/test_no_direct_tmux_consumers.py::test_repo_wide_field_sweep_is_empty`
passes at HEAD only because `_FIELD_SWEEP_ALLOWED` was widened to ten entries.

- Every `TmuxSessionManager(` construction outside the tmux runtime module and the
  `gobby.agents.tmux` factory goes away; `runner_lifecycle_agents.py` resolves through
  the registry (2.1).
- Identity minting leaves `src/gobby/terminals/`: `TerminalLeaseRegistry.attach` takes
  `attachment_id: str` from the caller (`terminal_ws.py` mints it with `uuid4()` at the
  handler) and `spawn_web_terminal` takes `terminal_id` and `spawn_key` from its caller
  (`servers/routes/terminals.py` / `terminal_ws.py` allocate them). The guard's scan set
  stays `src/gobby/terminals/`.
- `_FIELD_SWEEP_ALLOWED` is left at the branch's widened set in this leaf. The nine
  files the branch added to it (`spawn_models.py`, `spawn_executor.py`,
  `resume_executor.py`, `spawners/base.py`, `spawn_agent/_failure_cleanup.py`,
  `_runtime.py`, `_execution.py`, `_health.py`, `_implementation.py`) are migrated in
  2.3, which narrows the set to the source plan's entries in the same commit (2.3.5), so
  G stays green at both closes.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out); the guard tests 2.2.1 and 2.2.2 name pass without skips.

**Acceptance:**

- 2.2.1 - No module in the guard's `_SPAWN_PATHS` scan set — which gains `src/gobby/runner_lifecycle_restart.py` in this leaf — constructs `TmuxSessionManager` or `TmuxSpawner`; the two lifecycle modules resolve tmux through the runtime registry. test: `tests/terminals/test_no_direct_tmux_spawn.py::test_spawn_paths_use_runtime`.
- 2.2.2 - No module under `src/gobby/terminals/` calls `uuid4()`; attachment and terminal ids are caller-supplied parameters. test: `tests/terminals/test_no_direct_tmux_spawn.py::test_identity_generation_absent_from_runtimes`.
- 2.2.3 - `TerminalLeaseRegistry.attach(attachment_id=...)` and `spawn_web_terminal(terminal_id=..., spawn_key=...)` reject missing ids with `ValueError` and never generate their own. test: `tests/terminals/test_lease_authority.py::test_attach_requires_caller_supplied_attachment_id`.

### 2.3 Remove tmux aliases from `SpawnResult` and the MCP spawn path [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_models.py::SpawnResult`
- `src/gobby/agents/spawners/base.py::SpawnResult`
- `src/gobby/agents/spawners/__init__.py::*` — scope-reason: re-exports `SpawnResult`
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: `_promote_prepared` and the result assembly stop carrying tmux fields
- `src/gobby/agents/resume_executor.py::*` — scope-reason: result consumers read `terminal_id`
- `src/gobby/mcp_proxy/tools/spawn_agent/_runtime.py::*` — scope-reason: `_tmux_runtime_metadata` keyed on the alias
- `src/gobby/mcp_proxy/tools/spawn_agent/_execution.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_health.py::*` — scope-reason: `_check_tmux_session_alive` replaced by a runtime liveness check
- `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py::*` — scope-reason: failed-spawn cleanup reads `run.tmux_session_name`, a field 2.1 of the source epic removed
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: alias consumer in the tool response; the response assembly moves out to keep the merged file under the ceiling
- `src/gobby/mcp_proxy/tools/spawn_agent/_response.py`
- `tests/mcp_proxy/tools/spawn_agent/test_health.py::*` — scope-reason: health check moves to runtime liveness
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py::*` — scope-reason: failure cleanup asserts runtime termination
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py::*` — scope-reason: response shape loses tmux keys
- `tests/agents/test_spawn_executor.py::*` — scope-reason: spawn-executor tests gain typed-failure, reaper, and timeout cases
- `src/gobby/agents/tmux/spawner.py::*` — scope-reason: the tmux spawner is the producer that fills the three fields
- `src/gobby/agents/spawn_executor_providers.py`
- `src/gobby/agents/spawn_executor_support.py::*` — scope-reason: constructs `SpawnResult`
- `src/gobby/terminals/web_spawn.py`
- `tests/agents/test_spawn_executor_droid.py::*` — scope-reason: constructs `SpawnResult` with tmux fields
- `tests/agents/test_srt_spawn.py::*` — scope-reason: SRT spawn fakes assert tmux fields
- `tests/integration/test_terminal_mode_worktrees.py::*` — scope-reason: constructs `SpawnResult` with tmux fields
- `tests/mcp_proxy/tools/test_spawn_agent_speed.py::*` — scope-reason: constructs `SpawnResult` with tmux fields
- `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::*` — scope-reason: failure-cleanup fakes assert kill-by-name
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_runtime.py::*` — scope-reason: runtime metadata fakes read the alias
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py::*` — scope-reason: sets `tmux_session_name` on `SpawnResult` fakes at four sites
- `tests/agents/test_tmux.py::*` — scope-reason: migrate every SpawnResult tmux-field assertion to terminal identity
- `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py::*` — scope-reason: replace the SpawnResult tmux-field mutation used by the provider fake
- `tests/workflows/test_step_snapshot_semantics.py::*` — scope-reason: patches `_execution._check_tmux_session_alive` by string, a symbol this leaf replaces
- `tests/terminals/test_no_direct_tmux_consumers.py`

Closes B-8, B-9, and C-8 (the allowlist narrowing and the reader migration).

Consumer sweep (`gcode usages` on both `SpawnResult` definitions): every caller and
constructor site is in the Targets above. Two module importers were examined and need
no change: `test_verified_review_regressions.py` imports only `SpawnRequest`, and
`spawners/__init__.py` re-exports the `SpawnResult` name, which keeps its name.

Removing a model field is an exhaustive sweep, not a main-path edit: every producer
(`tmux/spawner.py`, `spawn_executor_providers.py`, `spawn_executor_support.py`,
`spawn_executor.py`, `web_spawn.py`), every destructure/consumer, and every test
constructor or assertion of `SpawnResult(... tmux_session_name=...)` /
`tmux_socket_name` / `tmux_socket_path` is migrated here. Before the guard run, sweep
with `gcode grep -F "SpawnResult(" src/ tests/` plus a literal search for each field name
restricted to `SpawnResult` sites (session rows legitimately keep a
`tmux_session_name`; do not touch those), and migrate every hit.

`SpawnResult` (both definitions) still carries `tmux_session_name`, `tmux_socket_name`,
`tmux_socket_path`; the MCP spawn path keys on them (`_runtime.py` metadata,
`_execution.py` response assembly, `_health.py` `_check_tmux_session_alive` →
`TmuxSessionManager(config)` via `agents/tmux/__init__.py`, `_failure_cleanup.py`
kill-by-name). `_failure_cleanup.py` reads `run.tmux_session_name`, which no longer
exists on the run model, so failed-start spawns never kill their tmux session: the row
says `exited` while the pane lives on.

- Delete the three fields from both `SpawnResult` classes and every producer/consumer.
  `SpawnResult.terminal_id` is the only identity; tmux details are read from
  `terminal_manager.get(terminal_id).locator` by the few operator surfaces that display
  them.
- Health: replace `_check_tmux_session_alive` with
  `registry.resolve(row.backend).is_live(row)` where `row = terminal_manager.get(result.terminal_id)`.
- Failure cleanup: resolve `run.terminal_id` → `terminal_manager.get` → `runtime.terminate(row, grace)` →
  `terminal_manager.mark_exited`; never kill by name.
- The MCP tool response exposes `terminal_id` (and `backend`); drop the tmux keys.
  `_implementation.py` is 947 lines on `0.5.0` (746 on the branch) and the merged file
  lands near the ceiling: move the tool-response assembly (the dict-building that
  follows the spawn result in `spawn_agent_impl`, plus its helpers) into a new
  `src/gobby/mcp_proxy/tools/spawn_agent/_response.py` in this leaf so
  `_implementation.py` ends well under 900 lines; no behaviour change beyond the key
  removal.
- Narrow `_FIELD_SWEEP_ALLOWED` to the entries the source plan (2.5.13) allows —
  `src/gobby/terminals/tmux_runtime.py`, `src/gobby/agents/tmux/session_manager.py`,
  `src/gobby/agents/tmux/spawner.py` — plus `src/gobby/runner_lifecycle_processes.py`,
  which the branch's guard already exempts for its process-table reader and which no QA
  finding covers; the nine spawn-path entries are removed in the same commit that
  migrates them.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/spawn_agent tests/agents/test_spawn_executor.py tests/agents/test_native_spawn.py tests/workflows/test_step_snapshot_semantics.py`.

**Acceptance:**

- 2.3.1 - Neither `SpawnResult` class has a `tmux_*` field and no module under `src/gobby/agents` or `src/gobby/mcp_proxy/tools/spawn_agent` reads one. test: `tests/terminals/test_no_direct_tmux_consumers.py::test_repo_wide_field_sweep_is_empty`.
- 2.3.5 - `_FIELD_SWEEP_ALLOWED` contains only the three source-plan entries and `src/gobby/runner_lifecycle_processes.py`, and the repo-wide field sweep passes against that set. file: `tests/terminals/test_no_direct_tmux_consumers.py`.
- 2.3.2 - The spawn health check reports a tmux-backed and a native-backed terminal dead/alive through the runtime registry, with no tmux manager constructed. test: `tests/mcp_proxy/tools/spawn_agent/test_health.py::test_health_check_uses_runtime_liveness_for_both_backends`.
- 2.3.3 - A spawn that fails after its terminal reached `live` terminates the terminal through its runtime and marks the row `exited`; the fake runtime records exactly one `terminate`. test: `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py::test_failed_spawn_terminates_terminal_through_runtime`.
- 2.3.4 - The `spawn_agent` tool response carries `terminal_id` and `backend` and no `tmux_session_name`. test: `tests/mcp_proxy/tools/spawn_agent/test_execution.py::test_spawn_response_is_backend_neutral`.

### 2.4 Typed native spawn failures and host respawn with backoff [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: `_runtime_spawn` and `_promote_prepared` gain typed failure handling
- `src/gobby/terminals/web_spawn.py`
- `src/gobby/terminals/host_manager.py`
- `src/gobby/terminals/native_runtime.py`
- `src/gobby/terminals/host_client.py`
- `src/gobby/agents/spawn_models.py::SpawnResult`
- `src/gobby/servers/websocket/terminal_ws.py`
- `tests/servers/test_terminal_ws_create.py`
- `tests/agents/test_native_spawn.py`
- `tests/terminals/test_host_manager.py`
- `tests/servers/test_terminals_routes.py`
- `tests/agents/test_spawn_executor.py::*` — scope-reason: add the locator-unavailable promotion regression required by acceptance 2.4.3

Closes E-2, E-16, E-17 (the `commit_spawn` `ConnectionError` leak), B-7.

Producer ownership: everything this leaf's acceptance asserts is produced here or
upstream. `CommitTransportError` is raised by `HostClient.spawn_commit` in
`host_client.py` (targeted here); the typed refusal and `HostEpochChangedError` paths
already exist on the branch. Resolving a `request_written=True` outcome by `list`
needs the reconnect and event machinery of 2.9, so that reconcile lives in 2.9
(acceptance 2.9.8); until 2.9 lands a `commit_indeterminate` row is settled by the 2.5
stale-pending reaper, which terminates the attempt's own resource — `spawn_key` for
tmux, `terminate_host_id(process->>'host_terminal_id')` for native — and fails the row.

Today `TerminalHostManager.start()` with no `gterm` binary sets `native_available=False`
and the daemon boots degraded, but a native spawn then reaches
`manager.create_pending(backend="native")` → `runtime.reserve_observer()` →
`NativeTerminalRuntime._ensure()` → `HostUnavailableError`; `_runtime_spawn` catches only
`HostCommandError`, so the exception propagates, `manager.fail_pending()` never runs,
and the pending row leaks (the ageing sweep lives in `reconcile`, which only runs from a
health loop that is never armed after a failed start). `web_spawn.py` has the same hole
(`reserve_observer` outside its `except`).

The typed codes this leaf establishes also have to survive the WebSocket boundary, and
today they do not. `spawn_web_terminal` returns a `WebSpawnResult` carrying the exact
refusal string, and `_handle_terminal_create`
(`src/gobby/servers/websocket/terminal_ws.py`) discards it, replying only
`{type, request_id, success, terminal_id, backend}`. Every distinct cause — host
absent, capacity, epoch change, locator failure, commit refusal, timeout, lost CAS —
therefore collapses into one undecodable `success: false`, which is what leaves 6.2's
`Refused{code}` with no source. The handler adds `code` to the reply on failure only,
set verbatim from `WebSpawnResult.error` and bounded to 128 characters (the codes are
short typed strings; the bound exists so a stray exception message cannot inflate a
reply). A successful create is unchanged and carries no `code` key, so the success
golden 4.5 already pins stays byte-identical. 4.5 adds the matching refused golden to
the corpus it owns, and 6.2 decodes it. `_promote_prepared` does not catch the
`ConnectionError` that `commit_spawn`'s reconnect-and-verify re-raises. When tmux
`display-message` fails, `_promote_prepared` promotes with `locator={}` /
`locator_key=""`, which passes the non-null CHECKs and collides on the partial unique
index at the second such spawn (`UniqueViolation`). `handle_host_death` never respawns
the host; `restart_count` / `backoff_seconds` are dead fields.

- Every failure around `reserve_observer` / `prepare_spawn` / `commit_spawn` is
  classified by the boundary it crossed before the row transitions. The outcome table
  is the contract; `_runtime_spawn`, `_promote_prepared`, and `spawn_web_terminal`
  implement it identically (one shared helper `classify_native_spawn_failure`), and
  `SpawnResult` gains `error: str | None` (stable code) and `error_detail: str | None`
  (host state or message). Nothing is swallowed silently.

  | Boundary | Signal | Effect on host | Row transition | `SpawnResult.error` |
  |---|---|---|---|---|
  | before `reserve_observer` returns | `HostUnavailableError` (`absent`, `degraded`) | none | `fail_pending(reason=error_detail)` | `native_host_unavailable` |
  | `reserve_observer` / `prepare_spawn` | typed host refusal (`capacity`, `stale`, `not_native`, `host_draining`) | none | `fail_pending` | the host's code verbatim (`capacity`, …) |
  | `prepare_spawn` | `HostEpochChangedError` | prepared slot belongs to a dead epoch | `fail_pending` | `host_epoch_changed` |
  | `commit_spawn` | `CommitTransportError(request_written=False)` | none: `writer.write()` was never invoked, so no request byte reached the transport | `fail_pending` | `native_host_unavailable` |
  | `commit_spawn` | `CommitTransportError(request_written=False)` whose `cause` is an `asyncio.CancelledError` (cancelled while encoding, on the `closed` check, or waiting for the write lock) | none | `fail_pending` | none: `CancelledError` is re-raised to the caller instead of a `SpawnResult` |
  | `commit_spawn` | `CommitTransportError(request_written=True)` (any failure once `writer.write()` has been invoked: drain error, deadline, cancellation, EOF, lost reply) | unknown: the PTY may be live | row stays `pending`; settled by the 2.9 list-reconcile (2.9.8) or, before 2.9 lands, by the 2.5 reaper | `commit_indeterminate` until settled |
  | `commit_spawn` | typed host reply (`exec_timeout`, `exec_failed`, `gate_timeout`) | host already killed the group | `fail_pending` | the host's code verbatim |

  The transport boundary is observable: `HostClient.spawn_commit` raises one typed
  `CommitTransportError(request_id, request_written: bool, cause)` for every socket,
  deadline, or cancellation failure. The flag is conservative: it is `False` only for
  a failure that happens before `writer.write(encoded)` is invoked (encoding, the
  `closed` check, acquiring the write lock, a pre-write socket error) and flips to
  `True` the moment `write()` is called — an asyncio `StreamWriter` may hand part or
  all of the line to the kernel inside `write()` and cannot report how much reached
  the host after a `drain()` failure, so everything from that call onward (drain
  error, deadline, `CancelledError`, EOF, or a reply that never arrives) is
  indeterminate. The two commit rows are therefore distinguished by a fact the
  client recorded, never inferred from the exception class or from how many bytes
  "probably" left. Cancellation stays observable at both boundaries, and it is never
  converted into a typed `SpawnResult`:

  - `request_written=False` with a `CancelledError` cause (cancelled while encoding,
    on the `closed` check, or waiting for the write lock): the client removes the
    request id from the id-to-future correlation map, the executor calls
    `fail_pending` on the row with `native_host_unavailable`, and the `CancelledError`
    is re-raised to the caller. Nothing about the host changed, so the row is settled
    immediately rather than left for a reconciler.
  - `request_written=True` with a `CancelledError` cause (cancelled after the write):
    the executor leaves the row `pending` with `commit_indeterminate` exactly as the
    non-cancel path does and re-raises that `CancelledError` instead of returning a
    `SpawnResult`, so the caller sees its cancellation while the row is settled later
    by 2.9's list-reconcile or the 2.5 reaper. The client does not resolve or drop the
    correlation entry silently: it marks that request id abandoned and removes it, and
    the reader logs and discards any reply that later arrives for an abandoned id
    instead of resolving a cancelled future. Abandoned ids are never reused.

  The reader survives both paths and unrelated in-flight requests are unaffected;
  every other transport failure stays on the typed `SpawnResult` path.

  Refusal codes are never collapsed into a generic failure; tests assert the exact
  code for each row of the table.
- `_promote_prepared`: when `prepared.locator_key` is `None` or empty, terminate by
  `spawn_key` through the runtime, `fail_pending`, and return
  `error="locator_unavailable"`; never write an empty locator.
- Host respawn: `handle_host_death` schedules `_spawn_and_connect` with backoff
  (named default 1 s → 30 s doubling, reset after 60 s healthy), increments
  `restart_count`, exposes `backoff_seconds` in `health_state()`, and gives up (stays
  degraded, logs once) after 5 consecutive failures until `start()` is called again.
  The health loop is armed even when the initial start fails so `reconcile` ages out
  pending rows.

  Restart is singleflight. The manager owns one `_restart_task: asyncio.Task | None`
  and a `_restart_generation: int`, both guarded by one `asyncio.Lock`. Every
  observer of a dead or unreachable host — the health loop, a request-path
  `HostConnectionLost` / reconnect failure, and 2.9's event reader — calls
  `ensure_restart()`, which under the lock returns the running task when one exists
  and otherwise bumps the generation and starts one; callers await
  `asyncio.shield(task)`, never their own spawn and never the bare task — awaiting
  the task directly would let one cancelled waiter (a request handler whose client
  went away, the event reader being torn down) cancel the restart for the health
  loop and every other observer. The task rotates the token, spawns the host,
  replaces the clients, and publishes the new epoch only if its generation is still
  current under the lock, and on completion (success, failure, or cancellation) it
  clears `_restart_task` under the same lock so the next `ensure_restart()` starts a
  fresh one. Only `stop()` cancels the task: it bumps the generation (invalidating
  any in-flight publication), cancels and drains the task, then tears down, so a
  restart that completes during shutdown never publishes a client to a stopped
  manager.

  A generation bump fences the task that exists, not the task nobody has created yet.
  The completion callback clears `_restart_task` under the lock, so the moment the
  cancelled task unwinds the slot is empty, and any observer arriving after that point
  — a request handler whose connection dropped as the host was killed, or 2.9's event
  reader seeing EOF because `stop()` is killing the host out from under it — passes
  both guards, bumps the generation itself, and mints a restart whose generation is
  current by construction. It then rotates the token and publishes a client into a
  manager that is mid-teardown or already stopped. The lifecycle state must therefore
  gate creation, not just publication: the manager's existing `_stop_requested` flag
  moves under the restart lock, `stop()` sets it there **before** it cancels anything,
  `ensure_restart()` refuses to mint a task while stopping or stopped, and publication
  requires the generation and the lifecycle state to agree. `start()` clears the flag
  under the same lock, which is the only way back. This adds no new state —
  `_stop_requested` already exists and already means exactly this — it only puts it
  under the lock that every other restart decision already takes.

  The refusal is **raised, never returned**. `ensure_restart()` returns
  `asyncio.Task` and nothing else, because every caller's next statement is
  `await asyncio.shield(task)` and a union return would make that a `TypeError` at
  exactly the moment the daemon is shutting down. Under the lock, a stopping or
  stopped manager raises `HostManagerStopped` (a new exception in `host_manager.py`)
  before any task is minted, so no generation is bumped, no token is rotated, and no
  client is published. All three production observers treat it as normal shutdown
  rather than as a restart failure, and each terminates its own retry path instead of
  looping: the health loop stops scheduling further probes and exits its loop; the
  request-path observer converts it into the same typed `HostUnavailableError`
  (`degraded`) the outcome table above already routes to `native_host_unavailable`, so
  a spawn racing shutdown fails its pending row rather than hanging; and 2.9's event
  reader stops reconnecting and exits `_event_reader_loop` without touching the cursor.
  None of them logs it as an error.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_native_spawn.py tests/terminals/test_host_manager.py tests/servers/test_terminals_routes.py`.

**Acceptance:**

- 2.4.1 - A native spawn on a daemon whose host is absent returns a `SpawnResult` with `error="native_host_unavailable"`, the pending row is `exited` with a reason, and no exception escapes `execute_spawn`. test: `tests/agents/test_native_spawn.py::test_native_spawn_without_host_fails_pending_with_typed_error`.
- 2.4.2 - The same refusal and row cleanup happen for `spawn_web_terminal` and for a `CommitTransportError(request_written=False)` raised from `commit_spawn`; a `request_written=True` failure leaves the row `pending` for reconciliation instead. test: `tests/servers/test_terminals_routes.py::test_web_spawn_without_host_is_typed_refusal`.
- 2.4.3 - A tmux promotion with no locator terminates the session by spawn key, fails the row, and two such spawns in a row raise no `UniqueViolation`. test: `tests/agents/test_spawn_executor.py::test_promote_without_locator_fails_typed`.
- 2.4.4 - After a host death the manager respawns the host with doubling backoff, `health_state()` reports `restart_count` and `backoff_seconds`, and after five failures it stays degraded without further attempts. test: `tests/terminals/test_host_manager.py::test_host_death_respawns_with_backoff`.
- 2.4.5 - The health loop runs `reconcile` even when the initial host start failed, so a leaked pending row ages out. test: `tests/terminals/test_host_manager.py::test_health_loop_armed_after_failed_start`.
- 2.4.6 - `HostClient.spawn_commit` raises `CommitTransportError` with `request_written=False` for a failure before `writer.write()` is invoked (closed client, pre-write socket error) and `request_written=True` for every failure after it (drain error, deadline, cancellation during drain, EOF, reply never received); the former fails the row with `native_host_unavailable`, the latter leaves the row `pending` with `SpawnResult.error="commit_indeterminate"` and touches no host state. test: `tests/agents/test_native_spawn.py::test_spawn_outcome_table`.
- 2.4.7 - A typed reserve or prepare refusal (`capacity`, `stale`, `not_native`, or `host_draining`) fails the pending row and preserves the exact host code. test: `tests/agents/test_native_spawn.py::test_spawn_outcome_table`.
- 2.4.8 - `HostEpochChangedError` during prepare fails the pending row with `host_epoch_changed` and never promotes it. test: `tests/agents/test_native_spawn.py::test_spawn_outcome_table`.
- 2.4.9 - Three observers reporting host death in the same tick (health loop, a request-path connection loss, and a direct `ensure_restart()` call) produce exactly one token rotation and one host spawn, and `stop()` during the backoff wait cancels the restart so no client is published afterwards. Two later observers are refused rather than fenced: one calling `ensure_restart()` after the cancellation but before the drain completes, and one calling it after the drain but while teardown is still running, each raise `HostManagerStopped`, mint no task, rotate no token, and publish no client; the same call after `stop()` returns is refused identically, and only `start()` makes `ensure_restart()` mint again. The refusal reaches the production observers as an exception, never as a value they would shield: with `stop()` in progress, the health loop exits its loop without scheduling another probe, an in-flight request-path spawn fails its pending row with `native_host_unavailable`, and neither logs an error. test: `tests/terminals/test_host_manager.py::test_restart_is_singleflight_and_stop_invalidates`.
- 2.4.10 - A typed `exec_timeout`, `exec_failed`, or `gate_timeout` reply leaves the process group dead, fails the row, and preserves the host code. test: `tests/agents/test_native_spawn.py::test_spawn_outcome_table`.
- 2.4.11 - Against a real `HostClient` over a paused socket, a spawn task cancelled after `writer.write()` of the commit request raises `CancelledError` to its caller (no `SpawnResult` is returned), leaves the row `pending` with `commit_indeterminate`, marks the request id abandoned so a reply arriving afterwards is logged and dropped rather than resolving a cancelled future, and leaves the reader and an unrelated in-flight request unaffected. test: `tests/agents/test_native_spawn.py::test_post_write_cancellation_propagates_and_row_settles_later`.
- 2.4.12 - Two observers awaiting the same `ensure_restart()` task during its backoff wait, one of which is cancelled, leave the restart running to completion: the surviving waiter receives the new epoch, exactly one host spawn occurs, and the cancelled waiter observes only its own `CancelledError`. test: `tests/terminals/test_host_manager.py::test_waiter_cancellation_does_not_cancel_shared_restart`.
- 2.4.13 - A spawn task cancelled before `writer.write()` (blocked on the write lock held by another request) raises `CancelledError` to its caller, drops its correlation entry, and leaves the row failed with `native_host_unavailable`; the request holding the lock completes normally. test: `tests/agents/test_native_spawn.py::test_pre_write_cancellation_propagates_and_row_fails`.
- 2.4.14 - Every web-spawn refusal reaches the client as a code rather than a bare `success: false`. `_handle_terminal_create` emits `terminal_create_result` carrying `code` set to `WebSpawnResult.error` whenever `success` is false, and each of `native_reserve_unavailable`, `native_host_unavailable`, `host_epoch_changed`, a typed reserve/prepare refusal, a commit refusal, `spawn timed out`, `cancelled`, and `lost_cas_conflict` arrives with its exact string; a successful create carries no `code` key at all. test: `tests/servers/test_terminal_ws_create.py::test_create_result_carries_the_refusal_code`.

### 2.5 Stale-pending reaper, timeout settlement, and resume identity keys [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: `reap_stale_pending_terminals` and the timeout branch of `_runtime_spawn` change
- `src/gobby/agents/resume_executor.py::*` — scope-reason: daemon-stop resume keys are written from the terminal row
- `src/gobby/runner_lifecycle_agents.py::*` — scope-reason: daemon-stop resume keys are read by constant
- `src/gobby/runner_lifecycle_restart.py`
- `src/gobby/storage/daemon_resume_keys.py::*` — scope-reason: the key constants are the single source for writer and reader
- `src/gobby/storage/terminals.py`
- `src/gobby/terminals/host_manager.py`
- `src/gobby/terminals/host_reconcile.py`
- `src/gobby/terminals/web_spawn.py`
- `tests/agents/test_spawn_executor.py::*` — scope-reason: spawn-executor tests gain typed-failure, reaper, and timeout cases
- `tests/test_runner_lifecycle_restart_replay.py::*` — scope-reason: adds the resume-key reader test
- `tests/servers/test_terminal_ws_create.py`
- `tests/storage/test_terminals.py`

Closes B-5, B-6, B-11.

The resume-key reader (`_resolve_provisional_daemon_resume_row`) lives in
`src/gobby/runner_lifecycle_restart.py` after the 2.1 split of
`src/gobby/runner_lifecycle_agents.py`; the B-11 reader change below lands there, and
any literal key left in `runner_lifecycle_agents.py` is replaced by the constant import.

- B-5: `reap_stale_pending_terminals` does `if await runtime.is_live(row): continue`,
  so a pending row older than `spawn_in_doubt_seconds` whose `spawn_key` session exists
  is left pending forever and the session is never killed. Stale + found → terminate the
  attempt's resource then `fail_pending_attempt`; stale + not found →
  `fail_pending_attempt`. Fresh rows are untouched.

  "Terminate the resource" is backend-specific because a pending row has no locator, and
  `NativeTerminalRuntime.terminate` resolves its kill target through `_host_id`, which
  reads `locator["host_terminal_id"]` and raises `TerminalWriteError(stage="none")` when
  the locator is absent. A blanket `runtime.terminate(row, grace)` therefore cannot
  terminate a pending native row against the real runtime — only against a fake that
  ignores the locator — so the reaper does not call it for one. A stale pending **tmux**
  row is terminated by `spawn_key`, exactly as the timeout done-callback does. A stale
  pending **native** row is terminated through the same captured-resource form the
  done-callback uses: the reaper reads the current generation's
  `process->>'host_terminal_id'` under `settle_lock` (the identity `record_process`
  writes before commit, below) and kills that host id, and a row whose `process` carries
  no identity — a native attempt reaped before its `record_process` landed — is failed
  without a kill, because there is no prepared slot to kill yet. A `live` row is
  unaffected: its locator is filled and `runtime.terminate` works as before.

  The native kill needs a call that takes an identity rather than a row, and none
  exists: `NativeTerminalRuntime.terminate(terminal, grace)` resolves its target through
  `_host_id(terminal)`, which reads `locator["host_terminal_id"]`. This leaf therefore
  adds one method beside it, `NativeTerminalRuntime.terminate_host_id(host_terminal_id,
  grace_seconds)`, carrying the two lines `terminate` already runs once `_host_id`
  returns (`await self._client.kill(host_id, grace_ms=…)`); `terminate` is refactored to
  call it, so there is one kill implementation and one place the grace default lives.
  The reaper calls `terminate_host_id` for a native row and never hands a locator-less
  row to `runtime.terminate`.

  That kill runs inside `settle_lock` against a host that can be gone, so its failure
  outcomes are named rather than left to the blanket `except Exception` the reaper wraps
  `is_live` in. `HostUnavailableError` — raised by `NativeTerminalRuntime._ensure` when
  the manager holds no client — leaves the row `pending` and unfailed, because a
  resource that may still be alive must keep the row that records it; the reaper logs it
  and continues to the next row, so one unreachable host never aborts the sweep. A later
  sweep retries, and if the host is genuinely gone `handle_host_death` reaps the process
  group from `process` and orphans the row there instead. `HostManagerStopped` ends the
  sweep immediately and cleanly: the daemon is shutting down, the remaining rows keep
  their state for the next start, and nothing is logged as an error. Any other exception
  from the kill leaves that one row pending exactly as `HostUnavailableError` does and
  the loop continues.

  A native row left `pending` by a 2.4 `CommitTransportError(request_written=True)`
  (`commit_indeterminate`) is an ordinary stale-pending row to the reaper and takes
  exactly the native path above: once older than `spawn_in_doubt_seconds` it is
  terminated by the current generation's `process->>'host_terminal_id'` when the host
  reports it live, and failed either way, so the indeterminate commit is settled here
  until 2.9's list-reconcile (2.9.8) takes over the fast path. `spawn_key` terminates
  tmux rows and only tmux rows anywhere in this leaf.
- B-6: on `request.timeout_seconds` the executor runs `_kill_spawn_key` while the
  shielded `prepare_task` is still inside `create_session`; the kill of a not-yet-created
  name is swallowed, the session is then created, and the row stays pending. Leave the
  row pending, attach `prepare_task.add_done_callback` that either promotes (if the
  caller is gone, terminate by spawn key and `fail_pending_attempt`) or records the
  exception, and return the timeout as a typed `SpawnResult(error="spawn_timeout")`.
  The reaper (B-5) then settles anything the callback could not.

  The callback is owned by its attempt generation. A retry through
  `request.retry_terminal_id` reuses the row and its `spawn_key`
  (`bump_attempt_generation` refreshes `attempt_generation` / `attempt_started_at`),
  so a late completion of attempt A must never touch attempt B's PTY. The callback
  therefore captures `(attempt_generation, attempt_started_at)` at attempt start
  together with the resource its own prepare created (the prepared
  `host_terminal_id`, or the tmux session name it asked for): promotion is a CAS on
  the captured generation (`promote_to_live` gains the same
  `attempt_generation` / `attempt_started_at` guard `fail_pending_attempt` already
  has), and when the guard fails or the caller is gone the callback terminates only
  its captured resource (`kill` by `host_terminal_id` for native; the tmux kill is
  skipped when the row's current generation differs, since the session name is the
  shared `spawn_key`) and never calls `_kill_spawn_key` against a row of a newer
  generation. The reaper (B-5) applies the same guard.

  The guard is claimed before the kill, not after it. A CAS that runs only at
  `fail_pending_attempt` leaves a window between observing a stale row and
  terminating its resource in which a retry can bump the generation and create a
  session under the same `spawn_key` (or a new listed slot), which the old path then
  kills. `TerminalManager` (`src/gobby/storage/terminals.py`) therefore owns one
  in-process lock per terminal, exposed as an async context manager
  `settle_lock(terminal_id)`, and every attempt transition holds it: the retry path around
  `bump_attempt_generation`, the reaper's observe → terminate → `fail_pending_attempt`
  span, the timeout done-callback's promote-or-terminate span, the ordinary
  `_promote_prepared` promotion, `web_spawn.spawn_web_terminal`'s promotion, and 2.9's
  list-reconcile settlement of a `commit_indeterminate` row. Ordinary promotion belongs
  in that list and was missing from it: the reaper's span is a read-then-write, so a
  promotion that lands between the reaper observing a stale `pending` row and killing
  its resource leaves the session dead with the row `live` — the generation guard
  cannot catch it because an ordinary promotion bumps no generation. Inside the lock each
  settlement re-reads the row and compares `(attempt_generation, attempt_started_at)`
  with the values it captured before any host query or kill; a mismatch means a newer
  attempt owns the row and the settlement does nothing. The daemon is the only writer
  of terminal rows, so this single-process lock is the ownership claim; no new row
  state is introduced. Native settlements still kill only their captured
  `host_terminal_id`.

  Each owner's span is exact, because a per-terminal lock held across a remote await
  is a stall, not a fence. `_promote_prepared` does **not** wrap itself: it completes
  `bind_observer` and `runtime.commit_spawn(prepared)` — the two remote round trips,
  the second bounded only by `commit_deadline_ms` (30 s by default) — with no lock
  held, and only then acquires `settle_lock(terminal_id)` exactly once, around the
  generation re-read, the `promote_to_live` CAS, and the `lost_cas_conflict` cleanup
  that kills the captured resource when the CAS loses. Wrapping the whole method
  would let one slow commit block that terminal's exit settlement, retry, and reaper
  for the full deadline; acquiring after the commit costs nothing, because everything
  the lock protects is the CAS and the kill that may follow it. The locked region is
  factored out as one named helper (`settle_promotion`) whose contract is that the
  caller holds no lock and it acquires exactly one, so the timeout done-callback
  reaches promotion by calling that helper rather than by taking `settle_lock` and
  then calling into a path that takes it again — an `asyncio.Lock` is not reentrant
  and that nesting is a self-deadlock, not a contention. The callback's own
  terminate-and-fail branch keeps its single acquisition.

  `_promote_prepared` is not the only production promoter, and the second one is a
  supported ingress rather than a corner case. `web_spawn.spawn_web_terminal` — the
  row-owning primitive `_handle_terminal_create` calls for every web-created terminal —
  performs its own `manager.promote_to_live` and its own `lost_cas_conflict` kill with no
  lock held, so the reaper's observe → kill span races it exactly as it raced ordinary
  promotion, and a web-created row can be killed while recorded `live`. It also never
  calls `record_process`, so a web-created native row carries no
  `process->>'host_terminal_id'` and its pending-window exit matches no `settle_exit`
  guard.

  Its third gap is the one B-6 closes for the agent path and nobody closed here:
  `spawn_web_terminal` shields `prepare_task` and then abandons it. On
  `request.timeout_seconds` it calls `_kill(runtime, spawn_key, manager.get(terminal_id))`
  against a row that is still `pending` and therefore locator-less — for native that
  reaches `_host_id`, raises `TerminalWriteError(stage="none")`, and is swallowed by
  `_kill`'s bare `except Exception: return`, so nothing is killed — then calls
  `manager.fail_pending` and returns. The shielded task keeps running with no done
  callback, so a native slot or tmux session that appears afterwards outlives the row,
  and because the row is already `failed` the B-5 reaper will never look at it again.
  The bind-failure and lost-CAS branches call the same locator-less `_kill`, and
  `CommitSpawnRefusedError` kills nothing at all. This is the same defect B-6 describes,
  on the ingress B-6 did not cover.

  All three are closed by reuse, not by a parallel mechanism: `spawn_web_terminal`
  persists the prepared `host_terminal_id` through the same unconditional
  `record_process` call the agent path makes before commit, and reaches promotion through
  `settle_promotion`, inheriting the generation re-read, the CAS, and the lost-CAS
  cleanup under one acquisition. It also takes B-6's settlement whole: the same
  `prepare_task.add_done_callback` owned by its attempt generation, capturing
  `(attempt_generation, attempt_started_at)` and the resource its own prepare created,
  so a late prepare either promotes through `settle_promotion` or terminates its own
  captured resource and calls `fail_pending_attempt`. Every post-prepare failure branch
  routes through that one owner instead of `_kill(runtime, spawn_key, row)`: timeout,
  cancellation, bind failure, `CommitSpawnRefusedError`, and lost CAS all leave the
  callback attached and kill by captured `host_terminal_id` for native and by
  `spawn_key` for tmux. `_kill`'s fabricated-`Terminal` fallback goes away with its last
  caller — a row that is genuinely gone has no resource this ingress can own, and the
  reaper settles anything the callback could not. `manager.fail_pending` is replaced by
  `fail_pending_attempt` on every branch so a web attempt cannot fail a row a retry
  already owns.

  The helper needs one file owner, and it already has one. `settle_promotion` stays in
  `src/gobby/agents/spawn_executor.py` — public rather than underscore-private, because
  it now has a caller outside the module — and `web_spawn.py` imports it exactly as it
  already imports `derive_spawn_key` from that module today (`web_spawn.py:9`). That
  direction is the only one that exists: `spawn_executor` imports nothing from
  `web_spawn`, so the import graph stays acyclic with no new module. Moving the helper
  and `derive_spawn_key` into a new backend-neutral spawn-support module would relocate
  two symbols and both callers to buy an import direction the code already has, so this
  leaf does not do it. The no-lock-held precondition is the same for both callers.

  The order against every other lock is fixed and one-directional:
  `settle_lock` → `HostClient`'s per-connection write lock → the 2.4 restart lock.
  A settlement may kill or terminate inside its span, which takes the write lock for
  one line write and may call `ensure_restart()` if that connection is gone; nothing
  in the reverse direction exists, because the restart task rotates the token, spawns
  the host, and publishes a client without touching a terminal row, and the write lock
  is released before any reply is awaited. 4.1's per-terminal write-authority lock is
  disjoint from this order in both directions: no settlement takes it, and 4.1's
  dispatch and lease mutations never take `settle_lock` — the exit path's own lease
  cleanup runs after `settle_exit` returns.

  Process exit needs a settlement of its own, and today it has none. `mark_exited`
  CASes `live → exited` and `orphaned → exited` only; a `terminal_exited` for a row that
  is still `pending` matches no branch, returns `None`, and is dropped. That is
  reachable on the ordinary success path, not just under a crash: a target that exits
  faster than its own commit reply — `gterm gate -- /bin/true`, or any command that
  fails immediately after a successful `execvp` — delivers its exit while the row is
  still `pending`, the exit is discarded, and the commit's `promote_to_live` then CASes
  `pending → live`, leaving a dead process recorded live until some later sweep notices.

  Settling it needs an identity the exit actually carries, and the attempt generation is
  not one. Nothing on the exit path produces `(attempt_generation, attempt_started_at)`:
  the host's `emit_exit` reads `slot.host_terminal_id`, and the daemon decodes
  `terminal_exited` as `{host_terminal_id, exit_code}`. What the host does hold for every
  slot is `Identity { terminal_id, spawn_key }` alongside `host_terminal_id`, so 2.9's
  event (the control-plane push, whose shape is pinned by
  `crates/gterminal/tests/fixtures/wire_golden/control_event_terminal_exited.json`)
  carries exactly `terminal_id`, `host_terminal_id`, and `exit_code`: the first names the
  row, the second names the attempt. `host_terminal_id` is the attempt discriminator
  because a retry through
  `request.retry_terminal_id` reuses both the row id and the `spawn_key` while each native
  attempt prepares its own host slot. `spawn_key` is deliberately **not** on the event:
  `settle_exit` consumes the row id and the attempt id and nothing else, and a third
  identity field nobody reads is a golden fixture and a decoder to maintain for no
  behaviour.

  A `live` row already stores that id in its locator, but a `pending` row does not —
  `promote_to_live` writes the locator at promotion. The prepared id therefore goes into
  the pending row's existing `process` metadata, which
  `spawn_executor._persist_and_bind` already writes through
  `record_process(terminal_id, {...})` before commit: one more JSONB key on an UPDATE
  that already runs, with no schema change and no migration. `record_process` today
  only fires when `prepared.process is not None`, so it is called unconditionally for
  native rows and carries the prepared `host_terminal_id` whether or not the host
  reported a pgid. It stays native-only, because the `pending`-exit window is: tmux's
  `commit_spawn` is a purely local promotion with no host round-trip and no reply to
  lose, so a tmux row is never `pending` across a remote await, and a tmux pane's exit
  reaches the daemon through an observer slot that exists only after the row is live.
  Tmux exit recovery therefore stays on that observer and liveness path and never
  reaches `settle_exit` at all.

  An attempt-scoped id in a row-level column needs a lifetime, and `process` currently
  has neither an invalidation point nor a single writer. Two failures follow from that,
  and both are closed here. **Stale retention:** `bump_attempt_generation` refreshes
  `attempt_generation` and `attempt_started_at` but leaves `process` untouched, so from
  the moment attempt B bumps the row until B's own `record_process` lands, the row still
  advertises attempt A's `host_terminal_id` — and a delayed A exit arriving in that
  window satisfies the pending guard and exits B's row. `bump_attempt_generation`
  therefore drops the discriminator in the same UPDATE
  (`SET process = process - 'host_terminal_id'`, alongside the fields it already sets),
  so a row that has been re-attempted advertises no attempt identity until the new
  attempt writes one, and an exit landing in the gap matches nothing. It deletes that one
  key rather than nulling the column, because `process` carries a second consumer with a
  different lifetime: `list_live_by_machine` returns `state IN ('pending','live')` and
  `TerminalHostManager.handle_host_death` reaps each returned row's process group from
  `row.process` alone, so `{pgid, start_time}` is the only record of a predecessor
  attempt's process group. Attempt A's native PTY can still be alive when B bumps —
  native retries do not wait for A, unlike the tmux rule below — and a host death in that
  window would find nothing to reap if the object were nulled, leaking A's process group.
  Deleting only `host_terminal_id` disarms the exit guard (`process->>'host_terminal_id'`
  is then SQL NULL and equals no reported id) while leaving the reap record intact, so
  the identity has an attempt-scoped lifetime and the cleanup record keeps its row-scoped
  one. A NULL `process` stays NULL under the same operator.

  **Stale restoration:** clearing the key at the bump is not enough on its own, because
  nothing stops the superseded attempt from writing it straight back.
  `_promote_prepared` calls `manager.record_process` before any CAS and outside
  `settle_lock` (`src/gobby/agents/spawn_executor.py:452-456`), and `record_process`
  guards only `state = 'pending' AND backend = 'native'`, so attempt A completing after
  B's bump writes A's `host_terminal_id` onto B's row and A's late exit then satisfies
  `settle_exit`'s pending guard against B — the exact failure the bump was added to
  prevent, reintroduced one statement later. The write therefore carries the attempt it
  belongs to: `record_process(terminal_id, process, *, attempt_generation,
  attempt_started_at)` adds both to its `WHERE`, matching the CAS shape
  `fail_pending_attempt` and `promote_to_live` already use, and the caller passes the
  pair it captured at attempt start (the same pair the done-callback captures). A
  superseded attempt's write then matches no row and is a no-op, so no lock is needed
  here either.

  **Identity erasure:** `record_process` is also a whole-object write (`SET process =
  %s`) and `_persist_and_bind` is not its only caller —
  `host_manager.TerminalHostManager.handle_spawn_prepared` (`host_manager.py:227`) and
  `host_reconcile.reconcile_host_inventory` (`host_reconcile.py:134`) both write
  `{pgid, start_time}` for a pending row, and either one landing after the identity
  would erase it and drop that attempt's exit. Splitting the two concerns closes this
  and the previous failure with less mechanism than guarding three writers: the
  discriminator gets exactly one writer, and the cleanup record gets a write that cannot
  touch the discriminator at all.

  `record_process` becomes the sole identity writer. It still requires
  `host_terminal_id` in its mapping and raises on a mapping without it, it carries the
  attempt CAS above, and it merges rather than overwrites (`SET process =
  COALESCE(process, '{}'::jsonb) || %s`) so it cannot erase a `pgid` a host event
  recorded first. The two host-event writers stop calling it and call a new
  `TerminalManager.merge_process_reap_record(terminal_id, {"pgid", "start_time"})`,
  the same `COALESCE(process, '{}'::jsonb) || %s` merge restricted to those two keys.
  That writer needs no generation guard and no mandatory identity key, because it can
  neither mint nor erase `host_terminal_id`: the worst a superseded event can do is
  leave a predecessor's `pgid` in the row.

  That residue is bounded and deliberate. `{pgid, start_time}` is a best-effort
  host-death reap record, not an attempt-authoritative one — `handle_host_death` reaps
  whatever group the row last recorded, and reaping a predecessor's group is the
  behaviour round 13's `SET process = NULL` was found to have broken. The
  attempt-authoritative cleanup is the captured-`host_terminal_id` kill the
  done-callback and the reaper perform, and neither reads `pgid`. Threading an attempt
  generation onto the `spawn_prepared` control event to make the reap record exact would
  add a wire field, a golden, and a decoder to sharpen a backstop that is already
  correct for the case it exists to cover.

  The settlement itself is one new `TerminalManager.settle_exit(terminal_id,
  host_terminal_id)`: an identity-guarded CAS `pending → exited` whose `WHERE` also
  requires `process->>'host_terminal_id'` to equal the reported id, falling through to
  the existing `live → exited` branch guarded the same way against
  `locator->>'host_terminal_id'` — where `promote_to_live` writes the id for a native
  row — and returning `None` when neither branch matches. Both guards read the native
  identity only, because 2.9's event reader is the sole caller and it is a native
  control-plane path; a tmux locator key would be a branch with no caller. That is the
  shape `fail_pending_attempt` already uses — the guard lives in the statement, not in a
  read the caller performs first — which is why the exit path needs no lock: two
  single-statement CASes cannot interleave, so the read-then-write window a lock would
  have closed does not exist. Both orderings converge without a sweep. Exit first leaves
  the row `exited`, and the commit's `pending → live` CAS finds no `pending` row and
  fails, so the spawn is reported as one that started and exited. Promotion first leaves
  the row `live` with its locator filled, and the exit takes the `live → exited` branch.
  A stale exit from attempt A arriving after B has replaced it matches neither guard and
  settles nothing. `mark_exited` is left alone: its six other callers are sweeps and
  cleanups operating on rows they already believe live, none of them has a finding behind
  it, and a committed test pins its current pending-blind behaviour. 2.9's event reader
  calls `settle_exit` instead of `mark_exited`, and proves live delivery in 2.9.2; this
  section proves only the storage transition.

  Lock-cell lifetime is refcounted rather than permanent. Each map entry is a cell
  `(lock, borrowers)`; entering `settle_lock(terminal_id)` creates the cell if absent
  and increments `borrowers` **before** awaiting `lock.acquire()`. The acquire and the
  body then run inside one `try`, and a single `finally` decrements the same cell
  object the entry captured — never a fresh map lookup — releases the lock only when an
  `acquired` flag records that `lock.acquire()` actually returned, and deletes the map
  entry when the count reaches zero and the mapped cell is still that identical object.
  A borrower cancelled while queued on `acquire()` therefore unwinds through that same
  `finally`, drops its reference, and re-raises the `CancelledError`, so a cancelled
  waiter can never strand a cell. The daemon's event loop is single-threaded and neither step awaits
  between the map lookup and the count change, so a coroutine that will use a cell has
  always incremented it first: at `borrowers == 0` no owner and no waiter can exist,
  and minting a fresh lock for the next user is therefore identity-safe. This is the
  whole reclamation rule — no owner/waiter tracking, no map mutex, and no attempt,
  attachment, or lease inspection — and it bounds the map by the number of terminals
  being settled concurrently rather than by the terminals the process has ever seen.

  Because tmux attempts share one session name, the skipped kill above would leak a
  session whenever the newer attempt fails, exits, or is reaped before the older
  attempt's `create_session` finally lands. Two rules close that: (1) a retry through
  `request.retry_terminal_id` on a tmux row whose previous attempt's `prepare_task` is
  still unsettled in this daemon is refused with the typed
  `SpawnResult(error="retry_attempt_unsettled")` and no generation bump — the caller
  retries once the timed-out attempt's done-callback has settled the row (terminated by
  `spawn_key` and failed, or promoted); (2) a retry attempt whose tmux
  `create_session` fails because the session name already exists (the only way that
  happens after rule 1 is a predecessor attempt's creation surviving a daemon restart,
  and `idx_terminals_spawn_key` guarantees the name belongs to this row) kills that
  session by `spawn_key` before `fail_pending_attempt`, so the failure leaves no live
  session behind. Native attempts are unaffected: each prepares its own
  `host_terminal_id` and kills only that.
- B-11: `resume_executor.py` writes `SPAWN_KEY_KEY` from `terminal_result.tmux_session_name`
  (gone) and `runner_lifecycle_agents.py` reads a literal `"daemon_stop_resume_spawn_key"`
  falling back to `terminal_id`. Write `spawn_key` from `manager.get(terminal_id).spawn_key`
  and `terminal_id` under `TERMINAL_ID_KEY`; the reader imports both constants from
  `daemon_resume_keys.py` and resolves the terminal row first.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_spawn_executor.py tests/agents/test_resume_executor.py`.

**Acceptance:**

- 2.5.1 - A stale pending row whose backend resource exists is terminated by that backend's identity and failed — a tmux row by `spawn_key`, a native row by `terminate_host_id(process->>'host_terminal_id')` — while a fresh pending row is untouched and a stale row with no resource is failed with no kill. test: `tests/agents/test_spawn_executor.py::test_stale_pending_with_live_session_is_terminated_and_failed`.
- 2.5.2 - A spawn that times out during `create_session` returns `error="spawn_timeout"`, and when the session appears afterwards it is terminated and the row failed by the done-callback. test: `tests/agents/test_spawn_executor.py::test_timeout_settles_late_session`.
- 2.5.3 - Daemon-stop resume writes `spawn_key` and `terminal_id` under the shared constants and the restart reader resolves the terminal row for both backends. test: `tests/test_runner_lifecycle_restart_replay.py::test_daemon_stop_resume_keys_round_trip`.
- 2.5.4 - Attempt A times out, attempt B retries the same row and goes live, then A's prepare completes late: the row stays live on B's generation and locator, A's own prepared PTY is killed by its captured `host_terminal_id`, and B's PTY is untouched (the same holds with the order of A's completion and B's promotion swapped). test: `tests/agents/test_spawn_executor.py::test_late_timeout_completion_never_touches_retry_attempt`.
- 2.5.5 - A native row left `pending` with `commit_indeterminate` by a lost commit reply is settled by the reaper once stale: when the host reports the attempt live it is terminated by the current generation's `process->>'host_terminal_id'` and failed, otherwise it is failed directly; a stale native row whose `process` carries no identity is failed with no kill attempted; and a fresh `commit_indeterminate` row is untouched. The terminate case runs against a real `NativeTerminalRuntime` (not a fake that ignores the locator), proving the reaper calls `terminate_host_id` and never `runtime.terminate` on a locator-less pending row, which raises `TerminalWriteError(stage="none")` in `_host_id`. test: `tests/agents/test_spawn_executor.py::test_reaper_settles_commit_indeterminate_rows`.
- 2.5.6 - A tmux retry requested while the timed-out attempt's prepare task is still unsettled is refused with `retry_attempt_unsettled` and bumps no generation; after that attempt's late `create_session` lands, the done-callback terminates the session by `spawn_key` and fails the row, and a retry then succeeds with exactly one live session at every point. test: `tests/agents/test_spawn_executor.py::test_tmux_retry_waits_for_unsettled_attempt`.
- 2.5.7 - A tmux retry whose `create_session` fails with a duplicate-session error kills the `spawn_key` session before failing the attempt; with the fake tmux runtime reporting the name pre-existing, no session named `spawn_key` survives and the row is `failed`. test: `tests/agents/test_spawn_executor.py::test_tmux_retry_duplicate_session_is_killed_before_failing`.
- 2.5.8 - With the reaper paused after observing a stale pending row and before its kill, a retry on the same row blocks on `settle_lock`; once the reaper resumes it terminates and fails the old attempt, the retry then bumps the generation and creates its session, and that session is never killed. With the order reversed (retry completes its bump and session creation first, reaper then acquires the lock), the reaper re-reads the row, sees the newer generation, and kills nothing. test: `tests/agents/test_spawn_executor.py::test_reaper_and_retry_serialize_on_settle_lock`.
- 2.5.9 - After 500 terminals are spawned, settled, and reaped, `TerminalManager`'s settle-lock map is empty; while a settlement holds the lock with two coroutines queued behind it, the map holds exactly one cell and all three coroutines hold the same lock object; and cancelling one of those queued coroutines *before* it acquires the lock drops exactly its borrow — the survivor still acquires the same lock object, the cancellation propagates, and the entry disappears once the last borrower returns. test: `tests/storage/test_terminals.py::test_settle_lock_cells_are_released_at_zero_borrowers`.
- 2.5.10 - `settle_exit(terminal_id, host_terminal_id)` settles an exit that arrives while the row is still `pending`, and neither ordering can record a dead process `live`: with the exit delivered before the commit reply the row ends `exited` on the `process->>'host_terminal_id'` guard and the subsequent `promote_to_live` finds no `pending` row and reports a spawn that started and exited; with the promotion first the row goes `live` and the exit takes the `live → exited` branch on the `locator->>'host_terminal_id'` guard; both end `exited` with no reaper pass. An exit naming attempt A's prepared `host_terminal_id` that arrives after a retry has bumped the row to attempt B matches neither guard, returns `None`, and leaves B's row untouched in both the `pending` and the `live` case, and the prepared id reaches the row through `record_process` even when the host reported no pgid. test: `tests/storage/test_terminals.py::test_pending_exit_and_promotion_linearize_to_exited`.
- 2.5.11 - Ordinary promotion holds `settle_lock` for its CAS and no longer: with the reaper paused after observing a stale `pending` row and before terminating its resource, a concurrent `_promote_prepared` for the same row blocks on the lock, so the reaper's terminate-and-fail completes against the attempt it observed and the promotion then finds no `pending` row; with the promotion first, the reaper acquires the lock afterwards, re-reads a `live` row, and kills nothing. Neither schedule leaves a killed session recorded `live`. The span excludes the remote round trips: with `commit_spawn` paused for longer than the reaper's poll interval, the lock is unheld — the reaper acquires it, settles a *different* stale row, and an exit for this terminal settles without waiting on the commit — and the promotion acquires it only after the commit returns. A timeout done-callback that reaches promotion completes rather than deadlocking, because it delegates to the one helper that acquires the lock instead of acquiring it first. The web ingress is held to the same contract: with the reaper paused after observing a stale `pending` row created by `terminal_create`, a concurrent `spawn_web_terminal` promotion for that row blocks on the lock and both orderings end with no killed resource recorded `live`; and a web-created native row carries its prepared `host_terminal_id` in `process` before commit, so an exit delivered while it is still `pending` settles it `exited`. test: `tests/agents/test_spawn_executor.py::test_ordinary_promotion_and_reaper_serialize_on_settle_lock` and `tests/servers/test_terminal_ws_create.py::test_web_spawn_settles_promotion_under_the_lock`.
- 2.5.12 - The attempt identity in `process` is neither retained past its attempt nor erased by a later writer, and dropping it does not drop the predecessor's cleanup record. `bump_attempt_generation` deletes only the `host_terminal_id` key in the same UPDATE, so an exit naming attempt A's `host_terminal_id` that arrives after B's bump but *before* B's own `record_process` matches no guard and leaves the row `pending` for B; once B records its own id, B's exit settles the row and A's still does not. In that same gap the row still carries A's `{pgid, start_time}`, so `handle_host_death` — which reads `list_live_by_machine`, a query that includes pending rows — reaps A's process group instead of finding a null `process`, and a row that never recorded a process is skipped rather than raising. `record_process` raises on a mapping without `host_terminal_id` and merges rather than overwrites, so a `pgid` a host event recorded first survives it and an exit delivered immediately after either write still settles the row `exited`. test: `tests/storage/test_terminals.py::test_attempt_identity_is_cleared_on_bump_and_never_erased`.
- 2.5.13 - No superseded attempt can restore the discriminator, and no secondary writer can mint or erase one. `record_process` carries the attempt CAS: attempt A captures its pair, B bumps the row, A's late `record_process` matches no row and returns `None`, the row still advertises no `host_terminal_id`, and A's late exit settles nothing; B's own `record_process` on its captured pair succeeds and B's exit then settles the row. `merge_process_reap_record` writes only `pgid` and `start_time` — called with a mapping carrying `host_terminal_id` it rejects the extra key rather than writing it, and called after `record_process` it leaves the discriminator intact — so `handle_spawn_prepared` and `reconcile_host_inventory` can be replayed in any order around a bump without ever changing which attempt an exit settles. test: `tests/storage/test_terminals.py::test_process_writers_are_attempt_scoped`.
- 2.5.14 - No `spawn_web_terminal` failure branch abandons its shielded prepare. For each of timeout, cancellation, bind failure, `CommitSpawnRefusedError`, and lost CAS, the prepare completes *after* the branch returns and the attempt-owned done-callback terminates exactly the resource that attempt created — the prepared `host_terminal_id` for native, the `spawn_key` session for tmux — leaving no host slot in `list` and no live session, and the row `failed` through `fail_pending_attempt`; with the row instead re-attempted and live on a newer generation, the same late callback kills only its own captured resource and leaves the newer attempt untouched. test: `tests/servers/test_terminal_ws_create.py::test_web_spawn_settles_its_late_prepare`.
- 2.5.15 - The reaper's native kill survives an unreachable host without losing rows or resources: with a real `NativeTerminalRuntime` whose client raises `HostUnavailableError`, the stale row stays `pending` and unfailed, the sweep continues and settles the next stale row in the same pass, and a later sweep against a reachable host terminates and fails it; with the client raising `HostManagerStopped`, the sweep returns immediately, logs no error, and leaves every remaining row untouched. test: `tests/agents/test_spawn_executor.py::test_reaper_native_kill_failure_outcomes`.

### 2.6 Host control safety: observer slots, control-line cap, snapshot boundaries, reap spin [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/state.rs`
- `crates/gterminal/src/host/native_ops.rs`
- `crates/gterminal/src/host/mod.rs`
- `crates/gterminal/src/host/control.rs`
- `crates/gterminal/src/host/frames.rs`
- `crates/gterminal/src/host/embed.rs`
- `crates/gterminal/src/host/poll.rs`
- `crates/gterminal/tests/control_protocol.rs`
- `crates/gterminal/tests/embed.rs`
- `crates/gterminal/tests/carve_guard.rs`

Closes D-7, D-8, D-9, D-10, and performs the `host/state.rs` decomposition that 2.7,
2.9, and 8.1 depend on.

- Decomposition first: `host/state.rs` is 977 lines and this leaf, 2.7, 2.9, and 8.1
  all add to it. Before any behaviour change, move the per-terminal verbs
  (`HostState::kill`, `write`, `write_paste`, `resize`, `snapshot`, the `kill_group`
  helper introduced below) into `crates/gterminal/src/host/native_ops.rs` as
  `impl HostState` blocks, register the module in `crates/gterminal/src/host/mod.rs`, and leave `state.rs`
  with the state model, spawn/observer bookkeeping, and `broadcast_frames`. Target
  sizes after this leaf: `state.rs` ≤ 700, `native_ops.rs` ≤ 350. 8.1 later moves the
  queue accounting into `host/backpressure.rs`. Add a ceiling test to
  `tests/carve_guard.rs` (`no_production_source_reaches_1000_lines`) that walks
  `crates/gterminal/src` and `crates/gclient/src`, skipping files with the
  `// generated: bindgen` header, so every later leaf's G run measures it.
- D-7: `embed.rs` inserts tmux observer slots with `pgid: 0`; a control `kill`
  on that `host_terminal_id` reaches `libc::killpg(0, SIGTERM)` in `HostState::kill`
  and kills the host's own process group; `write` on the same slot answers
  `{"ok":true,"written":true}` without writing. In `HostState::kill`, `write`,
  `write_paste`, `resize`, and `snapshot`, refuse slots with `locator.is_some()` (tmux
  observers) with a typed error `not_native`. Introduce one `fn kill_group(pgid: i32, sig)`
  helper that returns `Err` for `pgid <= 0` and is the only `killpg` call site.
- D-8: `control.rs` uses `BufReader::lines()` and checks `line.len() >= 2 MiB` only
  after buffering the whole line. Read with `read_until(b'\n')` into a buffer capped at
  `MAX_CONTROL_LINE + 1`; on overflow send `{"ok":false,"error":"control_overflow"}` and
  close the connection.
- D-9: `HostState::snapshot` truncates history with `joined[overflow..]` on a `String`,
  which panics off a char boundary and kills the connection task mid-snapshot (the
  capture-before-kill path). Reuse the boundary walk already in
  `poll.rs::truncate_attach_history`; expose it as `pub(crate) fn trim_to_char_boundary`.
- D-10: after `reap_observer` drops an attachment's `tx`, `frames.rs` `recv_opt`
  returns `None` forever and the `select!` loop spins at 100 % CPU until the peer leaves.
  On `None`, emit the typed close (`terminal_exited` or `observer_released`) and break.

Tests are real host tests through `tests/host_support` (a running `gterm host` with a
tmux observer slot and a native terminal): kill/write/resize/snapshot on the observer
slot return `not_native` and the host's `ping` still answers; a 3 MiB line without
newline gets `control_overflow` and the host survives; a snapshot of multibyte history
over the cap returns a valid UTF-8 string; after reap the frame task exits within one
tick (assert via the host's `/proc`-free CPU proxy: the connection closes with the typed
frame within 100 ms).

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out; groups 3, 4, 7 mandatory here).

**Acceptance:**

- 2.6.1 - Control `kill`, `write`, `write_paste`, `resize`, and `snapshot` against a tmux observer slot return `not_native`, the host process group is untouched, and `ping` keeps answering. test: `crates/gterminal/tests/control_protocol.rs::observer_slots_refuse_native_only_verbs`.
- 2.6.2 - `killpg` is reachable only through a helper that refuses `pgid <= 0`. symbol: `kill_group`.
- 2.6.3 - A control peer that sends more than 2 MiB without a newline receives `control_overflow` and is disconnected with bounded memory. test: `crates/gterminal/tests/control_protocol.rs::control_line_overflow_closes_without_buffering`.
- 2.6.4 - Snapshot truncation of multibyte scrollback never panics and returns valid UTF-8 within the byte cap. test: `crates/gterminal/tests/control_protocol.rs::snapshot_truncates_on_char_boundaries`.
- 2.6.5 - After an observer is reaped its frame connection receives a typed close and the task exits instead of spinning. test: `crates/gterminal/tests/embed.rs::reaped_observer_frame_task_exits`.
- 2.6.6 - `host/state.rs` is under 700 lines with the per-terminal verbs living in `host/native_ops.rs`, and the ceiling test fails when any hand-maintained file under either crate reaches 1,000 lines. test: `crates/gterminal/tests/carve_guard.rs::no_production_source_reaches_1000_lines`.

### 2.7 Prepared-spawn commit barrier and exec verification [category: code] (depends: 2.5, 2.6)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/spawn.rs`
- `crates/gterminal/src/host/state.rs`
- `crates/gterminal/src/host/gate.rs`
- `crates/gterminal/src/host/mod.rs`
- `crates/gterminal/src/bin/gterm.rs`
- `src/gobby/terminals/host_client.py`
- `src/gobby/terminals/native_runtime.py`
- `src/gobby/config/terminal_host.py`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated wholesale for the new field
- `crates/gterminal/tests/host_lifecycle.rs`
- `tests/terminals/test_native_runtime.py`
- `tests/config/test_terminal_host.py`
- `tests/config/test_terminal_host_config.py`
- `src/gobby/terminals/host_control.py`
- `src/gobby/terminals/host_manager.py`
- `tests/terminals/host_fakes.py`
- `tests/terminals/test_host_manager.py`

Closes D-6 and the missing `commit_deadline_ms` on the daemon side. Depends on 2.5 as
well as 2.6 because it edits `native_runtime.py` and `test_native_runtime.py`, which
the 2.4/2.5 chain also owns; the two P2 chains join here and 2.8/2.9 stay behind it.

`spawn_commit` holds the `inner` mutex across a blocking `open(O_WRONLY)` of the gate
FIFO; if the child died before opening, the host wedges while lock-free `ping` reports
healthy. The source-plan step "host dies → pipe closes → child exits" is false for a
named FIFO (the child has no inherited fd; its blocked `open()` never returns — the
leaked `/bin/sh -c read _ < "$1"` children prove it). Nothing verifies exec after commit,
and `commit_deadline_ms` is never sent by `host_client.py` / `native_runtime.py`, so the
30 s default always applies.

- Replace the FIFO gate with two inherited pipes, both created by the host, and
  replace the `/bin/sh` prepared wrapper with a native gate helper: the PTY child the
  host spawns is `gterm gate -- <argv…>` (the host's own binary via
  `std::env::current_exe()`, a new `gate` subcommand dispatched from
  `crates/gterminal/src/bin/gterm.rs` and implemented in
  `crates/gterminal/src/host/gate.rs`). A shell cannot hold fd 4 open across its own
  `exec` while closing it on the target's — with `FD_CLOEXEC` set before `/bin/sh`
  starts the shell's exec closes it (false-success EOF); without it the target inherits
  it (EOF only at target exit, so every commit times out). The helper owns both
  lifetimes explicitly:
  - **gate pipe** — the helper inherits the read end as fd 3; the host keeps the write
    end. The helper blocks on `read(3)`; host death closes the write end → EOF → the
    helper exits 0 without exec'ing. Commit writes one byte to the write end *outside*
    the state lock, bounded by `commit_deadline_ms` (default 30 000).
  - **exec-status pipe** — the helper inherits the write end as fd 4 *without*
    `FD_CLOEXEC` (so it survives the host→helper exec); the host keeps the read end.
    After the gate byte arrives the helper sets `FD_CLOEXEC` on fd 4 with `fcntl` and
    calls `execvp(argv[0], argv)`. Success replaces the image, the kernel closes fd 4,
    and the host reads EOF. Any death of the helper after the gate byte closes fd 4 the
    same way, so EOF alone does not prove replacement, and the host cannot tell the two
    apart after the fact: on a successful `execvp` of a short-lived target the process
    may already be gone by the time the host wakes, so gating promotion on "the child is
    still running" would report `gterm gate -- /bin/true` as `exec_failed`. The
    ambiguity is therefore narrowed at its source: **no pre-exec exit path the helper
    can take deliberately is silent on fd 4**. Every fallible step between the gate byte
    and the `execvp` (argv/`CString` marshalling, the `fcntl`, any allocation) writes a
    record and exits 127 instead of unwinding, and `gate.rs` installs a panic hook that
    writes `panic <argv0>\n` to fd 4 before the process dies (the workspace uses the
    default unwind strategy, so the hook runs), which turns every helper *defect* into a
    typed `exec_failed`. Two residues stay, and the plan names them rather than
    pretending they do not exist: `abort()` — a double panic, an allocation abort, a
    `SIGABRT` — and a fatal signal such as `SIGKILL` both terminate the process without
    running any hook, so each still presents as a bare EOF. Both are reported as a spawn
    that started and exited, which is observationally identical to the target being
    aborted or signalled immediately after a successful exec. That report is only honest
    if the row actually reaches `exited`, which is the settlement 2.5 owns (2.5.10): the
    exit and the promotion are linearized, so neither residue can leave a dead process
    recorded `live`. On an `execvp`
    failure the helper writes one record `<errno-name> <argv0>\n` (`ENOENT`, `EACCES`,
    `ENOEXEC`, `E2BIG`, …, bounded to 512 bytes) to fd 4 and exits 127. The host reads
    the status pipe after the gate write with the same deadline: EOF within the
    deadline → commit succeeds and the row may be promoted; a status record → SIGKILL
    the group, reap, and answer
    `{"ok":false,"error":"exec_failed","code":"<errno-name>","detail":"<argv0>"}`;
    neither within the deadline → SIGKILL, reap, `{"ok":false,"error":"exec_timeout"}`.
    A record that is malformed or truncated is reported as `exec_failed` with
    `code: "malformed_status"`. Arrival at the `execvp` call never counts as success.
- `host_client.spawn_commit(..., commit_deadline_ms)` and
  `NativeTerminalRuntime.commit_spawn` pass `commit_deadline_ms` from
  `TerminalHostConfig.commit_deadline_ms` (new field, `int`, default 30 000, `ge=1000`).
  The field is a config-registry carrier: add it to the Pydantic model, regenerate
  `runtime_config_contract.json` (G compares it byte-for-byte), and extend
  `tests/config/test_terminal_host.py` / `test_terminal_host_config.py` with the default,
  the bound, and the forwarded scalar.
- `PreparedChild::drop` kills the group if never committed (already true; keep and
  test).

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out; groups 3, 4, 7 mandatory here); `pgrep -fl 'read _ <'` is empty after the host suite.

**Acceptance:**

- 2.7.1 - Killing the host while a prepared child is waiting makes the child exit within 1 s (pipe EOF), with no blocked `read` child left behind. test: `crates/gterminal/tests/host_lifecycle.rs::host_death_releases_prepared_child`.
- 2.7.2 - A prepared child that dies before commit does not wedge the host: `spawn_commit` returns a typed error within the deadline and `ping` answers throughout. test: `crates/gterminal/tests/host_lifecycle.rs::commit_after_child_death_is_typed_not_wedged`.
- 2.7.3 - Commit succeeds only on exec-status EOF: a prepared command naming a nonexistent binary is reported `exec_failed` with `code="ENOENT"`, a non-executable file `code="EACCES"`, a file with an invalid image `code="ENOEXEC"`, and a helper that cannot reach `execvp` (the test `SIGSTOP`s the prepared child before committing with a 500 ms deadline) `exec_timeout`; no EOF is observed before the gate byte is written, and in all four the group is dead afterwards. test: `crates/gterminal/tests/host_lifecycle.rs::commit_proves_exec_or_reports_failure`.
- 2.7.4 - The daemon sends `commit_deadline_ms` from configuration on every `spawn_commit`, the field round-trips through the config registry, and the checked-in runtime config contract carries it. test: `tests/terminals/test_native_runtime.py::test_commit_spawn_sends_configured_deadline`.
- 2.7.5 - `TerminalHostConfig.commit_deadline_ms` defaults to 30 000, rejects values under 1 000, and appears in `runtime_config_contract.json`. test: `tests/config/test_terminal_host.py::test_commit_deadline_ms_default_and_bounds`.
- 2.7.6 - Every pre-exec exit path the helper can take deliberately reports itself on fd 4, so none is mistaken for a successful commit: with a fault injected after the gate byte and before `execvp` (a marshalling failure and a forced panic, each selected by a test-only environment variable read once at helper startup), `spawn_commit` answers `exec_failed` with the injected code, the group is reaped, and no row is promoted to `live`. The two residues are asserted as residues, not as silence: a helper killed by `abort()` and one killed by `SIGKILL` before `execvp` both present as bare EOF and are accepted at the commit boundary, and the host reports each as a successful commit followed by an exit rather than as a wedge or a typed exec failure. The same test asserts the converse, that `gterm gate -- /bin/true` — whose target exits before the host observes EOF — still returns a successful commit. Every clause here is observed at fd 4 and the host commit boundary, which is all a host-only test can see; the daemon-side row settlement these three cases must produce is 2.9's to prove (2.9.13), because a Rust host test has no `TerminalManager` row to read. test: `crates/gterminal/tests/host_lifecycle.rs::pre_exec_death_is_typed_and_instant_exit_still_commits`.
- 2.7.7 - `HostControlClient` and `TerminalHostManager` forward a non-default configured `commit_deadline_ms` on every `spawn_commit`, and the fake records the same scalar. test: `tests/terminals/test_host_manager.py::test_spawn_commit_forwards_configured_deadline`.

### 2.8 Host process hygiene and shutdown drain [category: code] (depends: 2.7)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/mod.rs`
- `crates/gterminal/src/host/control.rs`
- `crates/gterminal/src/host/embed.rs`
- `crates/gterminal/tests/host_support/mod.rs`
- `crates/gterminal/tests/host_lifecycle.rs`
- `src/gobby/terminals/host_manager.py`
- `tests/terminals/test_runtime_contract.py`
- `tests/e2e/test_external_terminal_attach.py`
- `tests/e2e/conftest.py::*` — scope-reason: `terminate_process_tree` fallback gains a ppid scan
- `tests/terminals/test_host_manager.py`
- `tests/terminals/conftest.py`
- `crates/gterminal/tests/control_protocol.rs`

Closes D-5, D-11, E-7.

- D-5: the host exits only on `host_shutdown` or SIGTERM; nothing notices its socket
  directory being removed, so an unreachable host runs forever (24 leaked hosts on the
  review machine). In `run()`, on the existing 30 ms ticker, `stat` `control_path`; when
  the inode is gone, drain-exit.
- D-11: `host_shutdown` ignores `grace_ms` (`let _grace = …`), `run()` aborts tasks,
  unlinks, sleeps 150 ms, and PTY groups die only through `PaneRuntime::drop`;
  `AttachTerminal` is still accepted while draining; the control `"attach"` verb exists
  only so `host_lifecycle.rs` can observe `host_draining`. Implement an explicit drain:
  set `draining`, refuse new `attach`/`spawn`/`spawn_prepared` with `host_draining`,
  SIGHUP every native group, wait `grace_ms`, SIGKILL survivors, close frame
  connections with `host_draining`, then exit. Delete the fake control `"attach"` verb;
  `embed::attach_frame` checks `draining`. The 3.1.22 test spawns a real terminal before
  shutting down.
- E-7 (daemon): `TerminalHostManager._host_shutdown` sends the RPC then returns even
  when the process is alive. New sequence: RPC with `grace_ms` → wait ≤ grace+1 s for
  the pid to exit → SIGTERM → wait 2 s → SIGKILL, for adopted and self-spawned hosts
  alike (pid from the pidfile).
- E-7 (tests): `host_support::spawn_host` returns a `HostProc`-style RAII guard (copy
  `tests/embed_support/mod.rs` `HostProc`) so panics cannot leak hosts;
  `test_runtime_contract.py` and `test_external_terminal_attach.py` register a
  pidfile-based finalizer that SIGKILLs the host group; `tests/e2e/conftest.py`
  `terminate_process_tree` macOS fallback kills children by ppid scan instead of
  `killpg` on the daemon's group (the host is started with `start_new_session=True`).

Leak accounting is scoped to test-owned hosts. Every host a fixture starts writes its
pid into a pidfile under the fixture's state directory; the session-scoped autouse
fixture in `tests/terminals/conftest.py` snapshots the `gterm host` PID set at session
start, collects every pidfile the session created, and at session end asserts (a) each
pidfile's pid is gone and (b) the live PID set minus the start snapshot is empty — i.e.
no host the session started survives, while hosts that predate the session (the
leaked review-machine hosts) are ignored. The Rust `HostProc` guard does the same
per-test through its `Drop`. G group 7 is this check, never a global `pgrep` assertion.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out); group 7 (test-owned leak check) is mandatory and is also asserted by the session-scoped autouse fixture in `tests/terminals/conftest.py`.

**Acceptance:**

- 2.8.1 - Removing the host's socket directory makes it exit within 200 ms. test: `crates/gterminal/tests/host_lifecycle.rs::host_exits_when_socket_dir_disappears`.
- 2.8.2 - `host_shutdown{grace_ms}` refuses new attaches and spawns with `host_draining`, SIGHUPs live terminals, SIGKILLs survivors after the grace, and is idempotent; the test spawns a real terminal first. test: `crates/gterminal/tests/host_lifecycle.rs::host_shutdown_drains_and_is_idempotent`.
- 2.8.3 - The control protocol has no `attach` verb. test: `crates/gterminal/tests/control_protocol.rs::attach_is_not_a_control_verb`.
- 2.8.4 - `TerminalHostManager.stop()` leaves no host process for adopted or self-spawned hosts, escalating RPC → SIGTERM → SIGKILL. test: `tests/terminals/test_host_manager.py::test_stop_escalates_to_sigkill`.
- 2.8.5 - Every host-starting Rust and Python fixture is RAII/finalizer based; the session-scoped leak check passes on a machine that has unrelated `gterm host` processes running before the session starts, and fails when a fixture deliberately leaks one (mutation recorded in the leaf's evidence). test: `tests/terminals/conftest.py::test_owned_host_leak_check_ignores_preexisting_hosts`.

### 2.9 Host client correctness: reconnect, event delivery, blocking sleeps [category: code] (depends: 2.8)
`kind: deliverable`

Targets:
- `src/gobby/terminals/host_client.py`
- `src/gobby/terminals/native_runtime.py`
- `src/gobby/terminals/host_manager.py`
- `src/gobby/terminals/host_reap.py`
- `src/gobby/terminals/host_reconcile.py`
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: stops poking the runtime's private `_frame_host_epoch`; the terminal-host subsystem wiring moves out to keep the file under the ceiling
- `src/gobby/runner_lifecycle_terminal_host.py`
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: same private-attribute poke
- `src/gobby/terminals/tmux_runtime.py`
- `crates/gterminal/src/host/control.rs`
- `crates/gterminal/src/host/state.rs`
- `crates/gterminal/src/host/embed.rs`
- `crates/gterminal/src/pane/runtime.rs`
- `crates/gterminal/src/protocol/wire/tests.rs`
- `crates/gterminal/tests/embed.rs`
- `tests/terminals/test_wire_golden.py`
- `crates/gterminal/tests/fixtures/wire_golden/control_hello.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_host_shutdown.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_kill.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_list.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_ping.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_release_observer.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_reserve_observer.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_resize.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_snapshot.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_spawn_commit.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_spawn_prepared.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_spawn.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_subscribe_events.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_write_paste_off.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_write_paste_on.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_write.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_event_terminal_exited.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_event_overflow.json`
- `tests/terminals/test_native_runtime.py`
- `tests/terminals/test_host_manager.py`
- `tests/terminals/test_runtime_contract.py`
- `tests/terminals/test_host_client.py`
- `crates/gterminal/tests/wire_golden.rs`

Closes E-4, E-5, E-6, E-11, E-12, E-13, E-14.

Control-line wire shape: every control request gains `"id": <u64>` and every response
echoes it; pushed events carry `"event": "<name>"` and `"seq": <u64>` with no `id`. The
Rust and Python byte-exact goldens under `crates/gterminal/tests/fixtures/wire_golden/`
are regenerated for every `control_*.json` fixture in the same leaf (the two new
event fixtures are added) and `protocol/wire/tests.rs` and `test_wire_golden.py` assert
the new bytes. Regeneration happens where the corpus is written:
`crates/gterminal/tests/wire_golden.rs` is the generator, and its `write_json` helper
writes a fixture when the file is empty and byte-compares it otherwise, so every
`id`-bearing request shape and both new event fixtures are authored there and the
checked-in files follow from it rather than being hand-edited — the regeneration test
stays the authority over the corpus, which is why editing the JSON alone would leave
the leaf red.
Both sides pin the id semantics: a request without `id` is refused
`missing_id`; a response whose `id` matches no outstanding request is logged and
dropped by the client; a duplicate in-flight `id` is refused `duplicate_id`; responses
to interleaved requests are matched by `id`, never by arrival order (test issues `ping`
and `list` back-to-back and asserts each future resolves with its own payload).

Connection ownership on the daemon side: `HostClient` owns exactly one reader task per
control connection; it is the only code that reads the socket. Requests take a
write-only lock for the duration of one line write, allocate `id` from a monotonic
per-connection counter that is never reused, and register a future in an id → future
map before the write. The reader resolves the future whose `id` matches, logs and drops
an unmatched `id`, and never parses a pushed event (events arrive only on the dedicated
event connection below). On EOF, socket error, deadline, `reconnect`, cancellation, or
`close`, the reader fails every pending future atomically with a typed
`HostConnectionLost(reason)` and clears the map before the new connection accepts
requests, so a request can never resolve against a reply from a previous connection.
`FakeHostClient` implements the same ownership so the contract harness exercises it.
The reader's bound is the protocol's bound: every `asyncio.open_unix_connection` the
client makes — the initial control connection, every reconnect replacement, and the
event stream — passes `limit=MAX_CONTROL_LINE + 1`, because asyncio's default
`StreamReader` limit is 64 KiB and a valid `list` reply (up to 188 terminals with
1,024-byte titles) sits well above it while staying under the 2 MiB cap; the
`LimitOverrunError` branch of `read_payload` then fires only for lines the host
would also have refused. A new `tests/terminals/test_host_client.py` drives
`HostClient` against an in-process `asyncio.start_unix_server` fixture.

- E-4: `HostClient.reconnect(self, socket_path, expected_epoch=None)` requires a path,
  but `NativeTerminalRuntime.resize` / `terminate` / `reconnect` call `reconnect()` with
  no args through `HostManagerControl.__getattr__` — a `TypeError` on a real host; the
  suite passes only because `FakeHostClient.reconnect(self)` is no-arg. Add one
  `NativeTerminalRuntime._reconnect()` that passes `control_socket_path(self._socket_dir())`
  and the expected epoch; give `FakeHostClient.reconnect` the real signature.
- E-5: `subscribe_events` is issued on the shared control connection with no reader and
  no request-id correlation; the host pushes events on the same writer as responses and
  `read_payload` would consume a pushed event as the next response. Today
  `HostState.event_subs` is never drained, so no exit event is delivered and
  finalization relies on the periodic sweep. Implement live delivery properly: the
  daemon opens a dedicated control connection for events (`HostClient.open_event_stream()`
  returning an async iterator), `HostState::subscribe_events` registers that connection's
  writer, `emit_exit` / `emit_code` push to every subscriber with byte-bounded queues
  (`EVENT_QUEUE_BYTES`) and close-with-`event_overflow` on overflow, and the daemon's
  reader task calls `terminal_manager.settle_exit(terminal_id, host_terminal_id)` on
  `terminal_exited`. Add `id`
  correlation to every request/response on `HostClient` so a stray push can never be
  mistaken for a response. Remove the `runtime._subscribed = True` override from the
  contract harness.

  The pushed `terminal_exited` carries `terminal_id` (from the slot's `Identity`),
  `host_terminal_id`, and `exit_code` — and nothing else — because the daemon-side
  settlement (2.5) needs the row id to find the row and the host id to tell one attempt
  from another on a row a retry reuses, and consumes no third identity. The frame-level
  `TerminalExited` keeps its existing `{host_terminal_id, exit_code}` shape; this is the
  control-plane event, pinned separately by `control_event_terminal_exited.json`. The
  reader takes no lock: `settle_exit` is a guarded CAS, and any 4.1 lease cleanup the
  exit triggers runs after it returns.

  The control push is native-only, and it needs its own producer rather than a branch
  inside the tmux one. `emit_exit` lives in `host/embed.rs` and is not a general exit
  publisher: its only callers are the tmux poll loop (`embed.rs:370` on a dead pane and
  `embed.rs:407` on `PollClass::ConfirmedAbsence`), and it selects the slot with
  `slot.locator.as_ref().is_some_and(|l| l.locator_key() == key)`, so a native slot —
  which carries `locator: None` — is excluded by the lookup itself. A native-only
  predicate placed inside `emit_exit` is therefore dead code that publishes nothing while
  suppressing every reachable call, and the daemon would still be left with no native
  exit event at all. `emit_exit` stays exactly as it is: tmux-only, user-attachment-only,
  frame-level.

  The native producer hangs off the exit the host already observes and discards.
  `PaneRuntime` spawns a reaper thread that calls `child.wait()` and logs the status
  (`crates/gterminal/src/pane/runtime.rs:358-375`) before setting its
  `child_wait_completed` flag. That thread is the child-exit hook: `PaneRuntime` gains a
  completion signal beside that flag carrying the observed `Option<i32>` exit code, and
  `HostState::commit` (`host/state.rs:483-511`) — the one place a native slot becomes
  live, holding both the slot's `Identity` and its `host_terminal_id` — installs exactly
  one watcher per committed slot. The watcher publishes `{terminal_id, host_terminal_id,
  exit_code}` to every control subscriber and is the sole producer of the control-plane
  `terminal_exited`. One watcher per commit is what keeps it exactly-once: an
  uncommitted prepared slot has no watcher (its expiry path already reaps it), a killed
  slot is removed from `terminals` with its watcher, and a slot cannot be committed
  twice — `commit` returns early when `commit_state == Committed`.

  Because the producer is the native commit path, the identity domain is native by
  construction: `terminal_id` is the row UUID the daemon minted, never the
  `tmux:<socket>:<pane>` string `attach_tmux` puts in an observer slot's `Identity`. A
  tmux pane close reaches no control subscriber, so `settle_exit`'s `UUID()` parse can
  never see a locator key and `_event_reader_loop` cannot raise on an ordinary pane
  close. That is what makes 2.5's "tmux exit recovery never reaches `settle_exit`" true
  rather than assumed, and it leaves the daemon reader one identity domain to decode.

  The event stream is lossy and bounded, so recovery is owned by
  `TerminalHostManager`, not left to whoever notices: `_event_reader_loop` runs for the
  manager's lifetime; every pushed event carries a host-monotonic `seq` and the host's
  `epoch`, the reader persists the cursor `(last_event_epoch, last_event_seq)` on the
  manager — a sequence is meaningful only inside the epoch that minted it. On overflow
  close, EOF, connection error, or an `epoch` that differs from `host_epoch`, the reader
  (1) reconnects with the 2.4 backoff and re-authenticates (`hello`) — when the host
  process itself is gone the reader joins the manager's singleflight `ensure_restart()`
  (2.4) and resumes on the epoch that task publishes, never spawning or rotating
  tokens itself, and a `HostManagerStopped` from that call ends the loop cleanly: the
  daemon is shutting down, so the reader stops reconnecting, leaves the cursor as it
  stands, and logs nothing as an error, (2) when the
  host's epoch equals `last_event_epoch`, re-subscribes with `{"since": last_event_seq}`
  — the host replays from its ring buffer when it still holds that seq and otherwise
  answers `{"gap": true}`, (3) on a gap or an epoch change calls `list` and runs
  `reconcile_host_inventory` against the authoritative inventory (rows present in the
  DB but absent from `list` → `mark_exited`; rows listed live → refresh epoch/locator),
  then — after a same-epoch gap and after an epoch change alike — resets the cursor
  to `(epoch_of_the_list_reply, seq_of_the_list_reply)`: every
  `list` reply carries `{"epoch", "seq"}` taken under the host's event lock, so the
  list is the authoritative cut (keeping the evicted
  `last_event_seq` would make every re-subscribe answer `gap` again and loop),
  (4) resumes live delivery. The cut is replayable by construction because the
  subscription is already registered when it is taken: a `subscribe_events` whose
  `since` the ring has evicted answers `{"gap": true}` *and still registers the
  subscriber* from the host's current sequence under the event lock (the same for
  a subscribe without `since` after an epoch change), so the reader is subscribed
  before it calls `list`, buffers every event that arrives while the `list` request
  is in flight, applies the list as the authoritative inventory, drops the buffered
  events with `seq <= list.seq` (already reflected in the list), and delivers the
  rest in order. Listing after subscribing means no event emitted after the cut can
  be missed whatever the churn rate, and the ring buffer's capacity never enters
  the recovery path — a list-then-subscribe order would reopen the window in which
  events emitted between the two calls evict the cut. Within the current epoch,
  events with `seq <= last_event_seq` are dropped as duplicates; an event whose `epoch`
  differs from `last_event_epoch` is never compared by `seq`: an older epoch is
  dropped, a newer one triggers step (3). The periodic sweep remains as a backstop;
  the reader is the primary path.
- Lost commit reply (2.4's `CommitTransportError(request_written=True)` row): the
  same reconnect + `list` path settles a `commit_indeterminate` pending row. After
  reconnecting, look the prepared `host_terminal_id` up in `list` by `spawn_key` /
  terminal id. Listed and live → promote the row with the listed locator (`live`);
  listed but still prepared (the commit never arrived) → `kill` the prepared slot by
  `host_terminal_id` and `fail_pending_attempt` with `commit_indeterminate` resolved
  to `not_committed`; absent → `fail_pending_attempt` resolved to `not_found`; host
  unreachable after the 2.4 backoff budget (five attempts) → leave `pending` for the
  2.5 reaper. Every step is bounded by `commit_deadline_ms` (2.7). The lookup, the
  `kill`, and the promotion or failure all run while holding 2.5's
  `TerminalManager.settle_lock(terminal_id)`, re-reading the row under the lock and
  comparing its `(attempt_generation, attempt_started_at)` with the captured values
  before the kill, and the CAS of 2.5 is applied on top, so a retry attempt that
  replaced the row in the meantime is never touched and never has its slot killed.
- E-6: `host_reap.py::reap_recorded_process` blocks the loop with `time.sleep` up to
  `shutdown_grace_seconds` per row (called from `async handle_host_death`);
  `host_reconcile.py` awaits `unknown_grace_seconds` per unknown host row inside the
  health-loop reconcile. Run the reap in `asyncio.to_thread`; wait one grace for the
  whole unknown set.
- E-11: `runner_lifecycle_subsystems.py` and `orchestration.py` poke `_frame_host_epoch`;
  the health-loop reconnect updates `host_epoch` but not the runtime's copy. The runtime
  reads `host_manager.host_epoch` through `HostManagerControl.host_epoch` only; delete
  `_frame_host_epoch` from both runtimes (`tmux_runtime.py` carries the same
  constructor-injected copy for its host-observer locators, and the poke's `hasattr`
  guard hit it too). `runner_lifecycle_subsystems.py` is 890 lines on the branch:
  move the terminal-host subsystem start/stop wiring (the functions that construct
  `TerminalHostManager`, bind the native runtime, and arm the health loop — the code
  that contained the poke) into a new `src/gobby/runner_lifecycle_terminal_host.py`
  in this leaf so the file ends under 800 lines; `orchestration.py` imports the moved
  names from the new module.
- E-12: `is_live` matches on bare `host_terminal_id`; include `host_epoch` in the match.
- E-13: `except ValueError` around `reconcile_host_inventory` swallows storage errors;
  catch only the typed `HostEpochMismatchError` / `ProjectOwnershipConflictError` it is
  meant for.
- E-14: `tests/terminals/test_host_manager.py` has `@pytest.mark.asyncio` on a sync
  `def`; fix.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out); `GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_runtime_contract.py` against a real `gterm` binary.

**Acceptance:**

- 2.9.1 - `resize`, `terminate`, and `reconnect` on a real host survive a host restart by reconnecting with the socket path and epoch; the fake client shares the production signature. test: `tests/terminals/test_runtime_contract.py::test_kill_and_resize_retry_across_reconnect`.
- 2.9.2 - A native terminal exit is delivered to the daemon as a pushed event within one tick and the row is `exited` before any sweep runs; a stray push is never consumed as a response. The event carries exactly `terminal_id`, `host_terminal_id`, and `exit_code` (matching `control_event_terminal_exited.json` byte-for-byte, regenerated from `wire_golden.rs`), and the reader routes it to `settle_exit` with both ids: a target that exits before its commit reply lands — `gterm gate -- /bin/true` against a real host — leaves the row `exited` rather than `live`, and an exit naming a superseded `host_terminal_id` leaves the current attempt's row untouched. test: `tests/terminals/test_runtime_contract.py::test_exit_event_is_delivered_live`.
- 2.9.3 - `handle_host_death` with ten recorded processes never blocks the event loop for longer than one grace period (measured with a loop-lag probe). test: `tests/terminals/test_host_manager.py::test_host_death_reap_does_not_block_loop`.
- 2.9.4 - After a health-loop reconnect the runtime sees the new epoch with no private attribute poke, and `is_live` rejects a recycled `host_terminal_id` from an older epoch. test: `tests/terminals/test_native_runtime.py::test_is_live_requires_matching_epoch`.
- 2.9.5 - A storage `ValueError` inside `reconcile_host_inventory` propagates instead of silently skipping the reconcile. test: `tests/terminals/test_host_manager.py::test_reconcile_storage_error_propagates`.
- 2.9.6 - Every control request carries `id` and responses are matched by it: interleaved `ping`/`list` resolve to their own payloads, a request without `id` is refused `missing_id`, a duplicate in-flight `id` is refused `duplicate_id`, and an unmatched response is dropped; the Rust and Python goldens for every `control_*.json` fixture carry the id field. test: `crates/gterminal/src/protocol/wire/tests.rs::control_ids_correlate_interleaved_requests`.
- 2.9.7 - After an `event_overflow` close or a host restart, the event reader reconnects, re-subscribes from `last_event_seq` within the same epoch, reconciles against `list` when the host reports a gap or a new epoch, and an exit that happened during the outage is reflected on the row; duplicate and stale-epoch events are dropped, and after a restart that resets the host's sequence below the old cursor every new-epoch event is still delivered; with the host's ring buffer already past `last_event_seq` in the same epoch, the reader lists once, persists the list reply's `(epoch, seq)` as its cursor, re-subscribes from that cut without a second `gap`, and delivers the next live event. test: `tests/terminals/test_runtime_contract.py::test_event_stream_recovers_after_overflow_and_restart`.
- 2.9.8 - A `commit_indeterminate` pending row is settled after reconnect from `list`: a listed live PTY promotes it, a listed-but-prepared slot is killed and the row failed with `not_committed`, an absent PTY fails it with `not_found`, and an unreachable host leaves it `pending` for the reaper; a row whose attempt generation changed meanwhile is untouched. test: `tests/terminals/test_runtime_contract.py::test_lost_commit_reply_is_settled_from_list`.
- 2.9.9 - When the host process dies while the event reader is reconnecting, the reader joins the manager's single restart task: one host spawn and one token rotation occur, and the reader resumes on the published epoch. test: `tests/terminals/test_host_manager.py::test_event_reader_joins_singleflight_restart`.
- 2.9.10 - A control reply of 1 MiB (above asyncio's 64 KiB default, below `MAX_CONTROL_LINE`) is decoded by `HostClient` on the initial connection, on a reconnect replacement, and on the event stream, and a line of `MAX_CONTROL_LINE` bytes or more is refused `request_too_large` without buffering past the cap. test: `tests/terminals/test_host_client.py::test_reader_limit_matches_control_line_cap`.
- 2.9.11 - Two reachable schedules for a `commit_indeterminate` row, matching the lock contract in 2.5 (the settlement captures the row and queries `list` before it acquires `settle_lock`): (a) the settlement is paused after its `list` lookup and before it acquires the lock, so a retry acquires the lock, bumps the generation, and creates its slot; the settlement then acquires the lock, re-reads the row, sees the newer generation, and kills nothing, leaving the retry's slot live; (b) the settlement acquires the lock first and holds it while the retry blocks, kills only the prepared slot it looked up, fails the row, and releases, after which the retry proceeds against the failed row. test: `tests/terminals/test_host_manager.py::test_list_reconcile_and_retry_serialize_on_settle_lock`.
- 2.9.12 - With the fake host emitting lifecycle events faster than its ring capacity throughout recovery, a same-epoch gap converges in exactly one subscribe-then-list cycle: the reader ends subscribed, its cursor equals the list reply's `seq`, every event with `seq > list.seq` emitted during the `list` round trip is delivered once and in order, and no second `gap` is answered. test: `tests/terminals/test_host_manager.py::test_gap_recovery_converges_under_ring_churn`.
- 2.9.13 - Every pre-exec residue 2.7 accepts at the host boundary still settles the daemon row, proven against a real `gterm` binary and a real daemon rather than inside the host. Three targets — a helper killed by `abort()` before `execvp`, one killed by `SIGKILL` before `execvp` (both selected by 2.7's test-only environment variable and both presenting as bare EOF), and `gterm gate -- /bin/true`, whose target exits before the host observes EOF — each reach a final `exited` row through the pushed `terminal_exited` and `settle_exit` alone, with no `live` row surviving exit delivery. The oracle is the final state, not the transient one, because 2.5.10 declares both schedules valid: exit-first never records `live`, while promotion-first passes through `live` and takes the `live → exited` branch, so a transient `live` falsifies nothing. Every other writer that can produce the same final state is disabled for the duration, each through a seam that leaves the path under test intact. The health loop's `list` reconciliation — which marks a listed-absent row `exited` on its own interval — is suppressed by the isolated daemon's temporary config setting `health_interval_seconds` beyond the test horizon: `_health_loop` sleeps before its first `reconcile()`, so a long interval means it never runs, and stopping `TerminalHostManager` instead would tear down the event reader this test exists to prove. The stale-pending reaper is disabled by its own switch, independently of the health loop. The event reader is instrumented to count `settle_exit` calls, and exactly one is required per terminal, so a broken push or a broken `settle_exit` fails the assertion instead of being masked by a sweep. After the assertion the test calls `manager.reconcile()` and runs one reaper pass explicitly and asserts each row still `exited`, proving the backstops converge on the same state rather than having produced it. test: `tests/terminals/test_runtime_contract.py::test_preexec_residues_and_instant_exit_settle_the_row`.
- 2.9.14 - When `stop()` is in progress while the event reader is reconnecting, `ensure_restart()` raises `HostManagerStopped`, the reader exits `_event_reader_loop` without reconnecting again, mutates no cursor, spawns no host, and logs no error; the manager's teardown completes with no reader task left running. test: `tests/terminals/test_host_manager.py::test_event_reader_exits_on_host_manager_stopped`.
- 2.9.15 - A committed native slot whose child exits pushes exactly one control-plane `terminal_exited` carrying that slot's row `terminal_id`, its `host_terminal_id`, and the observed `exit_code`, sourced from the `PaneRuntime` child-wait completion rather than the tmux poll loop: a target that exits on its own and one killed by signal both deliver, a prepared-but-uncommitted slot that expires delivers nothing, and killing a committed slot delivers at most one event and never a second after removal. test: `crates/gterminal/tests/embed.rs::native_child_exit_pushes_one_control_event`.
- 2.9.16 - A tmux observer whose pane dies never reaches the control plane: with a control subscriber attached and a tmux slot observed by the host, `PollClass::ConfirmedAbsence` fans the frame-level `TerminalExited` to the slot's user attachments and reaps the observer while the subscriber receives no `terminal_exited` event, so no `tmux:<socket>:<pane>` identity is ever handed to `settle_exit`; the same subscriber still receives the event for a native slot's exit, and the host stays healthy across both. test: `crates/gterminal/tests/embed.rs::tmux_pane_death_pushes_no_control_event`.

## P3: CI, packaging, and installer
`kind: framing`

**Goal**: Every workflow that runs on a push to `0.5.0` can pass, the release workflow
can publish, the lint gates check something, and `gobby install` never runs a 600 s
build chasing a release that does not exist.

### 3.1 Zig-gated vendor build test and crates.io package assertion [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `tests/gterminal/test_vendor_layer.py`
- `.github/workflows/release-gterminal.yml`
- `crates/gterminal/NOTICE.md`
- `crates/gterminal/Cargo.toml`
- `crates/gclient/Cargo.toml`
- `tests/cli/test_install_setup_gterm.py`

Closes A-1, A-2, A-7.

- A-1: `tests/gterminal/test_vendor_layer.py::test_helper_builds_vendored_libghostty_vt`
  is `@pytest.mark.slow`, but `pyproject.toml` `addopts` has no `-m "not slow"` and the
  `ci.yml` `test` job runs all of `tests/` on ubuntu without Zig, so `assert zig` fails
  and the merge gate is red on every push. Where Zig exists the test runs a multi-minute
  Ghostty build inside pytest. Change the test to `pytest.skip("zig not installed")`
  when `shutil.which("zig")` is `None`, and run the real build only when
  `GOBBY_RUN_VENDOR_BUILD=1` is set (same opt-in pattern as `GOBBY_RUN_WHEEL_UI_SMOKE`);
  otherwise it asserts only that the helper script parses and reports its requirement.
- A-2: `release-gterminal.yml` requires `vendor/portable-pty/LICENSE.md` in the
  `cargo package --list` output, but `crates/gterminal/vendor/portable-pty/` has its own
  `Cargo.toml` and cargo always excludes nested packages (verified: 0 portable-pty
  entries, 1232 libghostty entries), so the `test` job exits 1 and build/publish/release
  never run. Drop `vendor/portable-pty/LICENSE.md` from the required list (keep the
  `.patches.md` / `.patch` inputs, which are packaged). The published crate resolves
  upstream `portable-pty 0.9.0`; amend `NOTICE.md` to say the crates.io artifact resolves
  portable-pty from the registry and only the workspace build uses the patched vendored
  copy through `[patch.crates-io]`.
- A-7: both `Cargo.toml`s declare `license` and `license-file`; cargo warns on every
  package/publish. Drop `license-file`.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out); `cargo package -p gobby-terminal --list --no-verify` contains every path the workflow requires; `GOBBY_TEST_PROTECT=1 uv run pytest tests/gterminal` passes with Zig absent from `PATH`.

**Acceptance:**

- 3.1.1 - With no `zig` on `PATH` the vendor-layer suite passes with the build test skipped; with `GOBBY_RUN_VENDOR_BUILD=1` and Zig present it builds. test: `tests/gterminal/test_vendor_layer.py::test_helper_builds_vendored_libghostty_vt`.
- 3.1.2 - Every path the release workflow's package assertion requires is present in `cargo package -p gobby-terminal --list --no-verify`, and the assertion is exercised by a test that runs the same loop against the real listing. test: `tests/cli/test_install_setup_gterm.py::test_release_workflow_package_assertion_matches_cargo_listing`.
- 3.1.3 - `NOTICE.md` states the registry-resolved portable-pty for the crates.io artifact. file: `crates/gterminal/NOTICE.md`.
- 3.1.4 - `cargo package -p gobby-terminal --no-verify` and `cargo package -p gobby-client --no-verify` emit no license warnings. file: `crates/gterminal/Cargo.toml`.

### 3.2 Remove blanket lint allows from `gobby-terminal` [category: code] (depends: P1, 2.6, 2.9)
`kind: deliverable`

Targets:
- `crates/gterminal/src/lib.rs`
- `crates/gterminal/src/pty/mod.rs`
- `crates/gterminal/src/pty/actor.rs`
- `crates/gterminal/src/pty/backend.rs`
- `crates/gterminal/src/kitty_graphics/host_paint.rs`
- `crates/gterminal/src/kitty_graphics/host_stream.rs`
- `crates/gterminal/src/ghostty/mod.rs`
- `crates/gterminal/src/ghostty/bindings.rs`
- `crates/gterminal/src/ghostty/bindings/generated_01.rs`
- `crates/gterminal/src/ghostty/bindings/generated_02.rs`
- `crates/gterminal/src/ghostty/bindings/generated_03.rs`
- `crates/gterminal/src/ghostty/bindings/generated_04.rs`
- `crates/gterminal/src/ghostty/bindings/generated_05.rs`
- `crates/gterminal/src/raw_input.rs`
- `crates/gterminal/src/pane/osc.rs`
- `crates/gterminal/src/host/frames.rs`
- `crates/gterminal/src/input/mod.rs`
- `crates/gterminal/src/input/parse.rs`
- `crates/gterminal/src/input/encode.rs`
- `crates/gterminal/src/input/model.rs`
- `crates/gterminal/src/platform/mod.rs`
- `crates/gterminal/src/platform/fallback.rs`
- `crates/gterminal/src/platform/windows_process.rs`
- `crates/gterminal/tests/carve_guard.rs`
- `crates/gterminal/src/pane/runtime.rs`
- `crates/gterminal/src/platform/macos.rs`
- `crates/gterminal/src/platform/windows.rs`
- `crates/gterminal/src/protocol/wire_types.rs`

Closes A-3 / D-4.

`crates/gterminal/src/lib.rs` carries
`#![allow(dead_code, unused_imports, private_interfaces, clippy::all)]`, so every
`cargo clippy -p gobby-terminal -- -D warnings` step in `rust-ci.yml` and both release
workflows (including the Windows cross-lint) checks nothing. The full inventory of
`allow` sites on the branch (the guard below matches all of them today):

- crate/module-level blanket allows to delete: `lib.rs` line 7; the `pty` module's
  `mod.rs`, `actor.rs`, `backend.rs` line 1; the `kitty_graphics` module's
  `host_paint.rs`, `host_stream.rs` line 1 (`dead_code, unused_imports`); the `ghostty`
  module's `mod.rs` line 1 (`dead_code`) and the `#[allow(…)]` on its `bindings`
  module declaration.
- bindgen output that keeps file-level allows under the generated exemption:
  `bindings.rs` and `generated_01.rs` … `generated_05.rs` under `ghostty/bindings` —
  add the `// generated: bindgen — do not edit` header line as the first line of each;
  the guard exempts exactly files with that header, and nothing else.
- item-level `#[allow(dead_code)]` / `#[allow(unused_imports)]` without a
  `// reason:` to resolve one by one: `raw_input.rs` line 15; `osc.rs` lines 504, 510;
  `frames.rs` lines 265, 270; the `input` module's `mod.rs` line 5, `parse.rs` lines 5,
  12, 50, 63, 242, 252, 330, `encode.rs` lines 13, 62, 77, 95, 118, 170, `model.rs` line
  115; `actor.rs` line 249. Each becomes either deleted code (no path reaches it — the
  carve left it behind), wired code, or a `// reason:` comment on the same line for
  genuinely dormant platform code. The
  `cfg_attr(windows|not(any(unix, test)), allow(dead_code))` forms in the `platform`
  module's `mod.rs` line 141, `fallback.rs` line 97, `windows_process.rs` line 308, and
  `raw_input.rs` line 147 are platform-conditional and keep a `// reason:` too.
- item-level clippy and rustc allows the same guard predicate catches, each of which
  keeps its attribute with a same-line `// reason:` (the guard reads the reason
  marker, never the lint name): `pane/runtime.rs` lines 231, 254, 285, 322
  (`clippy::too_many_arguments` on the four pane-runtime constructors), `platform/macos.rs`
  line 310 (`clippy::unnecessary_cast`; its existing trailing comment becomes the
  `// reason:` form), `platform/windows.rs` lines 339 and 344
  (`non_camel_case_types` on Win32 type aliases), and `protocol/wire_types.rs` line
  609 (`deprecated`).

The line numbers above are the branch's, and this leaf follows 2.9 because 2.9 edits
`pane/runtime.rs` too — it hangs the native exit watcher off the `child.wait()` reaper
thread. The sweep therefore resolves each attribute by its lint and enclosing item
rather than by line, and any allow 2.9 introduces falls inside this leaf's inventory
instead of behind an already-green guard.

Delete the blanket attributes, run
`cargo clippy -p gobby-terminal --all-targets --all-features -- -D warnings` and the
default-feature and Windows-target variants the workflows run
(`cargo clippy -p gobby-terminal --target x86_64-pc-windows-msvc -- -D warnings`), and
fix what surfaces. Extend `tests/carve_guard.rs` with a test that reads every `.rs`
under `crates/gterminal/src` and `crates/gclient/src`, skips files whose first line is
the bindgen header, and fails on any `#![allow(`, any `#[allow(` or `cfg_attr(…, allow(`
without `// reason:` on the same line, and any `clippy::all` anywhere. Run that guard
first (red) and make the inventory above its initial failure list; the leaf closes
when it is green.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out); additionally `cargo clippy -p gobby-terminal --features vt-engine --all-targets -- -D warnings`.

**Acceptance:**

- 3.2.1 - Outside the bindgen-headed files no `#![allow(` attribute exists under `crates/gterminal/src` or `crates/gclient/src`, every item-level or `cfg_attr` `allow` carries a reason, `clippy::all` appears nowhere, and the guard's initial red run listed every site in the inventory above (recorded in the leaf's evidence). test: `crates/gterminal/tests/carve_guard.rs::no_blanket_lint_allows`.
- 3.2.2 - Clippy with `-D warnings` passes for the default feature set, `vt-engine`, `--all-targets`, and the Windows cross-lint target. file: `crates/gterminal/src/lib.rs`.

### 3.3 nextest PTY serialization group and build-env test isolation [category: config] (depends: 3.2, 3.1)
`kind: deliverable`

Targets:
- `.config/nextest.toml`
- `crates/gterminal/tests/build_env.rs`
- `tests/cli/test_install_setup_gterm.py`
- `tests/cli/test_cli_install.py::*` — scope-reason: drops the duplicated managed-binary inventory test

Closes A-6, A-8, A-9, A-10.

- A-6: the `gterm-pty` group filter `test(::pty::) | test(pty::) | test(frame_producer)`
  matches test *names*; the `frame_producer` binary's only test is
  `end_to_end_without_ratatui_frame`, and `host_lifecycle`, `control_protocol`,
  `frame_protocol`, `embed`, and `pane::runtime` tests run at full parallelism against
  real PTYs. Set
  `filter = 'package(gobby-terminal) & (test(/pty::|pane::runtime/) | binary(frame_producer) | binary(host_lifecycle) | binary(control_protocol) | binary(frame_protocol) | binary(embed))'`
  with `max-threads = 1`, and add a nextest `--list` based test in `build_env.rs` that
  asserts each of those binaries resolves into the group.
- A-8/A-9: `default_features_build_invokes_no_zig` is vacuous on a warm target dir (no
  `rerun-if-env-changed=ZIG` on the default path, so the build script is not re-run), and
  `missing_zig_reports_requirement` performs a nested vt-engine rebuild. Use a private
  `CARGO_TARGET_DIR` under the test's tempdir for both probes so the build script runs
  every time, and make the missing-Zig probe invoke only the build script
  (`cargo check -p gobby-terminal --features vt-engine` with `ZIG=/nonexistent`).
- A-10: `tests/cli/test_install_setup_gterm.py` asserts workflow substrings only; 3.1
  already added the listing-driven assertion (3.1.2), so this leaf deletes the substring
  tests it supersedes. `test_managed_native_binary_install_inventory` is duplicated in
  `tests/cli/test_cli_install.py` and `test_install_setup_gterm.py`; keep one.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out); `cargo nextest list -p gobby-terminal --profile ci` shows the group membership.

**Acceptance:**

- 3.3.1 - `host_lifecycle`, `control_protocol`, `frame_protocol`, `embed`, `frame_producer`, and every `pty::` / `pane::runtime` test run in the single-threaded `gterm-pty` group. test: `crates/gterminal/tests/build_env.rs::pty_group_captures_host_binaries`.
- 3.3.2 - `default_features_build_invokes_no_zig` fails when the build script is forced to call Zig on the default path (mutation check recorded in the leaf's evidence) and passes otherwise, with no nested vt-engine rebuild. test: `crates/gterminal/tests/build_env.rs::default_features_build_invokes_no_zig`.
- 3.3.3 - The managed-binary inventory test exists once. file: `tests/cli/test_install_setup_gterm.py`.

### 3.4 Installer short-circuit for unpublished helper pins [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `src/gobby/cli/install_setup_gterm.py`
- `src/gobby/cli/install_setup_gclient.py`
- `src/gobby/cli/install_setup.py::_run_managed_native_binary_installs`
- `src/gobby/install/distribution.py::verify_homebrew_managed_bins`
- `src/gobby/install/distribution.py::_inspect_homebrew_helper`
- `src/gobby/install/version_pins.py`
- `src/gobby/runner_maintenance/binaries.py::bin_freshness_loop`
- `src/gobby/runner_maintenance/__init__.py::*` — scope-reason: re-exports `bin_freshness_loop`
- `tests/cli/test_install_setup_gterm.py`
- `tests/cli/test_install_setup_gclient.py`
- `tests/install/test_distribution.py::*` — scope-reason: unpublished-helper cases
- `tests/install/test_version_pins.py::*` — scope-reason: pins gain the published flag
- `tests/cli/test_install_setup.py::TestRunDaemonSetup.test_homebrew_mode_installs_required_runtimes_but_skips_managed_helper_installs`
- `tests/test_runner_bin_freshness.py::*` — scope-reason: the loop's direct tests gain the unpublished-pin case and lose any assertion that an unpublished pin raises

Closes A-4, A-5.

Consumer sweep (`gcode usages` on the four exact symbols): `_run_managed_native_binary_installs`
has one caller, `run_daemon_setup` in the same file, and one string patch in
`tests/cli/test_install_setup_gdaemon.py` (`monkeypatch.setattr(install_setup,
"_run_managed_native_binary_installs", lambda: None)`) that survives unchanged because
the signature does not change; `bin_freshness_loop` is re-exported unchanged by
`runner_maintenance/__init__.py` and passed as an opaque loop by `run_daemon`
(`runner_lifecycle.py`) and `_default_loops` / `start_periodic_tasks`
(`runner_lifecycle_periodic.py`), none of which read its behaviour; the one remaining
literal hit outside that call graph, the `"bin_freshness_loop": _idle_loop` entry of
`tests/wiki/test_watcher_lifecycle.py::_loops`, is a substitution registry keyed by loop
name and is intentionally unchanged, because this leaf changes neither the loop's name
nor its registration; its direct tests
live in `tests/test_runner_bin_freshness.py` (targeted); the other module importers of
`install_setup.py` (`install_files_home.py`, `install_setup_gdaemon.py`,
`install_setup_impeccable.py`, `test_install_ghook.py`, `test_files_home.py`,
`test_public_ghook_install.py`) do not name any targeted symbol and need no change.
`tests/cli/test_install_setup.py` patches `verify_homebrew_managed_bins` and
`_run_managed_native_binary_installs` by string in the homebrew-mode test; that test's
mocked status list gains the two `ok=True, reason="not yet published"` rows so the
`run_daemon_setup` path is exercised with unpublished helpers present.

No `gterm-v*` / `gclient-v*` release tags exist, the crates are unpublished, and the
formulae are absent from `GobbyAI/tap`. Every `gobby install` therefore runs the whole
fallback chain for both binaries: in a source checkout with Zig it runs
`cargo build --release -p gobby-terminal --features vt-engine` (600 s timeout);
elsewhere GitHub 404 → binstall (≤ 60 s, always fails) → `cargo install --git …` (up to
600 s). The daemon's `bin_freshness_loop` then raises `SourceUnavailableError` for both
pins every cycle. Under the Homebrew distribution `verify_homebrew_managed_bins()` raises
`HomebrewDistributionError` for the two missing helpers, so `gobby install` hard-fails.

This leaf follows 3.3 because both edit `tests/cli/test_install_setup_gterm.py`.

- Pins stay strings. `MANAGED_BIN_VERSION_PINS` (the one existing carrier in
  `src/gobby/install/version_pins.py`; no alias, re-export under another name, or
  second mapping is introduced) keeps its uniform `dict[str, str]` shape
  (every consumer — freshness loop, CLI semver compare, distribution, hook, gcode —
  iterates it as `name → version floor` and none of them changes). Publication state
  lives beside it in `version_pins.py` as
  `UNPUBLISHED_MANAGED_BINS: frozenset[str] = frozenset({"gterm", "gclient"})` with a
  `is_published(name) -> bool` helper; the operator release step empties the set.
  `test_version_pins.py` asserts the mapping stays homogeneous (every value is a
  string) and that the set only names keys present in the mapping.
- Installer state table, used verbatim by `install_gterm`, `install_gclient`,
  `_run_managed_native_binary_installs`, `bin_freshness_loop`, and
  `_inspect_homebrew_helper`; one state and lookup result has exactly one outcome:

  | `is_published` | `GOBBY_BUILD_TERMINAL_FROM_SOURCE` | tag lookup | outcome |
  |---|---|---|---|
  | `False` | unset | not performed | `{"skipped": True, "reason": "gterm 0.1.0 is not yet published"}`; no cargo, binstall, or GitHub call |
  | `False` | `1` | not performed | source build from the checkout (`cargo build --release -p gobby-terminal --features vt-engine`), typed failure if the checkout or Zig is absent |
  | `True` | unset | exact `gterm-v<pin>` tag found | release download → binstall → `cargo install --git` fallback chain as today |
  | `True` | unset | no exact tag | typed failure `ManagedBinaryReleaseMissing("gterm-v0.1.0")`; no fallback chain (a published pin with no tag is a release bug, not a reason to build for ten minutes) |
  | `True` | `1` | not performed | source build, as row 2 |

  `_run_managed_native_binary_installs` echoes the skip reason or raises the typed
  failure; `bin_freshness_loop` logs one info line per unpublished pin at startup and
  never raises for it; `_inspect_homebrew_helper` marks an unpublished helper
  `ok=True, reason="not yet published"` and `verify_homebrew_managed_bins` never
  raises for it, while a missing published helper still raises.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `GOBBY_TEST_PROTECT=1 uv run pytest tests/cli tests/install`.

**Acceptance:**

- 3.4.1 - `gobby install` with unpublished gterm/gclient pins completes without invoking cargo, binstall, or GitHub for either binary and prints the skip reason. test: `tests/cli/test_install_setup_gterm.py::test_unpublished_pin_short_circuits_install`.
- 3.4.2 - The freshness loop logs once and raises nothing for unpublished pins. test: `tests/test_runner_bin_freshness.py::test_bin_freshness_skips_unpublished_pins`.
- 3.4.3 - Homebrew verification passes with the two helpers absent while they are unpublished and fails for a missing published helper. test: `tests/install/test_distribution.py::test_unpublished_helpers_do_not_fail_homebrew_verification`.
- 3.4.4 - A published pin with no exact tag yields `ManagedBinaryReleaseMissing` naming the tag and runs no fallback method; every row of the state table is exercised with the outcome it names. test: `tests/cli/test_install_setup_gclient.py::test_installer_state_table`.
- 3.4.5 - `MANAGED_BIN_VERSION_PINS` values are all strings and every generic consumer is untouched; `UNPUBLISHED_MANAGED_BINS` names only pinned binaries. test: `tests/install/test_version_pins.py::test_pins_stay_homogeneous_strings`.
- 3.4.6 - An unpublished pin with source-build opt-in performs only the checkout build and fails typed when the checkout or Zig is unavailable. test: `tests/cli/test_install_setup_gclient.py::test_installer_state_table`.
- 3.4.7 - A published pin with its exact tag uses the release-download, binstall, and git-install fallback chain in order. test: `tests/cli/test_install_setup_gclient.py::test_installer_state_table`.
- 3.4.8 - A published pin with source-build opt-in performs the checkout build without a tag lookup. test: `tests/cli/test_install_setup_gclient.py::test_installer_state_table`.

## P4: Write path, WebSocket, and web client
`kind: framing`

**Goal**: Every byte that reaches a terminal goes through `WriteCoordinator` under a
single lease authority, every WebSocket reply matches the golden corpus, and the web
terminal can type.

### 4.1 `WriteCoordinator` as the single write authority [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/terminals/write_coordinator.py`
- `src/gobby/terminals/leases.py`
- `src/gobby/servers/websocket/terminal_ws.py`
- `src/gobby/servers/websocket/handlers/core.py::*` — scope-reason: the legacy `terminal_input` path without `attachment_id` is deleted
- `tests/terminals/test_write_coordinator.py`
- `tests/terminals/test_write_outcomes.py`
- `tests/terminals/test_native_runtime.py`
- `tests/servers/test_terminal_ws_lease.py`
- `tests/servers/test_tmux_mixin.py::*` — scope-reason: legacy input-bridge routing tests are deleted with the path
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: `init_orchestration` mints the single `TerminalLeaseRegistry` and injects it into the coordinator
- `src/gobby/runner_init/servers.py::*` — scope-reason: `init_servers` publishes the registry and coordinator on the container and hands both to the WebSocket server
- `src/gobby/servers/websocket/server.py::*` — scope-reason: `configure_terminals` receives the shared registry and coordinator
- `src/gobby/servers/websocket/tmux.py::*` — scope-reason: `_init_tmux` stops minting a `TerminalLeaseRegistry`
- `src/gobby/agents/lifecycle_monitor.py::AgentLifecycleMonitor.__init__`
- `src/gobby/terminals/services.py`
- `src/gobby/app_context.py::ServiceContainer`
- `src/gobby/storage/terminals.py`
- `tests/terminals/fakes.py`
- `tests/terminals/test_composition_roots.py`
- `src/gobby/servers/websocket/proxy_relay.py`
- `tests/servers/test_terminal_ws_viewport.py`
- `tests/servers/test_tmux_bridge_authority.py`
- `tests/terminals/test_lease_authority.py`

Closes B-10 / C-22, C-19, C-21, and the lease-source half of C-5.

Composition graph (owned here, because "the injected `TerminalLeaseRegistry`" has no
injector on the branch): `init_orchestration` constructs
`WriteCoordinator(runner.terminal_manager, tmux_runtime)` with no registry, while
`TmuxMixin._init_tmux` and `terminal_ws.py`'s lazy `_leases()` each mint their own
`TerminalLeaseRegistry()`, `ServiceContainer` has no `write_coordinator` field (so
`http.py`'s `getattr(services, "write_coordinator", None)` hands the MCP registries
`None`), and no composition root ever assigns the WebSocket server's
`write_coordinator`. This leaf makes one object graph: `init_orchestration` creates
exactly one `TerminalLeaseRegistry`, passes it to
`WriteCoordinator(manager, runtime, lease_registry=…)`, and stores both on the runner;
`ServiceContainer` gains `write_coordinator` and `lease_registry`; `init_servers` sets
them on the container and passes them through `WebSocketServer.configure_terminals`
(the existing post-construction seam, which gains the two parameters); `_init_tmux`
and `_leases()` stop minting registries (a missing registry is a composition error,
never a fallback). `AgentLifecycleMonitor.__init__`'s inline default-`TerminalServices`
construction (manager, tmux runtime, runtime registry, coordinator) is a move out of
`src/gobby/agents/lifecycle_monitor.py` (929 lines once 1.1's merge resolves — 847 on the
branch, 893 on `0.5.0`, both sides' additions kept) into a `TerminalServices.standalone(db, tmux)`
classmethod in `src/gobby/terminals/services.py`, which builds its coordinator with
its own registry through the same constructor; `__init__` calls it and shrinks. Neither
`AgentLifecycleMonitor.__init__`'s signature nor `ServiceContainer`'s existing fields
change (the two new fields default to `None`), so their other consumers (`runner.py`,
`agents/runner.py`, `http.py`, `reactions.py`, `tools/build.py`, and their tests) are
untouched.
`tests/terminals/fakes.py` builds the fake coordinator the same way, and
`tests/terminals/test_composition_roots.py` proves identity through the production
container: the registry reachable from the coordinator, the WebSocket server, and the
MCP terminal tools is one object.

Startup reclamation of orphaned WebSocket latches, owned here because this is where the
coordinator is minted. `unresolved_writes` is a durable jsonb column on the terminal row
capped at `UNRESOLVED_WRITE_MAX_ENTRIES = 32` (`src/gobby/storage/terminals.py`), while
the `ws:{attachment_id}:` keys 4.3 writes into it are named after an attachment that
lives only in the daemon's memory. Graceful finalization clears them (4.3), but a hard
process exit does not: after restart the attachment registry is empty, every client
receives a fresh `attachment_id`, and the old prefixes are unreachable by any later
finalize — so a terminal that outlives a few daemon crashes with indeterminate writes in
flight can exhaust its 32-entry cap and refuse every subsequent write for the rest of its
life. `TerminalManager` gains a bulk
`clear_orphaned_attachment_writes(machine_id: str)` that deletes exactly the
`ws:`-prefixed entries from this project's terminal rows *whose `machine_id` equals the
argument* in one statement, leaving MCP, attention, and every other key namespace
untouched, and `init_orchestration` calls it once with `require_machine_id()`
(`src/gobby/utils/machine_id.py`), immediately after constructing the coordinator and
before any server accepts a socket. The machine predicate is load-bearing, not
defensive: several daemons on different machines share one PostgreSQL hub and one
project, `terminals.machine_id` (`uuid NOT NULL`, 1.1's 403 DDL) records where a
terminal actually executes, and a project-wide sweep on machine A would erase live
latches belonging to a running daemon on machine B mid-write. Scoped that way the sweep
needs no liveness check and is correct unconditionally, because no attachment id on a
row this daemon owns predates this process; `MemoryTerminalStore`
(`tests/terminals/fakes.py`) implements the same signature.

`grant_lease` / `takeover_lease` have no production callers but are called directly by
`tests/terminals/test_write_outcomes.py` and `tests/terminals/test_native_runtime.py`
(both in guard set G). Those tests migrate in this leaf to grant and take over through
`TerminalLeaseRegistry` (`attach(attachment_id=…)` / `take_control`), with no
compatibility shim left on the coordinator. Sweep `gcode grep -w grant_lease tests/ src/`
and `gcode grep -w takeover_lease tests/ src/` on the epic worktree before the guard run.

- C-22: `WriteCoordinator.write` persists the unresolved-write latch (`_persist`)
  before `_revalidate_lease`; a `StaleTerminalLeaseError` leaves the key in
  `unresolved_writes`, and 32 stale refusals exhaust `UNRESOLVED_WRITE_MAX_ENTRIES`
  for every writer. Revalidate first; a pre-dispatch refusal of any kind never persists
  a latch (`run_sequence` already orders this correctly — make `write` match).
- Lease source: `WriteCoordinator._leases`, `grant_lease`, and `takeover_lease` have no
  callers; `TerminalLeaseRegistry` is the live lease authority. Delete the coordinator's
  private lease table and make `_revalidate_lease` consult the injected
  `TerminalLeaseRegistry` (`holder(terminal_id)`, `generation(terminal_id)`). Operator
  writes must carry `attachment_id` and `expected_lease_generation`; `origin="daemon"`
  (used by 4.2 for MCP) is lease-exempt; `origin="attention"` is gated by
  `set_attention_gate` (4.2 wires the caller).

  One linearization boundary per terminal. Today the coordinator's per-terminal
  `asyncio.Lock` covers revalidation through dispatch, while the registry's
  `take_control` / `release_control` / `finalize` / `finalize_websocket` are plain
  synchronous mutations any handler can run while a write is awaiting the runtime
  (`tmux send-keys`, the host socket): a takeover can land after `_revalidate_lease`
  passed and before the bytes reach the PTY, so the displaced holder's write still
  lands, and a `finalize` can clear the `ws:{attachment_id}:` latches (4.3) under an
  in-flight write that then settles indeterminate with its latch gone. The lock map
  moves from the coordinator to `TerminalLeaseRegistry` (`lock(terminal_id) ->
  asyncio.Lock`, one lock per terminal, created on first use); `WriteCoordinator.write`
  / `run_sequence` acquire it from the
  registry for the revalidate-through-dispatch span exactly as they do today, and
  every lease mutation becomes a coroutine that acquires the same lock
  (`take_control`, `release_control`, `finalize`, `finalize_websocket`, `clear_on_exit`,
  and `attach` when it assigns control). A takeover therefore waits for the in-flight
  write to settle and the next write from the old holder is refused stale; a
  finalize runs only between writes. `clear_on_exit` is on that list because 4.3 makes
  it the exit-side mutation of exactly the same state — it drops the terminal's
  `_leases` entry and the attachment residue whose finalization clears the
  `ws:{attachment_id}:` latches — so a terminal that exits mid-write would otherwise
  delete the lease and the latch out from under a dispatch that has already passed
  revalidation, which is the same missing-latch indeterminate this lock exists to
  prevent. It becomes an async mutation and takes the lock like the rest. The lock cell's lifetime is independent of the
  lease entry and refcounted exactly as 2.5's `settle_lock`: the registry exposes
  `lock(terminal_id)` as an async context manager over a cell `(lock, borrowers)`,
  entering increments `borrowers` before awaiting `acquire()`, and one `finally` around
  both the acquire and the body decrements the captured cell object, releases only when
  an `acquired` flag records a completed acquisition, and deletes the entry at zero when
  the mapped cell is still that object — so a writer or mutation cancelled while queued
  on `acquire()` unwinds without stranding a borrower. `finalize` / `finalize_websocket` drop the lease
  record and never touch the cell, so a coroutine still owning or queued on that lock
  and an attach arriving right after the finalize all serialize on the same cell —
  dropping the cell with the lease is what would let a re-attach mint a second lock and
  run concurrently with the queued waiters. Deleting at `borrowers == 0` is safe for
  the same single-threaded reason given in 2.5 (every future user has already
  incremented), and it bounds the map by concurrent writers per terminal instead of by
  every terminal the daemon has ever seen. Re-signing those methods obliges this leaf to
  migrate every caller, and they are not all in `terminal_ws.py`. Its handlers already
  run in coroutines, so those call sites gain `await` and nothing else, and
  `tests/terminals/fakes.py` keeps the same shape. `proxy_relay.py` also calls
  `self._owner._leases().finalize(...)` at two sites — inside `finalize_attachment` and
  in the `_on_socket_fail` cleanup loop — and both enclosing functions are already
  `async def`; left synchronous they would build a
  coroutine object, discard it, and silently skip the finalization while the guard run
  reports an un-awaited-coroutine warning.

  One of those two sites needs more than an `await`. `_on_socket_fail` finalizes the
  lease first and then pops the record and closes the frame, which is the correct order.
  `finalize_attachment` does the reverse: it pops the record, cancels the pump task,
  `await`s `record.frame.close()`, and only then finalizes. Making the finalize a
  coroutine at that position hands the event loop two yields — the close and the
  finalize — during which the record is already gone while the lease is still valid, so
  a concurrent `terminal_input` can take the per-terminal lock and dispatch a write for
  an attachment whose output transport has been dismantled. Awaiting a mutation that now
  queues on that lock makes the ordering load-bearing rather than incidental, so
  `finalize_attachment` is reordered to match its sibling: pop the record and discard it
  from `by_socket` synchronously, `await` the registry finalization immediately so it
  queues behind any in-flight dispatch and revokes authority before any later writer,
  and only then cancel the task and close the frame. The lifecycle emission still runs
  last, against the popped record. Three test modules call the re-signed methods
  directly rather than through a handler — `tests/servers/test_terminal_ws_viewport.py`,
  `tests/servers/test_tmux_bridge_authority.py`, and `tests/terminals/test_lease_authority.py`
  — and their cases become async and await the mutations here, in this leaf, because
  this leaf's own close gate runs the terminal guard group. 4.3 and 4.4 also target some
  of those files and both depend on 4.1, so the shared ownership stays ordered.
- C-21: `terminal_ws.py` imports `unittest.mock.Mock` and `_runtime_for` returns `None`
  for `Mock` instances; with no runtime or coordinator the handler reports
  `outcome="delivered"` without writing. Delete the import and the fall-through; a
  missing runtime or coordinator is a typed refusal `{"outcome": "refused", "code": "runtime_unavailable"}`.
  Rewrite the lease tests that relied on the fall-through against the fake runtime.
- C-19: `handlers/core.py` still accepts legacy `terminal_input` without
  `attachment_id` and writes `origin="automatic", action_key=f"ws-input:{run_id}"`, so
  one `IndeterminateWrite` latches the key and every later keystroke is `Suppressed`.
  Delete the legacy path; `terminal_input` without `attachment_id` is refused with
  `code: "attachment_required"`.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out).

**Acceptance:**

- 4.1.1 - A stale-lease refusal leaves `unresolved_writes` empty, and 40 consecutive stale refusals never raise `UnresolvedWriteCapacityError`. test: `tests/terminals/test_write_coordinator.py::test_stale_lease_refusal_persists_no_latch`.
- 4.1.2 - `WriteCoordinator` has no private lease table; lease checks read `TerminalLeaseRegistry`, and a takeover through the registry is observed by the coordinator's next write. test: `tests/terminals/test_write_coordinator.py::test_lease_registry_is_the_only_lease_source`.
- 4.1.3 - `terminal_input` with no runtime or no coordinator is refused with `runtime_unavailable`; no production module under `src/gobby/servers` imports `unittest.mock`. test: `tests/servers/test_terminal_ws_lease.py::test_missing_runtime_is_typed_refusal`.
- 4.1.4 - `terminal_input` without `attachment_id` is refused with `attachment_required` and no `ws-input:` action key is ever created. test: `tests/servers/test_terminal_ws_lease.py::test_input_requires_attachment`.
- 4.1.5 - `WriteCoordinator` exposes no `grant_lease` / `takeover_lease`, and the write-outcome and native-runtime suites drive leases through `TerminalLeaseRegistry`. test: `tests/terminals/test_write_outcomes.py::test_leases_come_from_registry`.
- 4.1.6 - The production composition root creates one `TerminalLeaseRegistry` and the same registry and `WriteCoordinator` instance reach the WebSocket server, `ServiceContainer`, and the MCP terminal tools; no module under `src/gobby/servers` constructs a `TerminalLeaseRegistry`. test: `tests/terminals/test_composition_roots.py::test_terminal_write_authority_is_singleton`.
- 4.1.7 - With the fake runtime paused inside dispatch of an operator write, a concurrent `take_control` by another attachment, a `release_control`, and a `finalize` of the writer each block until the write settles; the write's outcome is recorded under the writer's generation, the old holder's next write is refused stale, and no `ws:` latch is cleared while a write is in flight. test: `tests/terminals/test_write_coordinator.py::test_lease_mutations_linearize_with_dispatch`.
- 4.1.8 - Attach when it assigns control and finalize_websocket over multiple attachments both wait behind an in-flight dispatch on the same per-terminal lock, complete without nested-lock deadlock, and clear no write latch before dispatch settles. test: `tests/terminals/test_write_coordinator.py::test_attach_and_finalize_websocket_linearize_with_dispatch`.
- 4.1.9 - Two reachable schedules over one terminal's lock, both asserting that the cell borrowed by the queued coroutines is the identical object throughout: (a) with write A paused inside dispatch, `finalize` is queued next and write B after it, so B revalidates against the finalized lease and is refused stale while the new attachment's first write succeeds; (b) with write B queued before the `finalize`, B acquires the lock first, revalidates against the still-live old lease, and completes, after which the finalize and the new attachment's first write run in that order. Nothing interleaves in either schedule. test: `tests/terminals/test_write_coordinator.py::test_lock_cell_survives_finalize_and_reattach`.
- 4.1.10 - Driving 500 short-lived terminals through grant → write → finalize leaves the registry's lock map empty once every borrower has returned, while a terminal with one paused write and two queued writers keeps exactly one cell alive and hands all three the same lock object; cancelling one queued writer before it acquires the lock drops only its borrow, the remaining writer acquires the same lock object, and the cell disappears when the last borrower returns. test: `tests/terminals/test_write_coordinator.py::test_lock_cells_are_released_at_zero_borrowers`.
- 4.1.11 - With the fake runtime paused inside dispatch of an operator write, `clear_on_exit` for that terminal blocks until the write settles: the outcome is recorded under the writer's original lease generation with its `ws:{attachment_id}:` latch still present, and only afterwards does exit cleanup remove the lease entry, the attachments, and those latches. test: `tests/terminals/test_write_coordinator.py::test_clear_on_exit_linearizes_with_dispatch`.
- 4.1.12 - A terminal row carrying 32 `ws:{attachment_id}:` latches left by a process that died before finalizing, alongside MCP and attention latches, is swept clean of exactly the `ws:`-prefixed keys by the composition root at startup: the other keys survive byte-identical, a fresh attachment on that terminal writes successfully instead of being refused `unresolved_write_capacity`, and repeating the kill-and-restart cycle five times never accumulates a stranded key. The sweep is machine-scoped: in one project holding rows for two `machine_id`s, a restart on machine A clears only A's `ws:` keys and leaves every key on machine B's rows byte-identical. test: `tests/terminals/test_composition_roots.py::test_startup_reclaims_orphaned_attachment_write_latches`.
- 4.1.13 - Both proxy-relay cleanup paths and every direct test caller await the re-signed TerminalLeaseRegistry mutations, and the focused suites complete with no un-awaited-coroutine warning. test: `tests/servers/test_tmux_bridge_authority.py::test_async_lease_mutation_callers_are_awaited`.
- 4.1.14 - `ProxyHub.finalize_attachment` revokes lease authority before it yields to frame teardown: with the record's `frame.close()` paused and a `terminal_input` for that attachment racing it, the write is refused stale and no dispatch reaches the runtime after the finalization, and the finalize completes without deadlocking against the per-terminal lock the write holds. The same race against `_on_socket_fail` reaches the identical outcome, so the two cleanup paths are not distinguishable by ordering. test: `tests/servers/test_terminal_ws_lease.py::test_finalize_revokes_authority_before_frame_close`.

### 4.2 Route MCP `send_keys` and attention injection through the coordinator [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::*` — scope-reason: `send_keys` and `capture_output` switch to the daemon-privileged write origin
- `src/gobby/servers/routes/attention.py::*` — scope-reason: the respond path's router-local lock and direct runtime writes are replaced by coordinator writes
- `src/gobby/terminals/write_coordinator.py`
- `tests/mcp_proxy/test_sessions_terminal_tools.py`
- `tests/servers/test_attention_respond.py::*` — scope-reason: respond path asserts coordinator writes
- `src/gobby/storage/terminals.py`
- `tests/terminals/fakes.py`
- `tests/storage/test_terminals.py`

Closes C-4, C-6.

Durable carrier for the fingerprint: a latch entry is serialized by
`TerminalManager.persist_unresolved_write` (`src/gobby/storage/terminals.py`) and by
`MemoryTerminalStore.persist_unresolved_write` (`tests/terminals/fakes.py`) as
`{"at", "origin"}`; both gain an optional `fingerprint` (64 hex chars, `None` when the
writer supplied none), the existing `UNRESOLVED_WRITE_MAX_ENTRIES` /
`UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES` caps apply to the serialized entry including
the new field, and an entry persisted without a fingerprint (legacy or daemon origin)
is treated as matching any retry payload — it never refuses `action_key_conflict`.
`tests/storage/test_terminals.py` exercises the PostgreSQL path through the isolated
test hub.

- C-4: MCP `send_keys` sends `WriteRequest(origin="operator")` with no `attachment_id`,
  and `_revalidate_lease` raises `StaleTerminalLeaseError` whenever `attachment_id is None`
  for operator origin, so `send_keys` always fails against a live terminal row (and,
  before 4.1, left `mcp-send-keys:{sid}` latched). The daemon always passes
  `write_coordinator` (`http.py`). Use `origin="daemon"` (lease-exempt, 4.1) and
  `capture_output` reads through `runtime.snapshot`.
  `test_send_and_capture_are_backend_neutral` must actually invoke the registered tools
  against tmux and native fake runtimes.

  Action-key identity: the registered surface is `send_keys(session_id, keys, literal)`
  and defines no sequence, so the key source is made explicit. `send_keys` gains
  `idempotency_key: str | None = None` (1–128 chars, `[A-Za-z0-9._:-]`, validated and
  refused typed otherwise). When supplied, `action_key = f"mcp-send-keys:{session_id}:{idempotency_key}"`
  and the dedup contract is exactly the coordinator's unresolved-write rules — no
  separate idempotency record exists. While the key's write is unresolved (an
  `Indeterminate` latch in `unresolved_writes`), a retry with the same key and payload
  resolves the latch through capture and returns the resolved outcome without a second
  write; the latch carries the payload fingerprint (`sha256` of `keys` + `literal`,
  added to the latch entry in this leaf), and a retry under an unresolved key with a
  different fingerprint is refused `action_key_conflict`. A `Delivered` or `Refused`
  outcome clears the key, so a later call with the same key is a fresh write: callers
  that need exactly-once delivery retry only `indeterminate` outcomes. When absent,
  the tool mints `uuid4().hex` per invocation, uses it as the key, and returns it in
  the result as `idempotency_key` so the caller can retry an `indeterminate` outcome
  with that key. A per-invocation key is never reused, so it can suppress nothing.
  The tool schema documents both modes and the unresolved-only scope.
- C-6: the attention respond path uses a router-local `_TrackedEntryLock` and
  `_inject_via_runtime` → `runtime.write_key` / `write_text` directly, with the
  capture+fingerprint CAS outside the coordinator's lock; `set_attention_gate` has no
  caller; `inject_attention_answer_to_tmux_target` is still imported. Replace with
  `write_coordinator.write(WriteRequest(origin="attention", action_key=f"attention:{entry_id}:{attention_id}", …))`
  where the coordinator runs the capture + fingerprint compare inside its critical
  section (`run_sequence` with a `precondition` callable); delete the router lock and the
  tmux-named import.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_attention_respond.py tests/servers/test_attention_roster.py tests/mcp_proxy/test_sessions_terminal_tools.py`.

**Acceptance:**

- 4.2.1 - `send_keys` against a live tmux row and a live native row delivers through the coordinator with a `mcp-send-keys:` action key and no lease error; `capture_output` returns the runtime snapshot. test: `tests/mcp_proxy/test_sessions_terminal_tools.py::test_send_and_capture_are_backend_neutral`.
- 4.2.2 - An attention answer is written with `origin="attention"` under the coordinator lock with the fingerprint CAS inside it; a stale fingerprint is refused before any byte is written. test: `tests/servers/test_attention_respond.py::test_respond_writes_through_coordinator_with_cas`.
- 4.2.3 - `src/gobby/servers/routes/attention.py` has no router-local lock and imports nothing tmux-named. symbol: `respond`.
- 4.2.4 - `send_keys` with an explicit `idempotency_key` resolves a same-payload retry of an `indeterminate` outcome through capture (one write on the PTY, the resolved outcome returned), refuses a different payload under the same unresolved key with `action_key_conflict`, treats a retry after a `delivered` outcome as a fresh write, rejects a malformed key typed, and without a key returns a fresh per-invocation key that retries an `indeterminate` outcome to resolution. test: `tests/mcp_proxy/test_sessions_terminal_tools.py::test_send_keys_idempotency_key_contract`.
- 4.2.5 - Payload fingerprints round-trip through PostgreSQL and MemoryTerminalStore, preserve entry and byte caps, and handle legacy unresolved entries without a fingerprint deterministically. test: `tests/storage/test_terminals.py::test_unresolved_write_fingerprint_round_trip`.

### 4.3 Operator WS writes, lease events, attach failures, and scroll clamp [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/terminal_ws.py`
- `src/gobby/servers/websocket/proxy_relay.py`
- `src/gobby/terminals/leases.py`
- `src/gobby/terminals/ws_protocol.py`
- `src/gobby/runner_init/servers.py::*` — scope-reason: the composition root installs the write-fault seam on the terminal WS handler
- `src/gobby/servers/websocket/server.py::*` — scope-reason: `configure_terminals` accepts the optional `write_fault` callable and stores it for the terminal handler
- `tests/terminals/test_lease_authority.py`
- `tests/servers/test_terminal_ws_lease.py`
- `tests/servers/test_terminal_ws_viewport.py`
- `tests/servers/test_native_web_proxy.py`
- `tests/e2e/test_terminal_client_stack.py`
- `tests/e2e/conftest.py::*` — scope-reason: the isolated daemon fixture enables the fault seam

Closes C-5, C-10, C-11, C-12, E-8, and the daemon half of Q-1.

- C-5: `_handle_operator_write` calls `runtime.write_text(row, payload, False)`
  directly (no per-terminal lock, no latch, no action key); the coordinator branch is
  unreachable when a runtime registry is configured (always). Route every
  `terminal_input` / `terminal_paste` through
  `write_coordinator.write(WriteRequest(action_key=f"ws:{attachment_id}:{client_write_seq}", origin="operator", attachment_id=…, expected_lease_generation=…))`
  and map the outcome to `terminal_write_outcome`.
- C-10: the scroll clamp bound comes from the client (`max_rows`) and the daemon
  fabricates `terminal_scroll_offset_applied` while `proxy_relay` also forwards the
  host's, producing two applied events (first unclamped). Forward `SetScrollOffset` to
  the host and emit only the host's applied event; for tmux, the runtime clamps against
  its own history length.
- C-11: `_fanout_lease_lost` raw-sends to every client; release uses `_send_json`. Send
  `terminal_lease_lost` only to the displaced attachment's websocket through
  `_send_control`, and route release through `emit_lifecycle`.
- C-12: `_start_proxy_attach` silently returns on a missing runtime/opener/locator or an
  opener exception while the handler answers
  `terminal_attach_result{success: True, frame_delivery: "proxy"}`. Answer
  `{"success": False, "code": "host_unavailable"}` and finalize the attachment.
- Direct-attach producer: a granted `terminal_attach{frame_delivery: "direct"}` today
  answers without anything a client could connect to. The result gains a `direct`
  block — `{"host_epoch": <row.host_epoch>, "frame_socket_path": <host frame socket>,
  "host_terminal_id": <locator host_terminal_id>}` — read from the terminal row and
  the host manager at grant time, absent (`null`) for proxy grants; `ws_protocol.py`'s
  `terminal_attach_result` codec carries it, and 4.5's committed goldens capture both
  shapes. gclient (6.3) verifies the host's `Welcome.host_epoch` against this
  `host_epoch`; the web client never requests direct delivery and ignores the block.
- Attachment finalization owns its server-side residue. `TerminalLeaseRegistry.finalize`
  today marks the record `finalized` and leaves it in `_attachments` (and in
  `_by_websocket` when finalized by id), so every reconnect, proxy fallback, and
  daemon-restart re-attach (6.2, 6.3, 9.1) grows the registry for the daemon's
  lifetime. `finalize` deletes the record and its websocket membership instead —
  `get()` already answers `None` for both unknown and finalized ids, so every caller
  keeps refusing a late message as `stale_attachment` with no tombstone — and the
  terminal-exit path (`clear_on_exit`) drops the terminal's `_leases` entry. That exit
  path becomes an async mutation taken under 4.1's per-terminal registry lock, like
  every other lease mutation, so a terminal exiting mid-write cannot delete the lease,
  the attachment, or the latches below while a dispatch that already revalidated is
  still in flight. On each
  `FinalizedEvent` the handler also clears every unresolved-write latch whose key starts
  with `ws:{attachment_id}:` through the coordinator (the per-key `_clear`), so a
  write the client discarded (4.6) never outlives its attachment; inside one attachment
  the durable cap still applies and the 33rd unresolved write is refused with the
  existing typed `unresolved_write_capacity` outcome rather than silently dropped.
- E-8: `write_handler_faulted()` stats `$GOBBY_HOME/terminal_write_fault` on every
  write. Delete it. The fault seam is plain dependency injection, not configuration:
  `WebSocketServer.configure_terminals` (the composition seam 4.1 establishes) accepts
  `write_fault: Callable[[WriteRequest], str | None] | None = None` and stores it for
  the terminal handler (return a refusal code to fault the write, `None` to pass); the
  composition root in `runner_init/servers.py` installs it. Production passes `None`.
  The isolated e2e daemon runs out of process, so the fixture cannot hand it a Python
  callable; it enables the seam through the daemon's existing test-mode startup path:
  when the daemon starts with `test_mode` on and `GOBBY_TEST_WRITE_FAULT_PREFIX=<prefix>`
  in its environment, the composition root installs a callable that faults any write
  whose `action_key` starts with the prefix. The environment is read exactly once at
  composition, never per write; no `TerminalHostConfig` field, no Pydantic/registry/
  contract surface, no filesystem access on the write path.
- Q-1 (reported from the running UI during this review, not from the source lists):
  attach acknowledgement is not the output linearization point. `_handle_terminal_attach`
  awaits `_start_proxy_attach`, and `ProxyHub.start_proxy` starts its `_pump` before the
  handler sends `terminal_attach_result`, so `terminal_attach_history` and the first
  `terminal_output` frames can reach the browser before it knows the `attachment_id`.
  A client that filters frames by attachment then drops exactly the history it was
  supposed to render, and one that renders them writes into a view it is about to
  replace. Split attach into prepare and activate: the handler opens the frame
  connection, completes the host handshake, and registers the attachment **without**
  starting the pump; it sends `terminal_attach_result`; the pump starts only when that
  attachment's first valid `terminal_set_viewport` arrives, which is the client's
  renderer-readiness signal (4.6 sends it once its renderer is mounted and sized).
  Activation is once-only per attachment and idempotent — a repeated or concurrent
  first viewport starts exactly one pump, and every later viewport is an ordinary
  resize. Nothing is buffered daemon-side: the pump has simply not started, and the
  host's own ring is the only queue, so the host ordering already specified
  (`AttachHistory` before the first keyframe) reaches a mounted renderer. Writes are
  unaffected and need no readiness. A prepared attachment that never activates holds
  its observer slot exactly as an idle attached client does and is finalized by the
  same paths (socket close, relay loss, detach, terminal exit, explicit transport
  failure), releasing the slot; no new deadline is introduced.
- Losing the writer lease is **not** one of those paths. Lease ownership and attachment
  lifetime are separate axes: a takeover transfers the holder, bumps the lease
  generation, and sends `terminal_lease_lost` to the displaced attachment (4.3.3),
  which keeps its `attachment_id`, its registry record, its host observer slot, and its
  frame stream, and simply becomes read-only. That is what makes the take-back in 4.6
  and 6.3 legal — both re-acquire control with `terminal_take_control` under the same
  `attachment_id`. Finalizing on lease loss would delete the record, drop the observer,
  and turn the client's own take-back into the `stale_attachment` refusal 4.3.7
  defines, so the displaced pane would go blank and be unrecoverable without a fresh
  attach. The same holds for a prepared attachment: it may hold the lease before it has
  ever rendered, and losing it leaves the attachment prepared and read-only.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_terminal_client_stack.py -k write`.

**Acceptance:**

- 4.3.1 - Every operator WS write reaches the runtime only through `WriteCoordinator` with a `ws:{attachment_id}:{seq}` action key; a direct `runtime.write_text` call from the handler is impossible (the handler holds no runtime reference). test: `tests/servers/test_terminal_ws_lease.py::test_operator_writes_go_through_coordinator`.
- 4.3.2 - A scroll-offset request past the history end yields exactly one `terminal_scroll_offset_applied` carrying the host's clamped value. test: `tests/servers/test_terminal_ws_viewport.py::test_scroll_offset_applied_once_from_host`.
- 4.3.3 - On takeover only the displaced attachment receives `terminal_lease_lost`, through the lifecycle path. test: `tests/servers/test_terminal_ws_lease.py::test_lease_lost_targets_displaced_attachment_only`.
- 4.3.4 - A proxy attach with no reachable host answers `success: false, code: host_unavailable` and the attachment is finalized. test: `tests/servers/test_native_web_proxy.py::test_proxy_attach_failure_is_typed`.
- 4.3.5 - No production write path stats the filesystem; `TerminalHostConfig` has no callable field; the e2e fault seam is installed by the composition root from the test-mode environment read once at startup, and the E1 test observes the faulted write as a `terminal_write_outcome` refusal with no bytes on the PTY. test: `tests/e2e/test_terminal_client_stack.py::test_write_handler_fault_is_reported_not_lost`.
- 4.3.6 - A granted direct attach answers `terminal_attach_result` with a `direct` block carrying the row's `host_epoch`, the host frame socket path, and the `host_terminal_id`; a proxy grant answers `direct: null`. test: `tests/servers/test_native_web_proxy.py::test_direct_attach_result_carries_host_epoch_and_locator`.
- 4.3.7 - After 1 000 attach/finalize cycles across reconnect, proxy fallback, and socket close, the registry holds no finalized record and no websocket membership for a closed socket, a late message naming a finalized id is refused `stale_attachment`, and a terminal's exit removes its lease entry. test: `tests/terminals/test_lease_authority.py::test_finalized_attachments_are_reclaimed`.
- 4.3.8 - Finalizing an attachment clears every `ws:{attachment_id}:` unresolved-write latch on its terminal and no other key; 33 indeterminate-then-discarded writes inside one attachment make the 33rd a typed `unresolved_write_capacity` refusal, and a reconnect after finalization starts with an empty latch set. test: `tests/servers/test_terminal_ws_lease.py::test_finalize_clears_attachment_latches`.
- 4.3.9 - Against a real host, no `terminal_attach_history` or `terminal_output` frame reaches the socket before `terminal_attach_result`: for an attach to a terminal with pre-existing output the recorded frame order is result, then history, then the first keyframe, and the pump produces nothing until that attachment's first `terminal_set_viewport`. test: `tests/servers/test_native_web_proxy.py::test_no_output_precedes_attach_result`.
- 4.3.10 - Activation is once-only and prepared attachments clean up: two `terminal_set_viewport` messages sent back to back start exactly one pump (the second only resizes), and an attachment acknowledged and then abandoned without any viewport releases its host observer slot on socket close, detach, and terminal exit. test: `tests/servers/test_terminal_ws_viewport.py::test_pump_activation_is_once_only_and_prepared_attachments_release`.
- 4.3.11 - Losing the writer lease never finalizes the attachment: after a takeover the displaced attachment still exists in the registry, still receives `terminal_output` frames, and `terminal_take_control` under that same `attachment_id` is granted rather than refused `stale_attachment`; the same holds for a prepared attachment that held the lease before its first viewport. test: `tests/servers/test_terminal_ws_lease.py::test_lease_loss_keeps_attachment_observing_and_take_back_works`.

### 4.4 WS pagination, broadcast keying, relay and bridge cancellation [category: code] (depends: 4.1, 4.3)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/terminal_ws.py`
- `src/gobby/terminals/ws_protocol.py`
- `src/gobby/servers/routes/terminals.py`
- `src/gobby/runner_broadcasting.py::*` — scope-reason: the output reader and broadcast are keyed on `terminal_id` instead of `run_id`
- `src/gobby/servers/websocket/broadcast.py::*` — scope-reason: `terminal_output` emission takes the resolved terminal id; lifecycle broadcasts carry `seq`
- `src/gobby/servers/websocket/proxy_relay.py`
- `src/gobby/terminals/sync_bridge.py`
- `tests/servers/test_terminal_ws_create.py`
- `tests/servers/test_terminals_routes.py`
- `tests/servers/websocket/test_broadcast.py::*` — scope-reason: terminal_output keyed on terminal id; seq monotonicity
- `tests/servers/test_native_web_proxy.py`
- `tests/terminals/test_sync_bridge.py`

Closes C-7, C-9, C-16, C-20. Follows 4.3 because both edit `terminal_ws.py`,
`proxy_relay.py`, and `test_native_web_proxy.py`.

- C-7: `_handle_terminal_list` never reads `data["cursor"]`, so every continuation
  returns page 1 while the web hook re-requests on every `next_cursor` (unbounded loop
  above 100 rows); `encode_page` emits `str(selected[-1]["id"])` but `_parse_cursor`
  expects `created_at|id`, and WS rows from `inventory_item` have no `"id"` (`KeyError`
  on a byte cut). Pass the cursor through; `encode_page` emits
  `f"{created_at}|{id}"` from the row, shared by the REST route and the WS handler.
- Snapshot causality for paged lists plus live events (consumed by 4.6 and 6.2): the
  daemon owns one monotonic `lifecycle_seq` (a process-wide counter incremented under
  the broadcast lock for every `terminal_created` / `terminal_exited` /
  `terminal_renamed` / `terminal_lease_*` broadcast) and a `daemon_epoch` (boot id).
  Every lifecycle broadcast carries `{"seq", "daemon_epoch"}`. `terminal_list` takes
  the snapshot under the same lock on the *first* page: it reads `lifecycle_seq`
  and the upper ordering key (the `created_at|id` of the newest row at that
  instant), the reply carries `snapshot: {"daemon_epoch", "seq"}`, and the cursor
  embeds the epoch, the snapshot `seq`, that upper bound, and the last key served;
  later pages of that cursor are served from the same ordering with
  `last_key < row_key <= upper_bound`, so rows created after the snapshot never
  extend the listing and a listing under sustained creation still terminates, and
  they repeat the snapshot block. The cursor is opaque and validated before any
  query: one `encode_cursor` / `decode_cursor` pair in `ws_protocol.py`, shared by
  the REST route and the WS handler, emits urlsafe-base64 JSON
  `{"v": 1, "project_id", "filters", "epoch", "seq", "upper", "last"}` where
  `filters` is the normalized filter set of the first page (sorted states, any
  other list filter sorted) and `upper` / `last` are `created_at|id` keys.
  `decode_cursor` refuses with the typed `invalid_cursor` (REST 400, WS
  `terminal_list_result{"error": "invalid_cursor"}`) on undecodable base64 or JSON,
  a missing or extra field, `v != 1`, a non-ISO timestamp or non-UUID id in either
  key, `last > upper`, a `seq` above the daemon's current `lifecycle_seq`, a
  `project_id` other than the caller's, or `filters` differing from the request's
  normalized filters; a fully valid cursor whose `epoch` differs from the current
  `daemon_epoch` is the only `cursor_stale` (restart the listing). Nothing a client
  sends reaches `datetime.fromisoformat` or the SQL layer unvalidated. The cursor is
  a caller-owned pagination token and confers no authority: it is not signed, and
  the validation above is its whole contract. Membership is bound by the caller's
  `project_id` and the request's own filters, replay by the epoch; `seq`, `upper`,
  and `last` only select which page of the caller's own listing comes back, so a
  caller who edits them to other in-range values receives a page it could have
  requested afresh and nothing else — an unsigned cursor is therefore the whole
  mechanism, and a MAC or a server-side cursor store is deliberately not built. The
  one place a client-supplied `seq` could do damage is event replay, and that is closed
  on the client: the replay watermark is pinned from page one and never re-read, so a
  caller that edits `seq` shifts only which page it receives and can neither erase nor
  resurrect a lifecycle event in its own view. Pinning is what makes this work, and it
  is not optional. Later pages repeat the snapshot block, but a later page's `seq` can
  only be recovered from the cursor the caller sent, so the final page's block is an
  echo of caller-controlled state and is worthless as a watermark.
  Client contract,
  asserted on both consumers: read `snapshot: {"daemon_epoch", "seq"}` from the
  **first** page's reply and pin both values before issuing any continuation; buffer
  every lifecycle event received while paging in
  a bounded buffer (1,024 events; overflow discards the partial listing and restarts
  it, which also re-reads and re-pins the snapshot); when the final page arrives, install
  the roster atomically, then replay buffered events with `seq > pinned.seq` — the
  page-one value, never the block echoed on a later page and never the `seq` the client
  sent in its cursor — in order and drop the rest. A later page whose `snapshot` block
  differs from the pinned pair is ignored for replay purposes; events from a
  `daemon_epoch` other than the pinned one trigger a fresh listing.
  A create or exit that lands between pages is therefore neither erased nor
  resurrected: a row created after the snapshot is absent from every page and
  arrives through its buffered `terminal_created`. The golden corpus (4.5) carries
  the new fields.
- C-9: `runner_broadcasting.py` → `broadcast.py` emits
  `{"type":"terminal_output","terminal_id": run_id}`. Key the reader and broadcast on the
  resolved `terminal_id` (available where the reader is started) and drop the
  `_ALLOWED_FIELD_REMAINING` excuse in the consumer guard.
- C-20: `proxy_relay.py`'s sender task does not catch
  `websockets.exceptions.ConnectionClosed`; it dies with an unretrieved exception and
  `relay.closed` stays `False` until 64 queued entries trip `relay_overflow`. Catch it,
  mark closed, finalize.
- C-16: `sync_bridge.py`'s pre-dispatch timeout cancels only the outer future; the
  shielded inner `marked_write` still takes the lock, persists the latch, and writes,
  while the hook gets `IndeterminateWrite("cancelled before dispatch")`. Cancel the inner
  task when the `marker` is not yet set; only a write that has passed the marker is
  shielded.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out).

**Acceptance:**

- 4.4.1 - Listing 250 terminals over WS and over REST follows `next_cursor` to termination in three pages each, with identical ordering and no repeated rows. test: `tests/servers/test_terminal_ws_create.py::test_terminal_list_pagination_terminates`.
- 4.4.2 - A byte-budget cut in the middle of a page yields a cursor that resumes at the next row. test: `tests/servers/test_terminals_routes.py::test_byte_cut_cursor_resumes`.
- 4.4.3 - `terminal_output` broadcasts carry the terminal row id, and a run whose terminal was replaced broadcasts under the new id. test: `tests/servers/websocket/test_broadcast.py::test_terminal_output_keyed_on_terminal_id`.
- 4.4.4 - A closed browser socket marks the relay closed on the first failed send and finalizes without waiting for overflow. test: `tests/servers/test_native_web_proxy.py::test_relay_closes_on_connection_closed`.
- 4.4.5 - A hook write that times out before dispatch never reaches the PTY and reports the pre-dispatch cancellation; one that times out after the marker is shielded to completion. test: `tests/terminals/test_sync_bridge.py::test_pre_dispatch_timeout_writes_nothing`.
- 4.4.6 - A terminal created after page 1 and one exited after page 2 of a three-page `terminal_list` end up present and absent respectively once the client installs the roster and replays `seq > reply.seq`; a cursor from a previous daemon epoch is refused `cursor_stale`; lifecycle `seq` is strictly increasing across concurrent broadcasts; and a listing over WS and over REST during which a new terminal is created after every page still terminates with exactly the snapshot's rows, the later rows arriving only through replayed events. test: `tests/servers/test_terminal_ws_create.py::test_list_snapshot_and_event_replay_are_causal`.
- 4.4.7 - Over REST (400) and over WS (`terminal_list_result` error), an undecodable cursor, a missing or extra field, an unknown version, a malformed timestamp or id, `last > upper`, a `seq` above the current `lifecycle_seq`, another project's cursor, and a cursor replayed with different filters are each refused `invalid_cursor` without a query or a 500, while a valid cursor from a previous `daemon_epoch` is refused `cursor_stale` and a valid current cursor serves the next page. test: `tests/servers/test_terminal_ws_create.py::test_cursor_validation_is_typed_on_ws_and_rest`.
- 4.4.8 - With a page-1 terminal exiting between pages and the client's cursor `seq` edited to a later in-range value, both consumers still drop that terminal: replay is filtered by the `seq` pinned from page one, and the `snapshot` block echoed on the tampered final page is ignored, so the edited value changes which rows are paged and nothing about causality. test: `tests/servers/test_terminal_ws_create.py::test_replay_filter_ignores_client_supplied_seq`.

### 4.5 Delete legacy tmux WS handlers and enforce the golden corpus on emitters [category: code] (depends: 4.2, 4.4)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/tmux.py::*` — scope-reason: the legacy `_handle_tmux_*` handlers and `_broadcast_tmux_event` are deleted
- `src/gobby/servers/websocket/server.py::*` — scope-reason: mixin registration drops the deleted handlers
- `src/gobby/servers/routes/attention.py::*` — scope-reason: roster entries drop the `tmux` block
- `src/gobby/terminals/ws_protocol.py`
- `tests/servers/test_tmux_mixin.py::*` — scope-reason: deleted with the handlers
- `tests/servers/test_attention_roster.py::*` — scope-reason: roster asserts the terminal block instead of `entry["tmux"]`
- `tests/servers/test_terminal_ws_golden.py`
- `tests/terminals/test_wire_golden.py`
- `tests/fixtures/terminal_ws_golden/manifest.json`
- `web/src/hooks/__tests__/useTmuxSessions.test.ts::*` — scope-reason: control, outcome, paste, refresh, and golden-loading cases
- `web/tests/style-surfaces.spec.ts::*` — scope-reason: the WS fake speaks golden shapes
- `web/tests/terminal-colors.spec.ts::*` — scope-reason: the WS fake speaks golden shapes
- `tests/servers/test_terminal_ws_create.py`

Closes C-13, C-14, C-15. Follows 4.2 (both edit `src/gobby/servers/routes/attention.py` and its roster
tests) and 4.4 (the golden corpus must capture 4.4's final `terminal_list` snapshot
block, cursor shape, and lifecycle `seq` fields, and both edit `ws_protocol.py`).

- C-14: `servers/websocket/tmux.py` `_handle_tmux_*` and `_broadcast_tmux_event` still
  emit `{streaming_id, session_name, socket}` / `sessions:[…]`; `test_tmux_mixin.py`
  asserts them; both Playwright fakes (`style-surfaces.spec.ts`, `terminal-colors.spec.ts`)
  answer the legacy shape and the hook tolerates it
  (`if (data.success || typeof data.attachment_id === "string")`). Delete the handlers,
  the broadcast, and their tests; the Playwright fakes speak the golden shapes; the hook
  requires `attachment_id` (4.6 owns the hook change — this leaf updates the fakes and
  leaves the hook's tolerance removal to 4.6, sequenced so both land before G runs the
  web group).
- C-15: the roster still carries `entry["tmux"]` and `test_roster_terminal_block`
  iterates an empty roster. Emit only the `terminal` block (`terminal_id`, `backend`,
  `state`, `attach_name`) and make the test seed a roster with one tmux and one native
  entry.
- C-13: `test_terminal_ws_golden.py` proves only `encode(decode(raw)) == raw`; real
  emitters already differ (`terminal_attach_result` adds `success`, `terminal_list` omits
  `limit`, broadcasts add `timestamp` / `attachment_id: None`), no web test loads the
  fixtures, and `golden_fixtures()` lives in production `ws_protocol.py`. Move the
  generator output to committed JSON under `tests/fixtures/terminal_ws_golden/` (one
  file per `GOLDEN_NAMES` entry plus a `manifest.json` listing them), drive the handlers with the request fixtures and compare their
  replies byte-for-byte against the reply fixtures, and load the same JSON in the vitest
  hook suite.

  The corpus gains one shape it does not have today: a *refused* `terminal_create_result`.
  2.4 makes the handler carry `code` on failure, and a corpus that pins only the success
  reply would let the refusal shape drift unchecked while 4.5.3 still passed. `GOLDEN_NAMES`
  and `manifest.json` therefore gain a `terminal_create_result_refused` entry whose reply
  fixture is produced by driving the real handler with a create request the runtime
  refuses, so the fixture is generated the same way every other reply fixture is rather
  than hand-authored.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `cd web && npx playwright test tests/style-surfaces.spec.ts tests/terminal-colors.spec.ts`.

**Acceptance:**

- 4.5.1 - No `tmux_*` WebSocket message type is handled or emitted; a `tmux_list_sessions` request is answered with `unknown_type`. test: `tests/servers/test_terminal_ws_create.py::test_legacy_tmux_messages_are_unknown`.
- 4.5.2 - Roster entries carry a `terminal` block and no `tmux` block for both backends. test: `tests/servers/test_attention_roster.py::test_roster_terminal_block`.
- 4.5.3 - Every emitter reply matches its committed golden byte-for-byte when driven by the golden request; `ws_protocol.py` contains no fixture generator. test: `tests/servers/test_terminal_ws_golden.py::test_emitters_match_golden_replies`.
- 4.5.4 - The vitest hook suite decodes every committed golden without error. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts`.
- 4.5.5 - The corpus pins both `terminal_create_result` shapes and each is produced by the real handler: the success reply carries no `code` key, and the refused reply carries `success: false` with the runtime's exact `code`. test: `tests/servers/test_terminal_ws_golden.py::test_emitters_match_golden_replies`.

### 4.6 Web terminal input path: control, write outcomes, paste, refresh, fragments [category: code] (depends: 4.5)
`kind: deliverable`

Targets:
- `web/src/hooks/useTmuxSessions.ts::*` — scope-reason: the hook gains take/release control, write-outcome reduction, lease-lost state, paste, and cursor-following refresh
- `web/src/hooks/terminalWriteSettlement.ts`
- `web/src/hooks/terminalRosterSnapshot.ts`
- `web/src/hooks/terminalAttachmentReadiness.ts`
- `web/src/hooks/__tests__/terminalWriteSettlement.test.ts`
- `web/src/hooks/__tests__/terminalRosterSnapshot.test.ts`
- `web/src/hooks/__tests__/terminalAttachmentReadiness.test.ts`
- `web/src/hooks/terminalWsFragments.ts`
- `web/src/components/activity/terminal/terminalSessions.ts::*` — scope-reason: session rows expose `controlState` and `readOnlyReason`
- `web/src/components/activity/terminal/TerminalTab.tsx::*` — scope-reason: the renderer's mount identity moves off the streaming id and history is routed by attachment
- `web/src/components/activity/terminal/TerminalView.tsx::*` — scope-reason: applies the bounded attach history once before the first keyframe, owns the truncation marker and the scroll-pin/jump-to-bottom behaviour, and signals renderer readiness
- `web/src/components/activity/terminal/__tests__/TerminalTab.test.tsx::*` — scope-reason: read-only and uncertain states
- `web/src/components/activity/terminal/__tests__/TerminalView.test.tsx::*` — scope-reason: history application, truncation marker, and readiness signalling
- `web/tests/terminal-history-scroll.spec.ts::*` — scope-reason: this leaf owns the whole history-scroll spec, whose cases are rewritten for bounded attach history, the truncation marker, scroll-pin, and the mobile touch tiers
- `web/src/hooks/__tests__/useTmuxSessions.test.ts::*` — scope-reason: control, outcome, paste, refresh, and golden-loading cases
- `web/src/hooks/__tests__/terminalWsFragments.test.ts`
- `web/src/components/activity/terminal/__tests__/terminalSessions.test.ts::*` — scope-reason: direct JoinedTerminalSession/TmuxSession fixtures and exact join expectations gain control/read-only fields
- `web/src/components/activity/terminal/__tests__/TerminalSessionList.test.tsx::*` — scope-reason: terminal row fixtures gain control/read-only fields
- `web/src/components/activity/__tests__/SessionsTab.test.tsx::*` — scope-reason: mocked useTmuxSessions results gain control/read-only fields and actions

Closes C-3, C-17, C-18, and Q-2 (the client half of Q-1, whose daemon half 4.3 owns).
Follows 4.5 because 4.5 rewrites the Playwright fakes and the
hook test module this leaf extends, and because the hook loads 4.5's committed goldens.

- C-3: the hook never sends `terminal_take_control` / `terminal_release_control` /
  `terminal_paste`, has no `terminal_write_outcome` case, and `terminal_lease_lost` /
  `terminal_control_result` only bump `leaseGenerationRef`, so the server refuses every
  keystroke (`WriteAdmit(False, "held")`) and the web terminal cannot type. Implement:
  `takeControl(terminalId)` on focus and on the first keystroke when not the holder.
  The triggering keystroke follows one queue-once policy shared with gclient (6.3):
  it is held as the single `pendingInput` of that control request; on
  `terminal_control_result{granted:true}` it is sent as the first write under the
  installed `attachment_id` / lease generation, then cleared; on
  `granted:false`, a request timeout (the hook's existing request deadline),
  `terminal_lease_lost`, detach, or reconnect it is discarded and the refusal is
  shown (read-only banner naming the reason), so no input survives into another
  attachment generation. While the request is pending, further keystrokes are
  refused locally (not queued) and the banner shows "acquiring control".
  `releaseControl` on blur/unmount; a `terminal_write_outcome` reducer that tracks
  in-flight `client_write_seq`s, marks `delivered` / `refused` / `indeterminate`.
  An `indeterminate` outcome puts the terminal in an "uncertain" state whose only
  exit paths are explicit: the banner's *retry* action re-sends the same payload
  under the same `client_write_seq` (the same `ws:{attachment_id}:{seq}` action key),
  which the coordinator resolves through its unresolved-write capture path and answers
  with a terminal `delivered` / `refused` / `suppressed` outcome that clears the state;
  the banner's *discard* action drops the in-flight entry and clears the state without
  a write (the latch stays server-side until the attachment finalizes, when 4.3 clears
  every `ws:{attachment_id}:` key; a later `unresolved_write_capacity` refusal inside
  the same attachment is surfaced like any other refusal); and
  `terminal_lease_lost` or a detach supersedes it with the read-only or detached state.
  No other keystroke is accepted while uncertain, so the user never issues a second
  uncontrolled write. `terminal_lease_lost` → read-only with a take-back affordance; `paste(text)` →
  `terminal_paste` with the oversize refusal surfaced. The hook requires `attachment_id`
  on `terminal_attach_result` (C-14's tolerance removed).
- C-18: refresh sends `refresh-${Date.now()}` but the replace branch matches only
  `"init" | "refresh"`, so every refresh merges and killed terminals never leave the
  roster; `sessionEnded` is computed per page. Tag refresh requests with a
  `kind: "refresh"` field, replace the roster only when the final page arrives, and
  compute `sessionEnded` over the full set. The hook implements the 4.4 snapshot
  contract: the `snapshot: {"daemon_epoch", "seq"}` of the **first** page is pinned
  before any continuation is issued, lifecycle events that arrive while a listing is in
  flight are buffered, the roster is installed atomically on the final page, buffered
  events with `seq > pinned.seq` are replayed in order and older ones dropped (a
  `snapshot` block echoed on a later page never moves the watermark), and a
  `cursor_stale` refusal or a `daemon_epoch` other than the pinned one restarts the
  listing, re-pinning from the new page one.
- C-17: `terminalWsFragments.ts` decodes each slice separately while the producer cuts
  raw bytes at arbitrary offsets (U+FFFD on multibyte boundaries, corrupted JSON), and
  `reducer.tick()` is never called from the hook so `fragment_timeout` cannot fire.
  Accumulate `Uint8Array`s and decode once on completion; drive `tick` from a
  `setInterval` owned by the hook. The reducer's existing bounds are kept and
  asserted, not re-derived: `TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES` (16 MiB per
  assembly, `fragment_too_large`), `TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES`
  (64 MiB across the socket, `fragment_socket_budget`), and the
  `TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT_MS` deadline measured from the
  assembly's `startedAt` (fixed, never refreshed by later fragments). The missing
  piece is lifetime: the hook calls `reducer.disconnect()` on socket close and
  before every reconnect so no partial assembly or socket-byte accounting survives
  a connection, and clears the `setInterval` on unmount.

  Two accounting holes in those budgets are closed so the byte caps really do bound
  memory. A non-final fragment carrying zero payload bytes is rejected outright with
  the existing `fragment_too_large` sibling code `fragment_invalid` (a well-formed
  emitter never sends one), and every retained fragment charges a fixed
  `TERMINAL_WS_FRAGMENT_OVERHEAD_BYTES` (256) against both the per-assembly and the
  socket budget in addition to its payload. Retained metadata is therefore paid for at
  the same rate as data, which bounds fragments per assembly and concurrent assemblies
  through the two existing budgets. Separate fragment-count and concurrent-assembly
  caps stay unbuilt: they would add two more limits and two more typed closes to
  express a bound the charged overhead already enforces.
- Q-2 (the client half of 4.3's Q-1; the reported symptom is that terminal history
  cannot be scrolled in the web UI). Today `useTmuxSessions` forwards `terminal_attach_history`
  the moment it arrives, `TerminalTab` filters it against a `streamingIdRef` that a
  layout effect updates, and it keys `TerminalView` by `streamingId`, so the history
  frame is either dropped as unknown or written into a view the next render replaces.
  Three changes, all local to the web client:
  1. **Readiness is the first viewport, and it is a rendezvous.** The branch has no usable sender: the only
     `terminal_set_viewport` the hook emits is `refreshTerminal`'s, which carries
     neither `attachment_id` nor dimensions, so `_handle_terminal_set_viewport` drops it
     on the first `isinstance` check; the sized path is the lease-gated
     `resizeTerminal`, which `TerminalTab.onReady` calls today. Add an observe-safe
     `setViewport(terminalId, { rows, cols })` that sends the installed `attachment_id`
     with real dimensions and requires no write lease — the daemon handler is
     attachment-scoped, not lease-gated, so an observer can signal readiness. `onReady`
     is not the trigger, because readiness and attachment identity have independent
     lifetimes once the renderer stops remounting (item 2): a renderer mounted before
     the first attachment installs fires its only `onReady` too early, and a renderer
     still mounted across reconnect never fires a second one for the replacement
     attachment, so both orders would leave a prepared pump silent forever. Renderer
     readiness and the last measured `{rows, cols}` are therefore tracked as state
     independent of attachment identity — `onReady` and the resize callback write them —
     and one effect keyed by the installed `attachment_id` sends exactly one
     `setViewport` for that attachment as soon as both a ready renderer and real
     dimensions exist, whichever arrives last. The effect records the attachment id it
     sent for, so re-renders, resizes, and a repeated ready callback never send a second
     activation for the same attachment; later resizes are ordinary viewport updates
     that carry no activation meaning. That first send is the activation signal 4.3
     waits for. No pre-init byte buffer is introduced — the daemon has not started its
     pump, so there is nothing to hold.
  2. **Stable mount identity.** `TerminalView` is keyed by `terminal_id`, never by the
     streaming or attachment id, so re-attach, take-control, and lease changes reuse the
     mounted renderer instead of remounting and discarding its scrollback. Frames are
     routed to it by the `attachment_id` on the frame, compared against the installed
     attachment rather than a ref written in a layout effect.
  3. **Each attachment replaces the window it renders.** The attach-history payload is a
     bounded recent window of the same pane, so a replacement attachment's payload
     overlaps the tail the retained renderer already holds; appending it would duplicate
     lines and shift every scroll offset. The installed `attachment_id` is therefore the
     replace boundary: when it changes, the mounted component resets its terminal buffer
     — the React instance and its DOM node stay, only the buffer is cleared — and then
     writes that attachment's history exactly once, ahead of any keyframe or output for
     it. A duplicate history frame for the installed attachment is ignored and one
     naming a superseded attachment is dropped. Scrollback therefore survives every
     state change that keeps the same attachment and is rebuilt from the authoritative
     bounded window whenever the attachment is replaced — the same window a fresh attach
     would show, with no cross-generation cursor or lineage field to maintain.

  Bounded-history contract (unchanged bounds, made explicit): the attach-history payload
  is the host's bounded window — `DEFAULT_TMUX_ATTACH_HISTORY_LINES` (500) by default,
  never more than `MAX_TMUX_ATTACH_HISTORY_LINES` (2,000) or
  `MAX_TMUX_ATTACH_HISTORY_MAX_BYTES` (256 KiB) — and the plan does not raise it. When
  the host reports the payload truncated, the view shows an explicit truncation marker
  at the top of the scrollback rather than implying the buffer starts there. Scrolling
  inside the delivered window is local to the renderer and must work: scrolling up pins
  the viewport while live output keeps arriving, the pin survives new output, and a
  jump-to-bottom control returns to following. Scrolling past the delivered window uses
  C-10's `SetScrollOffset` path and is clamped by the host, so the edge of the window is
  honest instead of silently empty.

  Mobile is why history-before-keyframe is load-bearing rather than cosmetic.
  `@wterm/dom` 0.3.3 forwards wheel events to the host under mouse tracking but ships no
  touch-to-mouse encoder, so a touch drag pans WTerm's own overflow and nothing else:
  whatever scrollback the renderer holds locally is the entire mobile scroll range.
  Applying the bounded attach history into the mounted renderer before the first
  keyframe is therefore the mechanism that makes mobile scrolling work at all, and it is
  asserted at the two mobile tiers (440×956 portrait and 932×430 landscape) as well as
  with desktop wheel and trackpad input.

  Live tier-preview validation is a close gate for this leaf, not optional visual QA.
  Against the running daemon at `https://mbp.tail4125a0.ts.net/?tier-preview`, drive
  Chrome through the Chrome DevTools MCP in all three canonical tier-preview modes —
  Portrait 440×956, Landscape 932×430, and Desktop 1440×900/fill. Use one deterministic
  disposable tmux terminal seeded with more than one viewport of numbered lines before
  attaching, select it inside the same-origin preview iframe, and in each mode show that
  an older numbered line becomes visible, that output arriving while scrolled away does
  not snap the viewport to the bottom, that returning to the bottom resumes following,
  and that the console records no errors; capture a screenshot per mode into the leaf's
  close evidence. For the two mobile modes emulate touch and a coarse pointer, and
  assert the WTerm element actually has vertical overflow and
  `touch-action: pan-y pinch-zoom`; a programmatic native scroll inside the iframe is
  acceptable where the MCP exposes no trusted swipe primitive. Keep the Chrome window
  visible for the whole run — this MCP can hang on occluded or minimized windows.

This leaf is decomposed up front rather than after it trips the ceiling.
`useTmuxSessions.ts` is 726 lines on the branch, and the work above adds four
independently testable state machines to it. Three of them move out as pure modules
with no React or WebSocket dependency, each unit-tested directly:
`terminalWriteSettlement.ts` (the control request, its single `pendingInput`, and the
`client_write_seq` outcome reducer including the uncertain state's retry/discard exits),
`terminalRosterSnapshot.ts` (the paginated listing, the buffered-lifecycle replay against
`snapshot.seq`, and the overflow restart), and `terminalAttachmentReadiness.ts` (renderer
readiness, last-known dimensions, once-per-attachment activation, and the
history/replace-boundary decision). `terminalWsFragments.ts` already exists and keeps its
own module. `useTmuxSessions` retains the socket lifecycle, the effects, the `setInterval`
that drives `tick`, and the wiring between those modules, which keeps every touched
production file well under the 1,000-line ceiling instead of relying on a later
decomposition task.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `cd web && npm run type-check && npm run lint`, `cd web && npx playwright test tests/terminal-history-scroll.spec.ts`, and the live tier-preview validation above in all three canonical modes with its screenshots in the close evidence.

**Acceptance:**

- 4.6.1 - Focusing a terminal the client does not hold sends `terminal_take_control`; a keystroke after `terminal_control_result{granted:true}` is delivered and its `terminal_write_outcome` clears the in-flight mark; the test fails without the take-control call. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts`.
- 4.6.2 - `terminal_lease_lost` puts the terminal in read-only state with a take-back action; an `indeterminate` outcome shows the uncertain banner, its retry re-sends the same `client_write_seq` and the resulting terminal outcome clears the banner, its discard clears it without a write, and a lease loss supersedes it. test: `web/src/components/activity/terminal/__tests__/TerminalTab.test.tsx`.
- 4.6.3 - A paste over the size limit is refused and surfaced; a normal paste is sent as `terminal_paste`. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts`.
- 4.6.4 - After a refresh, killed terminals disappear from the roster and `sessionEnded` reflects the full paginated set; a `terminal_created` received between pages is present after install and a pre-snapshot event is dropped. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts`.
- 4.6.5 - A fragmented message cut inside a multibyte sequence reassembles to the original JSON, and a missing final fragment fires `fragment_timeout` after the configured interval. test: `web/src/hooks/__tests__/terminalWsFragments.test.ts`.
- 4.6.6 - When 1,025 lifecycle events arrive during pagination, the web client discards the partial roster, starts one fresh snapshot, applies no stale buffered event, and converges to the authoritative roster with bounded memory. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::paging_overflow_restarts_from_fresh_snapshot`.
- 4.6.7 - The keystroke that triggers `terminal_take_control` is delivered exactly once under the installed generation after `granted:true`, and is discarded with a visible refusal on `granted:false`, request timeout, `terminal_lease_lost`, detach, and reconnect; a second keystroke during the pending request is refused locally and never queued. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::triggering_keystroke_settles_once`.
- 4.6.8 - A stream of non-final fragments that never completes is dropped with `fragment_timeout` at exactly `startedAt + TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT_MS` even though fragments keep arriving (the deadline does not slide); an assembly crossing 16 MiB is dropped `fragment_too_large` and five concurrent assemblies whose running total crosses 64 MiB drop the crossing one `fragment_socket_budget` (the reducer is constructed with reduced `maxReassemblyBytes` / `maxSocketBytes` so the test stays small); and a socket close with assemblies in flight leaves `socketBytes === 0` and no buffers after the hook's `disconnect()`. test: `web/src/hooks/__tests__/terminalWsFragments.test.ts`.
- 4.6.9 - The hook's `setViewport` sends the installed `attachment_id` with real dimensions and works without a write lease (an observer activates the stream), and exactly one activation is sent per attachment in each of three orders: renderer ready before the attachment installs, attachment installed before the renderer reports ready, and a reconnect that installs a replacement attachment on a still-mounted renderer. It is the first viewport the hook emits for that attachment, repeated ready and resize callbacks send no second activation, and the attach-history frame for the attachment is applied to the renderer exactly once, before any keyframe or output; a history frame naming a superseded attachment is dropped and a duplicate is ignored. test: `web/src/components/activity/terminal/__tests__/TerminalView.test.tsx`.
- 4.6.10 - Every state change that keeps the same installed attachment (take control, release, lease-generation bump, write-outcome and banner transitions) keeps both the mounted `TerminalView` instance and its scrollback: the component is keyed by `terminal_id`, and a test that re-keys it by the streaming id fails. test: `web/src/components/activity/terminal/__tests__/TerminalTab.test.tsx`.
- 4.6.11 - In a real browser at desktop size, against a terminal whose pre-attach history exceeds one viewport, the mounted renderer shows that history after attach; wheel and trackpad scrolling reveal a line that was above the viewport, live output arriving while scrolled up does not snap the viewport back, jump-to-bottom resumes following, and a truncated payload renders the truncation marker at the top. test: `web/tests/terminal-history-scroll.spec.ts`.
- 4.6.12 - Live tier-preview validation passes against the running daemon at `https://mbp.tail4125a0.ts.net/?tier-preview` in Portrait 440×956, Landscape 932×430, and Desktop 1440×900/fill: with a disposable tmux terminal holding more than one viewport of numbered pre-attach history selected in the preview iframe, each mode reveals an older numbered line, does not snap to the bottom while output arrives, resumes follow on return to the bottom, and logs no console errors; the mobile modes run under emulated touch with the WTerm element showing vertical overflow and `touch-action: pan-y pinch-zoom`; one screenshot per mode is recorded in the leaf's close evidence. behavior: "live tier-preview validation" in `.gobby/plans/herdr-terminal-client-qa-fixes.md`.
- 4.6.13 - The same terminal at the 440×956 and 932×430 mobile tiers scrolls by touch (or the browser-equivalent native scroll gesture): a line above the initial viewport becomes visible, live output does not snap the viewport back, and returning to the bottom resumes following. The test fails if the attach history is applied after the first keyframe, because the renderer then has nothing local to pan. test: `web/tests/terminal-history-scroll.spec.ts`.
- 4.6.14 - Metadata is charged: a zero-payload non-final fragment is refused `fragment_invalid` and drops its assembly; a flood of one-byte fragments on a single id hits `fragment_too_large` from charged overhead alone, before the deadline; and a flood of distinct assembly ids each holding one one-byte fragment hits `fragment_socket_budget` from charged overhead alone (reduced caps in the test), after which `socketBytes` returns to 0 once the tracked assemblies are dropped. test: `web/src/hooks/__tests__/terminalWsFragments.test.ts`.
- 4.6.15 - A real host-to-web attach uses 500 history lines by default, clamps configured history to 2,000 lines and 256 KiB, reports truncation when either ceiling cuts the payload, and renders exactly the capped window with the truncation marker. test: `web/tests/terminal-history-scroll.spec.ts::bounded_history_caps_and_truncation`.
- 4.6.16 - Take-back, retry, discard, and jump-to-bottom use existing shared controls and are keyboard operable with visible AA focus, non-color-only state cues, 44×44 coarse-pointer targets, and correct light/dark rendering in the canonical portrait, landscape, and desktop tiers. test: `web/tests/terminal-history-scroll.spec.ts::terminal_actions_meet_accessibility_contract`.
- 4.6.17 - A reconnect that installs a replacement attachment whose bounded history overlaps the tail already rendered resets the mounted renderer's buffer before applying it: the same component instance is retained, every numbered line appears exactly once in order, the truncation marker appears only when that payload is itself truncated, the scroll position derives from the new window, and the attachment's first keyframe follows its history. test: `web/src/components/activity/terminal/__tests__/TerminalView.test.tsx::replacement_attachment_replaces_the_rendered_window`.
- 4.6.18 - Write/control settlement, paginated roster snapshot replay, and attachment readiness with history routing each live in their own pure module that `useTmuxSessions` imports, leaving the hook as the socket-and-effect composition layer, and every production TypeScript file this leaf writes — the hook and each new module — is under 1,000 lines at close. file: `web/src/hooks/terminalWriteSettlement.ts`.

## P5: Default backend and flip gate
`kind: framing`

**Goal**: The shipped default is `tmux`, the flip evidence is honest, and the checker
would have rejected the fabricated artifact mechanically.

### 5.1 Revert the default backend to tmux with honest evidence [category: code] (depends: P1, 2.7)
`kind: deliverable`

Targets:
- `src/gobby/config/terminals.py`
- `src/gobby/install/shared/config/config.yaml::*` — scope-reason: the `terminals.default_backend` value changes
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated wholesale
- `docs/evidence/native-backend-flip.md`
- `docs/guides/gterminal-development-guide.md`
- `tests/terminals/test_backend_selection.py`
- `tests/config/test_terminals.py`

Closes E-1, E-15.

`docs/evidence/native-backend-flip.md` cites `2026-W33` / `2026-W34` scheduled runs
(`…/actions/runs/2026081001`, `…/2026081701`) for commits authored on 2026-08-21, the
producer workflow first exists in one of those commits, all four run URLs are 404, the
run ids are date-encoded, and the bug query is `priority=1` not `priority <= 1`.
`tests/terminals/test_backend_selection.py` asserts the committed artifact passes the
gate, so the suite is green only because the artifact was shaped to the checker. The
default was flipped on that basis.

- `TerminalConfig.default_backend` default → `"tmux"`; `config.yaml` → `tmux`;
  regenerate `runtime_config_contract.json`. Runtime configuration is the persisted
  DB override set applied over the Pydantic defaults (`config/runtime_activation.py`);
  the bundled `config.yaml` is an export template that no runtime path reads, so the
  model default is the effective rollback for every hub that carries no
  `terminals.default_backend` override — fresh installs and upgraded hubs alike.
  Override policy: the branch never wrote an override (its flip changed the default
  and the template only), so a persisted `terminals.default_backend` can only be
  user-authored through `gobby-config:patch_config_values` and is preserved as the
  user's explicit choice; no config migration rewrites it. The effective projection
  is asserted, not assumed: `tests/config/test_terminals.py` builds the runtime
  config through the stored-snapshot seam with (a) no overrides and (b) an explicit
  `{"terminals": {"default_backend": "native"}}` override and checks the resolved
  `default_backend` and the backend a default spawn request resolves to
  (`resolve_terminal_backend(None, config)`).
- Replace the artifact with an honest stub: the two `## Run` sections are removed, a
  `## Status` section states "gate not satisfied: no scheduled runs exist yet", and the
  checker returns `ok=False` with reason `no_runs`. The committed-artifact assertion in
  `test_backend_selection.py` becomes conditional: when `default_backend == "native"` the
  artifact must pass; when `"tmux"` the test asserts the artifact is rejected with
  `no_runs`. Every `test_flip_gate_rejects_every_nonconforming_artifact` fixture stays.
- `docs/guides/gterminal-development-guide.md` rollback note: the live value is the
  model default plus any persisted override; an operator changes it through the
  `gobby-config` MCP server (`get_config_values` for the current `revision`, then
  `patch_config_values(expected_revision=…, values={"terminals": {"default_backend":
  "tmux"}})`); the bundled template is not a live setting and there is no
  `gobby config` CLI.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out).

**Acceptance:**

- 5.1.1 - A daemon with default configuration and no `gterm` binary boots `healthy` (not degraded) and a default spawn creates a tmux-backed terminal row. test: `tests/terminals/test_backend_selection.py::test_default_backend_is_tmux`.
- 5.1.2 - The committed evidence artifact is rejected by the gate with `no_runs`, and the assertion that it passes is active only when the default is `native`. test: `tests/terminals/test_backend_selection.py::test_committed_flip_artifact_state_matches_default`.
- 5.1.3 - The checked-in runtime config contract reports `terminals.default_backend = "tmux"`. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 5.1.4 - The development guide's rollback instructions name the `gobby-config` `patch_config_values` surface with its `expected_revision` and state that the bundled template is not a live setting. file: `docs/guides/gterminal-development-guide.md`.
- 5.1.5 - Built through the stored-snapshot seam, a config with no overrides resolves `default_backend == "tmux"` and a default spawn request resolves to the tmux backend, while an explicit `{"terminals": {"default_backend": "native"}}` override resolves to native for both — the persisted user choice is honored and nothing rewrites it. test: `tests/config/test_terminals.py::test_default_backend_projection_honors_overrides`.

### 5.2 Harden the flip checker and the weekly producer [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `src/gobby/config/terminals.py`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated wholesale if the checker changes add any config-visible field
- `scripts/verify_native_flip_evidence.py`
- `.github/workflows/terminal-parity-weekly.yml`
- `tests/config/test_terminals.py`
- `tests/terminals/test_backend_selection.py`
- `tests/scripts/test_verify_native_flip_evidence.py`

Closes E-9, E-10, and closes the fabricated-evidence class rather than narrowing it:
the offline checker rejects implausible shapes, and a remote verifier binds every
record to GitHub's own run metadata. Source-plan mapping, one item each: source item
5.3.3 (the gate checker's rejection matrix) is re-satisfied by 5.2.6, which keeps the
source test and its full fixture set green under the hardened checker, plus 5.2.1 and
5.2.2 for the new temporal and latest-pair rules; source item 5.3.4 (the evidence
artifact's recorded fields) is re-satisfied by 5.2.3 and 5.2.6, which pin the recorded
field set; source items 5.3.1 and 5.3.2 (the flip itself and its post-flip
preservation proof) are deferred in D1. The hardened `_check_slots` changes one
verdict class only: a non-qualifying slot elsewhere in the file no longer fails the
gate when a later adjacent qualifying pair exists (5.2.2). Every rejection row of
the source matrix keeps its verdict because each names a defect inside the pair
under test, and the positive fixtures keep passing.

- E-10: `check_native_backend_flip` validates neither `utc_timestamp ∈ weekly_slot`,
  nor `run_url` / `commit_sha` presence, nor run-id shape; `_check_slots` fails the gate
  on any non-qualifying slot anywhere in the file (blocking append-as-you-go evidence);
  `_latest_adjacent_pair` checks bug freshness against the later slot of the pair rather
  than the latest run; the bug query is `priority=1`. Add temporal-plausibility checks:
  `utc_timestamp` falls inside `weekly_slot`'s ISO week; `run_url` matches
  `https://github.com/<owner>/<repo>/actions/runs/<digits>` (no offline rule can
  distinguish a date-shaped id from a real one — `2026081001` is an ordinary integer
  above 10⁹ and real run ids will pass 2.0 × 10¹⁰ — so id authenticity is the remote
  verifier's job, below); `commit_sha` is 10–40 hex; the producer
  workflow file must be older than the run (`workflow_first_commit_at < utc_timestamp`,
  a field the producer writes). Let non-qualifying slots coexist (only the latest
  adjacent qualifying pair counts); check bug freshness against the latest run; require
  `priority <= 1` in the recorded bug query string.
- E-9: the weekly workflow records only `package_install`; the `4.3:` / `3.6:` lines
  the checker requires are never produced and 5.1/5.2 pass/fail is not captured
  (`if: always()` records on failure). Emit every required line from the actual job
  results (`4.3: pass|fail`, `3.6: pass|fail`, `package_install: …`, `run_url`,
  `commit_sha`, `utc_timestamp`, `workflow_first_commit_at`), and record only when the
  parity jobs succeeded. The two-OS matrix cells never write: each uploads its result
  lines as an artifact, and one post-matrix `record` job (`needs: [parity]`, `if:
  success() && github.event_name == 'schedule'` — a `workflow_dispatch` run exercises
  the matrix and records nothing, so no evidence the verifier would reject as
  non-schedule is ever committed; the only job granted `permissions: contents: write`;
  checkout with `persist-credentials: true` and `fetch-depth: 0`) downloads both
  artifacts, renders one slot carrying both platform records, and commits the
  evidence line to
  `docs/evidence/native-backend-flip.md` exactly once (bot author
  `github-actions[bot]`, message `[gobby-parity] record <weekly_slot>`), so a
  hand-authored slot is visible as a non-bot commit in
  `git log -- docs/evidence/native-backend-flip.md` and two cells can never race on
  one slot. SHA-pin `actions/upload-artifact`, `actions/download-artifact`, and
  `astral-sh/setup-uv` like every other action in the file.

  Field producers match the verifier exactly. `utc_timestamp` is the run's own
  creation time, read in the `record` job from
  `gh api repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID} --jq .created_at`
  (never `date` at record time, which on a long build lands an hour after creation);
  `workflow_first_commit_at` is
  `git log --diff-filter=A --format=%cI --reverse -- .github/workflows/terminal-parity-weekly.yml | head -1`
  over the full history the `fetch-depth: 0` checkout provides (a shallow checkout
  makes that command answer the tip commit and is exactly the bug); `weekly_slot` is
  the ISO week of `utc_timestamp`. The remote verifier compares `utc_timestamp` to the
  API's `created_at` for equality (both RFC 3339 UTC), so the recorded value and the
  verified value share one source.
- Remote provenance. Shape checks cannot distinguish a well-formed invented run from a
  real one, so the flip gate has two layers. The offline checker above stays
  network-free (it runs in daemon config validation and in G). The new
  `scripts/verify_native_flip_evidence.py` resolves every slot's `run_url` through
  `gh api repos/{owner}/{repo}/actions/runs/{id}` (and the run's jobs) and fails unless
  all of: `head_sha == commit_sha`; `path == .github/workflows/terminal-parity-weekly.yml`;
  `event == schedule`; `conclusion == success`; `created_at == utc_timestamp`; the
  jobs named in the `4.3:` / `3.6:` lines concluded `success`;
  and `git log` shows the slot's evidence commit authored by the bot. It reads the
  GitHub token from `GH_TOKEN` / `gh auth`, prints one `ok|fail: <reason>` line per
  slot, exits non-zero on any failure, and is the required pre-condition of the D1
  flip (D1's acceptance runs it against the two adjacent slots). Tests drive it with a
  recorded `gh api` fixture set covering: matching run; SHA mismatch; non-schedule
  event; failed job; timestamp drift; 404 run; non-bot evidence commit.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `GOBBY_TEST_PROTECT=1 uv run pytest tests/scripts/test_verify_native_flip_evidence.py`.

**Acceptance:**

- 5.2.1 - The fabricated artifact from `518cec5c41` (kept as a test fixture) is rejected offline for timestamps outside the slot, a producer newer than the run, and the `priority=1` bug query; its run ids are rejected by the remote verifier's 404 case (5.2.5), never by an offline shape rule. test: `tests/config/test_terminals.py::test_flip_gate_rejects_temporally_implausible_runs`.
- 5.2.2 - An artifact with one bad slot followed by two qualifying adjacent slots passes; bug freshness is judged against the latest run. test: `tests/config/test_terminals.py::test_flip_gate_latest_adjacent_pair_only`.
- 5.2.3 - A recorded artifact rendered from the workflow template contains every line the checker requires for both platforms, is produced only when every parity cell succeeded and the event is `schedule` (a `workflow_dispatch` run records nothing), and the single post-matrix `record` job — the only job with `contents: write`, checked out with `fetch-depth: 0` — authors the evidence line as the bot with `utc_timestamp` taken from the run's API `created_at` and `workflow_first_commit_at` from the workflow file's first commit. test: `tests/terminals/test_backend_selection.py::test_weekly_workflow_emits_required_lines`.
- 5.2.4 - Every `uses:` in the weekly workflow is SHA-pinned. file: `.github/workflows/terminal-parity-weekly.yml`.
- 5.2.5 - A well-formed slot whose run does not exist, whose `head_sha` differs, whose event is not `schedule`, whose parity job failed, whose `utc_timestamp` differs from the run's `created_at` (the fixture's run finished 70 minutes after creation and the slot carries the finish time), or whose evidence commit is not bot-authored fails the remote verifier with the matching reason, and a fully consistent slot passes. test: `tests/scripts/test_verify_native_flip_evidence.py::test_remote_verifier_binds_each_provenance_field`.
- 5.2.6 - Under the hardened checker the source 5.3.3 matrix keeps every verdict: rejection of runs one day apart, non-adjacent weekly slots (including a skipped week across a year boundary), a macOS-only slot paired with a Linux-only slot, adjacent slots where either slot is missing a platform, a `4.3:` / `3.6:` / package-install line missing from either slot, a bug count timestamped before the later run, a bug count just outside the inclusive 24-hour UTC window, and a same-year numeric `W(n)`/`W(n+1)` pair that is not consecutive Mondays; acceptance of a fully conforming artifact, the ISO `52→01` and `53→01` pairs, and the exact-boundary 24-hour fixture; and every qualifying run records workflow, weekly slot id, `run_url`, `commit_sha`, `utc_timestamp`, `workflow_first_commit_at`, platforms, the per-slot package-install result, the `4.3:` and `3.6:` lines, and the dated open-bug count with its query and query timestamp. test: `tests/terminals/test_backend_selection.py::test_flip_gate_rejects_every_nonconforming_artifact`.

## P6: gclient
`kind: framing`

**Goal**: `gclient` is the herdr v0.8.0 chrome rewired onto the daemon's REST/WS
control plane and the host's direct frame socket, with a real event loop — the 3.3 /
3.5 scope of the source plan, built for real.

The reference checkout is `~/.gobby/clones/herdr` at tag `v0.8.0`. herdr `src/ui/`
at that tag: `sidebar.rs` 2,848 lines, `mobile.rs` 1,514, `panes.rs` 1,498,
`dialogs.rs` 946, `navigator.rs` 643, `release_notes.rs` 559, `tabs.rs` 507,
`keybind_help.rs` 420, `settings.rs` 408, `sidebar/tokens.rs` 337, `tab_surface.rs` 327,
`status.rs` 326, `menus.rs` 315, `widgets.rs` 278, `scrollbar.rs` 191, `onboarding.rs`
114, `text.rs` 89; `src/ui.rs` 1,561; `src/layout.rs` 957; `src/raw_input.rs` 2,858.
`crates/gclient/src` today is 2,765 lines total with `ui/*.rs` stubs of 11–70 lines;
`crates/gterminal/src/lib.rs` already exports `input`, `layout`, `protocol`,
`raw_input`, `selection`, and `terminal_theme` for reuse, and `crates/gclient/Cargo.toml`
depends on `gobby-terminal` (default-features off), `ratatui 0.30`, `reqwest`, and
`tokio-tungstenite` (currently unused).

### 6.1 Import and carve the herdr chrome [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/mod.rs`
- `crates/gclient/src/ui/sidebar.rs`
- `crates/gclient/src/ui/sidebar_rows.rs`
- `crates/gclient/src/ui/sidebar_tokens.rs`
- `crates/gclient/src/ui/panes.rs`
- `crates/gclient/src/ui/pane_layout.rs`
- `crates/gclient/src/ui/dialogs.rs`
- `crates/gclient/src/ui/navigator.rs`
- `crates/gclient/src/ui/tabs.rs`
- `crates/gclient/src/ui/tab_surface.rs`
- `crates/gclient/src/ui/keybind_help.rs`
- `crates/gclient/src/ui/settings.rs`
- `crates/gclient/src/ui/status.rs`
- `crates/gclient/src/ui/widgets.rs`
- `crates/gclient/src/ui/scrollbar.rs`
- `crates/gclient/src/ui/text.rs`
- `crates/gclient/src/ui/chrome.rs`
- `crates/gclient/src/ui/chrome_render.rs`
- `crates/gclient/src/theme.rs`
- `crates/gclient/UPSTREAM.md`
- `crates/gclient/NOTICE.md`
- `crates/gclient/tests/ui_carve_guard.rs`
- `crates/gclient/tests/source_size.rs`
- `crates/gclient/tests/workspace.rs`

Closes the import half of D-1.

Bring herdr v0.8.0 `src/ui/` into `crates/gclient/src/ui/` for the keep-set, preserving
glyphs, layout arithmetic, truncation rules, and focus junctions exactly, and rewiring
data access from herdr's agents/workspaces model to Gobby's roster/terminal model:

Module map (herdr → gclient):

- `ui/sidebar.rs` (2,848) → `ui/sidebar.rs` + `ui/sidebar_rows.rs`: row rendering split out; both under 1,000 lines.
- `ui/sidebar/tokens.rs` (337) → `ui/sidebar_tokens.rs`: the token-usage column maps to Gobby session token stats.
- `ui/panes.rs` (1,498) → `ui/panes.rs` + `ui/pane_layout.rs`: BSP layout from `gobby_terminal::layout`.
- `ui/dialogs.rs` (946) → `ui/dialogs.rs`: plugin and worktree dialogs dropped.
- `ui/navigator.rs` (643), `ui/tabs.rs` (507), `ui/tab_surface.rs` (327), `ui/status.rs` (326), `ui/widgets.rs` (278), `ui/scrollbar.rs` (191), `ui/text.rs` (89) → same names.
- `ui/keybind_help.rs` (420) → same name, on the gclient keymap.
- `ui/settings.rs` (408) → `ui/settings.rs`: client-local preferences only.
- `src/ui.rs` (1,561) chrome parts → `ui/chrome.rs` + `ui/chrome_render.rs`: frame composition, focus ring, split borders.
- Dropped, listed in `UPSTREAM.md` as rejected: `ui/mobile.rs`, `ui/onboarding.rs`, `ui/release_notes.rs`, the `ui/menus.rs` plugin entries, agent-detection indicators, worktree/persistence surfaces.

Every imported file stays under 1,000 lines. Reuse `gobby_terminal::layout` (BSP),
`gobby_terminal::raw_input` (bracketed paste), `gobby_terminal::input`,
`gobby_terminal::selection`, and `gobby_terminal::terminal_theme`; never copy them.
`theme.rs` maps herdr's palette names to the Gobby theme read from `.impeccable.md`
(load the `impeccable` skill before editing colors; theme values are the one permitted
divergence from herdr). Because 7.1's token map normalizes theme values away, the
divergence gets its own contract test in `workspace.rs` (6.1.5): every herdr token
resolves to a value in the `.impeccable.md` state palette, each pair of state colors
(success, warning, error, info, neutral) differs in relative luminance by enough to
survive a grayscale render, and the focus ring uses the brand accent rather than a
hue shift alone; 7.2's divergence checklist cites that test for the theme row.
`UPSTREAM.md` carries a per-module accept/reject/rename map
with the herdr commit (`v0.8.0`) and per-file line counts; `NOTICE.md` describes what is
actually imported. `ui_carve_guard.rs` becomes real: it parses `UPSTREAM.md`, asserts
every accepted module exists with a `// upstream: herdr v0.8.0 src/ui/<file>` header,
every rejected module is absent, and no file under `crates/gclient/src` exceeds 999
lines (`source_size.rs` already enforces the ceiling; keep both).

The ported `TestBackend` render tests are the deliverable of 7.1; this leaf must leave
each module compiling with its herdr test module removed or `#[cfg(test)]`-ported as
part of 7.1's scope (no placeholder tests).

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out); `cargo clippy -p gobby-client --all-targets -- -D warnings` with no new `allow`s.

**Acceptance:**

- 6.1.1 - Every keep-set module exists under `crates/gclient/src/ui/` with the upstream header, every dropped module is absent, and the carve guard fails when a rejected module is added or a header is removed. test: `crates/gclient/tests/ui_carve_guard.rs::carve_matches_upstream_map`.
- 6.1.2 - No file under `crates/gclient/src` reaches 1,000 lines. test: `crates/gclient/tests/source_size.rs`.
- 6.1.3 - `gclient` links against `gobby_terminal::{layout, raw_input, input, selection, terminal_theme}` and contains no copied `layout.rs` / `raw_input.rs`. file: `crates/gclient/UPSTREAM.md`.
- 6.1.4 - `render_workspace` composes sidebar, tabs, pane surface, status bar, and dialogs from the imported modules for a scripted workspace of two terminals and one attention prompt, rendering into a `TestBackend` 120×40 without panicking. test: `crates/gclient/tests/workspace.rs::render_workspace_composes_imported_chrome`.
- 6.1.5 - The gclient theme maps every herdr token to an .impeccable.md-permitted TUI value, preserves deutan-safe and grayscale state distinctions, and renders keyboard focus without hue-only cues. test: `crates/gclient/tests/workspace.rs::theme_contract_is_accessible`.

### 6.2 Daemon data plane: REST and WebSocket [category: code] (depends: P1, 4.5)
`kind: deliverable`

Targets:
- `crates/gclient/src/daemon/mod.rs`
- `crates/gclient/src/daemon/ws.rs`
- `crates/gclient/src/daemon/rest.rs`
- `crates/gclient/src/daemon/live.rs`
- `crates/gclient/src/startup.rs`
- `crates/gclient/tests/ws_golden.rs`
- `crates/gclient/tests/reconciliation.rs`
- `crates/gclient/tests/daemon_live.rs`
- `crates/gclient/tests/mock_daemon/mod.rs`

Closes the data-plane half of D-1 (source 3.3.2, 3.3.5). Depends on 4.5 because the
committed golden corpus this leaf validates against is 4.5's output (and 4.5 already
carries 4.4's snapshot/seq fields).

`daemon/mod.rs` has only `ScriptedDaemon`; `daemon/ws.rs` is a codec; `reqwest` is used
only by the health probe; `tokio-tungstenite` has zero uses.

- Define `trait Daemon` (async): `list_terminals(cursor) -> Page<TerminalRow>`,
  `roster() -> Vec<RosterEntry>`, `respond(entry, attention_id, answer)`, `mark_seen`,
  `spawn(request) -> SpawnOutcome`, `terminate(terminal_id) -> KillOutcome`,
  `subscribe() -> EventStream`, `send(WsMessage)`, `notify(WsMessage)`.
  `send` and `notify` split the two settlement contracts the protocol actually has:
  `send` is for correlated request/reply verbs and pends on a `*_result`, while `notify`
  is for one-way messages the daemon never answers and resolves as soon as the socket
  write completes (it still fails with `Unavailable` if the write itself fails or the
  generation is already closed, and it registers nothing in the `request_id` map).
  `terminal_set_viewport` is the one-way case that forces the distinction:
  `_handle_terminal_set_viewport` answers nothing on success — only `terminal_error`
  with `invalid_dimensions` on a bad size — so routing 6.3's activation through `send`
  would leave a pending sender parked at exactly the transition that starts frames until
  the connection dropped. Every viewport activation and resize goes through `notify`;
  terminal input, `terminal_create`, and `terminal_kill` stay on `send`.
  `ScriptedDaemon` stays for tests;
  `LiveDaemon` (`daemon/live.rs`) implements it with `reqwest` against the routes the
  branch actually serves — `GET /api/terminals` (cursor pages, follows `next_cursor` to
  termination), `GET /api/terminals/{id}`, `/api/attention/roster`,
  `/api/attention/respond`, `/api/attention/seen` — and `tokio-tungstenite` against
  `/ws` using the bearer token `startup::ProbeEnv` already resolves. Spawn and
  terminate use the existing WebSocket verbs `terminal_create` / `terminal_kill`
  (there is no REST POST/DELETE for terminals on the branch and this plan adds none);
  `spawn` sends `terminal_create` and resolves on the correlated `terminal_create_result`,
  `terminate` sends `terminal_kill` and resolves on `terminal_kill_result`, both mapped
  to typed outcomes (`Created{terminal_id}`, `Refused{code}`, `Timeout`). Every REST
  call carries the bearer; a 401/403 maps to `DaemonError::Unauthorized`, 404 to
  `NotFound`, 5xx/connection errors to `Unavailable{retry_after}`, and a body that
  fails the golden decoder to `Protocol{detail}` — never a panic or a silent `Ok`.
  Socket ownership: `LiveDaemon` owns exactly one reader task per socket generation
  and it is the only code that reads the WebSocket. Requests carry a `request_id`
  minted from a monotonic counter (the daemon echoes it on every `*_result`, as the
  branch's `terminal_ws.py` already does); `spawn` / `terminate` register a
  `oneshot` in a `request_id → sender` map before writing, and the reader routes each
  `*_result` to its sender, drops an unmatched or older-generation `request_id`, and
  fans lifecycle messages out to every `subscribe()` receiver through bounded
  channels (256 entries; a receiver that falls behind is closed with a typed
  `Lagged` event and must re-list). On EOF, a read error, cancellation, `close`, or
  a reconnect, the reader fails every pending sender with
  `DaemonError::Unavailable` and clears the map before the replacement socket
  accepts requests, so an in-flight `spawn` can never resolve against a reply on a
  later generation.

  Terminal writes settle on a second map, because the protocol does not correlate them
  by `request_id`. `_handle_terminal_input`'s reply is `terminal_write_outcome`, whose
  fields are `terminal_id`, `attachment_id`, `client_write_seq`, `outcome`, and
  `reason` — the daemon never echoes a `request_id` on it, and only the listing,
  `terminal_create`, and `terminal_kill` verbs carry one. `send` therefore registers its
  `oneshot` in a `(attachment_id, client_write_seq) → sender` map keyed by the
  correlation the wire actually has, and the reader routes each `terminal_write_outcome`
  through that map. Everything else about the contract is unchanged: an unmatched or
  older-generation key is dropped, and EOF, a read error, cancellation, `close`, and
  reconnect fail and clear both maps together before the replacement socket accepts
  requests.

  Writes are not the only reply the protocol routes by attachment. `terminal_control_result`
  — the reply to both `terminal_take_control` and `terminal_release_control`, and the one
  6.3's queue-once keystroke waits on — carries `attachment_id`, `granted`, `reason`, and
  `lease_generation` and no `request_id`, so the `request_id` map cannot settle it either
  and 6.3.8's `granted:true` / `granted:false` transitions would have no sender at all.
  It gets the smallest correlation the wire supports: an attachment-scoped **single-flight**
  control waiter, `attachment_id → sender`, registered before the write and rejecting a
  second concurrent control request on the same attachment locally with the typed
  `ControlRequestInFlight` — single-flight rather than a queue because the pane already
  refuses further keystrokes while its request is pending, so a second outstanding control
  request per attachment is a client bug, not a schedule to support. Registration,
  routing, and teardown follow the write map exactly: an unmatched or older-generation
  `attachment_id` is dropped, and EOF, a read error, cancellation, `close`, reconnect,
  attachment finalization, and the request deadline fail and remove the waiter with the
  same typed error. That is three maps and one rule, not three rules.

  Single-flight bounds concurrent reuse of that key; the deadline is what would bound
  *sequential* reuse, and on its own it does not. `terminal_control_result` is the reply
  to both `terminal_take_control` and `terminal_release_control` and its four fields are
  identical in either case, so the only thing separating one operation's reply from the
  next is that the key is retired between them. Every other entry in the fence list
  retires it — EOF, a read error, cancellation, `close`, and reconnect end the
  generation, and finalization ends the attachment — but a deadline alone leaves both the
  attachment and the socket live, so the next take or release registers under the same
  `attachment_id` and a late reply from the timed-out request resolves it, a stale take
  satisfying a release or the reverse. The deadline therefore retires the key the same
  way every other fence does: expiring a control request finalizes that attachment
  locally and drives the replacement attach 6.3 already performs on the direct-to-proxy
  fallback, so the pane continues under a new daemon-minted `attachment_id` and the late
  reply misses the lookup and takes the unmatched-key drop. That is sound as a policy and
  not only as a correlation trick — a control request that never answered leaves the
  lease state unknown, so continuing to issue control on that attachment is unsound
  regardless of which reply eventually arrives. A `request_id` echoed on
  `terminal_control_result` would also work, and is rejected for the reason given below
  for `terminal_input`: it edits 4.3's handler, 4.5's codec, and the committed corpus
  from a section that owns none of the three, to add a correlation the attachment
  lifecycle already supplies. Every other
  attachment-routed message the daemon emits is classified rather than mapped:
  `terminal_set_viewport` and later resizes are one-way and go through `notify` (above);
  `terminal_scroll_offset_applied` is not consumed by `gclient`, whose copy mode reads
  history through the frame source's `AttachHistory` / `SetScrollOffset` (6.4), so the
  reader treats it as a lifecycle message and fans it out rather than registering a
  waiter for it. A verb that later needs a reply gains a waiter under this same rule.

  Those five events are all connection-scoped, and the key is not. An attachment can be
  finalized and replaced while the socket stays up — 6.3's direct-to-proxy fallback does
  exactly that — and none of them fires, so a write whose outcome the daemon will never
  send (it finalized the attachment instead of answering) parks its sender until the next
  reconnect, on a connection that is otherwise healthy. The workspace tombstone 6.3
  installs cannot help: it sits above `LiveDaemon` and never sees a reply the pending map
  consumed first. Attachment finalization is therefore a fence on the write map as well.
  The daemon-published event that fires is `terminal_attachment_finalized` — the name
  `terminal_ws.py`, the proxy relay, and the committed
  `terminal_ws_golden/attachment_finalized.json` all use, carrying `terminal_id`,
  `attachment_id`, `lease_generation`, and `reason`. Before the reader fans that event
  out to subscribers, and before the client installs a replacement attachment, it fails
  and removes every attachment-keyed pending entry naming the finalized `attachment_id`
  — the write entries and the single-flight control waiter alike — with the
  same typed error a connection-scoped clear uses. Fencing before fan-out is what makes
  the ordering observable to 6.3: the pane's `Detaching` transition is driven by the
  event, so a subscriber that sees the finalization can rely on that attachment's writes
  having already settled. Nothing tracks the retired id afterwards. The map is keyed by
  `(attachment_id, client_write_seq)` and a replacement attachment always carries a new
  daemon-minted `attachment_id`, so an outcome naming the finalized id can never collide
  with a live key: it misses the lookup and takes the unmatched-key drop the reader
  already performs. This
  is the attachment-scoped half of a rule the map already has connection-scoped; it adds
  no second registry and no third map. Threading a `request_id` through `terminal_input` and `terminal_write_outcome`
  instead would mean editing 4.3's handler, 4.5's codec, and the committed golden corpus
  to add a field duplicating a correlation the protocol already carries, in sections
  that do not own this change. `Daemon::reconnect()` is the public form of that path — it closes
  the current generation and returns once the replacement socket has completed the
  subscribe-first handshake below — and is what 6.3's `Detaching` deadline calls.
  The WS connection performs the subscribe-first reconciliation handshake the source
  plan specifies and 4.4 pins: subscribe, then `terminal_list` pages (pinning the
  `snapshot: {"daemon_epoch", "seq"}` of the first page before any continuation and
  buffering lifecycle events), then install the roster and apply buffered events with
  `seq > pinned.seq` — never a `snapshot` block echoed on a later page, which can only
  restate what the client's own cursor carried; a `cursor_stale` refusal or a
  `daemon_epoch` other than the pinned one restarts the listing and re-pins. Attachments are process-local on the daemon and are finalized the moment
  their websocket drops (4.3), so no attachment id survives a reconnect or a daemon
  restart: on reconnect the client tombstones every attachment id it held (late
  frames, lease events, or outcomes naming them are dropped; the tombstone set is
  exactly the ids of the previous socket generation and is replaced, never extended,
  on the next reconnect, since a message from an older socket cannot arrive on the
  new one), re-subscribes, and re-lists.

  Reattaching the panes is **not** `LiveDaemon`'s job, and drawing that line here is what
  keeps one pane from minting two attachments. `LiveDaemon` knows sockets, not panes: it
  owns socket replacement, tombstoning, the subscribe-first handshake, and the roster, and
  it finishes by declaring the generation **ready**. It issues no `terminal_attach` of its
  own. `Workspace` owns which panes are shown and what transport each one wants, so it is
  the sole issuer: on a generation becoming ready it issues exactly one fresh
  `terminal_attach` per shown pane and installs the `attachment_id` and lease generation
  from that pane's `terminal_attach_result`.

  Readiness is sticky state, not an event, because the only stream that could carry it is
  lossy by design. The lifecycle fan-out is bounded at 256 entries and closes a slow
  receiver with `Lagged`, and 6.2.9 already requires such a receiver to re-subscribe and
  re-list; re-listing restores roster rows but cannot replay a one-shot signal, so a
  `Workspace` that lagged across the transition — or that subscribed a moment after it —
  would never attach, and every shown pane would sit tombstoned behind a healthy socket.
  `LiveDaemon` therefore holds the current ready generation as a value: `subscribe()`
  returns it alongside the stream, and it is readable at any time, so a `Workspace`
  recovering from `Lagged` reads it during the same re-list 6.2.9 already performs. The
  transition is still published on the stream for the common case; the value is what makes
  the signal recoverable rather than a race. `Workspace` deduplicates by generation id —
  it attaches at most once per pane per generation — so seeing the transition both on the
  stream and in the value is idempotent, which is what lets the value be read freely
  without arbitration. Without this split both layers would attach —
  6.3's `Detaching` deadline invalidates the generation and then wants its own fresh proxy
  attach for that pane, while a `LiveDaemon` that reattached everything would already have
  attached it — leaving two live attachments for one pane, a leaked observer and lease
  record on the daemon, and whichever result landed last overwriting the other identity.
  Control is not carried across in either path: a pane that held the lease reacquires it
  with `terminal_take_control` under the new id (the same state machine 6.3 uses for proxy
  fallback).
- `ws.rs` keeps the safe-integer codec; every emitted message is validated against the
  committed golden corpus from 4.5 (`tests/fixtures/terminal_ws_golden/`), loaded by
  path in `ws_golden.rs`.
- `startup.rs` keeps `/api/health` (1.1) and exposes the token/URL to `LiveDaemon`.

`tests/mock_daemon/mod.rs` is an in-process HTTP+WS server speaking the golden shapes
(request fixtures in, reply fixtures out, with scripted faults: 401, 404, 500,
connection drop, malformed body, `cursor_stale`). `tests/daemon_live.rs` drives
`LiveDaemon` against it: one success case and one typed-failure case per trait method
(`list_terminals`, `roster`, `respond`, `mark_seen`, `spawn`, `terminate`,
`subscribe`), bearer propagation on every request, the error mapping above, pagination,
the reconnect re-registration, and `terminal_lease_lost` surfaced as a typed event.
`tests/e2e/test_terminal_client_stack.py` (9.1) adds the production-path workflow:
the real `gclient` against the isolated daemon selects a roster entry, spawns a
terminal over `terminal_create`, attaches direct, is forced onto the proxy fallback
(host socket removed), and terminates over `terminal_kill`, asserting each daemon-side
row transition.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out).

**Acceptance:**

- 6.2.1 - `LiveDaemon::list_terminals` follows `next_cursor` across three pages and yields every row once, and restarts the listing on `cursor_stale`. test: `crates/gclient/tests/daemon_live.rs::list_terminals_follows_cursor`.
- 6.2.2 - On WS reconnect `LiveDaemon` tombstones its old attachment ids, re-subscribes, re-lists, buffers events during paging, applies only events with `seq > pinned.seq` where the watermark is the first page's `snapshot`, and then publishes generation-ready — and it issues no `terminal_attach` itself, so a mock daemon that counts attach requests sees none from the data plane during the whole reconnect; a mock daemon that echoes a raised `seq` on the final page changes nothing about which events replay. The `Workspace` driven by that generation-ready signal then issues exactly one `terminal_attach` per shown pane and installs each new `attachment_id` and lease generation, holding no control until `terminal_take_control` succeeds under the new id. test: `crates/gclient/tests/reconciliation.rs::reconnect_reattaches_and_replays`.
- 6.2.3 - Every WS message the client emits matches the committed golden corpus byte-for-byte after canonicalization. test: `crates/gclient/tests/ws_golden.rs::emitted_messages_match_corpus`.
- 6.2.4 - `tokio-tungstenite` and `reqwest` are used by production code paths (`cargo udeps`-style check via a test that asserts `LiveDaemon` constructs a real client). symbol: `LiveDaemon`.
- 6.2.5 - Each `Daemon` method on `LiveDaemon` has an authenticated success case and a typed-failure case against the mock daemon: `roster`, `respond`, `mark_seen`, `spawn` (`terminal_create` → `Created` and `Refused{code}`, the latter decoded from the refused create-result golden 4.5 commits, with `code` carrying the daemon's exact refusal string and a golden that omits `code` on a `success: false` reply failing the test rather than yielding an empty code), `terminate` (`terminal_kill` → outcome), and `subscribe`; every request carries the bearer; 401/404/5xx/malformed bodies map to `Unauthorized` / `NotFound` / `Unavailable` / `Protocol`. test: `crates/gclient/tests/daemon_live.rs::every_method_has_success_and_typed_failure`.
- 6.2.6 - `gclient` neither calls nor declares any REST spawn/terminate route; spawn and terminate are the WS verbs. test: `crates/gclient/tests/ws_golden.rs::spawn_and_kill_use_websocket_verbs`.
- 6.2.7 - With `spawn`, `terminate`, and an active `subscribe()` interleaved on one socket, each `*_result` resolves its own `request_id`, lifecycle events reach the subscriber in order and never a command future, an unmatched `request_id` is dropped, and a connection drop during an in-flight `spawn` fails it with `Unavailable` before the reconnected socket serves any request. test: `crates/gclient/tests/daemon_live.rs::single_reader_routes_replies_and_events`.
- 6.2.8 - When 1,025 lifecycle events arrive during pagination, LiveDaemon discards the partial listing, starts one fresh snapshot, applies no stale buffered event, and converges to the authoritative roster. test: `crates/gclient/tests/reconciliation.rs::paging_overflow_restarts_from_fresh_snapshot`.
- 6.2.9 - When a subscriber exceeds its 256-entry channel, it receives Lagged, re-subscribes and re-lists without reusing stale state, and converges while the healthy socket generation remains valid. test: `crates/gclient/tests/reconciliation.rs::lagged_subscriber_relists_and_converges`.
- 6.2.10 - `LiveDaemon::send` resolves delivered and typed-refused terminal-input replies through the `(attachment_id, client_write_seq)` map — a mock daemon that answers `terminal_write_outcome` with no `request_id` at all (the shape `terminal_ws.py` emits) settles the sender, and two writes differing only in `client_write_seq` resolve to their own senders — while interleaved with spawn, terminate, and lifecycle events that settle on the `request_id` map; every pending entry in both maps fails and is removed on EOF, close, cancellation, or reconnect before the next socket generation accepts requests. Attachment finalization fences the write map on a socket that stays up: with a direct-to-proxy fallback finalizing an attachment that has an in-flight write, the sender fails when the `terminal_attachment_finalized` for that id is processed — before the event reaches any subscriber — rather than parking until the next reconnect when the daemon sends no outcome at all, and a `terminal_write_outcome` naming that finalized id arriving afterwards on the same socket resolves nothing, because the replacement attachment's key differs and the reader drops it as unmatched. The attachment-scoped control waiter settles under the same rule: a `terminal_control_result` carrying no `request_id` resolves the waiter for its `attachment_id` on both `granted:true` and `granted:false`, a second concurrent control request on that attachment is refused locally with `ControlRequestInFlight`, and finalization, the request deadline, EOF, close, cancellation, and reconnect each fail and remove it — a result naming the finalized id afterwards resolves nothing. An expired control request cannot be settled by its own late reply either: with the mock daemon withholding the reply past the deadline, the waiter fails, that attachment is finalized locally, the pane re-attaches under a new `attachment_id`, and a `terminal_control_result` for the retired id delivered afterwards on the same live socket resolves nothing — including the cross-verb case where the timed-out request was a take and the pane's next control request is a release. test: `crates/gclient/tests/daemon_live.rs::send_is_correlated_and_settled_across_failures`.
- 6.2.11 - Against a mock daemon that answers `terminal_set_viewport` with nothing at all, `notify` resolves once the write completes, registers no `request_id` entry, and leaves no pending sender to fail on the next reconnect; a write on an already-closed generation fails it `Unavailable`; and `ScriptedDaemon` records the same notification without a reply. test: `crates/gclient/tests/daemon_live.rs::notify_settles_without_a_reply`.
- 6.2.12 - Generation readiness survives a consumer that never sees the transition on the stream. A `Workspace` that subscribes after the generation is already ready reads the current ready generation from `subscribe()` and attaches every shown pane; a `Workspace` driven past its 256-entry channel exactly across the transition receives `Lagged`, re-subscribes, re-lists, reads the same value, and attaches every shown pane. In both schedules each shown pane produces exactly one `terminal_attach` for that generation — a second attach for the same pane fails the test — and a `Workspace` that receives both the stream transition and the value attaches once, not twice. test: `crates/gclient/tests/reconciliation.rs::ready_generation_is_readable_not_only_published`.

### 6.3 App shell, event loop, and terminal views [category: code] (depends: 6.1, 6.2, 4.6)
`kind: deliverable`

Targets:
- `crates/gclient/src/views/mod.rs`
- `crates/gclient/src/app/mod.rs`
- `crates/gclient/src/app/apply.rs`
- `crates/gclient/src/app/pane.rs`
- `crates/gclient/src/frame_source.rs`
- `crates/gclient/src/input.rs`
- `crates/gclient/src/main.rs`
- `crates/gclient/tests/client_loop.rs`
- `crates/gclient/tests/attention_flow.rs`
- `crates/gclient/tests/frame_source_live.rs`
- `tests/e2e/test_terminal_client_stack.py`

Closes the shell half of D-1 (source 3.3.1, 3.3.10, 3.5.1). Depends on 4.6 because the
write-outcome reducer contract (`delivered` / `refused` / `indeterminate`, lease-lost
read-only, paste refusal) this loop implements is finalized there. This leaf adds
`test_gclient_reaches_workspace` (6.3.5) to `tests/e2e/test_terminal_client_stack.py`;
9.1, which depends on this leaf, owns the rest of that file's rewrite.

`views::run_ready` builds `Workspace::scripted()`, assigns `_url/_token/_theme`, and
returns `Ok(())`; `Workspace` is hard-wired to `ScriptedDaemon`;
`UnixSocketFrameSource::connect` drops its `UnixStream` at scope end and `send` always
returns `NotConnected`.

- `run_ready` owns the loop: crossterm raw mode + alternate screen, a `tokio::select!`
  over terminal input, daemon events (`Daemon::subscribe`), frame-source frames, and
  a 16 ms render tick; renders through `ui::render_workspace` into a ratatui
  `Terminal<CrosstermBackend>`; exits on the quit key or daemon loss after the
  reconnect budget. The input branch never blocks the runtime: it receives from
  `gobby_terminal::raw_input::spawn_input_reader()`, which already runs the blocking
  stdin read on its own `std::thread` and feeds a bounded (256-entry) Tokio channel
  of framed `RawInputEvent`s, so an idle stdin leaves events, frames, timers, and
  signals progressing; the thread is detached and ends with the process after the
  loop restores the terminal on quit.
- `Workspace<D: Daemon>` is generic; production uses `LiveDaemon`, tests keep
  `ScriptedDaemon`. `apply.rs` reduces daemon events into workspace state (roster,
  terminals, leases, attention).
- `UnixSocketFrameSource` owns its `UnixStream`, connects to the
  `direct.frame_socket_path` the daemon's `terminal_attach_result` carries (4.3),
  reads `Welcome` and verifies its `host_epoch` against `direct.host_epoch` (a
  mismatch is a direct-frame failure), then attaches the `direct.host_terminal_id`
  with `frame_delivery: "direct"`. Transport fallback is a state machine, never a reuse of
  the finalized attachment: on any direct-frame failure the pane enters
  `Detaching{old_attachment_id}`, sends `terminal_detach` for the old id, and waits
  for the daemon's `terminal_detach_result` (or the lifecycle
  `terminal_attachment_finalized` naming that `attachment_id` — the event the daemon
  actually publishes on finalization, whose shape the committed
  `terminal_ws_golden/attachment_finalized.json` pins; either one alone finalizes the
  id and advances the state, and because 6.2 fences the pending-write map before that
  event fans out, no write for the old id is still outstanding when it does),
  which finalizes that id; it then issues a fresh
  `terminal_attach{frame_delivery: "proxy"}` and enters `Attached{new_attachment_id,
  lease_generation}` only on a `terminal_attach_result` carrying a *different*
  `attachment_id`; the old id is tombstoned in the workspace so any late frame, lease
  event, or write outcome naming it is dropped; proxy frames before the new attach
  result are discarded. Control is not carried across: after the proxy attach the pane
  is observing until the user (or focus-follows-control) sends `terminal_take_control`
  with the new attachment id. A second direct failure during `Detaching` does not
  re-enter the machine; a failed proxy attach surfaces the typed code in the status bar
  and leaves the pane detached. `Detaching` is bounded: it waits at most the same 2 s
  detach deadline 6.4's shutdown uses; if neither `terminal_detach_result` nor
  `terminal_attachment_finalized` arrives, the pane asks `LiveDaemon` to invalidate the current
  socket generation (6.2's reconnect path — the daemon's socket finalization is then
  the ownership fence, every attachment of the old generation is tombstoned, and any
  late old-generation result is dropped by the reader) and, once `LiveDaemon` publishes
  generation-ready, issues the fresh proxy attach on it. `Workspace` is the only issuer
  of that attach: 6.2's reconnect path re-subscribes and re-lists but attaches nothing,
  so this pane gets exactly one attach whether it arrived here through the deadline or
  through an ordinary reconnect, and no second attachment is minted for it. Loss of the
  daemon while `Detaching` takes the same route through the reconnect path. The status bar shows
  the active transport.
- Focus follows control: focusing a pane sends `terminal_take_control`; `terminal_lease_lost`
  renders the pane read-only with the herdr "observing" treatment and a take-back key;
  writes go through `terminal_input` with `client_write_seq` and the outcome reducer
  from 4.6's contract. A keystroke on a pane the client does not hold follows 4.6's
  queue-once policy: it is held as the pane's single `pending_input` while the
  `terminal_take_control` it triggered is in flight, sent once under the installed
  attachment id and lease generation on `granted:true`, and discarded with the
  reason shown in the status bar on `granted:false`, the request deadline, lease
  loss, detach, or a socket-generation change; further keystrokes during the pending
  request are refused locally and never queued.
- Proxy attachments must be activated, not merely granted. 4.3's Q-1 makes the daemon
  prepare a proxy attachment and hold its pump until the first attachment-scoped
  `terminal_set_viewport` arrives, so a client that never sends one sits `Attached` and
  receives no history, no keyframe, and no live output. Every path that installs a fresh
  proxy `attachment_id` — the initial proxy attach, the re-attach after a reconnect or
  socket-generation change, and the direct-to-proxy fallback above — therefore issues
  `notify(terminal_set_viewport{attachment_id, rows, cols})` with the pane's current
  dimensions immediately after the `terminal_attach_result` that installs the id and
  before it begins waiting for frames. It is `notify`, not `send` (6.2): the daemon
  answers a successful viewport with nothing, so a correlated `send` would park a pending
  sender here forever and the pane would never advance. The message needs no write lease
  (the daemon handler is
  attachment-scoped, not lease-gated), so an observing pane activates exactly like a
  controlling one; later pane resizes are ordinary viewport updates on the same
  one-way path. Direct attachments
  are unaffected — their frames come from the host socket, not the proxy pump.
- Attention prompts from the roster render through the imported dialog chrome; answers
  post through `Daemon::respond`.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out); `cargo run -p gobby-client -- --help` exits 0; plus `GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_terminal_client_stack.py::test_gclient_reaches_workspace` against rebuilt-and-reinstalled `gclient`/`gterm` binaries (the new-inode recipe in `docs/guides/gterminal-development-guide.md` § "Rebuild and reinstall") and the module's isolated daemon. Guard set G enumerates its Python files explicitly and names no `tests/e2e/` module, so without this clause the leaf would author 6.3.5's test and close without ever running it; 9.1 still owns the rest of that file.

**Acceptance:**

- 6.3.1 - Against a scripted daemon and scripted frame source, the loop renders frames, routes a keystroke to `terminal_input` only when the client holds the lease, keeps rendering frames and applying daemon events across several ticks while the input channel stays idle, and exits on the quit key. test: `crates/gclient/tests/client_loop.rs::loop_routes_input_and_frames`.
- 6.3.2 - The direct frame source connects to a real `gterm host`, verifies `host_epoch`, receives a keyframe, and detaches through the daemon when the socket drops. test: `crates/gclient/tests/frame_source_live.rs::direct_frames_verify_epoch_and_fall_back`.
- 6.3.6 - Proxy fallback after a direct-frame failure waits for the old attachment to finalize, attaches afresh with `frame_delivery: "proxy"`, installs the new attachment id and lease generation, drops late events addressed to the tombstoned id, and holds no control until `terminal_take_control` succeeds under the new id; a proxy attach refusal leaves the pane detached with the typed code shown. test: `crates/gclient/tests/client_loop.rs::proxy_fallback_uses_fresh_attachment`.
- 6.3.3 - `terminal_lease_lost` makes the focused pane read-only and the take-back key re-acquires control. test: `crates/gclient/tests/client_loop.rs::lease_lost_is_read_only_until_take_back`.
- 6.3.4 - An attention prompt in the roster opens the dialog and the answer reaches `Daemon::respond` with the correct `attention_id`. test: `crates/gclient/tests/attention_flow.rs::respond_reaches_daemon`.
- 6.3.5 - `gclient` started against a live isolated daemon reaches the workspace screen (not exit 0 after the probe). test: `tests/e2e/test_terminal_client_stack.py::test_gclient_reaches_workspace`.
- 6.3.7 - With the scripted daemon swallowing both `terminal_detach_result` and `terminal_attachment_finalized`, a pane in `Detaching` expires the 2 s deadline, invalidates the socket generation, waits for generation-ready, attaches afresh with `frame_delivery: "proxy"`, and drops a late old-generation detach result; daemon loss during `Detaching` reaches the same proxy attachment through the same path. Exactly one `terminal_attach` request and one `terminal_attach_result` are observed for that pane across the whole recovery, because `LiveDaemon` issues none and `Workspace` issues one — a second attach for the same pane fails the test. test: `crates/gclient/tests/client_loop.rs::detaching_deadline_reconnects_and_reattaches`.
- 6.3.10 - Either finalization signal alone advances `Detaching` on a socket that stays up, so no generation reset is spent on an ordinary fallback: with the scripted daemon suppressing `terminal_detach_result` and publishing only `terminal_attachment_finalized` for the old `attachment_id`, the pane leaves `Detaching` before the 2 s deadline, issues the proxy `terminal_attach` on the *same* socket generation, and enters `Attached` on a result carrying a different `attachment_id`; no reconnect occurs, the old id is tombstoned, and a write in flight when the finalization arrived has already failed rather than resolving against the new attachment. test: `crates/gclient/tests/client_loop.rs::finalization_alone_advances_detaching_on_the_same_socket`.
- 6.3.8 - The keystroke that triggers `terminal_take_control` is sent exactly once under the installed generation after `granted:true`, and is discarded with the reason shown on `granted:false`, the request deadline, `terminal_lease_lost`, detach, and a socket-generation change; a keystroke during the pending request is refused and never queued. test: `crates/gclient/tests/client_loop.rs::triggering_keystroke_settles_once`.
- 6.3.9 - Every path that installs a fresh proxy attachment — the initial proxy attach, the re-attach `Workspace` issues on generation-ready after a socket-generation change, and the direct-to-proxy fallback — notifies `terminal_set_viewport` carrying that new `attachment_id` and the pane's current rows/cols before waiting for frames, does so on the one-way path without holding the write lease, and only then receives history and its first keyframe; against a daemon that answers the viewport with no message at all and withholds frames until it arrives, all three paths still reach a rendered pane and no pane is left waiting on a reply. Each path installs exactly one attachment per pane, so exactly one viewport notification is observed per pane per generation. test: `crates/gclient/tests/client_loop.rs::proxy_attachments_activate_with_viewport`.

### 6.4 Copy mode, paste, persistence, teardown, and logging [category: code] (depends: 6.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/copy_mode.rs`
- `crates/gclient/src/persist.rs`
- `crates/gclient/src/teardown.rs`
- `crates/gclient/src/logging.rs`
- `crates/gclient/src/lib.rs`
- `crates/gclient/tests/copy_paste.rs`
- `crates/gclient/tests/persist.rs`
- `crates/gclient/tests/teardown.rs`

Closes the remaining D-1 surfaces (source 3.3.x copy/paste/persist/teardown).

- Copy mode reads history through `AttachHistory` / `SetScrollOffset` on the frame
  source, selects with `gobby_terminal::selection`, and copies through the OSC 52 path
  herdr uses; paste is lease-gated `terminal_paste` with the oversize refusal shown.
- Persistence writes `~/.gobby/client/<project>/workspace.json` (layout tree, tab order,
  focused pane) atomically on change and restores it on start; a corrupt file is
  renamed aside and logged.
- Teardown separates what synchronous `Drop` can guarantee from what needs the
  runtime. `TerminalModeGuard` is a synchronous, idempotent RAII guard that only
  restores the local terminal (leave alternate screen, disable raw mode and bracketed
  paste, show the cursor) and is installed before the loop starts; it runs on normal
  return, on `?`-propagated errors, and during panic unwinding, and never awaits or
  blocks on the network. Remote cleanup is an explicit async phase
  `shutdown(workspace, daemon, deadline)` that the loop runs on the quit key, on
  `SIGINT`/`SIGTERM`/`SIGHUP` (tokio signal handlers feeding the `select!`), and on
  daemon loss: it sends `terminal_release_control` for every held lease and
  `terminal_detach` for every attachment, awaits the results under a 2 s deadline,
  closes the WS, then drops the runtime — the ordering guarantees the guard's local
  restore happens last. A panic skips the async phase by design: the WS close the
  process exit causes is what finalizes the server-side attachments and leases (4.3's
  finalize-on-disconnect), and the test for the panic path asserts exactly that — the
  terminal is restored locally and the daemon finalizes on socket close — rather than
  asserting an awaited detach that `Drop` cannot make.
- Logging goes to `~/.gobby/logs/gclient.log` (tracing, daily rotation) — never to the
  TUI's stdout.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out).

**Acceptance:**

- 6.4.1 - Copy mode selects text from history fetched through the frame source and emits it via OSC 52; paste of an oversize payload is refused with the server's reason. test: `crates/gclient/tests/copy_paste.rs::copy_from_history_and_paste_refusal`.
- 6.4.2 - Workspace layout round-trips through `workspace.json` and a corrupt file is quarantined without crashing. test: `crates/gclient/tests/persist.rs::workspace_round_trip_and_corrupt_file`.
- 6.4.3 - On panic the terminal-mode guard restores the terminal synchronously (raw mode off, main screen) and the daemon finalizes the attachments and leases on the dropped socket; no async call is awaited from `Drop`. test: `crates/gclient/tests/teardown.rs::panic_restores_locally_and_server_finalizes_on_close`.
- 6.4.5 - On quit key and on `SIGTERM` the async shutdown releases every lease and detaches every attachment with awaited results under the deadline before the runtime drops, and the guard restores the terminal after; a daemon that never answers does not hang the exit past the deadline. test: `crates/gclient/tests/teardown.rs::graceful_exit_releases_and_detaches_within_deadline`.
- 6.4.4 - Logs land in `~/.gobby/logs/gclient.log` and nothing is written to stdout while the TUI runs. file: `crates/gclient/src/logging.rs`.

## P7: Herdr UI parity
`kind: framing`

**Goal**: Committed, mechanical evidence that gclient's chrome matches herdr v0.8.0
element-for-element, with every divergence listed and justified — the gate that would
have caught D-1.

### 7.1 Component goldens ported from herdr's `TestBackend` tests [category: test] (depends: 6.1)
`kind: deliverable`

Targets:
- `crates/gclient/tests/parity.rs`
- `crates/gclient/tests/parity/mod.rs`
- `crates/gclient/tests/parity/sidebar.rs`
- `crates/gclient/tests/parity/panes.rs`
- `crates/gclient/tests/parity/tabs.rs`
- `crates/gclient/tests/parity/dialogs.rs`
- `crates/gclient/tests/parity/navigator.rs`
- `crates/gclient/tests/parity/status.rs`
- `crates/gclient/tests/parity/chrome.rs`
- `crates/gclient/tests/parity/fixtures.rs`
- `crates/gclient/tests/parity/token_map.rs`

Closes the component-golden half of D-1: the chrome regressions D-1 reports are
per-widget render defects, and this leaf is the mechanical gate that would have caught
them at the component level before they reached a screen.

herdr v0.8.0 carries 163 `#[test]`s under `src/ui` and `src/ui.rs` that render into a
ratatui `TestBackend` and assert row text (`sidebar.rs` 40, `ui.rs` 31, `panes.rs` 12,
`tabs.rs` 5, `dialogs.rs` 5, `navigator.rs` 5, `status.rs` 4, the rest spread across the
smaller modules). Port every test whose module is in the keep-set into
`crates/gclient/tests/parity/` with its row-text expectations verbatim. `fixtures.rs`
maps herdr fixture state 1:1 onto Gobby state (herdr agent → Gobby roster entry /
terminal row; herdr workspace → gclient workspace; herdr attention → Gobby attention
prompt). `token_map.rs` normalizes colors through the Gobby↔herdr token map so that
glyphs, layout, truncation, and focus junctions must match exactly while theme values
are the one allowed divergence. Tests that exercise a dropped module are listed in the
7.2 checklist as `intentional divergence: dropped module <name>` and are not ported.
Count: the ported test count equals herdr's keep-set count; the test module asserts
that number against `UPSTREAM.md`'s table so silently dropping a test fails.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out).

**Acceptance:**

- 7.1.1 - Every herdr v0.8.0 render test in the keep-set exists under `crates/gclient/tests/parity/` with unchanged row-text expectations and passes. test: `crates/gclient/tests/parity.rs`.
- 7.1.2 - The ported count equals the keep-set count recorded in `UPSTREAM.md`, and the count test fails when one is removed. test: `crates/gclient/tests/parity/mod.rs::ported_count_matches_upstream_table`.
- 7.1.3 - Colors are compared through the token map; a glyph or alignment change in any imported module fails its parity test while a theme-value change does not. file: `crates/gclient/tests/parity/token_map.rs`.

### 7.2 Screen goldens and the divergence checklist [category: test] (depends: 7.1, 6.4)
`kind: deliverable`

Targets:
- `scripts/gclient_herdr_parity.sh`
- `crates/gclient/tests/parity_screens.rs`
- `crates/gclient/tests/fixtures/parity/empty_workspace.herdr.txt`
- `crates/gclient/tests/fixtures/parity/empty_workspace.gclient.txt`
- `crates/gclient/tests/fixtures/parity/roster_attention.herdr.txt`
- `crates/gclient/tests/fixtures/parity/roster_attention.gclient.txt`
- `crates/gclient/tests/fixtures/parity/split_live.herdr.txt`
- `crates/gclient/tests/fixtures/parity/split_live.gclient.txt`
- `crates/gclient/tests/fixtures/parity/help_dialog.herdr.txt`
- `crates/gclient/tests/fixtures/parity/help_dialog.gclient.txt`
- `docs/evidence/gclient-herdr-parity.md`
- `.github/workflows/rust-ci.yml`

Closes the screen-golden and CI-evidence half of D-1: 7.1 pins individual widgets, and
this leaf pins whole assembled screens against real herdr output and keeps that
comparison running in CI, which is where a D-1-class divergence between the two chromes
becomes visible.

`scripts/gclient_herdr_parity.sh` builds herdr at `v0.8.0` from
`${GOBBY_HERDR_CHECKOUT:-~/.gobby/clones/herdr}` (verifying the tag's commit), builds
`gclient`, and runs each binary in a 120×40 PTY driven through `gobby-terminal`'s own
PTY actor and VT engine (`--features vt-engine`) through four scripted states: empty
workspace; roster with one attention prompt; split panes with a live terminal; keybind
help and a dialog open. It captures both screens as text (one line per row, cells
rendered through the token map) into the fixture pairs above and diffs the chrome
regions (sidebar, tabs, status bar, borders, dialogs), masking the terminal-content
region. `parity_screens.rs` re-runs the diff against the committed fixtures and reads
`docs/evidence/gclient-herdr-parity.md`, a checklist with one row per chrome element
(`parity` or `intentional divergence: <reason>` — Gobby theme, dropped herdr module,
Gobby data field); any differing region not covered by an `intentional divergence` row
fails the test. `rust-ci.yml` gains a job that checks out herdr at the pinned commit and
runs the script so the fixtures cannot go stale silently; the job is required for the
`0.5.0` merge gate.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `scripts/gclient_herdr_parity.sh` exit 0 with regenerated fixtures identical to the committed ones.

**Acceptance:**

- 7.2.1 - The script produces the eight screen captures deterministically (two consecutive runs are byte-identical) for both binaries at 120×40. file: `scripts/gclient_herdr_parity.sh`.
- 7.2.2 - Every chrome region that differs between herdr and gclient in any of the four states has a matching `intentional divergence` row, and introducing an unlisted divergence (mutation recorded in the leaf's evidence) fails the test. test: `crates/gclient/tests/parity_screens.rs::no_unlisted_divergence`.
- 7.2.3 - The checklist enumerates every chrome element visible in the four states with `parity` or a justified divergence. file: `docs/evidence/gclient-herdr-parity.md`.
- 7.2.4 - CI runs the parity script against the pinned herdr commit on every push to `0.5.0`. file: `.github/workflows/rust-ci.yml`.

## P8: Host protocol completeness
`kind: framing`

**Goal**: The documented backpressure contract is implemented, and every 3.2.x
acceptance test of the source plan exercises the running host instead of asserting a
constant.

### 8.1 Backpressure: byte accounting, lag close, bounded control queue [category: code] (depends: P2, 3.2)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/state.rs`
- `crates/gterminal/src/host/backpressure.rs`
- `crates/gterminal/src/host/mod.rs`
- `crates/gterminal/src/host/frames.rs`
- `crates/gterminal/src/host/control.rs`
- `crates/gterminal/src/host/config.rs`
- `crates/gterminal/tests/frame_protocol.rs`
- `crates/gterminal/tests/control_protocol.rs`
- `docs/contracts/gterm-protocols.md`

Closes D-3. The queue accounting lives in a new `host/backpressure.rs` (per-attachment
`queued_bytes`, the drop-and-keyframe transition, lag and control deadlines, the event
subscriber ring buffer with `seq` that 2.9's recovery replays from); `state.rs` only
calls into it, so it stays under the ceiling the 2.6 guard enforces.

`state.rs::broadcast_frames` ends with `let _ = DELTA_QUEUE_BYTES; let _ = CONTROL_QUEUE_ENTRIES; … let _ = EVENT_QUEUE_BYTES;`;
`frames.rs` handles a full channel with `Err(_) => { att.desynced = true; }`; there is
no byte accounting, no separate control queue, no 2 s control deadline, no 5 s lag
close (`lag_timeout()` has no caller); the only bound is the 64-entry channel × up-to-2 MiB
frames per stalled attachment; `EventSub.queued` / `queued_bytes` are never updated.

Implement per `docs/contracts/gterm-protocols.md` "Backpressure": each attachment
keeps `queued_bytes`; a delta that would exceed `DELTA_QUEUE_BYTES` drops the queue and
replaces it with one keyframe (`desynced` → keyframe-on-next-tick); an attachment that
has not drained below the threshold within `lag_timeout()` (5 s) receives a typed close
`lagged` and is reaped; control responses use a separate non-coalescing bounded queue
(`CONTROL_QUEUE_ENTRIES`) with a 2 s delivery deadline that closes the control
connection `control_deadline` on expiry; event subscriptions are byte-bounded
(`EVENT_QUEUE_BYTES`) with `event_overflow` (2.9). The four constants become
`HostConfig` fields in `host/config.rs` carrying the documented defaults
(`delta_queue_bytes`, `control_queue_entries`, `event_queue_bytes`, the lag and
control deadlines) so the protocol tests can lower them; they have no daemon-side
carrier — `TerminalHostConfig`, `TerminalHostManager`'s argv forwarding, and
`runtime_config_contract.json` are unchanged, `host/mod.rs` parses no new flag for
them, and they are host-internal tuning with no operator consumer in this plan.
Update the contract document where the implementation pins a detail it left open
(e.g. keyframe-on-overflow) and state there that the caps are not operator
configuration.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out; groups 3, 4, 7 mandatory here).

**Acceptance:**

- 8.1.1 - A frame peer that stops reading receives at most `DELTA_QUEUE_BYTES` of queued deltas, then a single keyframe once it resumes. test: `crates/gterminal/tests/frame_protocol.rs::slow_observer_resyncs_with_keyframe`.
- 8.1.2 - A frame peer that never resumes is closed with `lagged` after the lag timeout and its slot is released; other observers are unaffected. test: `crates/gterminal/tests/frame_protocol.rs::lagged_observer_is_closed_and_released`.
- 8.1.3 - Control responses are never coalesced or dropped below the queue cap, and a control peer that does not read for 2 s is closed with `control_deadline`. test: `crates/gterminal/tests/control_protocol.rs::control_queue_is_bounded_and_deadlined`.
- 8.1.4 - Memory held per stalled attachment is bounded by the configured byte caps (measured through the host's `list` diagnostics exposing `queued_bytes`). test: `crates/gterminal/tests/frame_protocol.rs::queued_bytes_are_reported_and_bounded`.

### 8.2 Real host-driven 3.2.x acceptance tests [category: test] (depends: 8.1, 2.8)
`kind: deliverable`

Targets:
- `crates/gterminal/tests/control_protocol.rs`
- `crates/gterminal/tests/frame_protocol.rs`
- `crates/gterminal/tests/host_support/mod.rs`

Closes D-2.

These tests currently call another test and assert a constant
(`assert_eq!(format!("{}", 256), "256")`): `snapshot_is_byte_bounded_and_reports_truncation`,
`prepare_expires_or_replays_after_control_loss`, `reserve_observer_state_machine`,
`encoded_control_line_stays_inside_max`, `slow_observer_resyncs_or_lags_out`,
`reconnect_boundary_semantics_per_verb`, `only_the_control_socket_can_write`,
`subscribe_events_is_bounded_and_recovers_from_list`,
`committed_observer_entitlement_rebinds_under_saturation`,
`prepared_frame_loss_retains_entitlement_and_saturates`,
`release_observer_and_prepared_kill_without_disconnect`,
`native_scrollback_plateaus_at_configured_ceilings`,
`control_overflow_closes_attachment_bounded`,
`internal_observers_scale_with_live_native_terminals`. Three more are weak:
`worst_case_keyframe_fits_max_frame_size` uses `MAX_CELLS.min(200)`,
`attach_viewport_and_observer_sizing` never reads a frame,
`list_envelope_fits_under_line_cap` never talks to the host.

Rewrite each against a running host through `host_support` (RAII guard from 2.8) so
that it performs the named behaviour end-to-end: real snapshots over the byte cap with
the truncation flag; prepared spawns expiring after control loss and replaying after
reconnect; the observer reserve/bind/release state machine with typed errors on
illegal transitions; a full-size keyframe at `MAX_CELLS`; viewport/observer sizing read
back from frames; `list` at the line cap with the largest population the host admits
at its shipped ceilings — 124 native terminals (`max_attachments_total` 128 less the
four reserved lifecycle slots) plus 64 tmux observers (`max_attached_terminals`),
188 in all, every row carrying a 1,024-byte title and maximum-size bounded fields,
so the envelope is measured at the real admission limit rather than a round number
the host would refuse. Each test's name stays
(the source plan's acceptance items cite them), and each asserts on host responses or
frames, never on constants. A reviewer-facing table in the test file header maps test
→ source acceptance item (3.2.6, 3.2.8, 3.2.12–3.2.17, 3.2.19, 3.2.20, 3.2.22–3.2.25,
3.2.29–3.2.31).

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out; groups 3, 4, 7 mandatory here); `cargo nextest run -p gobby-terminal --features vt-engine --test control_protocol --test frame_protocol`.

**Acceptance:**

- 8.2.1 - None of the seventeen named tests calls another test or asserts a literal; each drives a live host and asserts on its responses. test: `crates/gterminal/tests/control_protocol.rs::no_test_delegates_to_another_test`.
- 8.2.2 - `worst_case_keyframe_fits_max_frame_size` renders a keyframe at the full `MAX_CELLS` geometry and proves it encodes under `MAX_FRAME_SIZE`. test: `crates/gterminal/tests/frame_protocol.rs::worst_case_keyframe_fits_max_frame_size`.
- 8.2.3 - `list_envelope_fits_under_line_cap` lists the maximum admissible population (124 native terminals plus 64 tmux observers, each with a 1,024-byte title and maximum-size bounded fields) from a live host and proves the envelope stays under the control line cap; the 189th admission is refused `capacity`. test: `crates/gterminal/tests/control_protocol.rs::list_envelope_fits_under_line_cap`.
- 8.2.4 - The header table maps every rewritten test to its source acceptance item. file: `crates/gterminal/tests/control_protocol.rs`.

## P9: End-to-end assertions and cleanup
`kind: framing`

**Goal**: The E1 stack test asserts what it claims, and the remaining minor findings
are closed.

### 9.1 Make the E1 stack test assert its clauses [category: test] (depends: P4, P8, 6.3, 6.4)
`kind: deliverable`

Targets:
- `tests/e2e/test_terminal_client_stack.py`
- `tests/e2e/conftest.py::*` — scope-reason: fixtures gain the SRT-enabled daemon variant and host-crash helpers

Closes E-19.

Each E1.1 clause is currently unasserted or tautological: daemon-down "writes cannot be
issued" never attempts a write; the browser finalize check passes on any send
exception; the write-handler fault assertion `native_frames._queue or before_frames >= 0`
is a tautology; "share one observer" is not asserted and
`isolated.clients() == clients_before or isolated.owner.pid` is a tautology;
exactly-once replay asserts equal `outcome` only, never that the PTY did not receive the
payload twice; indeterminate-not-retried is driven on a raw `HostClient`; prepared-uncommitted
bypasses the daemon; deadline reap does not check the `pgid` is dead; "streams resume"
after restart is not asserted; exit-during-restart, host crash, and final
capture-before-kill accept any status; SRT is disabled (`agent_sandbox.enabled: False`).

Rewrite each clause to assert the behaviour: attempt the write while the daemon is
down and assert the typed refusal; assert the browser attachment is finalized through
the lease registry's observable effects (below); inject the 4.3 fault hook and assert the `terminal_write_outcome` reports it with
no bytes on the PTY (read the PTY echo); assert two external clients share one
observer slot via the host `list`; for exactly-once replay, echo a nonce and assert it
appears once in the snapshot; drive indeterminate-not-retried through the daemon
runtime; create the prepared-uncommitted row through the daemon; assert the reaped
`pgid` is dead via `os.killpg(pgid, 0)` → `ProcessLookupError`; assert frames resume
after restart; pin the exact expected state for exit-during-restart (`exited`), host
crash (`orphaned`), and capture-before-kill (`exited` with the captured tail). Run the
stack with SRT enabled (`agent_sandbox.enabled: True`, `allowPty: True`).

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out) plus `GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_terminal_client_stack.py tests/e2e/test_external_terminal_attach.py` against `gterm` **and** `gclient` both rebuilt from the epic worktree and reinstalled through the new-inode recipe in `docs/guides/gterminal-development-guide.md` § "Rebuild and reinstall".

Rebuilding `gclient` and not just `gterm` is what makes the 6.4 edge above load-bearing. 9.1.11 drives the real client binary, so the copy mode, paste, persistence, teardown, and logging 6.4 delivers must be in the installed artifact before this leaf runs; a stale `gclient` would let the stack test pass against a client that predates the tail of Phase 6.

The stack test also carries the production-path gclient workflow that 6.2 specifies
(select a roster entry → `terminal_create` → direct attach → forced proxy fallback →
`terminal_kill`), asserting each daemon-side row transition. The workflow needs a
native row regardless of the shipped default (5.1 flips it to tmux and carries no
edge to this leaf): the isolated daemon fixture for this clause sets
`terminals.default_backend: native` in its temporary config, the `terminal_create`
request names `backend: "native"` explicitly, and the test asserts the created row's
`backend == "native"` before attempting the direct attach.

Browser-attachment finalization (9.1.5) has no storage carrier — attachments and
leases are process-local in `TerminalLeaseRegistry` — so the oracle is the registry's
WS-visible behaviour: after the browser socket drops, the test asserts the lifecycle
`terminal_attachment_finalized` broadcast names the dropped `attachment_id`, and a fresh attachment
to the same terminal is granted control by `terminal_take_control` without a takeover
(`terminal_control_result{granted: true, displaced: null}`).

**Acceptance:**

- 9.1.1 - No assertion in the file is a tautology (`or <truthy>`, `>= 0`, status sets of size > 1); a lint test parses the file's AST and fails on those shapes. test: `tests/e2e/test_terminal_client_stack.py::test_no_tautological_assertions`.
- 9.1.2 - The exactly-once replay clause proves the PTY received the nonce once. test: `tests/e2e/test_terminal_client_stack.py::test_replay_is_exactly_once_on_the_pty`.
- 9.1.3 - The stack runs with SRT enabled (`agent_sandbox.enabled: True`, `allowPty: True`) and every lifecycle clause pins a single expected state: exit-during-restart → `exited`, host crash → `orphaned`, capture-before-kill → `exited` with the captured tail. test: `tests/e2e/test_terminal_client_stack.py::test_lifecycle_states_are_pinned`.
- 9.1.4 - With the daemon stopped, an attempted write is refused with a typed error and no bytes reach the PTY. test: `tests/e2e/test_terminal_client_stack.py::test_daemon_down_refuses_writes`.
- 9.1.5 - A browser attachment whose socket drops is finalized by the lease registry: the `terminal_attachment_finalized` lifecycle broadcast names the dropped `attachment_id`, and a fresh attachment is granted control without a takeover; nothing is inferred from a send exception. test: `tests/e2e/test_terminal_client_stack.py::test_browser_disconnect_finalizes_attachment`.
- 9.1.6 - A faulted write (4.3 seam) is reported as a `terminal_write_outcome` refusal and the PTY echo shows the payload never landed. test: `tests/e2e/test_terminal_client_stack.py::test_write_handler_fault_is_reported_not_lost`.
- 9.1.7 - Two external clients attached to one native terminal share one observer slot as reported by the host `list`, and detaching one leaves the other's frames flowing. test: `tests/e2e/test_terminal_client_stack.py::test_external_clients_share_one_observer`.
- 9.1.8 - An indeterminate write is not retried by the daemon runtime (driven through `NativeTerminalRuntime`, not a raw `HostClient`), and the unresolved latch resolves through capture. test: `tests/e2e/test_terminal_client_stack.py::test_indeterminate_write_is_not_retried`.
- 9.1.9 - A prepared-uncommitted spawn created through the daemon has a durable `pending` row, and after the deadline the reaped group is dead (`os.killpg(pgid, 0)` raises `ProcessLookupError`) and the row is failed typed. test: `tests/e2e/test_terminal_client_stack.py::test_prepared_uncommitted_is_reaped_through_daemon`.
- 9.1.10 - After a daemon restart, a surviving native terminal is re-attached under a fresh `attachment_id` (the pre-restart id is finalized and never honoured), frames resume on the new attachment, and the browser receives a keyframe within the reconnect budget. test: `tests/e2e/test_terminal_client_stack.py::test_streams_resume_after_restart`.
- 9.1.11 - The real `gclient` completes select → `terminal_create{backend: "native"}` (row asserted `native`) → direct attach → proxy fallback → `terminal_kill` against the isolated daemon with each row transition (`pending` → `live` → attachment finalized → new proxy attachment → `exited`) asserted in storage. test: `tests/e2e/test_terminal_client_stack.py::test_gclient_select_spawn_attach_fallback_terminate`.

### 9.2 Protocol docs and vendor build-script parity [category: code] (depends: 2.9, 8.1, 3.3, 3.4, 5.1, 7.2)
`kind: deliverable`

Targets:
- `docs/contracts/gterm-protocols.md`
- `docs/guides/gterminal-development-guide.md`
- `scripts/build_vendored_libghostty_vt.sh`
- `crates/gterminal/build.rs`
- `crates/gterminal/vendor/zig-targets.txt`
- `crates/gterminal/tests/build_env.rs`

Closes A-13 and the documentation drift created by 2.7, 2.8, 2.9, and 8.1. Follows 3.3
(shared `build_env.rs`), 3.4 (documents the unpublished-pin state table), 5.1 (shared
`gterminal-development-guide.md`), and 7.2 (documents the parity script) so every
producer it documents or shares a file with has closed.

- A-13: `scripts/build_vendored_libghostty_vt.sh` omits `-Dtarget` for native builds
  while `build.rs` always passes it; make the script pass the same triple the build
  script computes (extract the triple map into a shared
  `crates/gterminal/vendor/zig-targets.txt` both read) and add a `build_env.rs` test that runs the script in `--print-command` mode and
  compares it with the build script's command for the host triple.
- Docs: `gterm-protocols.md` records the exec-ack pipe gate (2.7), the `control_overflow`
  / `control_deadline` / `lagged` / `event_overflow` closes, the dedicated event stream
  connection and request ids (2.9), the removal of the control `attach` verb (2.8), and
  the keyframe-on-overflow rule (8.1). `gterminal-development-guide.md` documents the
  parity script and the unpublished-pin install behaviour, and brings its
  § "Guard set G" (written by 1.1) up to date with every command and carve-out this
  epic changed, including the carve-outs that ended when their owner leaf closed.

Close gate: guard set G green (defined in `docs/guides/gterminal-development-guide.md` § "Guard set G": groups 1–7, the unaffected-group rule, and the known-red carve-out).

**Acceptance:**

- 9.2.1 - The helper script and `build.rs` produce the same Zig command for the host triple. test: `crates/gterminal/tests/build_env.rs::helper_script_matches_build_script_command`.
- 9.2.2 - The protocol contract documents every typed close and the event-stream connection introduced by this plan. file: `docs/contracts/gterm-protocols.md`.
- 9.2.3 - The development guide documents the parity script, and its § "Guard set G" matches the commands and carve-outs that are current at landing (every carve-out whose owner leaf has closed is removed). file: `docs/guides/gterminal-development-guide.md`.

## D1 Native default flip
`kind: deferred`

The flip to `default_backend: native` requires two real adjacent scheduled weekly runs
from the 5.2 producer that pass the hardened 5.2 offline checker *and* the 5.2 remote
provenance verifier (`scripts/verify_native_flip_evidence.py`). Those runs cannot exist
until the workflow has run on `0.5.0` for two consecutive weeks after landing, so the
flip is tail work of this epic that completes after L1, never before it. The landing
order is enforced by edges, not prose: the deferral task is created at expansion,
parented under this epic, with `blocked-by` edges on leaves 5.1 and 5.2 **and on the
epic's `merge` stage task** (the landing itself), so it cannot be claimed until the
code is on `0.5.0`; the epic's merge gate (L1) counts only non-deferred leaves, so the
open deferral task never blocks landing. Its validation criteria must cite source-plan
items `5.3.1` and `5.3.2`, the evidence file `docs/evidence/native-backend-flip.md`,
and a passing `scripts/verify_native_flip_evidence.py` run over the two adjacent
slots.

Source-plan mapping for the closed 5.3 leaf, one item each: 5.3.1 (the shipped
default is `native`, evidence linked, rollback documented) — deferred here; 5.3.2
(with the default flipped, explicit per-spawn backend selection and external-session
tmux handling are unchanged, and a default-native `terminal_create` still goes through
the shared spawn primitive and is refused before fork at `max_attachments_total - 4`)
— deferred here, proved by `tests/terminals/test_backend_selection.py::test_flip_preserves_explicit_and_external`
run with the flipped default in the same change as 5.3.1; 5.3.3 — re-satisfied by
5.2.6 (the retained source matrix) with 5.2.1 and 5.2.2; 5.3.4 — re-satisfied by 5.2.3
and 5.2.6; see that section's mapping paragraph.

```yaml
deferral:
  task_ref: "#TBD-created-at-expansion"
  reason: "The native default may ship only after two real adjacent weekly parity runs pass the hardened flip gate and the remote provenance verifier; the producer cannot have run before this epic lands, so the task is blocked by the merge stage and excluded from the landing gate. The flip and its post-flip preservation proof ship together."
  owner: "backend-developer"
  original_acceptance_items:
    - 5.3.1
    - 5.3.2
```

## L1 Landing into 0.5.0
`kind: verification`

The epic's `merge` stage lands the epic worktree on `0.5.0` through the dispatcher's
normal merge action once every **non-deferred** leaf is closed and guard set G is green
at the epic close; the D1 deferral task is tail work blocked by this stage and is
explicitly excluded from the landing gate (it is the only open child allowed at merge
time). Before the merge stage is approved:

- `git -C <epic worktree> merge-base --is-ancestor 0.5.0 HEAD` holds (the worktree is
  up to date with `0.5.0`; rerun leaf 1.1's regeneration commands if `0.5.0` moved the
  schema assets again).
- The pre-push hook passes (lint/format/type/ts/frontend).
- CI on the resulting push to `0.5.0` (`ci.yml` + `rust-ci.yml`, including the Windows
  cross-lint, the Linux Zig build, the full gterminal/gclient nextest, and the parity
  job) is the first real execution of those workflows for this code; a red run reopens
  the epic rather than being patched on `0.5.0`.
- `mark_worktree_merged` / `delete_worktree` close out `wt-task-20255-m4` (#20255's
  isolation worktree) after the landing commit is on `0.5.0`.
- Operator follow-ups after landing (outside this plan): publish `gterm-v0.1.0` /
  `gclient-v0.1.0` tags through the fixed release workflows, flip the `published` pins,
  ship the Homebrew formulae, and kill the leaked hosts on the review machine.

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: verification`

- reviewer_run: 753ff35d-0d2e-4e00-a6c2-ef2940ceecb5
- reviewer_session: 2573c6f0-5634-4ab9-a2ad-f4c403c99277 (#10890)
- verdict: needs_review
- findings:
- PR1-001 / blocking / §1.2 edited the closed source plan (managed hash drift) — accepted; schema rule moved to `## Constraints`, closed plan untouched
- PR1-002 / blocking / P2 sibling chains overlap without edges — accepted; 2.7 depends on 2.5 and 2.6
- PR1-003 / blocking / P4 protocol chain unordered — accepted; 4.4←4.3, 4.5←4.2+4.4, 4.6←4.5
- PR1-004 / blocking / cross-phase producer edges missing — accepted; 6.2←4.5, 6.3←4.6, 3.4←3.3, 9.2←3.3/3.4/5.1/7.2
- PR1-005 / blocking / LiveDaemon has no production-adapter acceptance — accepted; per-method success/typed-failure tests, mock daemon, production workflow in 9.1
- PR1-006 / blocking / 9.1 acceptance covers few E1 clauses — accepted; one item per corrected clause (9.1.4–9.1.11)
- PR1-007 / blocking / D1 cannot complete before landing under all-leaves-closed gate — accepted; L1 gate counts non-deferred leaves, D1 task blocked-by the merge stage
- PR1-008 / blocking / catalog.manifest.json missing from 1.2 Targets — accepted; added to 1.1 and 1.2 with regen order
- PR1-009 / blocking / attach/spawn_web_terminal callers untargeted — accepted; six test callers added to 2.2
- PR1-010 / blocking / SpawnResult.tmux_* removal sweep incomplete — accepted; tmux spawner, providers/support, and six test constructors added to 2.3
- PR1-011 / blocking / commit_deadline_ms config carriers untargeted — accepted; TerminalHostConfig, contract regen, config tests in 2.7
- PR1-012 / blocking / control id correlation without golden updates — accepted; all control_*.json goldens, wire tests, id semantics in 2.9
- PR1-013 / blocking / 3.2 Targets cover a subset of allow sites — accepted; full inventory, bindgen exemption by header
- PR1-014 / blocking / heterogeneous pin mapping — accepted; pins stay strings, UNPUBLISHED_MANAGED_BINS set
- PR1-015 / blocking / grant_lease/takeover_lease test callers — accepted; test_write_outcomes.py and test_native_runtime.py migrate in 4.1
- PR1-016 / blocking / write_fault_hook as a config field — accepted; constructor injection at the composition root, test-mode env read once
- PR1-017 / blocking / REST spawn/delete routes do not exist — accepted; spawn/terminate over WS terminal_create/terminal_kill
- PR1-018 / blocking / host/state.rs exceeds the ceiling across the chain — accepted; native_ops.rs split in 2.6, backpressure.rs in 8.1, ceiling guard
- PR1-019 / blocking / 401 drops live legacy identity — accepted; transactional backfill, zero-unmapped assertion before DROP
- PR1-020 / blocking / native spawn outcomes collapsed — accepted; outcome table with stable codes and list-based reconcile
- PR1-021 / blocking / exec ack before exec — accepted; CLOEXEC status pipe, EOF proves replacement
- PR1-022 / blocking / event stream has no recovery owner — accepted; reconnecting reader with seq/epoch reconcile
- PR1-023 / blocking / installer body contradicts acceptance — accepted; five-row state table
- PR1-024 / blocking / paged list plus events has no watermark — accepted; daemon_epoch + lifecycle seq + snapshot block, consumers in 4.6 and 6.2
- PR1-025 / blocking / shape checks cannot reject a fabricated run — accepted (modified); offline checker stays network-free, remote verifier script binds GitHub run metadata and is required by D1
- PR1-026 / blocking / proxy fallback reuses a finalized attachment — accepted; detach → finalize → fresh proxy attach → new id/generation → tombstone
- PR1-027 / blocking / Drop cannot await network cleanup — accepted; sync terminal-mode guard plus explicit async shutdown
- PR1-028 / blocking / global pgrep leak gate conflicts with out-of-scope hosts — accepted; test-owned PID accounting
- PR1-029 / blocking / send_keys action key has no identity source — accepted; optional idempotency_key with per-invocation fallback returned to the caller
- resolution_notes: All 29 findings accepted by the user (one vote each); every accepted repair is applied to the artifact after this checkpoint and base validation passes on the revised artifact. Review cap is 1, so this needs_review round is final: no further adversary round is launched. Human handoff: planning continues only through the explicit handoff tools (derive_plan_handoff_manifest / apply_plan_handoff_manifest) or the checkpoint menu; the round is persisted and finalized as a rejection checkpoint with completed_plan_review_rounds = 1.

```json plan-review-round
{"evidence_id":"72804f9b-ea8d-4344-a406-ba9f974e1829","plan_hash":"3d7fc824d7bd57132bb35dbf48e8d0416755573f7a5b2546fa934b2d1d9603a0","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"bd5904014d792036f964d4ee516fc4914608ea6bbe12fa5ca405e1f689ceee29","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":29,"total":31},"evidence_id":"72804f9b-ea8d-4344-a406-ba9f974e1829","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":11,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":12,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"89e300c2f742a54b028b44318cd8dd3005774fbbc3abd8fddea70f45badb9802","status":"valid"},"source_digest":"859e5caaef8fad0d054886a80e27acbd695507152cbe05b0b4639f12077afa52","version":1},"findings":[{"category":"traceability","check_key":"managed-plan-hash-drift","description":"Editing `.gobby/plans/herdr-terminal-client.md` will invalidate the closed epic’s registered hash and managed coverage manifest.","finding_id":"PR1-001-source-plan-registry-drift","fix":"Remove that historical-plan edit and state the corrected rule in this QA plan or a governing contract. If the old plan must change, specify a separate authorized plan-maintenance operation that updates registry state and regenerates coverage.","location":"§ 1.2","prevention":"Before targeting any `.gobby/plans/` file, check its plan row and managed coverage hash and name the authorized update path.","principle":"Managed plans and coverage artifacts must change through the authorized registry workflow.","root_cause":"Section 1.2 treats a closed governing plan as an ordinary source file.","section_id":"1.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"shared-runtime-owner-ordering","description":"Sections 2.4/2.5 and 2.6–2.9 overlap on `native_runtime.py`, `host_manager.py`, and shared tests, so isolated leaves can race or close against incoherent state.","finding_id":"PR1-002-p2-cross-chain-ordering","fix":"Make 2.7 depend on 2.5 as well as 2.6, then keep 2.8 and 2.9 behind 2.7.","location":"§§ 2.4–2.9","prevention":"Build the expanded dependency graph and add an edge for every shared production file or test seam.","principle":"Leaves that edit one runtime authority must be explicitly serialized.","root_cause":"P2 splits host/runtime work into sibling chains with overlapping files and no cross-edge.","section_id":"2.7","severity":"blocking"},{"category":"bad-sequencing","check_key":"ws-protocol-owner-ordering","description":"Sections 4.3 and 4.4 share handlers/relay tests, 4.4 and 4.5 share protocol emitters, 4.2 and 4.5 share attention routes, and 4.5 and 4.6 share the hook test without ordering.","finding_id":"PR1-003-p4-protocol-ordering","fix":"Make 4.4 depend on 4.3; make 4.5 depend on 4.2 and 4.4; make 4.6 depend on 4.5.","location":"§§ 4.2–4.6","prevention":"Cross-check every shared target and produced corpus against manifest dependency edges.","principle":"Protocol emitters, handlers, relays, goldens, and clients must advance in one ordered chain.","root_cause":"P4 sibling dependencies do not enforce the sequence the section text assumes.","section_id":"4.4","severity":"blocking"},{"category":"bad-sequencing","check_key":"artifact-producer-before-consumer","description":"6.2 consumes the 4.5 golden corpus, 6.3 consumes the 4.6 write-outcome contract, and 9.2 documents/overlaps work from 3.3, 3.4, 5.1, and 7.2 before those leaves must exist.","finding_id":"PR1-004-cross-phase-producer-edges","fix":"Add 4.5 to 6.2, 4.6 to 6.3, and the actual 3.3/3.4/5.1/7.2 producers to 9.2; serialize 3.3/3.4 shared test ownership.","location":"§§ 3.3, 3.4, 6.2, 6.3, 9.2","prevention":"For every named corpus, script, behavior, or shared file, verify a producer edge or co-locate the work.","principle":"A consumer leaf must depend on the leaf that creates or finalizes its artifact or contract.","root_cause":"Cross-phase text names producers but the dependency graph omits them.","section_id":"6.2","severity":"blocking"},{"category":"weak-testability","check_key":"production-adapter-acceptance","description":"`LiveDaemon` roster/respond/seen/spawn/terminate/auth/error mapping and the source plan’s select→spawn→attach→terminate workflow have no production-adapter acceptance.","finding_id":"PR1-005-live-daemon-acceptance","fix":"Add adapter tests for every method and bearer/error mapping, plus one production-path select-through-terminate workflow with proxy fallback asserted.","location":"§§ 6.2–6.3","prevention":"Map each production method to an authenticated success case, typed failure case, and end-to-end consumer test.","principle":"Production adapters need observable acceptance for every mutating capability and error boundary.","root_cause":"The plan relies on scripted trait tests and construction checks for most `LiveDaemon` behavior.","section_id":"6.2","severity":"blocking"},{"category":"weak-testability","check_key":"e1-clause-acceptance-parity","description":"Daemon-down refusal, DB finalization, fault/no-write behavior, observer sharing, prepared ownership, stream recovery, and several lifecycle clauses can disappear from derived validation criteria.","finding_id":"PR1-006-e1-acceptance-coverage","fix":"Add stable acceptance items for every corrected E1 clause, or one comprehensive item explicitly enumerating them and naming `test_terminal_client_stack_end_to_end`.","location":"§ 9.1","prevention":"Compare every behavioral clause in a deliverable body with a stable acceptance item and exact artifact.","principle":"Manifest validation criteria must cover every observable clause a deliverable claims to repair.","root_cause":"The section body enumerates many E1 repairs, while its three acceptance items cover only AST lint, one replay nonce, and broad lifecycle states.","section_id":"9.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"deferred-tail-landing-order","description":"D1 cannot complete before landing and cannot express landing as its predecessor under the stated all-leaves-closed merge gate.","finding_id":"PR1-007-deferred-flip-landing-cycle","fix":"Make landing an enforceable predecessor of deferred tail work, or move the native flip into a separately governed post-landing epic.","location":"§ D1 / § L1","prevention":"Model deferred external prerequisites and landing order before approving the tail-work topology.","principle":"Post-landing deferred work needs an enforceable landing predecessor without blocking that landing.","root_cause":"The deferral requires two weekly runs after landing while L1 says landing waits for every leaf to close.","section_id":"D1","severity":"blocking"},{"category":"traceability","check_key":"schema-catalog-inventory","description":"Registering migration 401 changes the embedded catalog inventory, so the leaf cannot satisfy its own freshness and identity gates as targeted.","finding_id":"PR1-008-schema-catalog-target","fix":"Add `crates/gcore/assets/schema/catalog.manifest.json` to Targets and regenerate it before root hash, identity JSON, and grant signatures.","location":"§ 1.2","prevention":"Inventory all generated schema carriers whenever MIGRATIONS changes.","principle":"Schema identity regeneration must include every derived inventory carrier.","root_cause":"`catalog.manifest.json` is consumed by freshness/root-hash gates but omitted from Targets.","section_id":"1.2","severity":"blocking"},{"category":"traceability","check_key":"required-parameter-callers","description":"Lease, WS-create, WS-lease, and backend-selection tests in guard set G still call the changed APIs without caller-minted IDs.","finding_id":"PR1-009-required-id-callers","fix":"Add all direct caller files to 2.2 Targets and update them before the full guard run.","location":"§ 2.2","prevention":"Run callers/constructor search for every signature change and target every result.","principle":"Required-parameter changes must migrate every caller and fake in the same ordered leaf.","root_cause":"The target inventory omits direct callers of `attach` and `spawn_web_terminal` that pass no new IDs.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"removed-field-constructor-sweep","description":"`SpawnResult.tmux_*` removal leaves the tmux spawner and agent/MCP/SRT/failure-cleanup fakes constructing or asserting removed fields.","finding_id":"PR1-010-spawnresult-callers","fix":"Target the tmux spawner and every direct constructor/assertion; derive display metadata from terminal rows.","location":"§ 2.3","prevention":"Use symbol/caller search plus literal field-name search across production and tests before removing a model field.","principle":"Deleting data fields requires an exhaustive producer, constructor, destructure, fake, and assertion sweep.","root_cause":"The plan scopes the main spawn path while omitting the tmux producer and multiple test constructors.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"config-derived-carrier-sweep","description":"Guard set G byte-compares `runtime_config_contract.json`, so the leaf is structurally incomplete.","finding_id":"PR1-011-host-config-carriers","fix":"Target `TerminalHostConfig`, the runtime config contract, and config tests; regenerate and verify the forwarded scalar.","location":"§ 2.7","prevention":"For every config field, enumerate model, nested registry, serialized contract, defaults, and tests.","principle":"New config fields must update their model, registry carriers, generated contract, and tests atomically.","root_cause":"`commit_deadline_ms` is described as new configuration while its model and derived contract are absent from Targets.","section_id":"2.7","severity":"blocking"},{"category":"traceability","check_key":"wire-shape-golden-parity","description":"Rust and Python byte-exact control fixtures remain id-less, so guard tests or consumers will drift.","finding_id":"PR1-012-control-id-goldens","fix":"Target Rust/Python wire-golden tests and control fixtures; define missing, duplicate, mismatched, and interleaved ID behavior.","location":"§ 2.9","prevention":"For any protocol field change, sweep fixtures, encoders, decoders, goldens, and interleaving tests in every language.","principle":"Cross-language wire-shape changes must update all golden producers and consumers atomically.","root_cause":"Correlation IDs are added to every control exchange without targeting the golden corpus.","section_id":"2.9","severity":"blocking"},{"category":"traceability","check_key":"exhaustive-guard-existing-matches","description":"The guard already matches `ghostty/bindings.rs`, `host/frames.rs`, `raw_input.rs`, and other sites outside 3.2 Targets.","finding_id":"PR1-013-lint-scan-targets","fix":"Expand Targets to every current match, remove blanket attributes, and retain only reasoned item-level exceptions.","location":"§ 3.2","prevention":"Run the proposed source scan during planning and add every existing match to Targets.","principle":"A new exhaustive guard must account for every existing match before the owning leaf closes.","root_cause":"The plan’s target list covers only some current blanket or unreasoned `allow` sites.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"shared-mapping-shape-consumers","description":"Freshness, CLI semver, distribution, hook, and gcode consumers still treat every pin value as a string.","finding_id":"PR1-014-version-pin-shape","fix":"Keep string floors plus a separate typed publication-status mapping, or migrate every entry and consumer to one `ManagedBinPin` type.","location":"§ 3.4","prevention":"Sweep iterations, comparisons, serialization, and tests before changing a shared mapping’s value type.","principle":"Shared mapping value-shape changes must migrate every generic consumer or preserve a uniform type.","root_cause":"Only two string pin values gain object metadata, creating a heterogeneous mapping.","section_id":"3.4","severity":"blocking"},{"category":"traceability","check_key":"deleted-method-caller-sweep","description":"Untargeted `write_outcomes` and `native_runtime` tests call methods 4.1 deletes.","finding_id":"PR1-015-write-coordinator-callers","fix":"Target and migrate every caller to `TerminalLeaseRegistry`; add no compatibility shim.","location":"§ 4.1","prevention":"Search production and tests for every deleted method and assign each result to the leaf.","principle":"Deleting authority methods requires migrating all callers, including test seams run by the guard set.","root_cause":"The claim that `grant_lease`/`takeover_lease` have no callers excludes direct branch tests.","section_id":"4.1","severity":"blocking"},{"category":"over-engineering","check_key":"single-consumer-config-knob","description":"`TerminalHostConfig.write_fault_hook` is a one-consumer executable knob that leaks into Pydantic registry/schema/contract machinery.","finding_id":"PR1-016-callable-config-mechanism","fix":"Replace it with constructor/app-context injection in the isolated server composition root, keeping the callable outside the config model.","location":"§ 4.3","prevention":"Before adding a config field, name multiple runtime values/consumers and prove ordinary dependency injection is insufficient.","principle":"Production config surface must earn each knob through a real configurable consumer and serializable value.","root_cause":"No production config consumer exists; only the isolated E1 test needs the callable, and a direct composition seam is sufficient.","section_id":"4.3","severity":"blocking"},{"category":"missing-requirement","check_key":"consumer-endpoint-existence","description":"POST `/api/terminals` and DELETE `/api/terminals/{id}` have no server implementation in scope before 6.2.","finding_id":"PR1-017-gclient-missing-server-ops","fix":"Add authenticated REST handlers/tests in an earlier leaf, or use the existing `terminal_create`/`terminal_kill` WebSocket contract.","location":"§§ 2.2 / 6.2","prevention":"Resolve every planned client method to an existing endpoint/message and add producer dependencies for missing ones.","principle":"A client operation must map to a server contract implemented by a prerequisite.","root_cause":"`LiveDaemon` assumes REST spawn/delete routes that the branch router does not expose.","section_id":"6.2","severity":"blocking"},{"category":"gobby-format","check_key":"production-source-ceiling","description":"The planned native verbs, pipe state, event delivery, correlation, and backpressure cannot fit under the repository ceiling.","finding_id":"PR1-018-host-state-decomposition","fix":"Add an explicit early split such as `native_ops.rs` and `subscribers/backpressure.rs`, with source-size validation at every affected leaf.","location":"§§ 2.6, 2.7, 2.9, 8.1","prevention":"Measure every targeted production file at planning time and add decomposition before projected growth crosses 999 lines.","principle":"Production source must stay below 1,000 lines throughout the leaf chain.","root_cause":"`host/state.rs` begins at 977 lines and absorbs four substantial features without a split.","section_id":"2.6","severity":"blocking"},{"category":"unhandled-edge","check_key":"migration-live-identity-preservation","description":"Active legacy runs can lose `tmux_session_name` without gaining a terminal row or `terminal_id`, contradicting the plan’s non-destructive claim.","finding_id":"PR1-019-migration-preserves-live-identity","fix":"Transactionally backfill terminal rows and references, reject unmappable rows, and drop the old column only after proving zero unmapped identities.","location":"§ 1.2","prevention":"Seed migration tests with live, exited, duplicate, malformed, and empty legacy identities before dropping source columns.","principle":"Existing-hub migrations must preserve live identity or fail safely before destructive removal.","root_cause":"The migration creates an empty terminal table and drops the only legacy tmux identity without a backfill.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"spawn-effect-boundary-outcomes","description":"A lost commit reply can leave a live PTY while the daemon marks the row failed, and codes such as capacity or `exec_timeout` are erased.","finding_id":"PR1-020-native-spawn-outcome-table","fix":"Add an explicit outcome table; preserve stable refusal codes and reconcile unknown commit outcomes via `list` and terminal/spawn identity.","location":"§§ 2.4 / 2.7","prevention":"Enumerate each async boundary as pre-effect, post-effect, or unknown and specify reconciliation before row transition.","principle":"Spawn state must distinguish proved no-effect refusal from typed host refusal and indeterminate post-effect response loss.","root_cause":"The plan catches diverse errors around reserve/prepare/commit and collapses them into one failed-row outcome.","section_id":"2.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"exec-success-sentinel","description":"A missing or non-executable target can acknowledge, fail `exec`, and still be reported committed.","finding_id":"PR1-021-exec-success-proof","fix":"Use a close-on-exec status pipe: EOF proves replacement; child-written errno/details reports exec failure. Bound both gate and status waits.","location":"§ 2.7","prevention":"Test nonexistent binary and permission denial whenever a design claims an exec-success barrier.","principle":"Commit success must prove target process replacement, not arrival at the `exec` statement.","root_cause":"The child writes its acknowledgement before `exec`.","section_id":"2.7","severity":"blocking"},{"category":"unhandled-edge","check_key":"bounded-stream-recovery","description":"One overflow or EOF can permanently lose exit/title/resize state and all later live delivery.","finding_id":"PR1-022-event-stream-recovery","fix":"Own a reconnecting reader in `TerminalHostManager`: re-authenticate, subscribe, list, reconcile by epoch/seq/last_seq, then resume.","location":"§§ 2.9 / 8.1","prevention":"For each overflow/EOF path, specify the owner, snapshot source, sequence watermark, and resume transition.","principle":"Every lossy bounded stream needs reconnect, gap detection, authoritative recovery, and generation fencing.","root_cause":"The plan closes event subscriptions on overflow without assigning recovery to the daemon reader.","section_id":"2.9","severity":"blocking"},{"category":"unhandled-edge","check_key":"installer-publication-state-table","description":"An implementer cannot satisfy both instructions for `published=true` with no tag.","finding_id":"PR1-023-installer-state-contradiction","fix":"Define: unpublished skips; published requires the exact tag and fails typed when absent; source-build override is explicit.","location":"§ 3.4","prevention":"Write a complete state table before describing installer branches and reuse it across all consumers.","principle":"One pin state and lookup result must have one deterministic install outcome.","root_cause":"The body says missing tags skip, while acceptance says a published missing tag fails typed.","section_id":"3.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"paged-snapshot-event-causality","description":"A concurrent create or exit can be erased or resurrected when the final page replaces client state.","finding_id":"PR1-024-roster-causal-snapshot","fix":"Add a stable snapshot token/watermark plus monotonic epoch/sequence; buffer during paging, install atomically, then replay newer events.","location":"§§ 4.4, 4.6, 6.2","prevention":"At every list+events boundary, specify snapshot epoch/watermark and replay ordering across all pages.","principle":"Paged snapshots combined with live events need a server-owned causal watermark.","root_cause":"The plan buffers/replaces pages but defines no stable snapshot token, event sequence, or stale-request fence.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"release-evidence-authority","description":"A well-formed nonexistent run can satisfy the proposed checks, so the fabricated-evidence class remains open.","finding_id":"PR1-025-flip-evidence-provenance","fix":"Resolve GitHub run/workflow/job provenance and compare SHA/time/conclusion, or consume a verifiable signed CI attestation.","location":"§§ 5.1–5.2","prevention":"Include a plausible fabricated record in negative fixtures and verify every provenance field against an authoritative source.","principle":"Evidence that gates a release must bind to an authority independent of the artifact being checked.","root_cause":"The checker trusts self-reported run ID, SHA, timestamps, and workflow creation time after only shape checks.","section_id":"5.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"finalized-attachment-transition","description":"Proxy delivery may reuse stale attachment identity or start before registration, while acceptance proves only detach.","finding_id":"PR1-026-proxy-fallback-fresh-attach","fix":"Await finalization, issue `terminal_attach(frame_delivery=proxy)`, require a new ID/generation, tombstone the old ID, then resume/control.","location":"§§ 6.2–6.3","prevention":"Model every transport fallback as detach/finalize → fresh attach → install new generation → optional control.","principle":"A finalized attachment ID can never authorize a fallback transport or renewed control.","root_cause":"The plan detaches on direct-frame failure and says to fall back without defining the fresh proxy attach/result transition.","section_id":"6.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"sync-drop-async-cleanup","description":"`Drop` cannot reliably await daemon calls; blocking can deadlock and spawned cleanup can be cancelled.","finding_id":"PR1-027-async-teardown-boundary","fix":"Use an idempotent synchronous terminal-mode guard plus explicit async shutdown for normal exits/signals; rely on WS close for panic-side server cleanup.","location":"§ 6.4","prevention":"Separate local synchronous cleanup from remote async cleanup and test runtime-shutdown/panic behavior.","principle":"Synchronous Rust `Drop` can guarantee local restoration, while network cleanup needs an explicit cancellation-safe async phase.","root_cause":"The plan assigns awaited detach/release guarantees to one RAII guard during panic and runtime teardown.","section_id":"6.4","severity":"blocking"},{"category":"weak-testability","check_key":"test-process-ownership-scope","description":"Guard set G and the autouse fixture can never pass reliably on a machine with unrelated `gterm host` processes.","finding_id":"PR1-028-test-owned-host-leak-gate","fix":"Compare before/after PIDs or scope to the isolated test state directory, and assert only test-owned hosts are gone.","location":"§ 2.8 / Constraints","prevention":"Snapshot baseline processes or tag test processes by state/socket directory before asserting cleanup.","principle":"Leak checks must distinguish test-owned processes from pre-existing user processes.","root_cause":"The global `pgrep` gate conflicts with the explicit out-of-scope decision to leave leaked review-machine hosts untouched.","section_id":"2.8","severity":"blocking"},{"category":"missing-requirement","check_key":"mcp-write-idempotency-source","description":"The implementer cannot construct the specified key, and using a constant or random fallback respectively suppresses later writes or loses replay semantics.","finding_id":"PR1-029-mcp-send-keys-identity","fix":"Add a caller-stable idempotency/sequence input with validation and replay/conflict tests, or define an equivalent durable per-invocation identity.","location":"§ 4.2","prevention":"Resolve every identifier in proposed action keys to an input, durable counter, or invocation context and test retries/conflicts.","principle":"Every deduplicated write action key needs a defined caller-stable identity source and replay contract.","root_cause":"The plan references `client_seq`, but the registered `send_keys(session_id, keys, literal)` surface defines no such value.","section_id":"4.2","severity":"blocking"}],"reviewer_session":"#10890","round":1,"round_number":1,"verdict":"needs_review"},"session_id":"096ddf5a-5dfb-454d-a5c1-d27d6086dd16"}
```

**Round 2** `kind: verification`

- reviewer_run: 093604bc-843e-411e-81c0-044297e0ab6b
- reviewer_session: d5442827-0da9-40c3-b5dc-597c60a02c62 (#10948)
- verdict: needs_review
- findings:
- PR2-001 / blocking / P1 cites no F-* IDs — accepted (modified); the review record is not in the repo, so 1.1/1.2 cite the F-class they close and the Overview states that P1 cites the class
- PR2-002 / blocking / 2.4 outcome table and 3.4 state table lack per-row acceptance — accepted; seven row-stable items added
- PR2-003 / blocking / 2.4 commit rows have four cells under five headers — accepted; rows rewritten with separate Boundary/Signal cells
- PR2-004 / blocking / six acceptance proof files unowned, 3.3/3.1 unordered — accepted; Targets added, 3.3 depends on 3.1 (3.4.2 moved to `tests/test_runner_bin_freshness.py`, the loop's existing direct test module)
- PR2-005 / blocking / no real-gclient external-tmux E2E — declined: the source plan routes tmux rows through the same host path (no separate client branch) and `tests/e2e/test_external_terminal_attach.py` covers discovery/attach at the daemon boundary; a real-gclient external-tmux E2E is new scope, recorded as out of scope in Constraints
- PR2-006 / blocking / 9.1 gclient workflow depends on the default backend 5.1 flips — accepted; the isolated daemon pins `terminals.default_backend: native` and asserts the row's backend
- PR2-007 / blocking / 2.2.4 needs 2.3's state — accepted; allowlist narrowing and its criterion move to 2.3 (2.3.5)
- PR2-008 / blocking / 6.3.5 proof lives in 9.1's file — accepted; `tests/e2e/test_terminal_client_stack.py` added to 6.3 Targets
- PR2-009 / blocking / two untargeted SpawnResult tmux-field sites — accepted; `test_tmux.py` and `test_spawn_agent_impl_provider.py` added to 2.3
- PR2-010 / blocking / bin_freshness_loop callers and patch consumers untargeted — declined: `run_daemon` / `_default_loops` / `start_periodic_tasks` pass the loop unchanged and `test_install_setup_gdaemon.py` patches a symbol whose signature does not change; only `tests/test_runner_bin_freshness.py::*` is a real consumer and is added by hand
- PR2-011 / blocking / branch sweep hit lists absent — accepted (modified); the planning-time sweep was run against `wt-task-20255-m4` for every removed/re-signed symbol and its misses folded into Targets; Constraints records the sweep commands and requires each leaf to record its own hit list in its evidence rather than in the plan
- PR2-012 / blocking / ConnectionError split by an unrepresented request-written fact — accepted; typed `CommitTransportError(request_written, request_id)`, listed-prepared outcome, 2.4.2/2.4.6 aligned
- PR2-013 / blocking / shell wrapper cannot emit errno — accepted (modified); shell-observable typed codes (`not_found`, `not_executable`) replace errno in the protocol and 2.7.3
- PR2-014 / blocking / control ids without reader ownership — accepted; one reader task, write-only lock, id allocator, pending map settled on EOF/reconnect/close
- PR2-015 / blocking / bare last_event_seq across epochs — accepted; cursor is `(host_epoch, seq)` and resets after epoch reconciliation
- PR2-016 / blocking / Delivered replay needs a record the coordinator does not keep — accepted (modified); acceptance narrowed to unresolved-key dedup with the payload fingerprint on the latch; a Delivered key is cleared and a retry is a fresh write
- PR2-017 / blocking / ≥ 10⁹ accepts the date-shaped ids — accepted (modified); the offline date-shape claim is dropped (real run ids will cross 2.0e10), 5.2.1 keeps the slot and producer-age rejections, the remote verifier is the id authority
- PR2-018 / blocking / host_epoch and direct locator have no producer — accepted; 4.3 emits them on a granted direct attach, 4.5 goldens carry them, 4.3.6 added
- PR2-019 / blocking / re-registering finalized attachment ids after reconnect — accepted; reconnect and daemon restart issue fresh attaches and reacquire control; 6.2.2 and 9.1.10 reworded
- PR2-020 / blocking / 9.1.5 reads attachment state storage does not hold — accepted (modified); finalization observed through the live lease registry's WS-visible effects (`terminal_detached` lifecycle for the dropped id, immediate grant to a fresh attachment)
- PR2-021 / blocking / matrix cells cannot push the evidence commit — accepted; single post-matrix `record` job with scoped `contents: write` commits once
- PR2-022 / blocking / uncertain banner has no resolution path — accepted (modified); the banner offers a same-sequence retry that resolves the latch by capture, lease loss supersedes it, and the E2E clears uncertainty with exactly one retry
- resolution_notes: Unattended round; the coordinator judged every finding. 20 accepted (9 with modifications recorded above), 2 declined with rationale. Repairs applied through apply_plan_review_repairs after this checkpoint; prose-only fixes hand-applied; base validation rerun on the revised artifact. Review cap raised by the user to ten further rounds or convergence, so a round 3 follows.

```json plan-review-round
{"evidence_id":"d5e5e4e1-7ec1-484a-9978-aaefd594e735","plan_hash":"75f08084c6167f80e52731cef07ecc5045e54d132169101c62eb4d27007c9926","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"6e42eb10883ad257b69003bdf256439d2c4ef8f5c4bc56150bc53b69ab289229","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":22,"total":26},"evidence_id":"d5e5e4e1-7ec1-484a-9978-aaefd594e735","lanes":[{"candidate_count":10,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":12,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"b42fafc5a4aae47985984ae6f30b14a0dd91405731dcc8c523d062762229249e","status":"valid"},"source_digest":"1f22584143d761df0ee54690fffc0195680f7f2afaa87f3c39557ad14bbd9d3b","version":1},"findings":[{"category":"missing-requirement","check_key":"qa-finding-id-parity","description":"No deliverable cites an F-* finding even though the Overview says every QA finding is cited. The unanswered requirement is which exact F-* findings P1 must close and which acceptance item proves each one.","finding_id":"PR2-001-f-finding-traceability","fix":"Read the governing QA record, add every exact F-* ID to 1.1 or 1.2 with a one-to-one acceptance mapping, or correct the Overview if that record contains no F-class findings.","location":"Overview and P1 / §§ 1.1–1.2","prevention":"Enumerate every governing finding ID and check that each appears in one deliverable with a matching acceptance item.","principle":"A plan that claims complete remediation must map every governing finding class to owned work and observable acceptance.","root_cause":"The Overview names F-* merge-drift findings but P1 describes drift work without any F identifier or review-record mapping.","section_id":"1.1","severity":"blocking"},{"category":"gobby-format","check_key":"table-row-decomposition","description":"The six native-spawn outcomes and five installer states lack row-stable acceptance parity. Existing individual items cover only some rows; aggregate items cannot identify which row failed.","finding_id":"PR2-002-table-row-acceptance","fix":"Add separate acceptance items for the four uncovered 2.4 outcomes and three uncovered 3.4 states.","location":"§§ 2.4 and 3.4 acceptance blocks","prevention":"For every deliverable table, pair each data row with one distinct acceptance item before review.","principle":"Each work-table row needs its own stable, observable acceptance item.","repairs":[{"items":[{"artifact":"test: `tests/agents/test_native_spawn.py::test_spawn_outcome_table`","prose":"A typed reserve or prepare refusal (`capacity`, `stale`, `not_native`, or `host_draining`) fails the pending row and preserves the exact host code."},{"artifact":"test: `tests/agents/test_native_spawn.py::test_spawn_outcome_table`","prose":"`HostEpochChangedError` during prepare fails the pending row with `host_epoch_changed` and never promotes it."},{"artifact":"test: `tests/agents/test_native_spawn.py::test_spawn_outcome_table`","prose":"A lost commit reply with a listed live PTY promotes the pending row, while an absent PTY fails it with `not_found`."},{"artifact":"test: `tests/agents/test_native_spawn.py::test_spawn_outcome_table`","prose":"A typed `exec_timeout`, `exec_failed`, or `gate_timeout` reply leaves the process group dead, fails the row, and preserves the host code."}],"kind":"add_acceptance","section_id":"2.4"},{"items":[{"artifact":"test: `tests/cli/test_install_setup_gclient.py::test_installer_state_table`","prose":"An unpublished pin with source-build opt-in performs only the checkout build and fails typed when the checkout or Zig is unavailable."},{"artifact":"test: `tests/cli/test_install_setup_gclient.py::test_installer_state_table`","prose":"A published pin with its exact tag uses the release-download, binstall, and git-install fallback chain in order."},{"artifact":"test: `tests/cli/test_install_setup_gclient.py::test_installer_state_table`","prose":"A published pin with source-build opt-in performs the checkout build without a tag lookup."}],"kind":"add_acceptance","section_id":"3.4"}],"root_cause":"Aggregate items 2.4.6 and 3.4.4 claim coverage for multiple rows, so row omissions can pass while total item counts remain plausible.","section_id":"2.4","severity":"blocking"},{"category":"gobby-format","check_key":"outcome-table-column-parity","description":"The five-column table's three commit rows contain four cells. As rendered, `no effect`, `fail_pending`, and the error code occupy the wrong columns, so the implementer cannot reliably distinguish signal, host effect, and row transition.","finding_id":"PR2-003-outcome-table-shape","fix":"Rewrite each commit row with separate Boundary and Signal cells and explicit Effect on host, Row transition, and `SpawnResult.error` cells.","location":"§ 2.4 native-spawn outcome table","prevention":"Render each behavior table and verify every row has the header's cell count and semantic alignment.","principle":"A state-transition table must preserve one semantic cell per declared column.","root_cause":"The final three rows combine boundary and signal into one cell, shifting effect, row transition, and error code under the wrong headers.","section_id":"2.4","severity":"blocking"},{"category":"gobby-format","check_key":"acceptance-artifact-target-ownership","description":"Six leaves require new named tests in unowned files. Adding `test_install_setup_gterm.py` to 3.1 also exposes an unordered shared target with 3.3.","finding_id":"PR2-004-acceptance-proof-targets","fix":"Add the six proof files to their owning sections and make 3.3 depend on 3.1.","location":"Targets for §§ 2.4, 2.8, 3.1, 3.3, 3.4, 4.5, and 6.1","prevention":"Diff every acceptance artifact path against its section Targets, then rerun shared-target ordering on prospective additions.","principle":"Every newly introduced acceptance artifact must be owned by its leaf, and shared proof files must be ordered.","repairs":[{"entries":["`tests/agents/test_spawn_executor.py::*` — scope-reason: add the locator-unavailable promotion regression required by acceptance 2.4.3"],"kind":"add_targets","section_id":"2.4"},{"entries":["`crates/gterminal/tests/control_protocol.rs`"],"kind":"add_targets","section_id":"2.8"},{"entries":["`tests/cli/test_install_setup_gterm.py::*` — scope-reason: replace workflow substring checks with package-list assertions"],"kind":"add_targets","section_id":"3.1"},{"kind":"add_dependency","on":["3.1"],"section_id":"3.3"},{"entries":["`tests/test_runner_lifecycle.py::*` — scope-reason: add unpublished-pin freshness-loop acceptance coverage"],"kind":"add_targets","section_id":"3.4"},{"entries":["`tests/servers/test_terminal_ws_create.py`"],"kind":"add_targets","section_id":"4.5"},{"entries":["`crates/gclient/tests/workspace.rs`"],"kind":"add_targets","section_id":"6.1"}],"root_cause":"Acceptance paths were added without updating Targets or recomputing shared-target dependencies.","section_id":"2.4","severity":"blocking"},{"category":"missing-requirement","check_key":"external-tmux-gclient-milestone","description":"Task #18802 requires gclient to discover and attach to an existing Ghostty/tmux terminal without relaunching it. No plan acceptance proves the real gclient's initial proxy attachment or writer-lease lifecycle for an externally owned tmux terminal.","finding_id":"PR2-005-external-tmux-client-path","fix":"Add the external-tmux branch to the gclient daemon/workspace flow and a real-gclient E2E test that discovers, proxy-attaches, acquires/releases control, detaches, and reattaches without spawning a replacement.","location":"§§ 6.2–6.3 and 9.1","prevention":"Trace every required backend × ownership combination through discovery, attach, control, detach, reattach, and E2E acceptance.","principle":"A first-release backend requirement needs a production client branch and an end-to-end proof on that backend.","root_cause":"The client work centers native direct frames and native proxy fallback; the existing external-tmux test exercises daemon/browser attachment rather than the real gclient.","section_id":"6.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"e2e-backend-precondition","description":"The direct-frame workflow is schedule-dependent: it may create native before 5.1 or tmux after 5.1. No dependency or fixture override guarantees the native socket required by the test.","finding_id":"PR2-006-native-e2e-backend-order","fix":"Configure/request native explicitly in the isolated daemon or spawn request and assert the created row's backend before direct attachment. Add an ordering edge only if the test intentionally consumes 5.1's shipped default.","location":"§ 9.1 production-path gclient workflow","prevention":"For every backend-specific E2E path, pin and assert the backend before exercising backend-only transport.","principle":"An end-to-end test must establish its backend precondition independently of concurrent default changes.","root_cause":"9.1 issues `terminal_create` without an explicit backend while unordered 5.1 changes the default from native to tmux.","section_id":"9.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"leaf-local-acceptance-closure","description":"Leaf 2.2 cannot close: item 2.2.4 requires `_FIELD_SWEEP_ALLOWED` to contain the final three entries only once downstream 2.3 lands.","finding_id":"PR2-007-downstream-allowlist-acceptance","fix":"Move the final narrowed-allowlist criterion to 2.3 and make 2.2 assert only the temporary state it can satisfy before 2.3.","location":"§ 2.2 acceptance item 2.2.4","prevention":"Evaluate every acceptance item at its leaf's close point, before any dependent leaf can run.","principle":"A leaf must satisfy its acceptance using its own changes and predecessors.","root_cause":"The criterion describes the final allowlist after 2.3 even though 2.3 depends on 2.2.","section_id":"2.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"leaf-local-proof-ownership","description":"Leaf 6.3 cannot create `tests/e2e/test_terminal_client_stack.py::test_gclient_reaches_workspace`; 9.1 owns that file and depends on 6.3.","finding_id":"PR2-008-downstream-e2e-acceptance","fix":"Move item 6.3.5 into 9.1, or add the file to 6.3 and implement the test before 6.3 closes.","location":"§ 6.3 acceptance item 6.3.5","prevention":"Check every named proof file against dependency direction and Targets ownership.","principle":"Acceptance evidence must exist in the leaf that is judged, or in an already-completed predecessor.","root_cause":"6.3 names a new test in a file owned only by downstream 9.1.","section_id":"6.3","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR1-010-spawnresult-callers","causal_section_ids":["2.3"],"check_key":"acceptance-observability","description":"`tests/agents/test_tmux.py` still asserts all three removed fields and `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py` mutates `tmux_session_name`; neither is targeted.","finding_id":"PR2-009-spawnresult-adjacent-consumers","fix":"Add both files to 2.3 and migrate them to terminal-id/backend-neutral assertions.","introduced_in_round":1,"location":"§ 2.3 Targets","prevention":"After a removed-field sweep, search both constructor syntax and every removed field name across source and tests.","principle":"Removed model fields require every assertion, mutation, constructor, destructure, and fake to be owned.","repairs":[{"entries":["`tests/agents/test_tmux.py::*` — scope-reason: migrate every SpawnResult tmux-field assertion to terminal identity","`tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py::*` — scope-reason: replace the SpawnResult tmux-field mutation used by the provider fake"],"kind":"add_targets","section_id":"2.3"}],"root_cause":"The round-1 consumer repair covered constructors but missed two non-constructor tmux-field sites.","section_id":"2.3","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR1-014-version-pin-shape","causal_section_ids":["3.4"],"check_key":"acceptance-observability","description":"`bin_freshness_loop` consumers in `runner_lifecycle.py`, `runner_lifecycle_periodic.py`, and `tests/test_runner_bin_freshness.py`, plus the installer patch in `test_install_setup_gdaemon.py`, are absent from Targets.","finding_id":"PR2-010-installer-adjacent-consumers","fix":"Add the lifecycle caller/registration symbols and both omitted test files to 3.4.","introduced_in_round":1,"location":"§ 3.4 Targets and consumer-sweep paragraph","prevention":"Run exact and literal consumer sweeps for each changed symbol, including injected callbacks and patch strings.","principle":"Exact symbol Targets require their production callers, registration seams, and direct/string-patch tests.","repairs":[{"entries":["`src/gobby/runner_lifecycle.py::run_daemon`","`src/gobby/runner_lifecycle_periodic.py::_default_loops`","`src/gobby/runner_lifecycle_periodic.py::start_periodic_tasks`","`tests/test_runner_bin_freshness.py::*` — scope-reason: update all direct freshness-loop publication-state tests","`tests/cli/test_install_setup_gdaemon.py::*` — scope-reason: update the string-patch consumer of managed native binary installation"],"kind":"add_targets","section_id":"3.4"}],"root_cause":"The publication-state repair treated re-exports as the full consumer surface and omitted lifecycle wiring and existing tests.","section_id":"3.4","severity":"blocking"},{"category":"traceability","check_key":"branch-overlay-consumer-sweep","description":"The snapshot acknowledges the overlay-index gap but omits contract-required hit lists. The newly verified 2.3 and 3.4 misses demonstrate that the current substitute evidence is incomplete.","finding_id":"PR2-011-branch-consumer-sweep-evidence","fix":"Run the field/signature/guard sweeps from `wt-task-20255-m4`, record complete hit lists in Constraints or owning sections, and qualify indexed files or justify file-wide scope once a usable overlay index exists.","location":"Constraints and §§ 2.2, 2.3, 3.2, 3.4, 4.1","prevention":"For every branch-only changed symbol, record the exact planned-branch command and its complete owned hit list.","principle":"When the code index cannot see the planned branch, literal sweep commands and complete hit lists are the evidence substitute.","root_cause":"The plan records a few commands or prose summaries without their hit lists, leaving most branch-only symbol consumers unauditable.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-020-native-spawn-outcome-table","causal_section_ids":["2.4"],"check_key":"edge-case-coverage","description":"The plan cannot distinguish pre-write cleanup from post-write pending, and item 2.4.2 broadly demands cleanup for `ConnectionError` while 2.4.6 requires a lost reply to stay pending.","finding_id":"PR2-012-commit-outcome-observability","fix":"Add a typed commit transport error carrying `request_written` and request id; reconcile live, prepared, absent, and unreachable states with bounded deadlines; align both acceptance items.","introduced_in_round":1,"location":"§ 2.4 native-spawn outcome table and items 2.4.2/2.4.6","prevention":"For every indeterminate I/O boundary, carry effect metadata and enumerate every authoritative follow-up observation.","principle":"Different durable transitions require an observable typed boundary signal and a total reconciliation state machine.","root_cause":"The round-1 outcome table splits one `ConnectionError` by an unrepresented request-written fact and omits a listed-but-still-prepared observation.","section_id":"2.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-021-exec-success-proof","causal_section_ids":["2.7"],"check_key":"edge-case-coverage","description":"POSIX shells expose exit statuses such as 126/127 and may handle `ENOEXEC`; they cannot reliably emit exact `errno=2` or `errno=13` as specified.","finding_id":"PR2-013-exec-errno-sentinel","fix":"Use a native exec-gate helper that calls `execve` and writes a bounded typed status record on failure, or change the protocol and acceptance to shell-observable stable codes.","introduced_in_round":1,"location":"§ 2.7 exec-status pipe","prevention":"Trace each protocol field to the exact syscall or typed producer that supplies it, including malformed and partial records.","principle":"A protocol may promise only failure data its producer can observe and serialize reliably.","root_cause":"The shell-based wrapper added for round 1 cannot retrieve the kernel errno promised on fd 4.","section_id":"2.7","severity":"blocking"},{"category":"unhandled-edge","check_key":"concurrent-response-dispatch","description":"The current client serializes write+read under one lock; removing that lock would permit concurrent reads on one stream. IDs alone cannot safely resolve interleaved `ping` and `list` futures.","finding_id":"PR2-014-control-response-dispatcher","fix":"Specify one reader task, a write-only lock, a monotonic non-reused ID allocator, an ID-to-future map, and atomic failure/removal of all pending futures on EOF, reconnect, cancellation, or close.","location":"§ 2.9 control request-id correlation","prevention":"For every multiplexed stream, specify reader ownership, pending-map lifecycle, cancellation, unmatched replies, EOF, and reconnect.","principle":"Multiplexed request IDs require one response reader and terminal settlement of every pending request.","root_cause":"The plan adds IDs and interleaving without assigning connection ownership or teardown behavior.","section_id":"2.9","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-022-event-stream-recovery","causal_section_ids":["2.9"],"check_key":"edge-case-coverage","description":"After host restart resets sequence numbers, `seq <= last_event_seq` can discard all new-epoch events even though inventory reconciliation ran.","finding_id":"PR2-015-epoch-scoped-event-cursor","fix":"Store `(host_epoch, seq)`, reset to the new epoch's replay baseline after reconciliation, and deduplicate only within that epoch.","introduced_in_round":1,"location":"§§ 2.9 and 8.1 event replay","prevention":"Pair every sequence with its epoch and test restart from a high old sequence to a low new sequence.","principle":"A monotonic sequence cursor is meaningful only within the generation that owns it.","root_cause":"The bounded-stream recovery fix persists bare `last_event_seq` across host epochs.","section_id":"2.9","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"PR1-029-mcp-send-keys-identity","causal_section_ids":["4.2"],"check_key":"edge-case-coverage","description":"No targeted carrier can return a prior Delivered outcome or detect a different payload under the same completed key. The plan's stated existing coordinator rules cover unresolved writes only.","finding_id":"PR2-016-idempotency-outcome-storage","fix":"Add a bounded idempotency record with terminal/action key, payload fingerprint, outcome, timestamps/expiry, atomic claim/complete transitions, restart and terminal-exit semantics; or narrow acceptance to unresolved-only recovery.","introduced_in_round":1,"location":"§ 4.2 MCP send_keys idempotency contract","prevention":"For each idempotency promise, identify the record, atomic transitions, fingerprint, retention bound, restart behavior, and eviction semantics.","principle":"Completed replay and payload-conflict guarantees require a bounded authoritative record of completed actions.","root_cause":"The round-1 identity fix assumes unresolved-write storage retains delivered outcomes and payload fingerprints, but delivery clears that state.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-025-flip-evidence-provenance","causal_section_ids":["5.2"],"check_key":"edge-case-coverage","description":"Both fabricated IDs, `2026081001` and `2026081701`, are decimal integers greater than `10^9`; the specified rule therefore accepts them while item 5.2.1 requires rejection.","finding_id":"PR2-017-date-shaped-run-id","fix":"Define and test an explicit date-shaped encoding rejection rule, or remove this offline claim and rely on the authoritative GitHub lookup.","introduced_in_round":1,"location":"§ 5.2 temporal-plausibility predicate and item 5.2.1","prevention":"Evaluate every named negative fixture against the proposed predicate before declaring the class closed.","principle":"A negative acceptance fixture must fail the exact predicate claimed to reject it.","root_cause":"The round-1 provenance repair equates a lower numeric bound with rejection of date encoding.","section_id":"5.2","severity":"blocking"},{"category":"missing-requirement","check_key":"consumer-field-producer-parity","description":"The direct frame source cannot verify the host epoch because the finalized attach result has no specified producer for it; 6.3 owns only gclient files.","finding_id":"PR2-018-direct-attach-epoch-producer","fix":"Assign an owning server/protocol leaf to emit `host_epoch` and the minimum direct locator data, update goldens and server tests, then validate both gclient and web consumers.","location":"§§ 4.3, 4.5, 6.2, and 6.3","prevention":"Trace each new wire-field read backward through codec, emitter, storage/runtime source, golden, and server test.","principle":"Every field a consumer verifies must have an owned producer, wire contract, and producer-side proof.","root_cause":"The client requires `host_epoch` from `terminal_attach_result`, while no server leaf adds that field or the minimum direct locator to the emitter/goldens.","section_id":"6.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"attachment-reconnect-generation","description":"A server-minted attachment bound to a websocket cannot be both finalized on socket loss and re-registered under the same ID after reconnect or daemon restart. No durable resume token, grace record, or resume verb exists.","finding_id":"PR2-019-reconnect-attachment-identity","fix":"Prefer finalizing/tombstoning old IDs, issuing fresh `terminal_attach` after reconnect, and reacquiring control; otherwise add a durable authenticated resume-token state machine with expiry and conflict rules.","location":"§§ 2.2, 4.3, 6.2–6.3, and 9.1","prevention":"Model attachment states and identifiers across every transport/restart transition before assigning client and server tasks.","principle":"Attachment identity must have one generation and finalization contract across disconnect, reconnect, fallback, and daemon restart.","root_cause":"Different sections independently specify socket-loss finalization, old-ID re-registration, fresh-ID fallback, and same-ID restart without a resume authority.","section_id":"6.2","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"PR1-006-e1-acceptance-coverage","causal_section_ids":["9.1"],"check_key":"acceptance-observability","description":"`terminals` cannot prove browser attachment finalization without falsely exiting the terminal itself, and no attachment table exists.","finding_id":"PR2-020-attachment-finalization-oracle","fix":"Observe finalization through an explicit live-registry read/test API, or add a durable attachment lifecycle schema and manager; revise 9.1.5 to name the chosen authoritative state.","introduced_in_round":1,"location":"§ 9.1 acceptance item 9.1.5","prevention":"For every E2E oracle, trace the asserted state to a concrete schema field or explicit read API.","principle":"An acceptance assertion must read a state the implementation actually owns and persists.","root_cause":"The expanded E1 criterion asks storage to expose attachment finalization, while the schema persists terminals only and leases/attachments are process-local.","section_id":"9.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-025-flip-evidence-provenance","causal_section_ids":["5.2"],"check_key":"edge-case-coverage","description":"Neither matrix cell can push, and enabling push in both would race two commits for the same weekly slot. D1 needs one scheduled run containing both platform results.","finding_id":"PR2-021-weekly-evidence-aggregation","fix":"Add a post-matrix aggregation job that downloads both cell results, renders both platform records, runs only after all parity cells succeed, grants scoped `contents: write` with push authentication, and commits once.","introduced_in_round":1,"location":"§ 5.2 weekly producer","prevention":"For every CI write, verify permissions/authentication, matrix fan-in, single-writer ownership, and concurrent update behavior.","principle":"One evidence artifact needs one authenticated writer after all parallel producers finish.","root_cause":"The provenance repair asks each two-OS matrix cell to commit while the workflow has read-only contents permission and disabled checkout credentials.","section_id":"5.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"indeterminate-write-terminal-state","description":"The uncertain banner can persist forever: no resolution event, capture comparison, same-action retry, timeout, or lease transition produces the stated next Delivered outcome.","finding_id":"PR2-022-indeterminate-ui-recovery","fix":"Define server-side resolution for the same action key or an explicit same-sequence retry/capture path, including timeout and lease-loss outcomes, and add an E2E test that clears uncertainty without a second uncontrolled write.","location":"§§ 4.3 and 4.6 write outcomes","prevention":"For every indeterminate outcome, name the resolver, trigger, timeout, retry identity, and every terminal state.","principle":"Every user-visible indeterminate state needs a defined authoritative recovery transition.","root_cause":"The UI becomes read-only and waits for a later Delivered outcome, while the server emits only the immediate result and read-only prevents another write.","section_id":"4.6","severity":"blocking"}],"reviewer_session":"#10948","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

**Round 3** `kind: verification`

- reviewer_run: dabe1fff-109f-4a68-aec6-fa3ee7d95611
- reviewer_session: #10958 (55a8aef5-9710-4a8c-9b4a-def05f0c6c24)
- verdict: needs_review
- findings:
- PR3-001 / blocking / 2.4 acceptance needs producers owned by 2.7/2.9 — accepted (modified); `src/gobby/terminals/host_client.py` joins 2.4's Targets so `CommitTransportError` is minted where the transport boundary lives, the list-reconcile of a lost commit reply moves to 2.9 (new 2.9.8, where reconnect, `list`, and `commit_deadline_ms` all exist upstream), and 2.4.6/2.4.9 are trimmed to what 2.4 can prove; before 2.9 lands a `commit_indeterminate` row is settled by the 2.5 reaper
- PR3-002 / blocking / D1 covers only source item 5.3.1 — accepted; D1 defers 5.3.1 and 5.3.2 (post-flip preservation proof), and 5.2 states that 5.2.1–5.2.3 re-satisfy source items 5.3.3 and 5.3.4
- PR3-003 / blocking / bootstrap coverage ledger absent — accepted (modified); the ledger needs the approved plan hash, the M1 leaf titles, and the epic ref `gobby build` mints, so it is generated at build handoff and recorded in `## Task Mapping` as a handoff obligation; authoring it before approval would pin a hash every later round invalidates
- PR3-004 / blocking / `MANAGED_BIN_VERSION_PINS` is not the carrier — accepted; every 3.4 reference becomes `MANAGED_BIN_VERSION_PINS` with an explicit no-alias statement
- PR3-005 / blocking / 4.1 does not own the composition graph — accepted (modified); typed repairs applied, then `WebSocketServer.__init__` replaced by a `server.py::*` entry naming `configure_terminals` (the branch's post-construction composition seam), plus `tmux.py::TmuxMixin._init_tmux` (mints the second registry today) and `lifecycle_monitor.py::AgentLifecycleMonitor.__init__` (the other `WriteCoordinator(` constructor site); `ServiceContainer` gains `write_coordinator` and `lease_registry` so `http.py`'s existing `getattr(services, "write_coordinator")` stops resolving to `None`
- PR3-006 / blocking / fingerprint has no storage carrier — accepted; `persist_unresolved_write` in `storage/terminals.py` and `MemoryTerminalStore` gain the field; the `tests/storage/test_terminals.py` target is a bare path (branch-only file)
- PR3-007 / blocking / 4.3 omits the server constructor — accepted (modified); `server.py` targeted as `::*` (the `write_fault` seam rides `configure_terminals`, the same composition seam 4.1 establishes)
- PR3-008 / blocking / migration status vocabulary — accepted; the three predicates become `status IN ('pending', 'running')` (the baseline's own active set, see `idx_agent_runs_pending_termination`), the body names the vocabulary, and 1.2.5 seeds every defined status
- PR3-009 / blocking / late timeout callback can kill the retry's PTY — accepted; the 2.5 done-callback is generation-scoped (captured `attempt_generation`/`attempt_started_at` and the resource it created), promotion is a generation CAS, and the A-timeout/B-retry/A-late test is added
- PR3-010 / blocking / exec-status fd lifetime — accepted; the shell wrapper is replaced by a native `gterm gate` helper that holds fd 3/4 open, sets `FD_CLOEXEC` on the status fd after startup, `execvp`s, and writes the real `errno` on failure; shell-observable codes are dropped for errno names
- PR3-011 / blocking / no singleflight restart owner — accepted; 2.4's `handle_host_death` becomes the single manager-owned restart task with a generation under a lock, 2.9's event reader and request-path reconnects join it, `stop()` invalidates then drains it; simultaneous-failure and stop-during-backoff tests added
- PR3-012 / blocking / discard leaves the latch — accepted (modified); attachment finalization clears every `ws:{attachment_id}:` latch through the coordinator, discard stays client-local, and the per-attachment cap surfaces as the existing typed `unresolved_write_capacity` refusal; >32 cycles and reconnect cleanup tested
- PR3-013 / blocking / finalized attachments accumulate — accepted (modified); 4.3's `finalize` deletes the record and its websocket membership (unknown and finalized ids already refuse identically as `stale_attachment`), terminal exit drops the lease entry, and the client tombstone set is bounded to the previous socket generation
- PR3-014 / blocking / producer/verifier parity — accepted; the `record` job runs only on `github.event_name == 'schedule'`, writes `utc_timestamp` from the run's API `created_at`, checks out with `fetch-depth: 0` for `workflow_first_commit_at`, and the verifier requires equality with `created_at`
- resolution_notes: All 14 findings accepted (seven with a narrower or relocated mechanism recorded above). Typed repairs for PR3-005, PR3-006, and PR3-007 are applied after this checkpoint; every other fix is hand-applied prose, then base validation runs.

```json plan-review-round
{"evidence_id":"c898cb03-8834-4700-a288-4164a909f543","plan_hash":"8103a60c96f37424ce5198f3b901e89bc4e8e36590efe9080356b39765002aaa","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c34b24ff10aa98f21ac545033feb839a5c23a8a9d5f3e38536223273e425730d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":14,"total":17},"evidence_id":"c898cb03-8834-4700-a288-4164a909f543","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"e583f841ed17be988c9f42f4535bb134ec752a570c0a1d707fc5b4478547223b","status":"valid"},"source_digest":"cf97d81d8046485f9ac0465fe1b2f0f297a80c5ee0afb4c26b71edc274bef14a","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"PR2-012-commit-outcome-observability","causal_section_ids":["2.4"],"check_key":"leaf-local-producer-ordering","description":"Section 2.4 requires `HostClient.spawn_commit` to emit `CommitTransportError(request_id, request_written)`, reconcile through `list`, and use `commit_deadline_ms`, but those producers are owned only by 2.7/2.9, which already depend on 2.4.","finding_id":"PR3-001-commit-transport-ordering","fix":"Move the minimum HostClient transport/error, deadline, and list-reconciliation prerequisites into 2.4 or an earlier leaf, or move the affected outcome rows and acceptance after 2.7/2.9; recompute edges without a cycle.","introduced_in_round":2,"location":"§ 2.4 outcome table and acceptance 2.4.2/2.4.6","prevention":"For each acceptance behavior, trace every producer file to the leaf that creates it and verify the dependency closure is acyclic.","principle":"A leaf must own or depend on every carrier required to satisfy its close-time acceptance.","root_cause":"The round-2 transport-observability fix was written into 2.4 while HostClient and deadline/reconnect producers remain in downstream 2.7 and 2.9.","section_id":"2.4","severity":"blocking"},{"category":"missing-requirement","check_key":"deferred-source-acceptance-parity","description":"Source item 5.3.2 still requires post-flip preservation of explicit backend selection, external tmux handling, and pre-fork capacity refusal; D1 cites only 5.3.1, and 5.3.3/5.3.4 also lack explicit mappings to 5.2.","finding_id":"PR3-002-native-flip-deferral-coverage","fix":"Add a one-to-one 5.3.1–5.3.4 mapping. Put every still-deferred obligation, including 5.3.2's default-native preservation proof, in D1 validation and `original_acceptance_items`; map checker/evidence obligations to exact 5.2 items.","location":"D1 Native default flip deferral","prevention":"Map every source acceptance ID one-to-one before replacing a closed epic leaf with remediation and deferral work.","principle":"A remediation plan must assign every governing source acceptance obligation to current work or an explicit deferral.","root_cause":"D1 retained only source item 5.3.1 while treating checker/evidence work as sufficient coverage of the whole native-flip leaf.","section_id":"D1","severity":"blocking"},{"category":"missing-requirement","check_key":"bootstrap-ledger-required","description":"`.gobby/plans/herdr-terminal-client-qa-fixes.coverage-ledger.yaml` is absent despite 33 deliverables and 170 acceptance items.","finding_id":"PR3-003-bootstrap-ledger","fix":"Generate and review the companion ledger with plan ID `herdr-terminal-client-qa-fixes`, the final plan hash, all deliverable/acceptance IDs, and expected implementation leaves before expansion.","location":"Plan companion artifacts","prevention":"Before approval, verify `<plan-stem>.coverage-ledger.yaml` exists, matches the final plan hash, and enumerates every deliverable and acceptance item.","principle":"Every new epic plan ships an adversary-reviewed bootstrap coverage ledger before expansion.","root_cause":"The plan contains a Task Mapping placeholder but no required companion ledger.","section_id":"__preamble__","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR1-014-version-pin-shape","causal_section_ids":["3.4"],"check_key":"symbol-carrier-identity","description":"Section 3.4 repeatedly names `MANAGED_BIN_PINS`, while the shared carrier and its consumers use `MANAGED_BIN_VERSION_PINS`; following the text would create a forbidden alias or parallel mapping.","finding_id":"PR3-004-version-pin-symbol","fix":"Replace every 3.4 prose and acceptance reference with `MANAGED_BIN_VERSION_PINS` and state explicitly that no alias or second mapping is introduced.","introduced_in_round":1,"location":"§ 3.4 publication-state body","prevention":"Resolve every named symbol to its indexed qualified name and run literal consumer search before finalizing the section.","principle":"Plan symbol names must resolve to the existing canonical carrier, especially when aliases are forbidden.","root_cause":"The round-1 mapping-shape repair shortened the real symbol name in prose.","section_id":"3.4","severity":"blocking"},{"category":"traceability","check_key":"single-authority-composition-closure","description":"4.1 can leave separate or missing `TerminalLeaseRegistry`/`WriteCoordinator` instances because orchestration, server initialization, `WebSocketServer.__init__`, `ServiceContainer`, shared fakes, and composition tests are untargeted; 4.2 MCP reachability fails through the same gap.","finding_id":"PR3-005-lease-composition-root","fix":"Own the full composition graph in 4.1: create one registry, inject it into the coordinator, pass the same objects through WebSocketServer and ServiceContainer, and prove identity through the production container.","location":"§ 4.1 Targets and production wiring","prevention":"Trace constructor calls and container fields from each authority object through every production and test composition root.","principle":"A single-authority refactor must migrate every constructor, composition root, container, and shared fake in its owning leaf.","repairs":[{"entries":["`src/gobby/runner_init/orchestration.py::init_orchestration`","`src/gobby/runner_init/servers.py::init_servers`","`src/gobby/servers/websocket/server.py::WebSocketServer.__init__`","`src/gobby/app_context.py::ServiceContainer`","`tests/terminals/fakes.py`","`tests/terminals/test_composition_roots.py`"],"kind":"add_targets","section_id":"4.1"},{"items":[{"artifact":"test: `tests/terminals/test_composition_roots.py::test_terminal_write_authority_is_singleton`","prose":"The production composition root creates one TerminalLeaseRegistry and passes that same registry and WriteCoordinator instance through WebSocketServer, ServiceContainer, and MCP registry setup."}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"The section changes authority classes and handlers without owning the production object graph that connects them.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR2-016-idempotency-outcome-storage","causal_section_ids":["4.2"],"check_key":"durable-entry-shape-carriers","description":"4.2 adds a payload fingerprint to unresolved latches, but `src/gobby/storage/terminals.py`, `tests/terminals/fakes.py`, and durable storage regression coverage are absent.","finding_id":"PR3-006-unresolved-fingerprint-carriers","fix":"Extend both store implementations with the fingerprint while preserving entry/byte caps and deterministic legacy-entry handling; add a PostgreSQL round-trip regression.","introduced_in_round":2,"location":"§ 4.2 unresolved-write fingerprint","prevention":"For each durable entry field, trace protocol, PostgreSQL store, in-memory fake, caps, legacy decoding, and round-trip tests.","principle":"A durable value-shape change must update every store implementation, fake, and persistence test atomically.","repairs":[{"entries":["`src/gobby/storage/terminals.py`","`tests/terminals/fakes.py`","`tests/storage/test_terminals.py::*` — scope-reason: add durable unresolved-write fingerprint round-trip, capacity, and legacy-entry coverage"],"kind":"add_targets","section_id":"4.2"},{"items":[{"artifact":"test: `tests/storage/test_terminals.py::test_unresolved_write_fingerprint_round_trip`","prose":"Payload fingerprints round-trip through PostgreSQL and MemoryTerminalStore, preserve entry and byte caps, and handle legacy unresolved entries without a fingerprint deterministically."}],"kind":"add_acceptance","section_id":"4.2"}],"root_cause":"The round-2 fingerprint fix updated coordinator semantics without owning the storage method that serializes each latch entry.","section_id":"4.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR1-016-callable-config-mechanism","causal_section_ids":["4.3"],"check_key":"constructor-injection-target-closure","description":"4.3 replaces the filesystem hook with a constructor-supplied `write_fault` callable but omits `WebSocketServer.__init__`, so the stated injection cannot be implemented from the section's Targets.","finding_id":"PR3-007-write-fault-constructor","fix":"Target `WebSocketServer.__init__`, store the optional dependency there, and keep `runner_init/servers.py` as the sole environment-reading composition root.","introduced_in_round":1,"location":"§ 4.3 Targets","prevention":"For every injected dependency, target the constructor definition, all construction sites, and one production-path test.","principle":"Constructor injection requires ownership of the defining constructor and every caller it changes.","repairs":[{"entries":["`src/gobby/servers/websocket/server.py::WebSocketServer.__init__`"],"kind":"add_targets","section_id":"4.3"}],"root_cause":"The round-1 config-knob repair targeted the composition caller and mixin while omitting the server constructor.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-019-migration-preserves-live-identity","causal_section_ids":["1.2"],"check_key":"migration-status-vocabulary","description":"The three shown predicates use `completed/failed/cancelled/killed`, while agent runs use `pending/running/success/error/timeout/cancelled`; successful, errored, and timed-out legacy runs would be backfilled as active orphaned terminals.","finding_id":"PR3-008-migration-status-vocabulary","fix":"Replace every shown predicate with the positive active set `status IN ('pending','running')`, name that vocabulary in the body, and test each defined status plus rollback and duplicate identities.","introduced_in_round":1,"location":"§ 1.2 migration 401 SQL excerpt","prevention":"Enumerate every defined status in migration fixtures and prefer a positive active-state predicate over an incomplete terminal-state exclusion.","principle":"Destructive migrations classify rows using the authoritative runtime vocabulary and fail closed on unknown states.","root_cause":"The live-identity backfill copied status names from a different lifecycle model.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"generation-owned-async-cleanup","description":"A late timed-out attempt can terminate or promote the newer attempt's PTY because retries reuse terminal ID/spawn key and cleanup is not scoped to the captured host terminal and generation.","finding_id":"PR3-009-spawn-attempt-generation","fix":"Carry `attempt_generation`/start time through prepare, commit, reconciliation, and callbacks; add generation-scoped promotion/claim CAS, terminate only the stale attempt's captured `host_terminal_id`, and add the A/B collision test.","location":"§§ 2.4–2.5 timeout callback and retry","prevention":"For every shielded task, carry generation through prepare, promote, reconcile, and cleanup; test A-timeout/B-retry/A-late completion deterministically.","principle":"Late async completion and cleanup must be owned by the attempt generation that created the external resource.","root_cause":"The callback terminates by a spawn key reused across attempts before its generation CAS can reject stale state.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-021-exec-success-proof","causal_section_ids":["2.7"],"check_key":"exec-status-descriptor-lifecycle","description":"If fd 4 has `FD_CLOEXEC` before `/bin/sh` starts, the shell exec closes it and the host sees false-success EOF; if it remains clear, the final target inherits it and the host times out.","finding_id":"PR3-010-exec-status-fd","fix":"Use a native gate helper or direct fork/exec path that starts with the status fd open, sets CLOEXEC after helper startup, calls `execve`, and writes bounded typed failure details; test that EOF cannot arrive before target replacement.","introduced_in_round":1,"location":"§ 2.7 exec-status pipe","prevention":"Trace descriptor flags across every fork/dup/exec boundary and prove EOF timing with a target that never execs.","principle":"An exec-success sentinel must survive helper startup and close only when the target image successfully replaces that helper.","root_cause":"The round-1 CLOEXEC design assigns both lifetimes to one descriptor before an intermediate shell exec.","section_id":"2.7","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-022-event-stream-recovery","causal_section_ids":["2.9"],"check_key":"restart-singleflight-generation","description":"Health, event-reader, and request recovery can concurrently rotate tokens, spawn hosts, replace clients, or publish a stale client after shutdown because the plan specifies no singleflight restart owner.","finding_id":"PR3-011-host-restart-singleflight","fix":"Define one manager-owned restart task and generation under a lock; every death/reconnect source joins it, and stop invalidates then cancels/drains it before teardown. Test simultaneous failures and stop during backoff.","introduced_in_round":1,"location":"§§ 2.4 and 2.9 host recovery","prevention":"Enumerate every restart trigger, assign one task/lock/generation owner, and test simultaneous failure plus stop-during-backoff.","principle":"Concurrent failure observers share one generation-owned recovery operation whose publication is invalidated by stop.","root_cause":"The event-stream recovery fix added another reconnect owner beside health and request paths without defining arbitration.","section_id":"2.9","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR2-022-indeterminate-ui-recovery","causal_section_ids":["4.3","4.6"],"check_key":"indeterminate-discard-server-cleanup","description":"Discard clears uncertainty and permits new writes while the durable `ws:{attachment_id}:{seq}` latch remains; attachment finalization does not specify clearing it, so repeated discard cycles can exhaust the 32-entry cap.","finding_id":"PR3-012-ws-discard-latch","fix":"Add an acknowledged server-side abandon operation that clears exactly that latch without writing, or keep the client blocked until detach and make finalization atomically clear attachment-owned latches; test more than 32 cycles and reconnect cleanup.","introduced_in_round":2,"location":"§ 4.6 uncertain banner discard","prevention":"For each client terminal state, name the server transition, acknowledgement, durable cleanup, capacity effect, reconnect behavior, and finalizer.","principle":"A client-visible abandon transition must settle or reclaim the authoritative server state it abandons.","root_cause":"The round-2 UI recovery added local discard but no server-side latch transition.","section_id":"4.6","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR2-019-reconnect-attachment-identity","causal_section_ids":["6.2","6.3","9.1"],"check_key":"finalized-attachment-reclamation","description":"Fresh IDs on reconnect and direct-to-proxy fallback accumulate finalized `_attachments` records and websocket membership, making memory and close-time cleanup grow with historical churn.","finding_id":"PR3-013-attachment-reclamation","fix":"Remove finalized records and reverse-index membership immediately, or retain a separately bounded TTL/LRU tombstone set; reclaim empty lease entries on terminal exit and add high-churn bound assertions.","introduced_in_round":2,"location":"§§ 4.3 and 6.2–6.4 reconnect/fallback lifecycle","prevention":"Walk attach, detach, socket close, fallback, reconnect, daemon restart, and terminal exit while asserting registry and reverse-index bounds.","principle":"Fresh-generation identifiers require bounded reclamation of finalized records and reverse indexes.","root_cause":"Fresh attachment creation was specified without a deletion or bounded tombstone policy for prior generations.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"evidence-producer-verifier-parity","description":"The workflow records `utc_timestamp` after long builds but the verifier allows only 10 minutes from run creation; `workflow_dispatch` would commit evidence later rejected as non-schedule; and `workflow_first_commit_at` has no reliable source under the default shallow checkout.","finding_id":"PR3-014-evidence-producer-authority","fix":"Gate the record job to `github.event_name == 'schedule'`; record authoritative run `created_at` from the Actions API; derive workflow first-commit time from full history or the commits API; test a long successful run, manual dispatch, and shallow checkout.","location":"§ 5.2 weekly producer and remote verifier","prevention":"For each evidence field, trace trigger, source API, clock, checkout depth, writer condition, and negative event branch end to end.","principle":"Every provenance field and accepted trigger must have an authoritative producer whose timing and event semantics match the verifier.","root_cause":"The verifier contract was strengthened without aligning the existing late timestamp, manual trigger, and shallow-history producer.","section_id":"5.2","severity":"blocking"}],"reviewer_session":"#10958","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

**Round 4** `kind: verification`

- reviewer_run: dbd1f84e-20bd-4cc8-942b-6646e40af4cd
- reviewer_session: #10961 (e7294bf1-423e-4277-95f2-70520a395aa9)
- verdict: needs_review
- findings:
- PR4-001 / blocking / migration 401 CTE reads `r.project_id` / `r.session_id`, which `agent_runs` lacks — accepted; `session_id` from `child_session_id`, `project_id` through a `sessions` join on `COALESCE(child_session_id, parent_session_id)`, `machine_id` from `agent_runs`; unresolved project fails the migration; fixtures added
- PR4-002 / blocking / 2.4.6 asserts the 2.5 reaper (downstream of 2.4) — accepted; 2.4.6 limited to what 2.4 owns, reaper settlement moved to new 2.5.5
- PR4-003 / blocking / `request_written` split at "fully written" is unprovable after a drain failure — accepted, modified: the field keeps its name and is defined conservatively (`False` only before `writer.write()` is invoked); every later timeout, cancellation, drain error, EOF, or lost reply is indeterminate; the boundary matrix is in 2.4.6
- PR4-004 / blocking / lease mutation and gated dispatch use separate locks — accepted, modified: one per-terminal `asyncio.Lock` owned by `TerminalLeaseRegistry`, acquired by the coordinator from revalidation through dispatch and by every registry mutation (`take_control`, `release_control`, `finalize`, `finalize_websocket` become coroutines); one race test (4.1.7) on the fake runtime, since the lock is backend-independent
- PR4-005 / blocking / asyncio 64 KiB default reader limit below the 2 MiB control cap — accepted; `limit=MAX_CONTROL_LINE + 1` on every control/event `open_unix_connection`, new `tests/terminals/test_host_client.py` target and 2.9.10
- PR4-006 / blocking / blocking stdin read inside the select loop — accepted, modified: the input branch receives from the existing `gobby_terminal::raw_input::spawn_input_reader()` (dedicated thread, bounded 256-entry channel); 6.3.1 extended with the idle-input progress assertion; no real-PTY worker-termination test (the detached reader thread ends with the process)
- PR4-007 / blocking / 200-terminal list fixture exceeds host ceilings — accepted; 8.2 lists the maximum admissible population (124 native + 64 tmux observers) with maximum-size bounded fields
- PR4-008 / blocking / 5.2 mapping drops the source 5.3.3 matrix and 5.3.4 field list — accepted, modified: new 5.2.6 binds the retained `test_flip_gate_rejects_every_nonconforming_artifact` matrix and the 5.3.4 field set to 5.2; mapping paragraphs in 5.2 and D1 updated; no duplicate fixtures
- PR4-009 / blocking / no theme-contract test for the permitted divergence — accepted; repair applied (6.1.5) plus a body sentence naming the assertions
- PR4-010 / blocking / `Cargo.lock` missing from 1.1 Targets — accepted; repair applied
- PR4-011 / blocking / four branch files with `#[allow(` outside the 3.2 inventory — accepted; repair applied, inventory lines added
- PR4-012 / blocking / backpressure caps called live configuration with no daemon carrier — accepted, modified: the typed repairs (daemon config model, forwarding, contract regeneration, forwarding test) were declined as mechanism without a consumer; 8.1 now states the caps are `HostConfig` fields with documented defaults and no daemon-side carrier
- PR4-013 / blocking / `LiveDaemon` has no single WS reader or pending-request fence — accepted; one reader task per socket generation, `request_id` → oneshot map, bounded subscriber fanout, pending requests failed on EOF/reconnect/close, new 6.2.7
- PR4-014 / blocking / paged listing unbounded under sustained creation — accepted; cursor embeds the snapshot upper ordering key, later pages enforce `last_key < row_key <= upper_bound`, bounded client event buffer restarts the listing on overflow; 4.4.6 extended
- resolution_notes: 14/14 accepted (6 with a narrower fix than proposed). Repairs applied through `apply_plan_review_repairs` for PR4-009, PR4-010, PR4-011; PR4-012's repairs declined; every other fix hand-applied as prose and acceptance edits, then base-validated.

```json plan-review-round
{"evidence_id":"e2c4ab5d-de34-48d0-a09f-576442b22bd0","plan_hash":"001f8776ed4e35e681d394c904ccc3686136fa5005eb5b7e46809db93619bea4","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"ac678a1edb34b3319e8c31d506b227c8b78f85369847e42761af1d62f5b51adf","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":14,"total":15},"evidence_id":"e2c4ab5d-de34-48d0-a09f-576442b22bd0","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"73ecf7247c9423b53d61d7f0cce7ac56b3f499cc13f1235578897d40d24f852a","status":"valid"},"source_digest":"33a27b3793a7261826175fea3295603c83cbf7abf03e87631f76d105bd4e05d8","version":1},"findings":[{"category":"unhandled-edge","check_key":"migration-live-identity-preservation","description":"The prescribed CTE selects r.project_id and r.session_id, but agent_runs carries machine_id plus child/parent/claimed session identifiers. Migration 401 therefore errors before it can preserve any live legacy identity.","finding_id":"PR4-001-migration-source-lineage","fix":"Use agent_runs.child_session_id for terminals.session_id, join sessions to obtain project_id, use agent_runs.machine_id, and treat a null child session or unresolved session/project as an unmapped identity that rolls back before DROP COLUMN. Add successful and failing lineage fixtures.","location":"§ 1.2 migration 401 backfill CTE","prevention":"Resolve every migration source column against baseline.sql and test null, missing-reference, duplicate, and rollback variants before dropping legacy state.","principle":"Migration backfills must derive every target field from columns and joins that exist in the authoritative source schema.","root_cause":"The SQL excerpt treats project_id and session_id as agent_runs columns instead of deriving them through child_session_id and sessions.","section_id":"1.2","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"PR3-001-commit-transport-ordering","causal_section_ids":["2.4","2.9"],"check_key":"leaf-local-acceptance-closure","description":"Item 2.4.6 requires the 2.5 stale-pending reaper to terminate and fail a commit-indeterminate row, while 2.5 depends on 2.4. Leaf 2.4 cannot close against its own manifest criteria.","finding_id":"PR4-002-reaper-leaf-closure","fix":"Limit 2.4.6 to the transport classification and durable pending/commit_indeterminate state that 2.4 owns. Move the reaper settlement assertion into a new or expanded 2.5 acceptance item; keep list-based settlement in 2.9.8.","introduced_in_round":3,"location":"§ 2.4 acceptance 2.4.6 versus § 2.5 dependency","prevention":"For every acceptance sentence, trace each producer to its owning leaf and verify that leaf is in the dependency closure.","principle":"A leaf's server-derived validation criteria may consume only behavior it owns or depends on.","root_cause":"The round-3 relocation left downstream reaper settlement inside upstream item 2.4.6.","section_id":"2.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR3-001-commit-transport-ordering","causal_section_ids":["2.4","2.9"],"check_key":"spawn-effect-boundary-outcomes","description":"A drain timeout or socket error can occur after part or all of the newline-terminated commit request reached the host while request_written remains false. Failing the row on that branch can orphan a committed PTY.","finding_id":"PR4-003-commit-partial-write","fix":"Define the flag conservatively as request_exposed_to_transport: false only before writer.write receives any encoded bytes. Once write() is invoked, classify timeout, cancellation, drain error, EOF, and lost reply as indeterminate and reconcile by request ID/list; test each boundary.","introduced_in_round":3,"location":"§ 2.4 CommitTransportError request_written boundary","prevention":"Test pre-write failure, partial write, cancellation during drain, full write with lost reply, and late response for every effectful request.","principle":"A no-effect transition requires proof that no request bytes were exposed to the transport; partial exposure is indeterminate.","root_cause":"The plan places the safe/indeterminate split at full write completion, which asyncio StreamWriter cannot prove after write/drain failure.","section_id":"2.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"single-authority-composition-closure","description":"A takeover, release, or finalization can change lease generation after coordinator revalidation but before tmux/native emits bytes. A displaced attachment can therefore write after control moved, and finalization can clear latches before an in-flight indeterminate write persists a new one.","finding_id":"PR4-004-lease-write-linearization","fix":"Route grant, takeover, release, finalize, attachment-latch cleanup, and operator dispatch through the same coordinator-owned per-terminal lock, or carry a registry token valid through the precise dispatch point. Add deterministic text/key/paste races for tmux and native.","location":"§§ 4.1 and 4.3 lease revalidation, takeover, release, and finalization","prevention":"Pause every gated write after revalidation, race takeover/release/finalize, and prove one deterministic before-or-refused outcome on each backend.","principle":"Lease mutation and lease-gated byte dispatch must share one per-terminal linearization boundary.","root_cause":"TerminalLeaseRegistry mutations and WriteCoordinator writes retain separate locks, and revalidation occurs before awaited runtime I/O.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"bounded-stream-recovery","description":"Valid list responses can exceed asyncio's 64 KiB default while remaining below the 2 MiB control cap. Without opening control and event Unix streams with a larger limit, HostClient raises LimitOverrunError and breaks event-gap and commit-indeterminate recovery.","finding_id":"PR4-005-control-reader-cap","fix":"Pass limit=MAX_CONTROL_LINE+1 to every initial and replacement control/event open_unix_connection call, or use one shared bounded incremental line reader. Add a real HostClient list response above 64 KiB and below 2 MiB, including maximum titles.","location":"§ 2.9 HostClient control and event stream readers","prevention":"Test every bounded line protocol just below and above both library defaults and declared protocol limits on initial and reconnected streams.","principle":"A protocol's advertised frame bound must also be the effective bound of every transport reader.","root_cause":"The plan raises the Rust-side control-line cap while leaving asyncio's default StreamReader limit implicit.","section_id":"2.9","severity":"blocking"},{"category":"unhandled-edge","check_key":"async-input-boundary","description":"An idle blocking terminal read can occupy the runtime path and prevent daemon events, frames, reconnect timers, signals, and the 16 ms render tick from progressing. Scripted input tests do not exercise that boundary.","finding_id":"PR4-006-gclient-input-bridge","fix":"Enable and use crossterm event-stream, or run blocking reads in one dedicated worker feeding a bounded Tokio channel with a defined shutdown handshake. Add a real-PTY test that leaves stdin idle while other branches advance and proves worker termination on quit and signal.","location":"§ 6.3 run_ready tokio::select input branch","prevention":"For each synchronous source in an async loop, specify executor/thread ownership, queue bound, cancellation, shutdown join, and idle-progress tests.","principle":"Synchronous terminal reads need an explicit bounded, cancellable bridge before joining an async event loop.","root_cause":"The plan names a tokio select branch but owns neither crossterm's async event stream nor a blocking-worker lifecycle.","section_id":"6.3","severity":"blocking"},{"category":"weak-testability","check_key":"production-adapter-acceptance","description":"Production maxima admit 124 native entitlements after four reserved lifecycle slots plus 64 tmux observers, for 188 total. The required real-host test cannot create 200 live terminals without bypassing the behavior it claims to verify.","finding_id":"PR4-007-host-list-capacity","fix":"Exercise the maximum admissible mixed population of 124 native plus 64 tmux observers and use maximum-sized bounded fields to test the list envelope. If 200 is a product requirement, raise and revalidate host capacity first.","location":"§ 8.2 acceptance 8.2.3","prevention":"Compute fixture cardinality from production bounds before naming scale acceptance, including reserved capacity.","principle":"A real-component acceptance test must construct its fixture through supported production admission limits.","root_cause":"The requested 200-terminal population exceeds the host's validated native and tmux observer ceilings.","section_id":"8.2","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"PR3-002-native-flip-deferral-coverage","causal_section_ids":["5.2","D1"],"check_key":"deferred-source-acceptance-parity","description":"Items 5.2.1–5.2.3 omit explicit proof for ISO 52→01 and 53→01, skipped and same-year non-consecutive weeks, complementary/missing platforms, missing per-slot 4.3/3.6/package lines, inclusive 24-hour boundaries, and the complete 5.3.4 field list.","finding_id":"PR4-008-native-flip-boundary-parity","fix":"Add stable 5.2 acceptance that enumerates every source 5.3.3 boundary fixture and every 5.3.4 evidence field with exact tests/artifacts. Keep D1 responsible for 5.3.1 and 5.3.2, then state the complete one-to-one mapping.","introduced_in_round":3,"location":"§ 5.2 acceptance mapping for source items 5.3.3 and 5.3.4","prevention":"Diff every source acceptance clause against replacement acceptance IDs and preserve each named positive, negative, boundary, and artifact field.","principle":"Replacing a source acceptance item requires one-to-one retention of its named boundary matrix and evidence fields.","root_cause":"The round-3 mapping declares generalized 5.2 coverage without carrying the source leaf's exact acceptance cases.","section_id":"5.2","severity":"blocking"},{"category":"weak-testability","check_key":"acceptance-observability","description":"An incorrect or hue-only gclient palette can satisfy all current acceptance because 7.1.3 normalizes theme values away. No item proves .impeccable.md's permitted TUI values, deutan-safe/grayscale state distinctions, or keyboard-visible focus.","finding_id":"PR4-009-gclient-theme-contract","fix":"Add a gclient theme-contract test using the existing workspace test target and bind 7.2 evidence to it.","location":"§§ 6.1, 7.1, and 7.2 theme mapping","prevention":"For every allowed golden divergence, name the governing contract and add an independent test for the exempt dimension.","principle":"A governed design divergence needs executable proof against the repository's accessibility and token contract.","repairs":[{"items":[{"artifact":"test: `crates/gclient/tests/workspace.rs::theme_contract_is_accessible`","prose":"The gclient theme maps every herdr token to an .impeccable.md-permitted TUI value, preserves deutan-safe and grayscale state distinctions, and renders keyboard focus without hue-only cues"}],"kind":"add_acceptance","section_id":"6.1"}],"root_cause":"Theme values are the permitted herdr divergence, while the parity token map deliberately ignores them and no separate contract test exists.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"acceptance-artifact-target-ownership","description":"Section 1.1 explicitly takes the branch's Cargo.lock, so the leaf changes that repository artifact, yet Cargo.lock is absent from Targets.","finding_id":"PR4-010-cargo-lock-target","fix":"Add Cargo.lock to 1.1 Targets.","location":"§ 1.1 Targets and branch lockfile selection","prevention":"Reconcile every conflict-resolution choice and acceptance artifact against Targets before validation.","principle":"Every file a deliverable explicitly chooses or changes belongs in that deliverable's Targets.","repairs":[{"entries":["`Cargo.lock`"],"kind":"add_targets","section_id":"1.1"}],"root_cause":"The merge instructions select the branch Cargo.lock but inventory only the downstream cleanliness check.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"exhaustive-guard-existing-matches","description":"pane/runtime.rs, platform/macos.rs, platform/windows.rs, and protocol/wire_types.rs contain current allow sites but are absent from 3.2 Targets and its initial-red inventory. The leaf cannot make its own guard green without changing unowned files.","finding_id":"PR4-011-lint-allow-targets","fix":"Add the four branch-only files to 3.2 Targets and remove each allow or attach the required same-line reason.","location":"§ 3.2 branch-wide allow inventory","prevention":"Run the exact guard predicate over every governed file and reconcile every initial-red hit against Targets.","principle":"An exhaustive source guard must assign every existing match to the leaf that changes or justifies it.","repairs":[{"entries":["`crates/gterminal/src/pane/runtime.rs`","`crates/gterminal/src/platform/macos.rs`","`crates/gterminal/src/platform/windows.rs`","`crates/gterminal/src/protocol/wire_types.rs`"],"kind":"add_targets","section_id":"3.2"}],"root_cause":"The written inventory missed four branch files that contain item-level allows rejected by the new guard.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"config-derived-carrier-sweep","description":"Queue byte caps, lag timeout, and control deadline become live HostConfig values, but 8.1 omits TerminalHostConfig, TerminalHostManager forwarding, runtime_config_contract.json, and tests proving a non-default reaches gterm.","finding_id":"PR4-012-backpressure-config-carriers","fix":"Own the missing daemon-side carriers and a forwarding acceptance in 8.1.","location":"§ 8.1 live HostConfig values","prevention":"For every new config field, sweep model, generated contract, CLI/env carrier, spawn forwarding, defaults/bounds, and non-default propagation.","principle":"A daemon-launched host configuration field must traverse model, registry contract, process construction, host parser, and propagation tests.","repairs":[{"entries":["`src/gobby/config/terminal_host.py`","`src/gobby/terminals/host_manager.py`","`crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated wholesale for new terminal-host backpressure fields","`tests/config/test_terminal_host.py`","`tests/terminals/test_host_manager.py`"],"kind":"add_targets","section_id":"8.1"},{"items":[{"artifact":"test: `tests/terminals/test_host_manager.py::test_backpressure_config_is_forwarded`","prose":"Daemon configuration validates every backpressure cap and deadline and forwards non-default values to the spawned gterm HostConfig"}],"kind":"add_acceptance","section_id":"8.1"}],"root_cause":"The section owns only Rust HostConfig while calling caps and deadlines live configuration.","section_id":"8.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"concurrent-response-dispatch","description":"Lifecycle events and terminal_create/terminal_kill replies share one WebSocket. Without one LiveDaemon reader task, an EventStream consumer can consume command replies or concurrent methods can read the same stream; in-flight requests can also survive into a replacement socket generation.","finding_id":"PR4-013-gclient-ws-dispatcher","fix":"Give LiveDaemon one reader task that routes correlated replies to an ID→future map and lifecycle messages to bounded subscribers. Atomically fail and clear pending futures on EOF, cancellation, reconnect, and close, drop unmatched/old-generation replies, and test interleaved spawn/kill/events plus reconnect.","location":"§ 6.2 LiveDaemon WebSocket command and event ownership","prevention":"For every multiplexed socket, specify reader ownership, request IDs, pending-map lifecycle, event fanout bounds, unmatched replies, EOF, cancellation, reconnect, and close.","principle":"A multiplexed request/event stream needs one reader and terminal settlement of every pending request across connection generations.","root_cause":"The trait exposes subscribe, send, spawn, and terminate without assigning a single reader, reply routing map, or reconnect fence.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-024-roster-causal-snapshot","causal_section_ids":["4.4","4.6","6.2"],"check_key":"paged-snapshot-event-causality","description":"Continuous terminal creation after each cursor can keep extending the database result set, prevent a final page, and grow buffered lifecycle events indefinitely. The first-page sequence watermark does not bound which rows later pages may include.","finding_id":"PR4-014-paged-list-stability","fix":"Capture an upper ordering key with snapshot.seq under the broadcast lock, embed epoch, watermark, upper bound, and last key in every cursor, enforce last_key < row_key <= upper_bound on later pages, and cap/restart buffered events. Add sustained-create paging tests for REST, web, and gclient.","introduced_in_round":1,"location":"§§ 4.4, 4.6, and 6.2 paged snapshot protocol","prevention":"At every list-plus-events boundary, specify snapshot membership bound, cursor contents, buffer cap, termination under churn, and replay ordering.","principle":"Paged snapshot reconciliation needs finite snapshot membership as well as a causal event watermark.","root_cause":"The repair adds daemon_epoch and snapshot.seq but says the cursor embeds only epoch, leaving later pages open to newly inserted rows.","section_id":"4.4","severity":"blocking"}],"reviewer_session":"#10961","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

**Round 5** `kind: verification`

- reviewer_run: c8e3552c-74b3-4af1-a5ef-7d7790a499f1
- reviewer_session: #10965
- verdict: needs_review
- findings:
- PR5-001-migration-version-collision / blocking / `401_terminals.sql` collides with `0.5.0`'s registered 401 and 402 — accepted (verified: `0.5.0` registers `401_model_metadata_reasoning.sql` and `402_task_close_reviews.sql`; the read-only merge probe also drifted from five to twelve conflicting paths, tree `62a906fbfc`). Migration renamed to 403 everywhere; 1.1 keeps `0.5.0`'s 399–402 entries, `latest_version: 402`, and gains the seven new conflict resolutions plus the `build_prompt_preamble` → `prompt_for("agent")` port that `0.5.0` #20697 forces on `spawn_executor_providers.py`.
- PR5-002-bootstrap-guard-closeability / blocking / 1.1 and 1.2 cannot satisfy guard set G while 2.1/2.2 own red tests — accepted, modified: running G group 1 at `518cec5c41` reproduces exactly ten failures (six owned by 2.1, three by 2.2, and `test_checked_in_contract_matches_registry`, which 1.1 already fixes through 1.1.8). A named known-red carve-out is added to Constraints and retired when the owning leaf closes; 2.1/2.2 keep their root-cause ownership rather than moving into the merge leaf.
- PR5-003-active-backend-rollback / blocking / persisted `native` override and nonexistent `gobby config set` — accepted, modified: runtime config is DB overrides over Pydantic defaults and the bundled `config.yaml` is export-only, so the `TerminalConfig` default change is the effective rollback for every hub; the branch never wrote an override, so a persisted `native` is user-authored and is preserved (no config migration). The guide names `gobby-config:patch_config_values`; 5.1.5 proves the no-override and explicit-override projections.
- PR5-004-tmux-retry-attempt-ownership / blocking / late predecessor tmux creation leaks a session — accepted, modified: a tmux retry is refused while the prior attempt's prepare task is unsettled, and a retry whose `create_session` fails with tmux's duplicate-session error kills the `spawn_key` session before failing; no generation-specific temporary identities.
- PR5-005-same-epoch-gap-cursor / blocking / cursor not advanced after a same-epoch gap — accepted as proposed.
- PR5-006-detach-ack-deadline / blocking / `Detaching` waits forever — accepted, modified: reuse 6.4's 2 s detach deadline; on expiry invalidate the WebSocket generation through 6.2's reconnect path, then re-list and attach proxy afresh.
- PR5-007-first-input-settlement / blocking / triggering keystroke unspecified — accepted: one queue-once policy shared by 4.6 and 6.3.
- PR5-008-cursor-validation / blocking / untrusted cursor fields undefined — accepted: versioned opaque cursor bound to project and normalized filters; `invalid_cursor` for malformed or tampered, `cursor_stale` only for a valid old epoch.
- PR5-009-event-overflow-acceptance / blocking / overflow and lag paths untested — accepted; typed repairs applied (4.6, 6.2).
- PR5-010-livedaemon-send-coverage / blocking / `send` missing from the adapter matrix — accepted; typed repair applied (6.2).
- PR5-011-lease-mutation-variant-coverage / blocking / attach and `finalize_websocket` contention untested — accepted; typed repair applied (4.1).
- resolution_notes: 11/11 accepted (4 narrowed to the least mechanism that closes the defect). Typed repairs applied for PR5-009/010/011 through `apply_plan_review_repairs`; every other fix hand-applied as prose after this checkpoint.

```json plan-review-round
{"evidence_id":"de2ddf90-fb8f-4964-af53-c89872308176","plan_hash":"3b38fbf8be2926fc8850c51147108d1c1eeb687a5ca02d6315af5a7d065d2634","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"58450f1bcc9d820670fa07e3257b3845b943fbd56481cd80f2e5da37bb441d30","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":5,"emitted_findings":11,"total":16},"evidence_id":"de2ddf90-fb8f-4964-af53-c89872308176","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"ec5720ca76e3abdd889dfe24318738cbc3a1e91156e1635bc2531ab2241703c5","status":"valid"},"source_digest":"3f56a2be60efc70632eabd7b63b20b1ff1ff64c2cbcb6f3c0d41a49a2f70cb51","version":1},"findings":[{"category":"traceability","check_key":"migration-version-inventory","description":"`401_terminals.sql` collides with the registered `401_model_metadata_reasoning.sql`; `402_task_close_reviews.sql` is also already present, so the plan's receipts-through-400 fixtures and latest-asset=401 carriers are stale.","finding_id":"PR5-001-migration-version-collision","fix":"Update 1.1 to preserve migrations 401 and 402 and their latest-asset carriers. Rename the new migration and every body, target, receipt, fixture, acceptance, catalog, identity, grant, and CLI-contract reference to 403; seed upgrade fixtures with receipts through 402 before regenerating derived artifacts.","location":"§§ 1.1–1.2 schema lineage and latest-asset assumptions","prevention":"Before naming a migration, inventory MIGRATIONS and on-disk migration files at the target-branch tip, then sweep every latest-version and receipt fixture.","principle":"A numbered migration must use an unoccupied, monotonically increasing version from the target branch's live registry.","root_cause":"The plan froze the 0.5.0 migration inventory at version 400, while the current branch already registers 401 and 402.","section_id":"1.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"leaf-close-dependency-closure","description":"G requires the complete restart-replay and resume-executor suites for 1.1 and 1.2, while 2.1 explicitly owns five red restart tests and the red composer-resume test after the merge; the Overview says ten tests are red at branch HEAD. The first two leaves therefore cannot close.","finding_id":"PR5-002-bootstrap-guard-closeability","fix":"Make 1.1 the merge-and-bootstrap-repair leaf: inventory all ten known red G cases imported from `wt-task-20255-m4`, co-locate every repair needed for 1.1's full G run there, and remove or rescope the duplicate downstream ownership. Keep 1.2 behind that green bootstrap.","location":"§ 1.1 close gate, § 1.2 close gate, and § 2.1 red-test ownership","prevention":"Run the stated bootstrap guard against the exact merged tree during planning and assign every known red case to the earliest leaf that must make that guard green.","principle":"A leaf's required close gate must be satisfiable entirely from work in that leaf's dependency closure.","root_cause":"The universal G gate is applied to the bootstrap merge before downstream leaves repair tests that the merged branch is known to fail.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"live-config-source-of-truth","description":"An existing `terminals.default_backend=native` override remains effective after 5.1, so an upgraded hub can continue defaulting to native. The plan also never decides how to distinguish a value introduced by the fabricated flip from an intentional user override, and its cited `gobby config set` command is not a registered CLI surface.","finding_id":"PR5-003-active-backend-rollback","fix":"Specify a decision-complete provenance and overwrite policy for persisted native values, implement it through a supported one-time config migration or real mutation surface, and add an upgraded-hub fixture that seeds the persisted value and proves the effective default plus a default spawn become tmux. Replace the nonexistent operator command with the supported interface or add and test that command.","location":"§ 5.1 default-backend rollback and upgrade behavior","prevention":"For every default rollback, test fresh, auto-seeded upgrade, and user-overridden installations against the effective runtime projection.","principle":"A safety rollback must repair the effective installed state while preserving an explicit policy for user-authored overrides.","root_cause":"The deliverable changes model/template defaults, while runtime configuration applies persisted DB overrides after those defaults.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"retry-attempt-resource-ownership","description":"If attempt B fails or exits before timed-out attempt A finally creates the shared tmux session name, A's generation mismatch suppresses cleanup and the row is no longer pending, leaving an unowned live tmux session outside the stale-pending reaper.","finding_id":"PR5-004-tmux-retry-attempt-ownership","fix":"Either reject/defer a tmux retry until the prior prepare task settles, or prepare under a generation-specific temporary session identity that A can always kill and bind/rename only after its generation CAS succeeds. Add tmux-specific orderings where B promotes, fails, or is reaped before A completes and prove no session leaks.","location":"§ 2.5 timeout callback and retry generation handling","prevention":"Enumerate late-completion permutations for every backend: successor promotes, fails, exits, or is reaped before the predecessor completes.","principle":"Every late attempt must clean up the resource it created without relying on another generation's current state.","root_cause":"Tmux attempts reuse one spawn key, while old-generation cleanup is skipped to avoid killing the newer attempt's session.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"event-gap-cursor-advancement","description":"For a same-epoch ring-buffer gap, the manager lists and reconciles but keeps the evicted `last_event_seq`; its next subscribe can request the same unavailable sequence and loop on `gap` forever.","finding_id":"PR5-005-same-epoch-gap-cursor","fix":"Make every authoritative list response carry its event-sequence cut and atomically persist that epoch/sequence after both same-epoch gaps and epoch changes before subscribing again. Extend 2.9.7 with an evicted same-epoch cursor and prove the next subscription starts from the list cut and resumes live delivery.","location":"§ 2.9 event-stream gap recovery","prevention":"Test replayable gap, same-epoch evicted gap, epoch change, duplicate events, and repeated resubscription from the persisted cursor.","principle":"Recovery from an unreplayable event gap must advance the consumer cursor to the authoritative snapshot cut before resubscription.","root_cause":"The plan resets `(last_event_epoch, last_event_seq)` after list reconciliation only when the host epoch changes.","section_id":"2.9","severity":"blocking"},{"category":"unhandled-edge","check_key":"detach-finalization-timeout","description":"If both detach notifications are lost while the socket stays open, the pane remains `Detaching` forever and never issues the fresh proxy attach.","finding_id":"PR5-006-detach-ack-deadline","fix":"Add a detach-ack deadline. On expiry, invalidate and reconnect the WebSocket generation so socket finalization is the ownership fence, then re-list and request a fresh proxy attachment; drop late old-generation results. Test lost reply, lost lifecycle event, daemon loss during detaching, and late responses.","location":"§§ 6.2–6.3 direct-frame fallback `Detaching` state","prevention":"For every request-backed state, test lost reply, lost lifecycle echo, disconnect, timeout, cancellation, and late old-generation response.","principle":"Every protocol wait state needs a bounded terminal transition that preserves the server-side ownership fence.","root_cause":"Proxy fallback waits indefinitely for either `terminal_detach_result` or `terminal_detached` on an otherwise healthy WebSocket.","section_id":"6.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"control-acquisition-input-settlement","description":"The triggering keystroke has no documented buffering, visible refusal, timeout, or cleanup policy, so it can be silently lost or retained across an invalid attachment generation.","finding_id":"PR5-007-first-input-settlement","fix":"Choose one shared bounded policy for web and gclient: queue the triggering input exactly once until `terminal_control_result`, then send it under the installed generation or visibly refuse and clear it on denial, timeout, lease loss, detach, or reconnect. Add all transition tests.","location":"§ 4.6 and § 6.3 focus-follows-control input paths","prevention":"Exercise input before grant, grant denial, lost grant reply, timeout, lease loss, detach, and reconnect for every focus-follows-control client.","principle":"Every user input event must settle as delivered, refused, queued, or indeterminate.","root_cause":"Both clients initiate asynchronous control acquisition on the first keystroke but specify behavior only for a later keystroke after the grant.","section_id":"4.6","severity":"blocking"},{"category":"unhandled-edge","check_key":"untrusted-cursor-validation","description":"Malformed timestamps/UUIDs, truncated fields, `last_key > upper_bound`, future sequence values, and reuse under another project/filter set have no defined outcome; they can produce 500s or causally invalid rosters.","finding_id":"PR5-008-cursor-validation","fix":"Define a versioned opaque cursor bound to project, normalized filters, daemon epoch, snapshot sequence, upper bound, and last key. Strictly validate decoding and ordering; return `invalid_cursor` for malformed/tampered contexts and `cursor_stale` only for a valid old epoch. Add matching REST and WS tests for each boundary.","location":"§ 4.4 REST/WS paged snapshot cursor","prevention":"Fuzz cursor decoding, field types, ordering bounds, sequence bounds, project binding, and filter binding identically across REST and WebSocket.","principle":"An untrusted cursor must reconstruct one valid snapshot context or fail with a typed error before querying.","root_cause":"The cursor grows to carry epoch, sequence, upper bound, and last key, while validation is specified only for an old epoch.","section_id":"4.4","severity":"blocking"},{"category":"weak-testability","check_key":"bounded-stream-recovery","description":"Neither client proves that event 1,025 discards the partial paged roster and starts one fresh listing, and gclient never proves a lagged subscriber re-lists after its 256-entry channel closes.","finding_id":"PR5-009-event-overflow-acceptance","fix":"Add stable web and gclient acceptance that forces both capacity boundaries and proves bounded memory, one fresh snapshot, no stale replay, and convergence.","location":"§§ 4.4, 4.6, and 6.2 bounded lifecycle buffers","prevention":"For each bounded queue, force capacity plus one and assert discarded partial state, restart ownership, stale-event rejection, and final authoritative convergence.","principle":"Every bounded event buffer needs executable proof of its overflow transition and eventual convergence.","repairs":[{"items":[{"artifact":"test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::paging_overflow_restarts_from_fresh_snapshot`","prose":"When 1,025 lifecycle events arrive during pagination, the web client discards the partial roster, starts one fresh snapshot, applies no stale buffered event, and converges to the authoritative roster with bounded memory"}],"kind":"add_acceptance","section_id":"4.6"},{"items":[{"artifact":"test: `crates/gclient/tests/reconciliation.rs::paging_overflow_restarts_from_fresh_snapshot`","prose":"When 1,025 lifecycle events arrive during pagination, LiveDaemon discards the partial listing, starts one fresh snapshot, applies no stale buffered event, and converges to the authoritative roster"},{"artifact":"test: `crates/gclient/tests/reconciliation.rs::lagged_subscriber_relists_and_converges`","prose":"When a subscriber exceeds its 256-entry channel, it receives Lagged, re-subscribes and re-lists without reusing stale state, and converges while the healthy socket generation remains valid"}],"kind":"add_acceptance","section_id":"6.2"}],"root_cause":"Acceptance covers ordinary replay and sustained creation, while omitting the stated 1,024-event paging overflow and 256-entry subscriber lag paths.","section_id":"4.4","severity":"blocking"},{"category":"weak-testability","check_key":"production-adapter-acceptance","description":"`send`/`terminal_input` is absent from production-adapter and single-reader tests, so input replies can be misrouted or leave pending futures across EOF, close, cancellation, or reconnect while scripted 6.3 tests remain green.","finding_id":"PR5-010-livedaemon-send-coverage","fix":"Add a production `LiveDaemon::send` matrix covering delivered and typed-refused write outcomes, interleaving with spawn/terminate/events, unmatched IDs, and atomic pending-map failure/clear on EOF, explicit close, cancellation, and reconnect.","location":"§ 6.2 `LiveDaemon::send` and shared WebSocket dispatcher","prevention":"Generate the adapter acceptance matrix from the production trait and require every method to appear in success, typed-failure, interleaving, and disconnect cases.","principle":"Every production adapter method using a multiplexed request map needs success, refusal, correlation, and generation-settlement acceptance.","repairs":[{"items":[{"artifact":"test: `crates/gclient/tests/daemon_live.rs::send_is_correlated_and_settled_across_failures`","prose":"LiveDaemon send resolves delivered and typed-refused terminal-input replies by request_id while interleaved with spawn, terminate, and lifecycle events, and every pending send fails and is removed on EOF, close, cancellation, or reconnect before the next socket generation accepts requests"}],"kind":"add_acceptance","section_id":"6.2"}],"root_cause":"The body puts `send` into the request-id/oneshot map, while 6.2.5 and 6.2.7 enumerate every other relevant method.","section_id":"6.2","severity":"blocking"},{"category":"weak-testability","check_key":"lease-mutation-linearization-coverage","description":"Attach assignment and websocket-wide finalization can bypass or recursively reacquire the lock without any acceptance failing, risking overlap with dispatch, nested-lock deadlock, or premature latch clearing.","finding_id":"PR5-011-lease-mutation-variant-coverage","fix":"Add concurrency acceptance that pauses a write during attach-with-control and during `finalize_websocket` over multiple attachments, proves both wait on the same per-terminal lock without deadlock, and proves latches remain until the in-flight write settles.","location":"§§ 4.1 and 4.3 shared per-terminal lock","prevention":"Enumerate every public mutation that acquires the authority lock and pause it behind in-flight dispatch, including multi-record wrapper paths.","principle":"When multiple mutation entry points share one linearization boundary, each independently callable path needs a contention test.","repairs":[{"items":[{"artifact":"test: `tests/terminals/test_write_coordinator.py::test_attach_and_finalize_websocket_linearize_with_dispatch`","prose":"Attach when it assigns control and finalize_websocket over multiple attachments both wait behind an in-flight dispatch on the same per-terminal lock, complete without nested-lock deadlock, and clear no write latch before dispatch settles"}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"The body moves attach-with-control and `finalize_websocket` under the shared lock, while 4.1.7 exercises only takeover, release, and single finalize.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"#10965","round":5,"round_number":5,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

**Round 6** `kind: verification`

- reviewer_run: 9ab2bd33-7091-49d9-9294-0122a411a1cb
- reviewer_session: #10970
- verdict: needs_review
- findings:
- PR6-001 / blocking / bootstrap ledger has no review stage and absence is not fail-closed — accepted (modified); the ledger's rows are a deterministic projection of the approval round's sealed `manifest_entries` (the adversary's own derived output) and the plan's acceptance items, so the approving round is the ledger's review; the handoff sequence now asserts the file exists and re-runs the generator's consistency check before `gobby build` and again before the first leaf claim, because `verify_bootstrap_ledger` skips an absent companion. Declined the portion that changes `verify_bootstrap_ledger`'s absence semantics: that is core behavior for every plan and out of this plan's scope.
- PR6-002 / blocking / lock cell dropped with the lease while owners or waiters hold it — accepted; the per-terminal lock cell lives for the registry's lifetime (bounded by terminal rows seen in-process) and is never dropped by `finalize`, so a re-attach after finalize reuses the same cell; 4.1.9 pins the finalize/waiter/re-attach race with a cell-identity assertion.
- PR6-003 / blocking / unsigned cursor fields can be altered to other valid values — accepted (narrowed); the plan never claimed authentication, and the cursor confers no authority: it is bound to the caller's project, the request's filters, and the epoch, and editing `seq`/`upper`/`last` within the validated ranges only changes which page the editor receives, which a fresh request could ask for anyway. 4.4 now states that trust model explicitly; a MAC or server-side cursor store is declined as mechanism without a consumer.
- PR6-004 / blocking / reaper and list-reconcile kill before the generation claim — accepted; `TerminalManager.settle_lock(terminal_id)` (one in-process `asyncio.Lock` per terminal) is held by the retry generation bump, the reaper's observe-kill-fail span, the timeout done-callback, and 2.9's list-reconcile settlement; every settlement re-reads the row under the lock and compares the captured generation before any kill. 2.5.8 and 2.9.11 pin reaper-vs-retry and list-reconcile-vs-retry.
- PR6-005 / blocking / list cut can be evicted before re-subscribe — accepted (modified); the reader subscribes first and lists second: a `subscribe_events` that answers `gap` still registers the subscriber from the host's current seq, the reader buffers events while `list` is in flight, applies the list as the authoritative cut, and drops buffered events with `seq <= list.seq`, so the cut is replayable by construction and no ring capacity is involved. 2.9.12 pins convergence under churn above ring capacity.
- PR6-006 / blocking / post-write `CancelledError` collapsed into a normal `SpawnResult` — accepted; the executor leaves the row `pending` with `commit_indeterminate` exactly as the non-cancel path does, then re-raises the `CancelledError`. 2.4.11 pins caller cancellation plus independent row settlement.
- PR6-007 / blocking / waiter cancellation cancels the shared restart task — accepted; waiters await `asyncio.shield(_restart_task)`, only `stop()` cancels the task, and the task clears itself under the lock on completion. 2.4.12 pins two waiters with one cancelled.
- PR6-008 / blocking / fragment assembly unbounded — accepted (narrowed); the branch reducer already enforces `TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES` (16 MiB per assembly), `TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES` (64 MiB per socket), and a fixed non-sliding deadline from `startedAt`; the gap is that the hook never calls `reducer.disconnect()` on close or reconnect. 4.6 now wires that and 4.6.8 asserts the retained bounds, the non-sliding deadline, and disconnect clearing. Fragment-count and concurrent-assembly caps are declined: the peer is the authenticated local daemon and memory is already bounded by bytes and the deadline.
- resolution_notes: 8/8 accepted (three narrowed to the provable gap); no typed repairs this round, all fixes hand-applied as prose plus acceptance items 2.4.11, 2.4.12, 2.5.8, 2.9.11, 2.9.12, 4.1.9, 4.6.8; base validation re-run after the edits.

```json plan-review-round
{"evidence_id":"264493ba-190f-49cc-b01c-5547f06ff43f","plan_hash":"f14d353a8942f440320a21ec54bc999f30e1bcccd1c49309491e3ac653a043d1","round_number":6,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"4ca911b909b0d0f5436e241230262ff74422adb9df9aac07c66249a8f624d7d0","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":8,"total":8},"evidence_id":"264493ba-190f-49cc-b01c-5547f06ff43f","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"f18841257f4f38d5063ca1831221d2992189fdfb56424d5e57474fe2cebf2260","status":"valid"},"source_digest":"f6148d9107ab489ce6f05dfa47d1a05c67120314ec93fb07fca2b599d6349bf9","version":1},"findings":[{"category":"missing-requirement","causal_finding_id":"PR3-003-bootstrap-ledger","causal_section_ids":["Task Mapping"],"check_key":"bootstrap-ledger-required","description":"The plan generates `.coverage-ledger.yaml` only after final approval and expansion-mode validation, while `docs/contracts/plan-coverage.md` requires every new epic ledger to be adversary-reviewed before expansion; `verify_bootstrap_ledger` also skips a missing companion, so the stated pre-claim gate is not fail-closed.","finding_id":"PR6-001-bootstrap-ledger-review-order","fix":"Define a handoff sequence that generates the final-hash/root-ref/M1-bound ledger, adversarially reviews that exact companion, and fails on absence before any expansion or leaf claim. Update Task Mapping and the handoff verifier accordingly.","introduced_in_round":3,"location":"Task Mapping / bootstrap-ledger handoff","prevention":"For every handoff artifact, map creation, adversarial review, validation, expansion, and first-claim ordering against the governing contract, and prove absence fails closed.","principle":"A required companion artifact must exist in the exact form and lifecycle stage where the governing contract requires adversarial review.","root_cause":"The round-3 repair deferred ledger generation until build handoff because its final hash, root ref, and M1 leaves are late-bound, leaving no review stage for the generated artifact and relying on a verifier that skips an absent file.","section_id":"Task Mapping","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-004-lease-write-linearization","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"`finalize` can remove lock L1 while another coroutine still owns or awaits it, and a new attach can then create L2 for the same terminal. Operations on L1 and L2 run concurrently, defeating the single write/lease linearization boundary that 4.1 is meant to establish.","finding_id":"PR6-002-lease-lock-cell-reuse","fix":"Keep a stable lock cell independent of lease lifetime, or evict it under a map mutex only after proving no owner, waiter, or retained reference and comparing cell identity. Add a deterministic finalize/waiter/re-attach race test.","introduced_in_round":4,"location":"§ 4.1 shared per-terminal lock lifecycle","prevention":"Test lock-cell deletion against current owners, queued waiters, finalization, and immediate key recreation; require identity-safe eviction.","principle":"A per-key serialization cell remains authoritative until every owner and queued waiter holding its identity has drained.","root_cause":"The plan drops the lock-map entry with the lease record, even though coroutines may still hold or await the old lock.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR5-008-cursor-validation","causal_section_ids":["4.4"],"check_key":"untrusted-cursor-validation","description":"A caller can alter `seq`, `upper`, `last`, or another validly shaped field while preserving the listed checks, changing pagination and replay semantics without receiving `invalid_cursor`. The plan therefore does not satisfy round 5's accepted tamper-rejection requirement.","finding_id":"PR6-003-cursor-tamper-binding","fix":"Authenticate a canonical cursor payload with a daemon-epoch secret or use server-side opaque cursor IDs, validate the binding before semantic parsing, and add field-by-field valid-mutation tests for REST and WS.","introduced_in_round":5,"location":"§ 4.4 REST/WS opaque cursor encoding","prevention":"Mutate each cursor field to a different individually valid value and require both REST and WS decoders to reject every unauthorized mutation.","principle":"A cursor claimed to reject tampering must authenticate or server-bind every field that controls snapshot membership and replay.","root_cause":"The accepted repair uses unsigned urlsafe-base64 JSON and validates only shape, ranges, and request equality.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR3-009-spawn-attempt-generation","causal_section_ids":["2.5"],"check_key":"generation-owned-async-cleanup","description":"A retry can advance the row generation after stale/list observation but before termination. The old path can then kill the newer attempt's shared `spawn_key` or listed slot, while the later CAS merely refuses the stale database transition.","finding_id":"PR6-004-attempt-settlement-claim","fix":"Atomically claim the captured generation into a non-retryable settling state before querying or terminating host resources, compare an attempt-specific resource identity where available, and add reaper-vs-retry and list-reconcile-vs-retry tests.","introduced_in_round":3,"location":"§§ 2.5 and 2.9 stale-pending/list reconciliation","prevention":"Pause every settlement path immediately before its external effect and race a generation bump; prove the effect occurs only after an atomic ownership claim.","principle":"A generation guard must claim ownership before any irreversible external side effect.","root_cause":"The reaper and lost-commit reconciler terminate a host resource before the generation-guarded row failure, leaving a retry window between observation and kill.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR5-005-same-epoch-gap-cursor","causal_section_ids":["2.9"],"check_key":"event-gap-cursor-advancement","description":"Events produced after the list lock is released can evict the advertised sequence before re-subscription, yielding another gap and an unbounded list-gap-list loop. Capturing the cut under the lock alone does not make the later subscribe atomic.","finding_id":"PR6-005-list-subscribe-atomicity","fix":"Make snapshot acquisition and subscription one host-side atomic operation, subscribe first while buffering the authoritative list, or retain the cut until subscriber acknowledgement; add sustained-churn recovery acceptance.","introduced_in_round":5,"location":"§ 2.9 list-cut to event re-subscribe transition","prevention":"Test churn exceeding ring capacity in every list-to-subscribe window and require bounded convergence without repeated gaps.","principle":"A recovery snapshot cut must remain replayable until the subscriber is registered.","root_cause":"The plan captures the list sequence under the event lock, releases that lock, and subscribes in a later operation against a bounded ring.","section_id":"2.9","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-003-commit-partial-write","causal_section_ids":["2.4"],"check_key":"edge-case-coverage","description":"A cancelled spawn task can return `commit_indeterminate` normally and continue caller workflow instead of propagating cancellation, even though the row correctly remains pending for reconciliation.","finding_id":"PR6-006-commit-cancellation-propagation","fix":"Record the indeterminate reconciliation metadata, then re-raise `CancelledError`; keep non-cancellation transport failures on the typed `SpawnResult` path. Test observed caller cancellation and independent eventual row settlement.","introduced_in_round":4,"location":"§ 2.4 post-write commit transport outcome","prevention":"For every async boundary, separately test timeout, transport failure, caller cancellation, and shutdown cancellation, including both caller outcome and durable recovery state.","principle":"Structured cancellation remains observable after the operation durably hands indeterminate external state to recovery.","root_cause":"`CancelledError` after `writer.write()` is wrapped as ordinary `CommitTransportError` and converted into a normal `SpawnResult`.","section_id":"2.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR3-011-host-restart-singleflight","causal_section_ids":["2.4","2.9"],"check_key":"restart-singleflight-generation","description":"Cancelling a request-path or event-reader waiter during backoff can cancel the shared restart task, aborting recovery for the health loop and all other observers despite the stated singleflight ownership.","finding_id":"PR6-007-restart-waiter-shielding","fix":"Have waiters await `asyncio.shield(shared_restart_task)`, reserve direct cancellation for `stop()`, clear or replace the task from an owner-controlled completion path under the lock, and test two waiters where one is cancelled.","introduced_in_round":3,"location":"§§ 2.4 and 2.9 shared `ensure_restart()` task","prevention":"For each shared task, enumerate creation, waiter cancellation, owner cancellation, completion cleanup, and replacement under concurrency.","principle":"Cancellation of one singleflight waiter must not cancel the shared operation for other waiters.","root_cause":"Callers await the manager-owned restart task directly, so ordinary asyncio cancellation propagates into that task.","section_id":"2.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"A peer can keep sending non-final fragments and grow browser memory indefinitely; a timer alone is insufficient if arrivals refresh it, and reconnect cleanup is unspecified.","finding_id":"PR6-008-fragment-assembly-bounds","fix":"Add maximum assembled bytes, fragments, concurrent assembly IDs, and a fixed non-sliding deadline; clear on overflow, timeout, and reconnect with typed outcomes, and add continuous/interleaved-fragment tests.","location":"§ 4.6 `terminalWsFragments` assembly","prevention":"Fuzz fragmented input with endless non-final fragments, interleaved assemblies, oversized totals, reconnects, and sliding-timer attempts.","principle":"Application-level fragment reassembly must independently bound bytes, fragment count, concurrent assemblies, and lifetime.","root_cause":"The plan adds byte accumulation and drives timeout ticks but defines no assembly-size, fragment-count, concurrency, or fixed-deadline limit.","section_id":"4.6","severity":"blocking"}],"reviewer_session":"#10970","round":6,"round_number":6,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

- reviewer_run: 314b3f2b-f180-4a24-a5cd-adfa9f5ad625
- reviewer_session: #10973 (child session 6fb278d4-919d-4721-b688-72300e5b2607)
- verdict: needs_review
- findings:
- PR7-001-bootstrap-ledger-order / blocking / bad-sequencing / Task Mapping - the ledger cannot bind the `root_task_ref` that `gobby build` mints while being generated before that command runs - accepted, narrowed: generation moves to immediately after `gobby build` mints the epic; rows stay a projection of the sealed M1 entries and the plan's acceptance items (the generator never reads the created task tree); the label/row consistency check runs at generation and `verify_bootstrap_ledger` plus the existence assertion run before the first leaf claim. The alternative "add a handoff-tooling deliverable" is declined: no production code gates expansion on the ledger, so new tooling would be unjustified mechanism.
- PR7-002-self-contained-guard-gates / blocking / gobby-format / Constraints - all 33 deliverables close on the plan-global shorthand `G green`, which an expanded leaf never receives - accepted, modified: instead of inlining the guard set 33 times, 1.1 writes `docs/guides/gterminal-development-guide.md` section "Guard set G" (groups 1-7 verbatim, the unaffected-group rule, the known-red carve-out with owners, the test-owned host-leak rule) and every deliverable's Close gate names that file and section, so each leaf resolves the gate from a checked-in artifact.
- PR7-003-downstream-reconcile-acceptance / blocking / bad-sequencing / 2.4 - acceptance 2.4.11 required 2.9's list-reconcile, which is downstream of 2.4 - accepted: 2.4.11 is narrowed to caller-visible cancellation plus the row left `pending` with `commit_indeterminate`; the list-driven promotion assertion stays in 2.9.8 and the 2.4 -> 2.9 direction is unchanged.
- PR7-004-deferred-ledger-parity / blocking / traceability / D1 - Task Mapping promised one D1 ledger item with the deferral task as its leaf - accepted: confirmed against `_coverage_items` and `_leaf_mismatches` that a deferred section yields one row per `original_acceptance_items` entry carrying `deferral_target` and no leaves, so the ledger lists D1/5.3.1 and D1/5.3.2 with empty `expected_leaves`.
- PR7-005-lock-map-lifetime-bound / blocking / unhandled-edge / 2.5 - retaining every `settle_lock` / lease lock for the manager's lifetime bounds the maps by cumulative terminal ids, not by a runtime bound - accepted, modified: both maps hold refcounted cells borrowed and released around every use and deleted at zero borrowers. The daemon's asyncio loop is single-threaded, so borrow/release needs no map mutex and no owner/waiter bookkeeping to stay identity-safe; the fuller pool with attempt/attachment/lease checks is declined as unnecessary mechanism. Churn acceptance added to both sections.
- PR7-006-prewrite-cancellation-correlation / blocking / unhandled-edge / 2.4 - cancellation before `writer.write()` was converted into a `native_host_unavailable` `SpawnResult`, swallowing `CancelledError`, and request-map cleanup was unspecified - accepted: `request_written=False` with a `CancelledError` cause drops the correlation entry, fails the pending row, and re-raises; `request_written=True` records `commit_indeterminate`, marks the request id abandoned, and re-raises; replies for abandoned ids are logged and dropped.
- PR7-007-cursor-seq-causal-binding / blocking / unhandled-edge / 4.4 - a caller who edits `seq` can make its own client drop a real exit as pre-snapshot - accepted, modified: the MAC and the server-side cursor store are declined (the tamperer is the only victim; membership is already bound by `project_id` and the epoch, so a forged in-range `seq` returns a page the caller could request afresh). The causal defect is fixed at its root instead: both consumers filter buffered events by the `seq` of the server's final-page reply, never by a value the caller sent, so a tampered cursor cannot corrupt replay.
- PR7-008-zero-byte-fragment-bound / blocking / unhandled-edge / 4.6 - zero-payload fragments retain metadata while charging nothing against either byte budget - accepted, modified: empty non-final fragments are rejected and each retained fragment charges a fixed metadata overhead against both the per-assembly and socket budgets, which bounds concurrent assemblies through the existing budgets; separate fragment-count and concurrency caps stay declined. 4.6.8 gains a zero-byte flood and a many-id flood.
- PR7-009-list-reconcile-lock-schedule / blocking / weak-testability / 2.9 - acceptance 2.9.11 asserted a retry that bumps the generation while blocked on the lock the settlement holds - accepted: rewritten as two reachable schedules (settlement paused before it acquires the lock, so the retry bumps first and the resumed settlement kills nothing; settlement holding the lock first, killing only its looked-up prepared slot before the retry proceeds against the failed row).
- PR7-010-lease-lock-fifo-schedule / blocking / weak-testability / 4.1 - acceptance 4.1.9 expected a write queued before `finalize` to observe that finalize under a FIFO lock - accepted: rewritten as two schedules (finalize queued before write B, which is then refused stale; and write B queued before finalize, which completes under the old lease), keeping the lock-cell identity assertion in both.
- resolution_notes: 10 of 10 findings accepted, 4 modified as recorded above. Applied by hand (no typed repairs were offered): Task Mapping's ledger ordering and D1 parity rewritten; a guard-set anchor added to 1.1 (new acceptance item and doc target) with all 33 Close gates naming it; 2.4's cancellation rows and 2.4.11 corrected; refcounted lock cells specified in 2.5 and 4.1 with churn acceptance; 4.4's cursor contract switched to reply-derived replay filtering; 4.6's fragment bounds extended; 2.9.11 and 4.1.9 rewritten as reachable schedules.

```json plan-review-round
{"evidence_id":"b889208c-89fe-4f61-a356-3de1845b6fdf","plan_hash":"ffeacab4633f4f2e43495d70d69e4bf27c26b1ebc3eb6baaf3e01439d87817fb","round_number":7,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"aa5e54a32ca18398677a58fe8ffc7818b0a3c17be6459864704ac6b05c38184e","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":10,"total":10},"evidence_id":"b889208c-89fe-4f61-a356-3de1845b6fdf","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"0e5b1401979cf7b6a820a9fb4b015ea296b4799789f0949f46b1e4e6c9bbf869","status":"valid"},"source_digest":"e55415e369276e6c7c5a4f7d77bfc84fb9014c8afb9069219d2ec4f43e8e317e","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"PR6-001-bootstrap-ledger-review-order","causal_section_ids":["Task Mapping"],"check_key":"bootstrap-ledger-required","description":"The handoff says the ledger binds the epic root ref that `gobby build` mints, yet also generates and checks the ledger before invoking `gobby build`. It then substitutes the approval review of inputs for the contract's review of the exact companion file. No executable step can produce the stated final ledger at the stated time.","finding_id":"PR7-001-bootstrap-ledger-order","fix":"Name a supported handoff that obtains the final root ref without expanding, generates the final-hash/M1/root-bound ledger, adversarially reviews that exact file, and makes absence fail before expansion. If current tooling cannot provide that boundary, add the required handoff-tooling deliverable.","introduced_in_round":6,"location":"Task Mapping / bootstrap-ledger handoff","prevention":"Map root creation, final hash binding, manifest derivation, ledger generation, adversarial review, expansion, and first claim in executable order; verify every required value exists at its step.","principle":"A required companion must be generated with its final identifiers and adversarially reviewed before expansion.","root_cause":"The Round 6 repair equates review of deterministic inputs with review of the exact companion and schedules generation before `gobby build`, although that command mints the required `root_task_ref`.","section_id":"Task Mapping","severity":"blocking"},{"category":"gobby-format","check_key":"self-contained-task-sections","description":"All 33 implementation sections depend on undefined external shorthand such as `G green`. An implementing agent receives only its section, so it cannot reconstruct the seven guard groups, allowed unaffected dispositions, known-red carve-outs, or test-owned host-leak rule.","finding_id":"PR7-002-self-contained-guard-gates","fix":"Replace each `G green` shorthand with the exact commands and carve-outs applicable to that leaf, and add an acceptance item preserving that close policy in the derived manifest. A single checked-in guard-runner command is also valid if its full policy and all 33 consumers are named.","location":"Constraints / Guard set G and every deliverable Close gate","prevention":"Render every deliverable in isolation and confirm each referenced gate is either fully defined there or invoked through one concrete repository command with a stable contract.","principle":"An expanded leaf must carry every command, carve-out, and pass condition needed to execute and close it from its own section.","root_cause":"All deliverables use the plan-global shorthand `G green`, while expansion validation criteria contain acceptance text and do not carry the Constraints section that defines G, applicability exceptions, known-red ownership, and leak accounting.","section_id":"Constraints","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"PR6-006-commit-cancellation-propagation","causal_section_ids":["2.4"],"check_key":"acceptance-producer-ownership","description":"Section 2.4 cannot close acceptance 2.4.11 because its final assertion requires 2.9's list-reconcile to promote the pending row. Adding a dependency from 2.4 to 2.9 would reverse the intended chain.","finding_id":"PR7-003-downstream-reconcile-acceptance","fix":"Narrow 2.4.11 to caller-visible cancellation plus preservation of the pending `commit_indeterminate` row. Move the fake-host list-driven promotion assertion entirely into 2.9.8 and keep 2.9 downstream of 2.4.","introduced_in_round":6,"location":"§ 2.4 acceptance 2.4.11 and § 2.9 acceptance 2.9.8","prevention":"For every acceptance clause, resolve its production symbol to the owning leaf and verify that leaf is current or upstream before finalizing dependencies.","principle":"A leaf's close-time acceptance may depend only on behavior produced by that leaf or an upstream dependency.","root_cause":"The Round 6 cancellation repair combined 2.4's propagation behavior with eventual list reconciliation that the section body explicitly assigns to downstream leaf 2.9.","section_id":"2.4","severity":"blocking"},{"category":"traceability","check_key":"bootstrap-ledger-deferred-parity","description":"D1 has both 5.3.1 and 5.3.2, but Task Mapping promises a singular D1 ledger item with the deferral task as its leaf. The current coverage and bootstrap verifiers will therefore omit one item and mismatch the other.","finding_id":"PR7-004-deferred-ledger-parity","fix":"Specify two ledger rows, D1/5.3.1 and D1/5.3.2, with empty `expected_leaves`, retaining the deferred task in each coverage row's `deferral_target`. If the ledger should model deferral targets directly, add and test that verifier change explicitly.","location":"D1 deferral object / Task Mapping ledger projection","prevention":"For every deferred section, compare original acceptance IDs, coverage rows, `deferral_target`, ledger `expected_leaves`, and bootstrap verification field-by-field.","principle":"Bootstrap-ledger rows must preserve one-to-one acceptance identity and the manifest's distinction between implementation leaves and deferral targets.","root_cause":"Task Mapping describes one D1 item and treats the deferral task as a leaf, while D1 carries two original acceptance IDs and coverage represents deferrals through `deferral_target`, not `leaves`.","section_id":"D1","severity":"blocking"},{"category":"unhandled-edge","check_key":"lock-cell-lifetime-bound","description":"Terminal churn grows both `TerminalManager.settle_lock` and `TerminalLeaseRegistry.lock` maps without limit. Matching the cumulative set of terminal IDs seen in-process is not a finite runtime bound.","finding_id":"PR7-005-lock-map-lifetime-bound","fix":"Use an identity-safe lock-cell pool for both maps: borrow and release cells under a map mutex, track owners and waiters, and evict only at zero references when mapped identity still matches and no active attempt, attachment, or lease remains. Add churn tests proving both maps return to a fixed bound while preserving the existing race guarantees.","location":"§§ 2.5 and 4.1 per-terminal lock maps","prevention":"For every persistent per-key map, test owner, waiter, recreation, terminal completion, and high-churn reclamation; require a measurable steady-state bound.","principle":"Per-key serialization state needs stable identity while referenced and a finite, identity-safe reclamation rule after the key becomes inactive.","root_cause":"The plan fixes premature lock eviction by retaining every lock for the daemon lifetime and calls the map bounded by terminal rows, but it defines no terminal-row retention bound or lock-cell reclamation condition.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR6-006-commit-cancellation-propagation","causal_section_ids":["2.4"],"check_key":"edge-case-coverage","description":"Cancellation while waiting for the write lock or otherwise before `writer.write()` is converted into a normal `native_host_unavailable` result. The plan also does not say how the cancelled request leaves the id-to-future map or how a late reply avoids resolving a cancelled future.","finding_id":"PR7-006-prewrite-cancellation-correlation","fix":"For `request_written=False`, remove the correlation entry, fail the pending row, and re-raise `CancelledError`. For `request_written=True`, record `commit_indeterminate`, retire or mark the request abandoned, and re-raise; late replies for abandoned IDs are logged and dropped. Add a real concurrent HostClient test covering both paths.","introduced_in_round":6,"location":"§ 2.4 commit outcome table / § 2.9 correlated reader","prevention":"Cancel each protocol request before lock acquisition, during write/drain, and after write; assert caller cancellation, row state, future-map cleanup, reader survival, and unrelated request progress.","principle":"Cancellation remains caller-visible at every await boundary and retires only its own correlation state.","root_cause":"The Round 6 repair re-raises `CancelledError` only after `writer.write()` and leaves request-future cleanup and late-reply behavior unspecified.","section_id":"2.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR6-003-cursor-tamper-binding","causal_section_ids":["4.4"],"check_key":"untrusted-cursor-validation","description":"A caller can change unsigned `seq` to an in-range later value. If a terminal from page 1 exits at that sequence between pages, final roster installation reinstates it and the client drops the exit as pre-snapshot, causing causal corruption within the caller's own project.","finding_id":"PR7-007-cursor-seq-causal-binding","fix":"Authenticate the canonical cursor payload with a daemon-epoch secret and verify it before semantic parsing, or use a server-side opaque cursor ID. Add REST, web, and gclient tests for a page-1 terminal exiting between pages with only `seq` changed; require `invalid_cursor` and a fresh listing.","introduced_in_round":6,"location":"§ 4.4 unsigned cursor contract consumed by §§ 4.6 and 6.2","prevention":"Mutate every cursor field to another individually valid value and trace its effect through pagination plus event replay in every consumer.","principle":"Every cursor field that controls snapshot reconciliation must be integrity-bound to the snapshot that minted it.","root_cause":"The Round 6 repair treats `seq` as a harmless page selector even though both clients use it to discard buffered lifecycle events.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR6-008-fragment-assembly-bounds","causal_section_ids":["4.6"],"check_key":"edge-case-coverage","description":"Zero-length non-final fragments consume no byte budget but retain chunk metadata and increment indices; many zero-byte assemblies likewise evade the socket-byte cap. The fixed deadline bounds duration, not peak allocation or work before the deadline.","finding_id":"PR7-008-zero-byte-fragment-bound","fix":"Reject empty non-final fragments and add explicit maximum fragments per assembly plus maximum concurrent assemblies, or charge fixed metadata overhead against the socket budget. Extend 4.6.8 with zero-byte fragment and many-ID floods that hit typed bounds before the deadline.","introduced_in_round":6,"location":"§ 4.6 fragment reducer bounds / acceptance 4.6.8","prevention":"Exercise minimum-size and zero-size protocol units, many concurrent IDs, fixed-deadline floods, overflow cleanup, and reconnect cleanup for every bounded reducer.","principle":"A bounded reassembly protocol must charge every retained allocation and unit of work, including zero-payload metadata.","root_cause":"The Round 6 repair bounds decoded bytes and elapsed time while explicitly declining fragment-count and concurrent-assembly caps.","section_id":"4.6","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"PR6-004-attempt-settlement-claim","causal_section_ids":["2.5","2.9"],"check_key":"generation-owned-async-cleanup","description":"Acceptance 2.9.11 is impossible under the stated lock contract. While settlement is paused after lookup and holds `settle_lock`, the retry cannot bump generation, so resumed settlement cannot observe a newer generation.","finding_id":"PR7-009-list-reconcile-lock-schedule","fix":"Rewrite 2.9.11 into two reachable schedules: retry-first bumps generation before reconciliation acquires the lock, so reconciliation later does nothing; settlement-first holds the lock while retry remains blocked, kills and fails the old attempt, then releases so retry proceeds.","introduced_in_round":6,"location":"§ 2.9 acceptance 2.9.11","prevention":"Draw the lock-owner timeline for each race acceptance and verify every asserted mutation is performed by the coroutine that can actually hold the lock at that point.","principle":"A deterministic concurrency acceptance test must describe a reachable happens-before ordering.","root_cause":"The Round 6 acceptance says a retry blocked on `settle_lock` has already advanced generation before the settlement holding that lock re-reads the row.","section_id":"2.9","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"PR6-002-lease-lock-cell-reuse","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"After the paused dispatch releases a fair FIFO per-terminal lock, the already queued second write acquires it before the later finalize, revalidates against the old live lease, and may dispatch. Finalize cannot retroactively make that write stale.","finding_id":"PR7-010-lease-lock-fifo-schedule","fix":"To test stale refusal, queue `finalize` before write B, then queue the new attach; after write A settles, finalize changes the lease before B revalidates. Separately test the current write-B-before-finalize ordering and expect B to complete under the old lease. Retain the lock-cell identity assertion in both schedules.","introduced_in_round":6,"location":"§ 4.1 acceptance 4.1.9","prevention":"Record waiter enqueue order beside each expected lease generation and outcome in concurrency acceptance criteria.","principle":"A lock-order test must align expected state with the order in which waiters were enqueued.","root_cause":"The Round 6 acceptance queues the second write before `finalize` yet expects that write to observe `finalize` and be stale.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"#10973","round":7,"round_number":7,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

**Round 8** `kind: verification`

- reviewer_run: 4f5562a3-c0e3-403f-839a-9d68d30b94bc
- reviewer_session: #10978
- verdict: needs_review
- findings:
- PR8-001-lock-cell-cancel-cleanup / blocking / unhandled-edge — a borrower cancelled inside `lock.acquire()` never runs the context exit, so the round-7 refcount can leak in both 2.5 and 4.1.
- PR8-002-exit-linearization / blocking / unhandled-edge — `clear_on_exit` mutates leases and attachment residue outside the per-terminal lock 4.1 establishes.
- PR8-003-attachment-readiness-rendezvous / blocking / unhandled-edge — with `TerminalView` keyed by `terminal_id`, `onReady` never fires again on re-attach, so the Q-1 activation viewport is never sent.
- PR8-004-history-generation-reconcile / blocking / unhandled-edge — a persistent renderer that applies each attachment's full bounded history appends overlapping lines across generations.
- PR8-005-gclient-proxy-viewport / blocking / missing-requirement — gclient's proxy attach, reconnect, and direct-to-proxy fallback paths never send `terminal_set_viewport`, so Q-1 leaves those panes silent.
- PR8-006-bounded-history-acceptance / blocking / weak-testability — the 500 / 2,000 / 256 KiB bounded-history contract has no acceptance item.
- PR8-007-web-control-accessibility / blocking / weak-testability — the new terminal controls have no keyboard, focus, non-hue, target-size, or theme acceptance.
- PR8-008-hook-size-ceiling / blocking / gobby-format — `useTmuxSessions.ts` is 726 lines and § 4.6 adds several state machines with no approved decomposition.
- resolution_notes: All eight accepted. PR8-001: both async lock context managers gain an `acquired` flag and a `finally` path that decrements the same cell object whether or not acquisition completed, plus queued-cancellation acceptance in 2.5 and 4.1. PR8-002: `clear_on_exit` becomes an async mutation under `TerminalLeaseRegistry.lock`, added to 4.1's mutation list with a paused-dispatch acceptance item. PR8-003: renderer readiness and last-known dimensions are tracked independently of attachment identity; one effect keyed by the installed `attachment_id` sends exactly one observe-safe viewport once both prerequisites hold, covering renderer-ready-before-attach, attach-before-ready, and reconnect without remount. PR8-004: accepted with the lower-mechanism form the finding names — the component instance stays mounted and its terminal buffer is reset when `attachment_id` changes, then that attachment's bounded history is applied once; 4.6.10 is narrowed to same-attachment state changes and an overlapping-reconnect case is added. PR8-005: 6.3 sends `terminal_set_viewport` with the fresh `attachment_id` and current pane dimensions immediately after every successful proxy attach result, across initial attach, reconnect, and direct-to-proxy fallback. PR8-006 and PR8-007: applied as typed `add_acceptance` repairs on 4.6. PR8-008: 4.6 gains three small pure-module targets (write/control settlement, paginated roster replay, attachment-readiness and history routing) with focused tests, keeps `useTmuxSessions` as the socket/effect composition layer, and adds acceptance that every production TypeScript file it touches stays below the 1,000-line ceiling. No finding was declined and no repair was deferred.

```json plan-review-round
{"evidence_id":"c1d79f82-587e-4b25-befc-d99403363f99","plan_hash":"0c257c115b80657a48d8eaed376f340c8e78f3d8148a16f9fd63049bda629e7f","round_number":8,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"87de2907abd5771e4059179d815def17a38b0493bb3946c132b0717f664232f1","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":8,"total":11},"evidence_id":"c1d79f82-587e-4b25-befc-d99403363f99","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"2f3c1e739fe88d10d3babbfd4d7ce4c0b94af9c7259f03c04fbbe797a0e2750b","status":"valid"},"source_digest":"16f0968f54f58819524e4a5bf1ff6e96fbe7062aac4e9edc54f945d5d818bd6d","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"PR7-005-lock-map-lifetime-bound","causal_section_ids":["2.5","4.1"],"check_key":"refcounted-lock-cancellation-cleanup","description":"A waiter cancelled during lock.acquire() never enters the context body, so the pre-incremented borrower can remain forever. Both settlement and lease lock maps then violate their zero-borrower reclamation and bounded-lifetime claims.","finding_id":"PR8-001-lock-cell-cancel-cleanup","fix":"Specify both async context managers with an acquired flag and a finally path that always decrements the same cell, releases only after successful acquisition, and identity-safely deletes at zero. Add queued-cancellation tests for TerminalManager.settle_lock and TerminalLeaseRegistry.lock.","introduced_in_round":7,"location":"§§ 2.5 and 4.1 refcounted lock-cell context managers","prevention":"Cancel a queued borrower before acquisition in every refcounted async lock map and assert borrowers, lock ownership, and map membership all drain correctly.","principle":"Every reference-count increment before a cancellable await must have cancellation-safe cleanup that preserves object identity.","root_cause":"The round-7 repair increments borrowers before lock.acquire() but describes decrement and eviction only on normal context exit.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"lease-exit-write-linearization","description":"Terminal exit can clear lease and attachment state while a coordinator write is awaiting runtime I/O, recreating the missing-latch and authority race the shared lock is meant to close.","finding_id":"PR8-002-exit-linearization","fix":"Make clear_on_exit an async mutation under TerminalLeaseRegistry.lock. Add a paused-dispatch test proving the write settles under its original generation before exit cleanup removes the lease, attachments, and latches.","location":"§§ 4.1 and 4.3 terminal-exit cleanup","prevention":"Inventory every mutation of leases, attachments, and write latches against the shared lock and pause an in-flight write while racing each mutation.","principle":"Every lease, attachment, or unresolved-write mutation that can race byte dispatch must share the per-terminal linearization boundary.","root_cause":"Section 4.1 enumerates locked lease mutations but omits clear_on_exit, while section 4.3 makes clear_on_exit delete the lease entry and attachment residue.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"attachment-readiness-rendezvous","description":"The renderer can become ready before the first attachment is installed, and it remains ready across reconnect. In both cases no later onReady callback is guaranteed, so the new attachment never sends terminal_set_viewport and its prepared pump remains silent.","finding_id":"PR8-003-attachment-readiness-rendezvous","fix":"Track rendererReady and the latest dimensions independently from attachment identity. Add an effect keyed by installed attachment_id that sends exactly one observe-safe viewport once both prerequisites exist, then test both initial arrival orders and reconnect without remount.","location":"§§ 4.3 and 4.6 proxy activation versus persistent renderer lifecycle","prevention":"Test renderer-ready-before-attach, attach-before-renderer-ready, and replacement attachment on an unchanged renderer; require one correctly keyed readiness message in each case.","principle":"A two-sided readiness handshake must handle both arrival orders and must run once for every resource generation.","root_cause":"Keying TerminalView by terminal_id removes the attachment-driven remount, but the plan leaves setViewport attached only to initialization-time onReady.","section_id":"4.6","severity":"blocking"},{"category":"unhandled-edge","check_key":"cross-attachment-history-reconciliation","description":"Applying one full bounded history payload for every new attachment appends overlapping recent output into the same renderer, duplicating lines and corrupting scroll offsets even though each payload is consumed once.","finding_id":"PR8-004-history-generation-reconcile","fix":"Use the lower-mechanism bounded-window policy: keep the component instance mounted, reset its terminal buffer when attachment_id changes, apply that attachment's authoritative bounded history once, then accept its first keyframe/output. Narrow 4.6.10 scrollback preservation to state changes that retain the same attachment, and add an overlapping-reconnect test.","location":"§ 4.6 stable TerminalView identity and per-attachment history replay","prevention":"Reconnect with two overlapping numbered history windows and assert every line, truncation marker, scroll pin, and first keyframe appears in one coherent order.","principle":"A persistent append-only consumer needs an authoritative replace boundary or a comparable cursor when replay snapshots overlap across generations.","root_cause":"Once-only filtering is scoped to one attachment; AttachHistory has no lineage cursor and the retained renderer already contains the previous attachment's tail.","section_id":"4.6","severity":"blocking"},{"category":"missing-requirement","check_key":"proxy-activation-consumer-parity","description":"gclient installs fresh proxy attachments during initial use, reconnect, and direct-to-proxy fallback without sending terminal_set_viewport. Those paths can report Attached while receiving no history, keyframe, or live output.","finding_id":"PR8-005-gclient-proxy-viewport","fix":"In gclient, send terminal_set_viewport with the fresh attachment_id and current pane rows/cols immediately after every successful proxy attach result and before waiting for frames. Extend reconnect, fallback, and production-stack tests to assert viewport-before-frame activation.","location":"§§ 4.3, 6.2, and 6.3 proxy attachment consumers","prevention":"For every required protocol message, enumerate initial attach, reconnect, and fallback paths in every production client and require post-transition frame delivery.","principle":"Every client of a protocol must satisfy each new server-side activation precondition.","root_cause":"Q-1 makes viewport receipt mandatory for all proxy pumps, while its sender work is mapped only to the web client.","section_id":"6.3","severity":"blocking"},{"category":"weak-testability","check_key":"bounded-history-contract","description":"No acceptance proves the selected bounded-history contract or generates its coverage label; an implementation can change or bypass the limits while satisfying 4.6.9–4.6.14.","finding_id":"PR8-006-bounded-history-acceptance","fix":"Add acceptance 4.6.15 proving the real host-to-web default and both ceilings, accurate truncation propagation, and the rendered marker.","location":"§ 4.6 Q-2 bounded-history contract and Acceptance","prevention":"Trace every numeric limit in Overview and deliverable prose to an acceptance item that exercises the exact limit and one crossing case.","principle":"Every governing numeric bound needs executable default, boundary, over-bound, and truncation evidence represented in the manifest.","repairs":[{"items":[{"artifact":"test: `web/tests/terminal-history-scroll.spec.ts::bounded_history_caps_and_truncation`","prose":"A real host-to-web attach uses 500 history lines by default, clamps configured history to 2,000 lines and 256 KiB, reports truncation when either ceiling cuts the payload, and renders exactly the capped window with the truncation marker."}],"kind":"add_acceptance","section_id":"4.6"}],"root_cause":"The body names 500, 2,000, and 256 KiB, while acceptance only checks a supplied truncation marker and more than one viewport.","section_id":"4.6","severity":"blocking"},{"category":"weak-testability","check_key":"product-ui-accessibility-contract","description":"The new terminal actions and state banners can pass the current tests while being inaccessible on keyboard or touch, losing focus visibility, relying on hue, or failing one theme.","finding_id":"PR8-007-web-control-accessibility","fix":"Add a focused acceptance item using existing Button/control primitives and verify keyboard and focus behavior, non-hue state cues, 44×44 coarse-pointer targets, and light/dark rendering in portrait, landscape, and desktop previews.","location":"§ 4.6 take-back, retry, discard, and jump-to-bottom controls","prevention":"For every new interactive control, add acceptance for shared primitives, keyboard operation, visible focus, non-color-only state, coarse-pointer size, and both themes.","principle":"New product-UI actions must prove canonical-component use and WCAG 2.2 AA interaction behavior at the tiers where they ship.","repairs":[{"items":[{"artifact":"test: `web/tests/terminal-history-scroll.spec.ts::terminal_actions_meet_accessibility_contract`","prose":"Take-back, retry, discard, and jump-to-bottom use existing shared controls and are keyboard operable with visible AA focus, non-color-only state cues, 44×44 coarse-pointer targets, and correct light/dark rendering in the canonical portrait, landscape, and desktop tiers."}],"kind":"add_acceptance","section_id":"4.6"}],"root_cause":"Behavioral acceptance covers action outcomes and scrolling, while the repository UI contract's keyboard, focus, non-hue, target-size, and theme requirements are absent.","section_id":"4.6","severity":"blocking"},{"category":"gobby-format","check_key":"production-source-size-ceiling","description":"Only 273 lines remain in useTmuxSessions.ts, while § 4.6 adds several independently testable state machines. The current target list makes a threshold-crossing monolith likely and gives the implementing agent no approved decomposition.","finding_id":"PR8-008-hook-size-ceiling","fix":"Add small pure module targets for control/write settlement, paginated roster replay, and attachment/history readiness, with focused tests for each. Keep useTmuxSessions as the WebSocket/effect composition layer and add acceptance that every new production TypeScript file remains below 1,000 lines.","location":"§ 4.6 Targets — web/src/hooks/useTmuxSessions.ts","prevention":"Measure each production target before approval, budget the described additions, and add decomposition targets whenever the task can cross the repository ceiling.","principle":"Hand-maintained production TypeScript must remain strictly below 1,000 lines, with decomposition planned before a threshold-crossing implementation.","root_cause":"The merged hook is already 726 lines and the leaf assigns it control settlement, pagination replay, fragment lifetime, attachment readiness, and history routing without decomposition targets.","section_id":"4.6","severity":"blocking"}],"reviewer_session":"#10978","round":8,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

**Round 9** `kind: verification`

- reviewer_run: 927afb9a-5ff6-4d90-acef-e405ea2d9e19
- reviewer_session: #10984
- verdict: needs_review
- findings:
- PR9-001-bootstrap-ledger-pre-expansion / blocking / bad-sequencing / Task Mapping - `gobby build --quick` dispatches expansion before step 3 writes the ledger, so the contract's "adversary-reviewed before expansion" ordering is not met - declined. Third re-litigation of this item (PR3-003 -> PR6-001 -> PR7-001) and the applied text is not wrong. `verify_bootstrap_ledger` has no production caller (only `src/gobby/plans/bootstrap_ledger.py::verify_bootstrap_ledger` and `tests/plans/test_bootstrap_ledger_revalidation.py`), so nothing reads the ledger during expansion and the ordering has no mechanical consequence; the generator never reads the created task tree, so the ledger stays an independent expectation regardless of when expansion ran; and steps 4 and 6 already make absence fail closed before the first leaf claim. The demanded pre-expansion boundary or handoff-tooling deliverable is the exact mechanism rounds 6 and 7 declined as unjustified.
- PR9-002-first-leaf-size-growth / blocking / gobby-format / 1.1 - leaf 1.1 targets two files at or above the 850-line `production-size-growth` threshold with no same-leaf decomposition - restructuring declined, measurement corrected. Verified against the real merge: `git merge-tree --write-tree 0.5.0 wt-task-20255-m4` yields `lifecycle_monitor.py` at 929 lines after the six conflict-marker lines resolve, and `_implementation.py` at 755 - the finding's 893/953 are pre-merge `0.5.0` counts, and `_implementation.py` is not near the ceiling after the merge at all. The lint also does not fire on 1.1 (`_has_new_split_target` is satisfied for both files), so there is no gate failure. Decomposing two files inside a twelve-path merge-conflict commit would make the merge unreviewable, and 4.1 already moves the inline `TerminalServices` construction out of `lifecycle_monitor.py`. Applied the one real correction: 4.1's stale "893 lines on the branch" becomes the measured post-merge count.
- PR9-003-bin-freshness-literal-consumer / nit / traceability / 3.4 - the literal `bin_freshness_loop` registry entry in the wiki watcher lifecycle test is absent from the claimed whole-hit consumer sweep - accepted; `tests/wiki/test_watcher_lifecycle.py::_loops` is named in 3.4 as intentionally unchanged because the loop name and registration stay stable.
- PR9-004-snapshot-watermark-source / blocking / unhandled-edge / 4.4 - later pages repeat the snapshot block from the caller-supplied cursor, so the final page's `seq` is an echo of client state and acceptance 4.4.8 is unsatisfiable - accepted with the finding's own lower-mechanism repair: both consumers pin page one's server-stamped `snapshot.seq` and `daemon_epoch` before any continuation and replay against that pinned watermark, ignoring later echoes. No MAC and no server-side cursor store.
- PR9-005-ws-latch-crash-recovery / blocking / unhandled-edge / 4.3 - a hard daemon crash strands durable `ws:{attachment_id}:` unresolved-write latches whose only cleanup walks the process-local attachment registry - accepted. Verified `unresolved_writes` is a durable jsonb column on the terminal row capped at `UNRESOLVED_WRITE_MAX_ENTRIES = 32`, cleared only per-key or wholesale on terminal exit. Startup reclamation is added to 4.1 rather than 4.3: 4.1 already owns `init_orchestration` / `init_servers` and the coordinator, and adding `src/gobby/storage/terminals.py` to 4.3 would trip `shared-target-ordering` against 4.2, which has no dependency path to 4.3.
- PR9-006-oneway-viewport-send / blocking / unhandled-edge / 6.3 - 6.2's `Daemon::send` is correlated and pends on a `*_result`, but `terminal_set_viewport` has no reply, so 6.3's activation step has no valid transport operation - accepted. Verified `_handle_terminal_set_viewport` answers nothing on success (only `terminal_error` on invalid dimensions). A one-way `Daemon::notify` is added, implemented in `LiveDaemon` and `ScriptedDaemon`, and used for every viewport activation and resize; correlated `send` stays for terminal input.
- resolution_notes: Four findings accepted (PR9-003, PR9-004, PR9-005, PR9-006) and applied as prose; PR9-002 accepted only as a measurement correction to 4.1; PR9-001 declined. No finding carried typed repairs, so `apply_plan_review_repairs` was not called.

```json plan-review-round
{"evidence_id":"7bbfb5f5-a063-4b90-a1a7-242bd0f0e93d","plan_hash":"153a2b21ebe5303578e978283967f0d2773c2d5c97526232ffecb2934a44d53b","round_number":9,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"4bdbf13d13149f413743ded4d3085685e21fb53a035212ec064a46f964c561c3","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":6,"total":7},"evidence_id":"7bbfb5f5-a063-4b90-a1a7-242bd0f0e93d","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"2a6db6fe2dfb7a8412de403058e9fe51171d6bfb01cc299bf60f8875659effcc","status":"valid"},"source_digest":"0a506fb994e4200f86c9a3e72fdf34a19b7c333ea04007537b422c8a807024c0","version":1},"findings":[{"category":"bad-sequencing","check_key":"bootstrap-ledger-required","description":"The applied handoff still contradicts the governing Bootstrap Ledger contract: the exact `.coverage-ledger.yaml` must be adversary-reviewed before expansion, yet step 2 runs an approved build dispatcher tick and step 3 creates the ledger afterward. `--quick` disables automation only after that tick, and the production verifier silently accepts an absent companion, so expansion can begin before the required reviewed file exists.","finding_id":"PR9-001-bootstrap-ledger-pre-expansion","fix":"Add an executable pre-expansion boundary that creates/registers the final epic ref without dispatching expansion; generate the final-hash/M1/root-bound ledger, adversarially review that exact file, and make absence fail closed before resuming. If current tooling has no such boundary, add and complete the handoff-tooling deliverable before this plan is approved.","location":"Task Mapping / bootstrap-ledger handoff steps 2–6","prevention":"Trace every handoff prerequisite against the actual command order and verify the gated artifact exists before the first dispatcher action.","principle":"A required companion artifact must exist in its final identity-bound form and be reviewed before the lifecycle action it gates.","root_cause":"The handoff substitutes review of deterministic inputs for review of the generated companion and uses `gobby build --quick`, whose dispatcher tick runs before automation is disabled.","section_id":"Task Mapping","severity":"blocking"},{"category":"gobby-format","check_key":"production-size-growth","description":"Leaf 1.1 targets `src/gobby/agents/lifecycle_monitor.py` at 893 lines and `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` at 953 lines without a same-leaf decomposition. The planned moves in 4.1 and 2.3 occur after 1.1 closes, so they cannot satisfy `production-size-growth` for the first leaf.","finding_id":"PR9-002-first-leaf-size-growth","fix":"Move both decompositions into 1.1: add the new same-extension bare-path Targets and section-local split/move paragraphs, then make 2.3 and 4.1 depend on and modify those already-created modules.","location":"§ 1.1 Targets and conflict-resolution steps 7 and 9","prevention":"Measure every production Target before approval and require a new same-extension target plus a named split/move in the first leaf that touches each file at 850 lines or more.","principle":"A deliverable targeting a production file at or above the proactive-growth threshold must perform its decomposition in that same leaf.","root_cause":"Section 1.1 treats the merge conflict as a preservation-only edit and defers both extractions to later leaves, although the targeted files already exceed the 850-line planning threshold.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"literal-consumer-inventory","description":"`tests/wiki/test_watcher_lifecycle.py::_loops` retains a literal `bin_freshness_loop` registry entry, but 3.4's claimed whole-hit inventory neither targets it nor names it as unchanged. The seam is low risk because the loop name and registration stay stable, yet the inventory claim is incomplete.","finding_id":"PR9-003-bin-freshness-literal-consumer","fix":"Add `tests/wiki/test_watcher_lifecycle.py::_loops` to the consumer-sweep paragraph as intentionally unchanged because the loop name and registration remain stable; target it only if implementation changes that registry assertion.","location":"§ 3.4 Consumer sweep","prevention":"After symbol-graph traversal, run a literal identifier sweep and record each remaining production/test hit as a Target or a named no-change disposition.","principle":"A plan that claims a whole-hit consumer sweep must record every literal registry seam as changed or intentionally unchanged.","root_cause":"Symbol usages found callable consumers, while the literal `_loops` registry in the watcher lifecycle test was outside that graph result and was not included in the follow-up literal sweep.","section_id":"3.4","severity":"nit"},{"category":"unhandled-edge","check_key":"snapshot-watermark-source","description":"The unsigned stateless cursor cannot support the stated `seq > reply.seq` replay rule. After page one, the server has no independent copy of the original snapshot `seq`; a caller can raise the in-range cursor value, the final page echoes it, and buffered lifecycle events are erased. This makes acceptance 4.4.8 impossible under the described mechanism.","finding_id":"PR9-004-snapshot-watermark-source","fix":"Use the lower-mechanism repair: both web and gclient pin the server-stamped `snapshot.seq` and `daemon_epoch` from page one and use that pinned watermark when the final page installs, ignoring or validating later echoes. Update 4.4, 4.6, and 6.2 tests so an edited continuation `seq` cannot change replay.","location":"§ 4.4 Snapshot causality and acceptance 4.4.8","prevention":"For every paged snapshot, identify where the original watermark lives across continuations and mutate each cursor field while checking client convergence.","principle":"A replay watermark must come from an immutable server observation, never from state the caller can rewrite between pages.","root_cause":"Later-page replies can recover the original snapshot sequence only from the unsigned client cursor, so the claimed final-page server stamp is an echo of caller-controlled state.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"attachment-latch-crash-recovery","description":"A hard daemon crash can strand durable unresolved-write keys from an indeterminate operator write. Graceful finalization clears `ws:{attachment_id}:` keys, but process death skips that path; after restart the attachment registry is empty and clients receive fresh IDs, leaving the old prefixes unreachable. Repeated crash cycles can permanently exhaust the 32-entry latch capacity.","finding_id":"PR9-005-ws-latch-crash-recovery","fix":"Add startup reconciliation that clears every durable `ws:` latch whose attachment cannot survive the process restart, while preserving MCP and attention keys. Target the owning startup/reconciliation path and add a hard-crash test covering indeterminate write → process death before finalize → restart → fresh attach → restored write capacity.","location":"§§ 4.3, 4.6, and 6.2 attachment finalization/reconnect","prevention":"Crash the daemon at every durable/process-local ownership seam, restart, and prove stale identities cannot retain capacity or authority.","principle":"Durable state keyed by a process-local identity needs a restart reclamation path before replacement identities are installed.","root_cause":"`ws:{attachment_id}:` latches persist in terminal rows, while the only cleanup walks process-local attachment records that disappear on hard daemon exit.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"one-way-message-settlement","description":"The new gclient activation step has no valid transport operation. Section 6.2 makes `Daemon::send` correlated and pending until a result arrives, while `terminal_set_viewport` is a one-way activation/resize message with no `terminal_set_viewport_result`. A real client can timeout or retain a pending sender at the exact transition required to start frames, and the scripted 6.3 test can hide the mismatch.","finding_id":"PR9-006-oneway-viewport-send","fix":"Add a one-way `Daemon` notify/write operation that returns after the socket write, implement it in LiveDaemon and ScriptedDaemon, and use it for every 6.3 viewport activation/resize. Keep correlated `send` for terminal-input outcomes, and test a mock server that emits no viewport result and withholds frames until the notification arrives.","location":"§§ 6.2–6.3 LiveDaemon API and proxy activation","prevention":"Classify every outbound WebSocket message as request/reply or one-way and test each one-way path against a server that emits no acknowledgement.","principle":"One-way protocol messages and correlated request/reply commands require distinct settlement contracts.","root_cause":"`LiveDaemon.send` is specified to allocate a request-id oneshot resolved by a `*_result`, while `terminal_set_viewport` intentionally has no result message.","section_id":"6.3","severity":"blocking"}],"reviewer_session":"#10984","round":9,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

- reviewer_run: b476c9cb-b4e2-49a9-99e4-0357a4ab4929
- reviewer_session: #10989
- verdict: needs_review
- findings:
- PR10-001-p7-leaf-local-d1-trace / blocking / traceability / 7.1 - P7 framing says the parity gate would have caught D-1 but neither 7.1 nor 7.2 names a finding ID, so an expanded leaf loses the trace the Overview promises for every non-P1 deliverable - accepted; verified the Overview clause ("every deliverable below cites the IDs it closes so the adversary and the close-time judge can trace each fix to its finding") and that D-1 appears only at the P7 framing line an expanded leaf never receives. One leaf-local sentence added to each: 7.1 closes the component-golden half of D-1, 7.2 the screen/CI parity-evidence half. No acceptance change; the existing items already prove the work.
- PR10-002-terminal-session-fixture-targets / blocking / traceability / 4.6 - three direct consumers of the changed JoinedTerminalSession/TmuxSession row shape are absent from Targets - accepted; verified on the branch that `terminalSessions.test.ts` builds rows through `makeTmuxSession` and asserts the joined output with `expect(joined).toEqual([...])`, `TerminalSessionList.test.tsx` constructs both `TmuxSession` and `JoinedTerminalSession` fixtures, and `SessionsTab.test.tsx` mocks `useTmuxSessions`. None is targeted by any other deliverable, so the typed `add_targets` repair applies cleanly to 4.6 with no `shared-target-ordering` exposure.
- PR10-003-exec-status-helper-death / blocking / unhandled-edge / 2.7 - "only image replacement can produce that EOF" is false, so a gate helper that dies after the gate byte and before `execvp` is reported as commit success - accepted, modified. The claim is wrong as written and the reachable cause is the plan's own new `gate.rs`: any pre-exec failure that exits without writing a status record (argv marshalling, the `fcntl`, a panic) closes fd 4 and presents as EOF. Fixed at that root - every fallible step between the gate byte and `execvp` writes `<code> <argv0>` and exits 127, and the helper installs a panic hook that writes `panic <argv0>` first, so a helper defect can no longer present as EOF. The finding's own remedy - reject EOF when the prepared child has already exited or was signaled - is declined: on a successful `execvp` of a short-lived program the target may already have exited by the time the host wakes, so a `waitpid` gate turns `gterm gate -- /bin/true` into a false `exec_failed`. An external signal delivered inside the exec window stays reported as a spawn that started and exited, because it is observationally identical to the target being signaled immediately after exec and the row's normal exit path settles it. New 2.7.9 pins the injected pre-exec failure.
- PR10-004-lease-loss-attachment-contradiction / blocking / unhandled-edge / 4.3 - lease loss is listed as an attachment-finalization trigger while 4.6 and 6.3 require the same `attachment_id` to survive lease loss as a read-only observer with a take-back action - accepted; the contradiction is real and self-defeating: 4.3.3 sends `terminal_lease_lost` to the displaced attachment, 4.3.7 refuses any later message naming a finalized id as `stale_attachment`, and 4.6.2 / 6.3.3 both take control back under that id. Writer lease and attachment lifetime are separate axes, so lease loss is removed from the finalization trigger list in the body and in 4.3.10; the displaced attachment keeps its observer slot and frame stream and goes read-only. Finalization is reserved for detach, socket or relay loss, terminal exit, and explicit transport failure. New 4.3.11 proves same-id take-back after a takeover.
- PR10-005-machine-scoped-startup-reclamation / blocking / unhandled-edge / 4.1 - round 9's startup sweep clears `ws:` latches from every terminal row of the project, which crosses machine boundaries on a shared hub - accepted; verified that the 403 DDL carries `machine_id uuid NOT NULL` on the same row as `unresolved_writes`, and that Gobby already scopes daemon-owned recovery by `require_machine_id()` throughout `storage/agents/`. A project-wide sweep on machine A therefore erases live latches owned by a running daemon on machine B. Scoped the statement to the restarting daemon's `machine_id`; the "no attachment id predates this process" argument only ever held for rows this machine owns. 4.1.12 gains the two-machine assertion.
- resolution_notes: All five findings accepted; PR10-003 accepted with a modified repair. Two are fixer-induced defects in text this coordinator applied - PR10-003 traces to round 3's PR3-010-exec-status-fd and PR10-005 to round 9's PR9-005-ws-latch-crash-recovery. Repairs applied: one typed `add_targets` (4.6) through `apply_plan_review_repairs`; the rest hand-applied as prose plus two new acceptance items (2.7.9, 4.3.11) and one amended item (4.1.12), with 4.3.10 corrected. Round 10 of cap 11.

```json plan-review-round
{"evidence_id":"ab031e33-f5a0-4916-9200-15e4cdd6cc2c","plan_hash":"bee8f4bcd9a7e0e449375e7651b6034b234f0b750f086c89af892615f204aa21","round_number":10,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"dde0c21315f59f40be68da42d0a124c0e91a4514ed7f781c9c1f6cef234aeeaa","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":5,"total":5},"evidence_id":"ab031e33-f5a0-4916-9200-15e4cdd6cc2c","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"b15ce8b5b6068701820b7c91c025a9f7cfb11e5135a11cc10bee340864559bc0","status":"valid"},"source_digest":"66060dc230fdfadd4e4269bc4c8ec7265418ed1c97770275f4740ad3c00d1a92","version":1},"findings":[{"category":"traceability","check_key":"qa-finding-id-parity","description":"P7 says its parity gate would have caught D-1, yet neither §7.1 nor §7.2 names D-1. Because expanded agents receive only the leaf section, both leaves lose the exact finding-to-work trace promised by the Overview.","finding_id":"PR10-001-p7-leaf-local-d1-trace","fix":"Add leaf-local traceability prose: §7.1 closes the component-golden portion of D-1, and §7.2 closes the screen/CI parity-evidence portion. Existing acceptance already proves the work.","location":"§§ 7.1–7.2 leaf bodies","prevention":"Review every deliverable body in isolation and confirm it names each governing QA finding ID without relying on phase framing.","principle":"Each deliverable must carry its governing requirement inside the self-contained section handed to its implementer and close-time judge.","root_cause":"D-1 appears only in P7 framing even though the Overview promises exact finding IDs in every non-P1 deliverable.","section_id":"7.1","severity":"blocking"},{"category":"traceability","check_key":"targets-complete","description":"§4.6 makes terminal session rows expose controlState and readOnlyReason, but three direct consumers are absent from Targets: terminalSessions.test.ts constructs TmuxSession rows and asserts exact join output, TerminalSessionList.test.tsx constructs row fixtures, and SessionsTab.test.tsx mocks the changed hook result.","finding_id":"PR10-002-terminal-session-fixture-targets","fix":"Add the three test files to §4.6 Targets and update their typed fixtures, exact joined-row expectations, and hook mock for the new control/read-only fields and actions.","location":"§ 4.6 Targets for terminal row/type consumers","prevention":"For each changed interface, sweep constructors, object literals, deep-equality expectations, component fixtures, and hook mocks before finalizing Targets.","principle":"A changed TypeScript row shape must inventory every direct constructor, exact object expectation, and typed hook mock.","repairs":[{"entries":["`web/src/components/activity/terminal/__tests__/terminalSessions.test.ts::*` — scope-reason: direct JoinedTerminalSession/TmuxSession fixtures and exact join expectations gain control/read-only fields","`web/src/components/activity/terminal/__tests__/TerminalSessionList.test.tsx::*` — scope-reason: terminal row fixtures gain control/read-only fields","`web/src/components/activity/__tests__/SessionsTab.test.tsx::*` — scope-reason: mocked useTmuxSessions results gain control/read-only fields and actions"],"kind":"add_targets","section_id":"4.6"}],"root_cause":"Targets cover the producer and primary hook tests but omit adjacent fixtures and exact equality consumers of JoinedTerminalSession/TmuxSession.","section_id":"4.6","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR3-010-exec-status-fd","causal_section_ids":["2.7"],"check_key":"edge-case-coverage","description":"The applied sentence “only image replacement can produce that EOF” is wrong: killing, panicking, or otherwise exiting the gate helper after the gate byte also closes fd 4. The host currently maps that EOF to commit success and may promote a row whose target image never ran.","finding_id":"PR10-003-exec-status-helper-death","fix":"Treat EOF as candidate success and reject it when the prepared child has already exited or was signaled before promotion; add a deterministic kill-after-gate-before-exec test requiring typed failure, group reaping, and no live row. If exact replacement proof remains required, use an OS-level exec notification or a directly observable exec primitive.","introduced_in_round":3,"location":"§ 2.7 exec-status pipe success proof","prevention":"Inject kill, signal, panic, ordinary exec failure, timeout, and immediate target exit at every gate/exec boundary and require each to settle without false live-row promotion.","principle":"Descriptor EOF proves closure, so the protocol must distinguish successful CLOEXEC closure from process death before exec.","root_cause":"The native-helper repair treats one cause of EOF as the only cause and never checks the prepared child's exit state before promotion.","section_id":"2.7","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"§4.3 says a prepared attachment “is finalized by the same paths (socket close, detach, lease loss, terminal exit),” and 4.3.10 repeats lease loss. §§4.6 and 6.3 instead require terminal_lease_lost to keep the pane attached and read-only with a take-back action using that same attachment_id. Finalization would delete the ID, stop observation, and make take-back stale_attachment.","finding_id":"PR10-004-lease-loss-attachment-contradiction","fix":"Remove writer-lease loss/takeover from attachment-finalization triggers. Preserve the displaced attachment and observer stream while transferring the holder, bumping generation, and emitting terminal_lease_lost. Reserve finalization for detach, socket/relay loss, terminal exit, and explicit transport failure; add a same-ID take-back test.","location":"§§ 4.3, 4.6, and 6.3 lease-loss transition","prevention":"Cross-check every lease event against attachment registry state, observer continuity, client state, and the next legal control action for activated and unactivated attachments.","principle":"Lease ownership and attachment lifetime are separate state axes; losing write control must preserve an observer attachment when clients support take-back.","root_cause":"The readiness cleanup paragraph copied lease loss into attachment-finalization triggers while the web and Rust client state machines retained same-attachment read-only recovery.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR9-005-ws-latch-crash-recovery","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"The new applied sentence makes clear_orphaned_attachment_writes delete ws:-prefixed entries from “every terminal row of the project” unconditionally. Gobby supports per-machine daemons sharing PostgreSQL, and terminal.machine_id identifies where execution lives; restarting machine A could erase unresolved attachment-write latches owned by a still-running daemon on machine B.","finding_id":"PR10-005-machine-scoped-startup-reclamation","fix":"Scope startup reclamation to require_machine_id() or an equally strong daemon-owner identity and update only rows placed on the restarting machine. Add a same-project two-machine test proving local ws: keys are removed while foreign-machine ws: keys and every non-ws namespace remain byte-identical.","introduced_in_round":9,"location":"§ 4.1 startup clear_orphaned_attachment_writes","prevention":"For every startup sweep, test a shared-datastore fixture with local and foreign live owners and prove only the local owner's abandoned state changes.","principle":"Startup recovery may reclaim only durable state owned by the restarting daemon.","root_cause":"The crash-recovery repair scopes cleanup by project while terminal execution ownership is machine-scoped in a shared PostgreSQL hub.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"#10989","round":10,"round_number":10,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

- reviewer_run: 9826c404-6e05-48c4-95f1-3a00cb50e334
- reviewer_session: #10995
- verdict: needs_review
- findings:
- PR11-001-exit-before-promotion / blocking / unhandled-edge / 2.7 - round 10's "no pre-exec exit path is silent on fd 4" and "the row's ordinary exit path settles it either way" are both too strong, and a `terminal_exited` that arrives while the row is still `pending` is dropped, so a later promotion records a dead process `live` - accepted, modified. Both halves verified. A panic hook does not run for `abort()` or `SIGKILL`, so the absolute claim is wrong. The linearization defect is the larger one and is not limited to the pre-exec case: `TerminalManager.mark_exited` (`src/gobby/storage/terminals.py`) CASes only `live -> exited` and `orphaned -> exited`, with no `pending` branch, so any target that exits before the commit reply lands - `/bin/true` included - has its exit silently dropped and is then promoted by `promote_to_live`'s `pending -> live` CAS. Repaired with mechanism the plan already owns rather than new machinery: 2.5 already targets `storage/terminals.py` and owns the refcounted `settle_lock` (PR7-005), the generation guards, and `fail_pending_attempt` (PR3-009), so the settlement and the lock discipline go there as new 2.5.10, and both orderings converge on `exited` with no sweep. 2.7 keeps only what it owns - the corrected fd-4 prose naming `abort()` and fatal signals as the explicit residue, and 2.7.6's injection assertions.
- PR11-002-commit-deadline-second-client / blocking / traceability / 2.7 - a second production `spawn_commit` path is untargeted, so acceptance 2.7.4's "every `spawn_commit`" is unsatisfiable - accepted; verified two independent implementations on the branch, `host_client.py::spawn_commit` (targeted) and `host_control.py::spawn_commit` (not), the latter emitting a two-field request with no deadline; `host_manager.py:237` awaits it without one, `tests/terminals/host_fakes.py` records `(terminal_id, spawn_key)` tuples, and `test_host_manager.py:618` asserts `client.spawn_commits == [(row.id, row.spawn_key)]`, an exact shape that breaks on the new field. Typed repairs applied. Ordering is clean: `host_manager.py` / `test_host_manager.py` are also owned by 2.4, 2.8, and 2.9, and the P2 chain 2.4 -> 2.5 -> 2.7 -> 2.8 -> 2.9 supplies a dependency path to each; `host_control.py` and `host_fakes.py` are owned by no other deliverable.
- PR11-003-async-lease-consumer-closure / blocking / traceability / 4.1 - round 4's coroutine conversion of the registry mutators left consumers outside `terminal_ws.py` un-migrated, so 4.1 cannot close with the terminal guard green - accepted; verified `proxy_relay.py` calls `self._owner._leases().finalize(...)` synchronously at two sites (inside `finalize_attachment` and `_on_socket_fail`, both already `async def`, so the repair really is just `await`), and that `test_terminal_ws_viewport.py`, `test_tmux_bridge_authority.py`, and `test_lease_authority.py` call the re-signed methods synchronously. Typed repairs applied and the over-narrow sentence corrected. Ordering is clean: 4.1 depends on P2 (covering 2.2), and 4.3 and 4.4 both depend on 4.1.
- PR11-004-write-outcome-correlation / blocking / unhandled-edge / 6.2 - round 5 routed terminal input through the `request_id` pending map, but the protocol does not correlate writes that way - accepted with the finding's own least-change fix. Verified against the branch: `terminal_ws.py::_write_outcome` emits exactly `type`, `terminal_id`, `attachment_id`, `client_write_seq`, `outcome`, and `reason`, with no `request_id`, which appears only on the listing, create, and kill verbs. A `send` on `terminal_input` would therefore pend until timeout or reconnect. The `request_id` map stays for the correlated verbs and a second pending-write map keyed by `(attachment_id, client_write_seq)` takes write outcomes, failing and clearing atomically on cancellation, EOF, close, and generation replacement; 6.2.10 is rewritten to assert that key. Threading `request_id` through 4.3 and 4.5's codecs, handlers, and sealed goldens is declined - it needs ownership in sections that do not have it and rewrites committed goldens to add a field the protocol already has an equivalent for.
- PR11-005-round10-acceptance-id / nit / gobby-format / V1 - round 10's prose names the new 2.7 acceptance item `2.7.9` while the applied section assigns `2.7.6` - accepted; both references are in round 10's prose projection, not in its sealed fence. Correcting here rather than rewriting settled history: **the two `2.7.9` references in the round 10 entry both mean `2.7.6`**, which is the injected pre-exec-failure proof. The neighbouring 4.1.12 and 4.3.11 references resolve as written.
- resolution_notes: All five findings accepted; PR11-001 and PR11-004 with modified repairs. Three of the five are fixer-induced - PR11-001 and PR11-005 from round 10, PR11-004 from round 5 - and two are fresh defects in never-reviewed ground (PR11-002, PR11-003), so the review has not yet reached the all-fixer-induced stop condition. Repairs applied: two typed `add_targets` plus one `add_acceptance` (2.7, 4.1) through `apply_plan_review_repairs`; prose and new acceptance items 2.5.10 hand-applied, with 2.7's EOF paragraph, 4.1's coroutine sentence, and 6.2's send contract corrected and 6.2.10 rewritten. Round 11; the cap is extended to 20 by the user, who set convergence or an all-fixer-induced round as the stop condition.

```json plan-review-round
{"evidence_id":"2828cc2e-d722-4a58-9374-0f97736aeef2","plan_hash":"5827006305c31267b4eb39aefd2bfb33467f2b355fd181472796cc3b27ecdfc6","round_number":11,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"51aa0633e0847c9f4510a47c058c327029144c4639e59e1112221832ba812f8e","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":5,"total":6},"evidence_id":"2828cc2e-d722-4a58-9374-0f97736aeef2","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"5ce1fe04a2e828ce7aa06231486cd44b3bbee27977b3c37da8c4a53cba6e8463","status":"valid"},"source_digest":"650a7b47f177300f241231a0bb49e03bf92c193bd017b506878d4d34fdff29e9","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"PR10-003-exec-status-helper-death","causal_section_ids":["2.7"],"check_key":"edge-case-coverage","description":"The applied §2.7 sentences “the helper has no pre-exec exit path that is silent on fd 4” and “the row's ordinary exit path settles it either way” do not hold. A panic hook does not run for `abort()` or `SIGKILL`; EOF can therefore be accepted, and even `/bin/true` can emit `terminal_exited` on §2.9's event socket before the commit reply promotes the row. The existing exit transition ignores `pending`, so the later CAS can leave a dead process recorded `live`.","finding_id":"PR11-001-exit-before-promotion","fix":"Replace the absolute no-silent-exit claim with explicit fatal-signal/abort semantics. Serialize matching-attempt exit delivery and promotion under `settle_lock`, carrying generation/host identity so a pending matching attempt can settle `exited` (or return a typed early-exit result) and promotion observes that settlement. Add deterministic exit-before-promotion and promotion-before-exit cases to 2.7.6, each requiring the final row to be `exited` without a later sweep.","introduced_in_round":10,"location":"§2.7 exec-status EOF contract / §2.9 terminal-exited delivery","prevention":"Inject normal exit, panic, abort, SIGTERM, and SIGKILL before and after exec, then permute exit-event and commit-response ordering under the attempt-generation lock.","principle":"Process-exit delivery and commit promotion for one attempt must share a linearization boundary and converge to one terminal state in every ordering.","root_cause":"The Round 10 repair classifies fatal pre-exec death as an ordinary started-and-exited spawn without checking whether the separate exit event can arrive while the durable row is still pending.","section_id":"2.7","severity":"blocking"},{"category":"traceability","check_key":"config-derived-carrier-sweep","description":"Acceptance 2.7.4 requires configured `commit_deadline_ms` on every `spawn_commit`, yet `HostControlClient.spawn_commit` still emits the two-field request, `TerminalHostManager` calls that implementation without the deadline, and its fake plus exact-call tests preserve the old shape. None of those four files is owned by §2.7.","finding_id":"PR11-002-commit-deadline-second-client","fix":"Extend §2.7 to carry `TerminalHostConfig.commit_deadline_ms` through `HostControlClient` and `TerminalHostManager`, update the fake call record, and prove a non-default scalar on this second production path.","location":"§2.7 Targets and acceptance 2.7.4","prevention":"For every protocol signature or config carrier, sweep all implementations, callers, fakes, and exact-call tests before finalizing Targets.","principle":"A config-derived wire field must reach every production implementation, caller, fake, and exact-call assertion for that protocol verb.","repairs":[{"entries":["`src/gobby/terminals/host_control.py::*` — scope-reason: `HostControlClient.spawn_commit` gains the configured deadline","`src/gobby/terminals/host_manager.py::*` — scope-reason: the manager forwards `TerminalHostConfig.commit_deadline_ms` through `HostControlClient`","`tests/terminals/host_fakes.py::*` — scope-reason: the fake control client records the deadline field","`tests/terminals/test_host_manager.py::*` — scope-reason: manager tests assert a non-default deadline on the second production path"],"kind":"add_targets","section_id":"2.7"},{"items":[{"artifact":"test: `tests/terminals/test_host_manager.py::test_spawn_commit_forwards_configured_deadline`","prose":"`HostControlClient` and `TerminalHostManager` forward a non-default configured `commit_deadline_ms` on every `spawn_commit`, and the fake records the same scalar"}],"kind":"add_acceptance","section_id":"2.7"}],"root_cause":"The plan inventories only the `HostClient`/`NativeTerminalRuntime` path and misses the independent `HostControlClient` path used by `TerminalHostManager`.","section_id":"2.7","severity":"blocking"},{"category":"traceability","check_key":"targets-complete","description":"The applied sentence “The WebSocket handlers in `terminal_ws.py` already run in coroutines, so the call sites gain `await` and nothing else” is incomplete. `proxy_relay.py` calls `finalize` synchronously, while `test_terminal_ws_viewport.py`, `test_tmux_bridge_authority.py`, and `test_lease_authority.py` call the changing registry methods synchronously. Deferring three of those files to §4.3 leaves coroutine objects un-awaited when §4.1 must close with the terminal guard green.","finding_id":"PR11-003-async-lease-consumer-closure","fix":"Move all four caller adaptations into §4.1 and await the mutations there. The files shared with §4.3 remain safely ordered because §4.3 already depends on §4.1.","location":"§4.1 coroutine conversion and Targets","prevention":"Run literal caller sweeps for every re-signed method and map each consumer to the owning leaf before relying on accumulated guard tests.","principle":"A leaf that re-signs synchronous methods as async must migrate every caller and test before that leaf's own close gate runs.","repairs":[{"entries":["`src/gobby/servers/websocket/proxy_relay.py::*` — scope-reason: relay and socket-failure cleanup await re-signed registry finalization","`tests/servers/test_terminal_ws_viewport.py::*` — scope-reason: direct attachment fixtures await the re-signed registry methods","`tests/servers/test_tmux_bridge_authority.py::*` — scope-reason: bridge-authority fixtures migrate direct registry calls to the async contract","`tests/terminals/test_lease_authority.py::*` — scope-reason: direct attach and take-control tests become async and await registry mutations"],"kind":"add_targets","section_id":"4.1"}],"root_cause":"The consumer inventory assumes all changing calls live in `terminal_ws.py`; relay cleanup and direct registry tests also call the re-signed methods.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR5-010-livedaemon-send-coverage","causal_section_ids":["6.2"],"check_key":"wire-shape-golden-parity","description":"The applied acceptance “LiveDaemon send resolves ... terminal-input replies by request_id” names a field the protocol does not carry. `terminal_input` and `terminal_write_outcome` correlate by `(attachment_id, client_write_seq)`; the handler cannot echo a nonexistent `request_id`, so the §6.2 sender can remain pending until timeout or reconnect.","finding_id":"PR11-004-write-outcome-correlation","fix":"Use the least-change contract: keep the general command map keyed by `request_id`, add a separate pending-write map keyed by `(attachment_id, client_write_seq)`, and route `terminal_write_outcome` through it with atomic failure/clear on cancellation, EOF, close, and generation replacement. Rewrite 6.2.10 to assert that exact key. The larger alternative—adding `request_id` throughout §§4.3/4.5 codecs, handlers, and goldens—requires explicit ownership there.","introduced_in_round":5,"location":"§6.2 `LiveDaemon::send` body and acceptance 6.2.10","prevention":"Before assigning a reply to a shared pending map, compare the exact request and response wire shapes, handler construction, and goldens for that verb.","principle":"A multiplexed pending-map key must exist end to end in the request codec, handler response, committed goldens, and client dispatcher.","root_cause":"The Round 5 repair generalized command `request_id` routing to terminal writes without checking the distinct attachment/sequence correlation contract already fixed by §§4.3 and 4.5.","section_id":"6.2","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"PR10-003-exec-status-helper-death","causal_section_ids":["2.7","V1"],"check_key":"acceptance-id-integrity","description":"Both Round 10 references to `2.7.9` are dangling; §2.7 contains items 2.7.1 through 2.7.6, and the injected pre-exec-failure proof is 2.7.6. The neighboring references to 4.1.12 and 4.3.11 resolve.","finding_id":"PR11-005-round10-acceptance-id","fix":"Preserve the settled Round 10 text and append a Round 11 correction stating that both `2.7.9` references mean `2.7.6`.","introduced_in_round":10,"location":"V1 Round 10 summary and JSON fence","prevention":"Resolve every acceptance ID against the post-repair section before appending a changelog round.","principle":"Every changelog reference to an acceptance item must resolve in the reviewed artifact.","root_cause":"Round 10 recorded the new §2.7 acceptance as `2.7.9` although the applied section assigns it `2.7.6`.","section_id":"V1","severity":"nit"}],"reviewer_session":"#10995","round":11,"round_number":11,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

**Round 12** `kind: verification`

- reviewer_run: a3cdcf00-c45b-4efa-8683-fc547ca889b6
- reviewer_session: #11000
- verdict: needs_review
- findings:
- PR12-001-exit-attempt-linearization / blocking / unhandled-edge / 2.5 - round 11's pending-exit repair compares an exit against `(attempt_generation, attempt_started_at)`, which the exit event does not carry, and puts exit delivery under `settle_lock` without putting ordinary promotion there - accepted, modified. Both halves verified against the branch. `frame_client.py:256-263` decodes `terminal_exited` as `{host_terminal_id, exit_code}` and `emit_exit` (`crates/gterminal/src/host/embed.rs:632`) sends `slot.host_terminal_id`; nothing on the wire carries an attempt generation, so the round-11 guard is unimplementable as written. The host does hold the identity that matters - `Identity { terminal_id, spawn_key }` (`host/state.rs:29`) is on every slot alongside `host_terminal_id` - so the repair carries both on §2.9's event and drops the generation comparison. The pending row cannot resolve `host_terminal_id` today because `promote_to_live` (`storage/terminals.py:287`) writes the locator only at promotion; the prepared id therefore goes into the existing pending-row `process` metadata that `record_process` (`:627`) already writes from `spawn_executor._persist_and_bind`, which adds one JSONB key and no migration. The settlement becomes an identity-guarded CAS rather than a lock-held read-then-write: `pending -> exited` guarded on `process->>'host_terminal_id'`, falling through to the existing `live -> exited` guarded on `locator->>'host_terminal_id'`, mirroring `fail_pending_attempt`'s shape. Two single-statement CASes linearize in Postgres, so the exit path needs no in-process lock and the read-verify-CAS window the lock was meant to close does not exist. The adversary's second half is a real defect on its own and is repaired as it stands: ordinary `_promote_prepared` was missing from the `settle_lock` owners, so the reaper's observe -> terminate -> `fail_pending_attempt` span was never serialized against the promotion it races, and it joins the list. The transition is a new `settle_exit` method rather than a change to `mark_exited`, whose six other callers have no finding behind them. §2.9's reader calls it, and live delivery is proved in 2.9.2 while 2.5.10 stays storage-local.
- PR12-002-fd4-acceptance-residue / blocking / weak-testability / 2.7 - acceptance 2.7.6 still opens with round 10's absolute "No pre-exec failure of the gate helper is silent on fd 4" while the round-11 body names `abort()` and fatal signals as explicit residue - accepted. Verified: the sentence is verbatim in the artifact and its cited test covers only marshalling failure and an unwind panic, so the acceptance contradicts its own section. Narrowed to helper-controlled recoverable failures and unwind panics, with the abort and fatal-signal cases stated as commit-level EOF whose durable postcondition through the repaired §2.5/§2.9 path is `exited` and never `live`.
- PR12-003-proxy-finalization-window / blocking / unhandled-edge / 4.1 - round 11's "the proxy-relay callers gain `await` and nothing else" preserves a call site that awaits frame close before finalizing the lease - accepted. Verified at `proxy_relay.py:216-235`: `finalize_attachment` pops the record, cancels the task, `await`s `record.frame.close()`, and only then calls `self._owner._leases().finalize(...)`. Adding `await` alone leaves the lease valid across that yield while the output transport is already dismantled, so a concurrent `terminal_input` can take the per-terminal lock and dispatch. The sibling `_on_socket_fail` (`:260-263`) already finalizes first, so the repair is to make `finalize_attachment` match it: pop, await registry finalization, then cancel and close. No new mechanism, and the two cleanup paths stop disagreeing.
- PR12-004-write-map-attachment-retirement / blocking / unhandled-edge / 6.2 - the new `(attachment_id, client_write_seq)` map is failed on EOF, close, cancellation, and reconnect, none of which fires when 6.3's direct-to-proxy fallback finalizes an attachment on a live socket - accepted. Verified: §6.3 tombstones the old id in the workspace, but that tombstone sits above `LiveDaemon`, so a pending write for the finalized attachment parks until the next reconnect when the daemon finalizes without answering. Attachment finalization becomes a pending-map fence in §6.2: before publishing finalization or installing a replacement, every entry for the old id is failed and removed and the id is retired for the current socket generation so a later outcome is dropped before sender lookup. 6.2.10 gains the same-socket fallback cases for a missing outcome and a late one.
- PR12-005-restart-after-stop / blocking / unhandled-edge / 2.4 - `ensure_restart()` is guarded only by the task slot and the generation, so an observer arriving after `stop()` clears the cancelled task can mint and publish a new generation during or after teardown - accepted. Verified: `host_manager.py` already carries `_stop_requested` (`:78`, `:128`, set at `:150` in `stop`), and the plan's own text says the completion callback clears `_restart_task` under the lock, which is exactly the slot a post-stop observer finds empty. 2.9's event reader is one such observer: it sees EOF as the host dies during shutdown and calls `ensure_restart()`. The generation bump invalidates the in-flight publication, not a newly minted task. Repair: bring the existing `_stop_requested` under the restart lock, set it before cancellation, refuse `ensure_restart()` while stopping or stopped, and require publication to agree with both the generation and the lifecycle state. 2.4.9 gains the after-cancellation and after-drain observers.
- PR12-006-storage-test-target / blocking / traceability / 2.5 - acceptances 2.5.9 and 2.5.10 both cite tests in `tests/storage/test_terminals.py`, which §2.5 does not target - accepted, typed repair applied verbatim. Verified: the only Target-block owner of that file is §4.2 (`@1977`), and §4.2 depends on 4.1 which depends on P2, so the shared-target edge is already ordered. §4.2 declares it as a bare path, which is the correct normalization for a branch-only file, so §2.5 declares it the same way.
- PR12-007-review-round-count / blocking / gobby-format / Constraints - the Constraints build invocation still hard-codes `--completed-plan-review-rounds 1` - accepted. Verified: line 43 carries the literal while Task Mapping's invocation (line 3736) correctly uses `<N>`. Replaced with `<N>` and an explicit statement that handoff substitutes the finalized V1 round count, leaving Task Mapping as the authoritative command.
- PR12-008-async-caller-proof / blocking / weak-testability / 4.1 - round 11 added four caller Targets and prose after §4.1's acceptance block was complete, so the sync-to-async migration has no acceptance and guard set G omits one of the direct test modules - accepted, modified. Verified: §4.1 carries 4.1.1 through 4.1.12 with no item covering the migration, and guard set G group 2 names seven `tests/servers/*` files, none of them `test_tmux_bridge_authority.py`. The typed repair is applied as written, but it is incomplete alone: an acceptance whose test lives in a file the close gate never runs proves nothing. Guard set G group 2 therefore also gains `tests/servers/test_tmux_bridge_authority.py`, which leaf 1.1 mirrors into the guide by 1.1.9 without a new Target.
- resolution_notes: All eight findings accepted; PR12-001 and PR12-008 with modified repairs. Five of the eight are fixer-induced - PR12-001, PR12-002, PR12-003, and PR12-008 from round 11, PR12-004 from round 11's own repair of a round-5 defect - and three are fresh defects in never-reviewed ground (PR12-005 in §2.4's singleflight restart, PR12-006 in §2.5's Targets, PR12-007 in Constraints), so the all-fixer-induced stop condition is not met. Repairs applied: one typed `add_targets` on 2.5 and one typed `add_acceptance` on 4.1 through `apply_plan_review_repairs`; everything else hand-applied - §2.5's exit-identity and lock-owner prose with 2.5.10 rewritten, §2.9's event carrier and 2.9.2, §2.7.6 narrowed, §4.1's relay-ordering prose, §2.4's stop-state guard with 2.4.9 extended, §6.2's pending-map fence with 6.2.10 extended, the Constraints build invocation, and guard set G group 2. Round 12 of a cap of 20.

```json plan-review-round
{"evidence_id":"d8e4683b-238b-4964-8974-09acef3a3ed1","plan_hash":"401328f00bf1dc49323e951877ac7ad5fda440c7d714fe7f6ce7fa9cd17de046","round_number":12,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"8f3a0bdeea80fed3cc08d81b27dd831e9369049de20bf9b23462ba24232d4405","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":8,"total":14},"evidence_id":"d8e4683b-238b-4964-8974-09acef3a3ed1","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"02d7104c633230739a5add758e6361dba5b2f85658c11aec2224ac903349a362","status":"valid"},"source_digest":"da16e4cd1bbc05d729e80c96e6367d1f5c480d9354d0ec8a16f2d7e9e9928ca7","version":1},"evidence_id":"d8e4683b-238b-4964-8974-09acef3a3ed1","findings":[{"category":"unhandled-edge","causal_finding_id":"PR11-001-exit-before-promotion","causal_section_ids":["2.5","2.7"],"check_key":"edge-case-coverage","description":"The applied §2.5 text says an exit whose `(attempt_generation, attempt_started_at)` matches the row settles under `settle_lock`, yet `terminal_exited` has no producer for that pair and §2.9 still says its reader calls `mark_exited` directly. A late exit from attempt A can therefore be compared with attempt B's current row. The lock also does not linearize the claimed race: the enumerated lock owners omit ordinary `_promote_prepared`, so promotion can CAS `pending → live` while exit holds the lock and then make `fail_pending_attempt` miss.","finding_id":"PR12-001-exit-attempt-linearization","fix":"Use the existing pending-row process metadata to persist the prepared `host_terminal_id`, carry that identity on `terminal_exited`, and compare it with the current attempt under `settle_lock`. Make both the generation-aware exit transition and ordinary promotion acquire that lock exactly once; have §2.9 call the new transition, release it before awaiting §4.1 lease cleanup, and add stale-A-exit-during-B plus both exit/promotion schedules. Keep §2.5 acceptance storage-local and prove live delivery in §2.9.2.","introduced_in_round":11,"location":"§2.5 pending-exit repair / §2.9 event reader and ordinary promotion","prevention":"For every attempt transition, trace the identity from producer through wire event to durable row, then enumerate every writer that must share its lock.","principle":"Exit settlement and promotion for one attempt require one carried identity and one common linearization boundary.","root_cause":"The Round-11 repair compares an exit against attempt fields the event does not carry and adds the settle lock to exit delivery without adding ordinary promotion to the same lock.","section_id":"2.5","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"PR11-001-exit-before-promotion","causal_section_ids":["2.5","2.7"],"check_key":"acceptance-observability","description":"Acceptance 2.7.6 still says, verbatim, “No pre-exec failure of the gate helper is silent on fd 4, so none is mistaken for a successful commit.” The repaired body now explicitly admits that `abort()` and fatal signals are silent and present as bare EOF. Its cited test covers only marshalling failure and an unwind panic, so the acceptance contradicts the applied body and remains unsatisfiable as written.","finding_id":"PR12-002-fd4-acceptance-residue","fix":"Narrow 2.7.6 to helper-controlled recoverable failures and unwind panics. Add explicit abort and fatal-signal cases whose commit-level observation may be EOF while the durable postcondition, through the repaired §2.5/§2.9 path, is `exited` and never `live`.","introduced_in_round":11,"location":"§2.7 acceptance 2.7.6","prevention":"After narrowing a behavior, diff every acceptance item and derived validation criterion for the old absolute claim.","principle":"Acceptance cannot promise an outcome the governing behavioral contract explicitly excludes.","root_cause":"Round 11 narrowed the body but left the absolute Round-10 acceptance sentence intact.","section_id":"2.7","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR11-003-async-lease-consumer-closure","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"`ProxyHub.finalize_attachment` currently removes the relay record, cancels its task, and awaits the frame's `close()` before calling `TerminalLeaseRegistry.finalize`. Adding only `await` at that existing call leaves the lease valid during the close await. A concurrent `terminal_input` can acquire the per-terminal lock and dispatch after its output transport has already been dismantled.","finding_id":"PR12-003-proxy-finalization-window","fix":"Snapshot and remove the relay record synchronously, then await registry finalization immediately so it queues behind an existing dispatch and revokes authority before any later writer. Cancel and close the frame only after that lock-held mutation returns. Add a test that pauses frame close, races `terminal_input`, and proves no post-finalization dispatch.","introduced_in_round":11,"location":"§4.1 async lease mutation migration / `ProxyHub.finalize_attachment`","prevention":"When re-signing a cleanup mutation as async, inspect every await before and after the call and race authority checks at each yield.","principle":"Transport teardown must revoke attachment authority before yielding to unrelated asynchronous cleanup.","root_cause":"The Round-11 repair says the proxy-relay callers gain `await` and nothing else, preserving a call site that currently awaits frame close before lease finalization.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR11-004-write-outcome-correlation","causal_section_ids":["6.2"],"check_key":"edge-case-coverage","description":"The new pending-write map is failed and cleared on cancellation, EOF, close, and reconnect, but the plan defines no lifetime boundary for same-socket attachment finalization or replacement. Direct-to-proxy fallback finalizes and tombstones the old attachment while keeping the socket; an absent outcome can strand its sender until a later reconnect, and a late old outcome reaches LiveDaemon's sender lookup before the workspace tombstone can discard it.","finding_id":"PR12-004-write-map-attachment-retirement","fix":"Make attachment finalization a LiveDaemon pending-map fence. Before publishing finalization or installing a replacement, atomically fail and remove every entry for the old attachment, mark that id retired for the current socket generation, and drop later outcomes before sender lookup. Extend 6.2.10 with same-socket fallback cases for both a missing outcome and a late outcome from the finalized attachment.","introduced_in_round":11,"location":"§6.2 `(attachment_id, client_write_seq)` pending-write map / §6.3 same-socket fallback","prevention":"For each pending map, cross-product request cancellation, identity finalization, same-connection replacement, reconnect, EOF, close, and late reply.","principle":"A pending operation must be retired when the identity authorizing it is finalized, even while its transport generation remains live.","root_cause":"The Round-11 map repair inventories connection-wide cleanup but omits attachment-scoped finalization and replacement.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"`stop()` bumps the restart generation, cancels and drains the current task, then tears down. `ensure_restart()` is only guarded by the task slot and generation; after the cancelled task clears itself, an event-reader or request-path observer can create a newer task and publish a client while teardown is still running or after the manager is stopped. Acceptance 2.4.9 covers only the task already in backoff.","finding_id":"PR12-005-restart-after-stop","fix":"Protect the existing `_stop_requested` state with the restart lock. Set it before cancellation, refuse `ensure_restart()` while stopping or stopped, and require publication to match both generation and lifecycle state. Extend 2.4.9 with observers entering after cancellation but before drain and after drain but before teardown completes.","location":"§2.4 singleflight `ensure_restart` / `stop()`","prevention":"Race every restart observer before cancellation, after task cleanup, and after drain; require the lifecycle state and generation to agree at creation and publication.","principle":"Once shutdown starts, no asynchronous observer may mint or publish a new runtime generation until an explicit start.","root_cause":"Generation invalidation fences the current restart task, while the restart entry point has no stopping or stopped-state guard.","section_id":"2.4","severity":"blocking"},{"category":"traceability","check_key":"targets-complete","description":"Acceptances 2.5.9 and 2.5.10 both add tests in `tests/storage/test_terminals.py`, but §2.5 does not target that file. It exists only on the source worktree, so the current normalization rule requires a bare-path Target. Section 4.2's later ownership is already ordered through its P2 dependency.","finding_id":"PR12-006-storage-test-target","fix":"Add `tests/storage/test_terminals.py` as a bare-path Target in §2.5.","location":"§2.5 Targets and acceptances 2.5.9–2.5.10","prevention":"For every new acceptance artifact, verify the cited file is in the same section's Targets and remains ordered against every other owner.","principle":"Every proof file changed by a deliverable must be declared in that deliverable's Targets.","repairs":[{"entries":["`tests/storage/test_terminals.py`"],"kind":"add_targets","section_id":"2.5"}],"root_cause":"The lock-cell and pending-exit acceptances were added after the original target inventory and their shared storage test file was never added.","section_id":"2.5","severity":"blocking"},{"category":"gobby-format","check_key":"completed-review-round-count","description":"The governing Constraints command still hard-codes `--completed-plan-review-rounds 1`. V1 already records eleven finalized rounds before this review, and Task Mapping correctly uses the final `<N>`. Following the Constraints command would seed build state with stale review history.","finding_id":"PR12-007-review-round-count","fix":"Replace the literal `1` with `<N>` and state that handoff substitutes the finalized V1 round count, or delete the duplicate command and point to Task Mapping's authoritative invocation.","location":"Constraints / Build invocation","prevention":"Before approval, compare every `--completed-plan-review-rounds` invocation with finalized V1 history and keep one authoritative handoff command.","principle":"The build handoff must carry the finalized adversarial-review count recorded by the plan's own changelog.","root_cause":"The original one-round literal was never updated when the review loop continued, while Task Mapping was later generalized to `<N>`.","section_id":"Constraints","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"PR11-003-async-lease-consumer-closure","causal_section_ids":["4.1"],"check_key":"acceptance-observability","description":"The new body requires both proxy-relay call sites and three direct test modules to await the re-signed lease mutations, yet §4.1 has no acceptance item for that migration. Guard set G runs `tests/terminals` and selected server tests, but not `tests/servers/test_tmux_bridge_authority.py`, so an un-awaited direct caller can survive the leaf close.","finding_id":"PR12-008-async-caller-proof","fix":"Add a focused acceptance proving both relay cleanup paths and all three direct test callers await the mutations with no un-awaited-coroutine warning; include the omitted bridge-authority suite in that proof.","introduced_in_round":11,"location":"§4.1 Round-11 async caller migration and close gate","prevention":"After every sync-to-async re-signing, map all production and test callers to a focused acceptance and verify the close command runs each one.","principle":"A signature migration is closeable only when every direct caller is exercised by stable leaf-local acceptance.","repairs":[{"items":[{"artifact":"test: `tests/servers/test_tmux_bridge_authority.py::test_async_lease_mutation_callers_are_awaited`","prose":"Both proxy-relay cleanup paths and every direct test caller await the re-signed TerminalLeaseRegistry mutations, and the focused suites complete with no un-awaited-coroutine warning."}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"Round 11 added four caller Targets and prose after the acceptance block was complete; guard set G omits one of the direct server test modules.","section_id":"4.1","severity":"blocking"}],"plan_hash":"401328f00bf1dc49323e951877ac7ad5fda440c7d714fe7f6ce7fa9cd17de046","reviewer_session":"#11000","round":12,"round_number":12,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

**Round 13** `kind: verification`

- reviewer_run: 649f19ff-df64-4128-91af-2c0f936852d9
- reviewer_session: #11008
- verdict: needs_review
- findings:
- PR13-001 / blocking / 2.5 attempt-scoped process identity has no lifecycle across generation bumps and is erased by the other `record_process` writers — accepted; `bump_attempt_generation` clears `process` in the same UPDATE, and `host_manager.handle_spawn_prepared` plus `host_reconcile` carry `host_terminal_id` on every write
- PR13-002 / blocking / 2.4 `ensure_restart()` refusal makes the return type a union under `asyncio.shield` with no production-caller migration — accepted; the refusal becomes a raised `HostManagerStopped`, the three observers consume it as normal shutdown, and 2.4.9 gains the production-caller races
- PR13-003 / blocking / 2.9 Targets list every generated control fixture but omit their generator — accepted; `crates/gterminal/tests/wire_golden.rs` added as a Target with the regeneration duty named
- PR13-004 / blocking / 2.7.6 asserts a daemon-side row transition from a Rust host test — accepted; 2.7.6 narrows to fd-4 and host commit-boundary observations, and new 2.9.13 proves the row settlement for abort, SIGKILL, and `/bin/true` through the real host plus daemon
- PR13-005 / blocking / 6.3 and 9.1 wait on `terminal_detached`, an event nothing emits — accepted; the lifecycle event is `terminal_attachment_finalized` throughout, it fences the pending-write map before fan-out, and a same-socket case is added
- PR13-006 / blocking / ordinary promotion's `settle_lock` span is unspecified and the timeout callback would self-deadlock — accepted; the remote bind and commit complete unlocked, the lock is taken once around re-read, CAS, and conflict cleanup, the global lock order is stated, and 2.5.11 gains the paused-commit and timeout-callback schedules
- PR13-007 / nit / `spawn_key` on the exit event and the tmux `pane_id` fallback in `settle_exit` have no consumer — accepted (modified); both carriers removed, and the suggested extra tmux fast-exit test declined because the section already proves tmux has no `pending`-across-remote-await window
- PR13-008 / nit / the per-generation retired-attachment-id set duplicates the pending-map fence — accepted; the set is removed and late outcomes fall through the existing unmatched-key drop
- resolution_notes: Eight findings, all accepted (PR13-007 with a modified fix). Six are fixer-induced defects in text rounds 10 through 12 wrote; PR13-003 and PR13-005 are fresh, so the all-fixer-induced stop condition is not met. Round 13 of a cap of 20. Typed repairs: PR13-003's `add_targets`. Everything else is hand-applied prose.

```json plan-review-round
{"evidence_id":"a0466f50-12f4-4130-adf1-3181add894c1","plan_hash":"d9dbe759ff47d66ef06879454dc4616207e6721800e4220365670f45e3041d06","round_number":13,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"b3a5f1d1acbc55841b4a79db20d8411ac5e37344118b2e03dd27a6d2e7c7eb15","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":8,"total":10},"evidence_id":"a0466f50-12f4-4130-adf1-3181add894c1","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"3aff448f479c861ed97af517e31bd87bb0b2f06fbd1fffb9549c379d4ecea687","status":"valid"},"source_digest":"0d2f0474169890732ac92f566057c457950f9140921a2431c1c01d92581bb460","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"PR12-001-exit-attempt-linearization","causal_section_ids":["2.5","2.9"],"check_key":"edge-case-coverage","description":"After attempt B bumps the row, process.host_terminal_id still names attempt A until B finishes prepare, so a delayed A exit satisfies settle_exit's pending guard and exits B. The inverse failure also exists: record_process replaces the whole JSONB object, while TerminalHostManager.handle_spawn_prepared and reconciliation write pgid/start_time without host_terminal_id, so a current attempt's valid discriminator can be erased and its pending exit dropped.","finding_id":"PR13-001-process-identity-lifecycle","fix":"Make bump_attempt_generation clear the prior process identity in the same UPDATE; make record_process an attempt-generation/started-at-guarded JSONB merge or require the full identity at every writer; migrate handle_spawn_prepared, reconciliation, fakes, and exact-shape tests. Extend 2.5.10 with stale-A-after-B-bump and secondary-writer-before-exit schedules, and require neither stale retention nor identity erasure.","introduced_in_round":12,"location":"§2.5 retry generation, process JSONB carrier, and acceptance 2.5.10","prevention":"For every attempt-scoped carrier, inventory creation, retry reset, every writer and round trip, late predecessor completion, and stale-event delivery before accepting the guard.","principle":"An attempt-generation boundary must invalidate every attempt-scoped identity atomically, and every later writer must preserve the current discriminator.","root_cause":"Round 12 placed host_terminal_id in a row-level JSONB object without defining its lifecycle across generation bumps or the replacing record_process writers outside _persist_and_bind.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR12-005-restart-after-stop","causal_section_ids":["2.4"],"check_key":"edge-case-coverage","description":"The plan still says callers pass ensure_restart's shared Task to asyncio.shield, yet the new stopped path returns host_manager_stopped. It never defines the refusal type or how the health loop, request-path observer, and event reader avoid shielding a non-task, treating refusal as a published restart, or continuing their retry loop. Acceptance 2.4.9 exercises direct calls only.","finding_id":"PR13-002-restart-refusal-api","fix":"Keep the return type non-union: raise a dedicated HostManagerStopped error before any Task is returned. Specify the health loop, request observer, and 2.9 event reader consume it as normal shutdown, publish no client or epoch, and terminate their retry path. Add production-caller stopping/stopped races to 2.4.9 and 2.9.9.","introduced_in_round":12,"location":"§2.4 ensure_restart stopped branch and §2.9 production observers","prevention":"After adding an async refusal, sweep health, request, background-reader, and direct callers; test each before stop, during drain, and after stop.","principle":"Every changed async result contract needs one concrete type and an explicit terminal branch at every caller.","root_cause":"Round 12 added a stopped-state refusal to an API otherwise described as returning a shared Task, without defining whether the refusal is raised or returned or migrating its production callers.","section_id":"2.4","severity":"blocking"},{"category":"traceability","check_key":"targets-complete","description":"Section 2.9 promises to regenerate every control_*.json fixture and changes control_event_terminal_exited.json, but crates/gterminal/tests/wire_golden.rs—the branch source that writes that entire corpus—is absent from Targets. A leaf receiving only §2.9 cannot implement the promised regeneration from its inventory.","finding_id":"PR13-003-control-golden-generator","fix":"Add crates/gterminal/tests/wire_golden.rs as a bare-path Target and update its control-corpus writer for request ids and the terminal-exited event; keep the existing regeneration test authoritative over the checked-in fixtures.","location":"§2.9 Targets for the id-bearing control corpus and terminal-exited event fixture","prevention":"For every generated fixture Target, trace the writer and add the generator to the same ordered deliverable before review.","principle":"A generated wire-shape change must target its generator alongside generated fixtures, codecs, and decoders.","repairs":[{"entries":["`crates/gterminal/tests/wire_golden.rs`"],"kind":"add_targets","section_id":"2.9"}],"root_cause":"The Targets inventory lists every control JSON fixture and its consumers while omitting the Rust test source that writes those fixtures.","section_id":"2.9","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"PR12-002-fd4-acceptance-residue","causal_section_ids":["2.7"],"check_key":"acceptance-observability","description":"Acceptance 2.7.6 cites crates/gterminal/tests/host_lifecycle.rs while requiring abort(), SIGKILL, and /bin/true to leave the Python TerminalManager row exited through §§2.5/2.9. A Rust host test cannot observe that daemon-side row. Acceptance 2.9.2 covers /bin/true and stale host identity, but it does not claim the abort and SIGKILL residue cases.","finding_id":"PR13-004-preexec-row-proof","fix":"Narrow 2.7.6 to fd-4 and host commit-boundary observations. Extend 2.9.2, or add a 2.9 acceptance in tests/terminals/test_runtime_contract.py, that drives abort(), SIGKILL, and /bin/true through the real host plus daemon and asserts exited without a reaper for all three.","introduced_in_round":12,"location":"§2.7 acceptance 2.7.6 and §2.9 live-delivery proof","prevention":"For every cross-language acceptance, map each asserted postcondition to a test process that runs all participating layers; move downstream state assertions to the downstream owner.","principle":"An acceptance artifact must execute the layer whose durable postcondition it claims.","root_cause":"Round 12 narrowed the host-side EOF contract yet left abort and fatal-signal durable row settlement inside a Rust-only acceptance artifact.","section_id":"2.7","severity":"blocking"},{"category":"unhandled-edge","check_key":"wire-shape-golden-parity","description":"Sections 6.3 and 9.1 wait for and assert terminal_detached, an event the plan never otherwise defines. The branch proxy and committed attachment-finalization golden use terminal_attachment_finalized. A lifecycle-only detach therefore does not fence LiveDaemon's pending writes or advance Detaching, forcing an unnecessary socket-generation reset, and 9.1.5 asserts an event that is never emitted.","finding_id":"PR13-005-finalization-event-name","fix":"Use terminal_attachment_finalized consistently in §§6.2, 6.3, and 9.1. Make LiveDaemon fail/remove pending writes before fan-out of that daemon-published event, then add a same-socket case where terminal_detach_result is suppressed and terminal_attachment_finalized alone advances directly to the proxy attach.","location":"§§6.2–6.3 Detaching lifecycle and §9.1.5 browser-disconnect oracle","prevention":"Before naming a lifecycle transition, compare handler emission, relay emission, committed golden, every decoder, and every state-machine wait on the exact type string.","principle":"A lifecycle state machine must consume the exact event name emitted by the daemon's committed wire contract.","root_cause":"The client plan names terminal_detached, while the branch proxy and attachment-finalization golden publish terminal_attachment_finalized.","section_id":"6.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR12-001-exit-attempt-linearization","causal_section_ids":["2.5","2.9"],"check_key":"edge-case-coverage","description":"The plan permits whole-method _promote_prepared locking across bind_observer, HostClient's write lock, commit reply, and conflict cleanup, which can hold the per-terminal lock for commit_deadline_ms and block exit/retry/reaper. It also permits the timeout callback to acquire settle_lock and call _promote_prepared, whose new acquisition would deadlock. Acceptance 2.5.11 covers only reaper versus ordinary promotion.","finding_id":"PR13-006-promotion-lock-boundary","fix":"Complete remote bind and commit-reply handling before acquiring settle_lock exactly once for the generation re-read, promotion CAS, and captured-resource conflict cleanup. Make the timeout callback delegate without pre-acquiring, or extract a named helper whose locked/unlocked contract is explicit. State ordering against restart, host-client write, and lease locks, and add paused-commit plus timeout-callback schedules.","introduced_in_round":12,"location":"§2.5 _promote_prepared settle_lock ownership and acceptance 2.5.11","prevention":"For every new lock owner, list acquisition/release lines, nested helpers, awaited remote operations, and all other locks acquired in the span; test each callback entry separately.","principle":"A non-reentrant transition lock needs one named owner, an exact await boundary, and a global order relative to every other lock.","root_cause":"Round 12 added ordinary promotion to the settle_lock owner list without specifying whether _promote_prepared or its timeout callback acquires it and which awaits lie inside.","section_id":"2.5","severity":"blocking"},{"category":"over-engineering","causal_finding_id":"PR12-001-exit-attempt-linearization","causal_section_ids":["2.5","2.9"],"check_key":"mechanism-justification","description":"The event carries spawn_key although settle_exit consumes only terminal_id and host_terminal_id. The same method adds a tmux pane_id fallback even though the specified control-event path is native and tmux observer exits use a separate host identity; no matching tmux caller is named. These additions add wire and storage ceremony without capability.","finding_id":"PR13-007-unused-tmux-exit-carriers","fix":"Use the simpler native-only control event carrying terminal_id, host_terminal_id, and exit_code; remove spawn_key and the pane_id fallback from settle_exit. Keep tmux exit recovery on its existing observer/liveness path and add a fast-exit test across create_session/display-message.","introduced_in_round":12,"location":"§2.5 settle_exit live fallback and §2.9 terminal_exited carrier","prevention":"For each new wire field and polymorphic branch, name the exact current caller and prove its identity domain matches before retaining it.","principle":"Wire fields and backend branches need a concrete consumer in the reviewed plan.","root_cause":"Round 12 generalized a native pending-exit repair to identity fields and a tmux locator branch that the specified settlement caller does not consume.","section_id":"2.5","severity":"nit"},{"category":"over-engineering","causal_finding_id":"PR12-004-write-map-attachment-retirement","causal_section_ids":["6.2"],"check_key":"mechanism-justification","description":"Finalization already fails and removes every pending entry for the old attachment_id, so a late outcome misses the existing sender lookup. Section 6.3 requires the replacement attachment_id to differ, eliminating cross-resolution. The retired-id set changes no outcome and grows once per finalized attachment until reconnect.","finding_id":"PR13-008-retired-id-set","fix":"Remove the retired-id set. Keep the attachment-scoped fence, let the existing unmatched-key path drop late outcomes, and retain 6.2.10's missing- and late-outcome cases against that direct form.","introduced_in_round":12,"location":"§6.2 per-socket-generation retired attachment-id set","prevention":"After adding an identity fence, replay the late message through the existing lookup before introducing a second registry; bound every per-connection collection.","principle":"A new stateful set needs a distinct consumer-visible outcome and a bound.","root_cause":"Round 12 retained both an attachment-scoped pending-map fence and a second retirement registry for the same late-outcome behavior.","section_id":"6.2","severity":"nit"}],"reviewer_session":"#11008","round":13,"round_number":13,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

**Round 14** `kind: verification`

- reviewer_run: e84c6080-6e58-4d3b-ad94-6bb0d804ed34
- reviewer_session: #11012
- verdict: needs_review
- findings:
- PR14-001-web-promotion-settlement / blocking / unhandled-edge / 2.5 - the web-terminal spawn path promotes outside the new `_settle_promotion` helper and never persists the pending attempt identity - accepted. Verified on the branch: `src/gobby/terminals/web_spawn.py::spawn_web_terminal` calls `manager.promote_to_live` directly at line 119 and performs its own `lost_cas_conflict` kill at 128-132, with no `settle_lock` anywhere in the module, and it never calls `record_process`, so a web-created native row carries no `process->>'host_terminal_id'` for `settle_exit`'s pending guard. `_handle_terminal_create` (`servers/websocket/terminal_ws.py:187`) is a production ingress, so this is a fifth promoting path that races the B-5 reaper exactly as `_promote_prepared` does. Repaired by extending §2.5's ownership rather than adding a second mechanism: `src/gobby/terminals/web_spawn.py` joins Targets, `spawn_web_terminal` persists the prepared `host_terminal_id` through the same `record_process` call and reaches promotion through the same `_settle_promotion` helper, and 2.5.11 gains the web-spawn schedule.
- PR14-002-process-cleanup-erased / blocking / unhandled-edge / 2.5 - fixer-induced (round 13, PR13-001-process-identity-lifecycle) - clearing the whole `process` object on `bump_attempt_generation` destroys the predecessor's host-death cleanup record - accepted, modified. Verified: `TerminalManager.list_live_by_machine` selects `state IN ('pending','live')` and `TerminalHostManager.handle_host_death` reaps process groups solely from `row.process` (`host_manager.py:202-214`), so between B's bump and B's own `record_process` a host death now finds nothing to reap and attempt A's process group survives. The adversary's fix (terminate and confirm the predecessor under `settle_lock` before bumping) would delete the captured-resource design 2.5.4 pins, so the repair is narrower and closes the same hole: `bump_attempt_generation` deletes only the discriminator (`SET process = process - 'host_terminal_id'`) instead of nulling the object, which leaves `{pgid, start_time}` available to `handle_host_death` while an exit landing in the gap still matches no guard. 2.5.12 is extended with the host-death-in-the-gap case.
- PR14-003-tmux-exit-routing / blocking / unhandled-edge / 2.9 - fixer-induced (round 13, PR13-007-unused-tmux-exit-carriers) - a tmux pane death routes a non-UUID identity into `settle_exit` - accepted. Verified on the branch: `crates/gterminal/src/host/embed.rs:407` calls `emit_exit` from the tmux poll loop on `PollClass::ConfirmedAbsence`, and `attach_tmux` builds the observer slot with `Identity { terminal_id: locator.locator_key() }` (`embed.rs:119-120`), so §2.9's E-5 push would carry a `tmux:<socket>:<pane>` string into the daemon reader's `settle_exit(terminal_id, ...)` and its `UUID()` parse, which also falsifies §2.5's claim that tmux never reaches `settle_exit`. Repaired at the source with one condition rather than a daemon-side filter: the control-plane push in `emit_exit` fires only for native slots (`slot.locator.is_none()`), tmux observer exits keep the existing frame-level `TerminalExited` fan-out and `reap_observer`, `crates/gterminal/src/host/embed.rs` and `crates/gterminal/tests/embed.rs` join §2.9 Targets, and new 2.9.15 pins it.
- PR14-004-gclient-e2e-close-gate / blocking / weak-testability / 6.3 - the leaf authors an E2E test its close gate never runs - accepted. Verified: guard set G groups 1 and 2 enumerate Python test files explicitly and neither names `tests/e2e/test_terminal_client_stack.py`; group 4 is `cargo nextest`, and 6.3's only extra clause is `cargo run -p gobby-client -- --help`. Ownership stays where the plan put it (6.3 authors the test, 9.1 owns the rest of the file), so the repair is one close-gate clause: a focused protected pytest invocation of `test_gclient_reaches_workspace` against rebuilt binaries.
- PR14-005-pending-native-termination / blocking / unhandled-edge / 2.5 - the reaper terminates a pending native row through a runtime call that cannot resolve its identity - accepted. Verified: `NativeTerminalRuntime.terminate` resolves the kill target through `_host_id` (`native_runtime.py:109-114`), which reads `terminal.locator['host_terminal_id']` and raises `TerminalWriteError(stage="none")` when the locator is absent; a pending row has no locator, and `is_live` still matches such a row by `terminal_id`/`spawn_key`, so B-5's stale-and-found branch is reachable and then raises. Repaired by reusing the captured-resource kill the timeout callback already owns: for a native pending row the reaper reads the current generation's `process->>'host_terminal_id'` under `settle_lock` and kills that id, and 2.5.5 exercises the real `NativeTerminalRuntime`.
- PR14-006-preexec-test-mask / blocking / weak-testability / 2.9 - fixer-induced (round 13, PR13-004-preexec-row-proof) - disabling only the stale-pending reaper leaves health-loop reconciliation able to satisfy 2.9.13's oracle - accepted. Verified: `TerminalHostManager._health_loop` calls `self.reconcile()` on every interval and `reconcile_host_inventory` marks a listed-absent row exited, so a broken event push still converges. Repaired by pausing both recovery paths for the tested terminal and asserting the row is `exited` before either is re-enabled, then re-enabling and asserting convergence still holds.
- PR14-007-never-live-contradiction / blocking / traceability / 2.9 - fixer-induced (round 13, PR13-004-preexec-row-proof) - 2.9.13's never-`live` oracle contradicts 2.5.10's promotion-first schedule - accepted. Verified against the plan text: 2.5.10 explicitly permits promotion first (`pending -> live` then `live -> exited`), so a transient `live` is a valid history for the same targets 2.9.13 forbids it for. Repaired by restating 2.9.13's oracle as a final state: each row ends `exited` with no `live` row surviving exit delivery, and the promotion-first schedule may pass through `live` transiently.
- PR14-008-attachment-result-correlation / blocking / unhandled-edge / 6.2 - the control reply 6.3 waits on has no correlation path in the data plane - accepted. Verified on the branch: `_handle_terminal_take_control` and `_handle_terminal_release_control` both emit `terminal_control_result` carrying `attachment_id`, `granted`, `reason`, and `lease_generation` and no `request_id` (`terminal_ws.py:343-364`), so §6.2's `request_id` map cannot settle it and 6.3.8's queue-once keystroke has no sender. Repaired with the smallest correlation the wire supports: an attachment-scoped single-flight control waiter that the reader routes `terminal_control_result` through, failed and removed on finalization, EOF, close, cancellation, reconnect, and generation replacement under the same rule the write map already carries; every other attachment-routed reply gclient does not consume is named as unconsumed rather than given a map. 6.2.10 is extended.
- PR14-009-reconnect-double-attach / blocking / unhandled-edge / 6.3 - reconnect reattachment and Detaching recovery both issue the post-generation attach - accepted. Verified against the plan text: 6.2.2 has the client attach every shown pane afresh after a reconnect while 6.3.7 has the Detaching pane issue its own fresh proxy attach once the new generation has re-listed, so one pane can mint two attachments. Repaired by naming one owner and removing the duplicate rather than adding arbitration: `LiveDaemon` owns socket replacement, subscribe-first reconciliation, and a generation-ready signal and issues no attach; `Workspace` owns shown panes and issues exactly one attach per pane on generation-ready. 6.2.2, 6.3.7, and 6.3.9 are rewritten around that boundary and 6.3.10 keeps the distinct same-socket path.
- resolution_notes: All nine findings accepted; eight applied as written or narrowed, one (PR14-002) applied with a smaller repair that preserves the captured-resource design 2.5.4 pins. Four are fixer-induced by round 13 (PR14-002, PR14-003, PR14-006, PR14-007), so the round does not meet the all-fixer-induced stop condition. No typed repairs rode on any finding; every fix is prose applied by hand. Plan hash unchanged at 66a2b50d366843b6662009c3893819448226a1333e2294f7c90a78b5cbdd2e4a across all four round-14 evidence rows, three of which were expired for prompt-delivery failures before this reviewed round.

```json plan-review-round
{"evidence_id":"dbc11177-c846-4e0c-8011-3c155cd0acdd","plan_hash":"66a2b50d366843b6662009c3893819448226a1333e2294f7c90a78b5cbdd2e4a","round_number":14,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"dd159842c8b6b642940ebee39103986468193f261371ac4555acd64b37fd3d9a","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":9,"total":12},"evidence_id":"dbc11177-c846-4e0c-8011-3c155cd0acdd","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"c7e50ac6966e0ccee7a517f1c3e02630d784edc7999014bca2c6abe904e3d54d","status":"valid"},"source_digest":"8b1a4f6108805303bf580684cc7473c8dafbd6417f6ac40c9f35b6bf807990f2","version":1},"evidence_id":"dbc11177-c846-4e0c-8011-3c155cd0acdd","findings":[{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"`terminal_create` reaches `spawn_web_terminal`, which directly calls `promote_to_live` and performs lost-CAS cleanup outside the new `_settle_promotion` helper; it also omits the pre-commit `host_terminal_id` persistence that pending exits require. A reaper can therefore observe `pending`, the web path can promote, and the reaper can kill a resource whose row is now `live`; a fast web-created native exit can also miss the pending identity guard.","finding_id":"PR14-001-web-promotion-settlement","fix":"Add `src/gobby/terminals/web_spawn.py` and its focused server test to §2.5 ownership. Share the identity-persistence and post-commit `_settle_promotion` path with agent spawns, or specify an equivalent exactly-once helper, and add web-spawn exit-before-commit plus paused-reaper/promotion schedules.","location":"§2.5 settlement-owner list and `_settle_promotion` contract","prevention":"Inventory every `promote_to_live` call site before declaring settlement-lock ownership complete, then test each call site against both reaper schedules.","principle":"Every pending-to-live transition that can race a reaper must share the same lock owner and CAS/cleanup span.","root_cause":"The promotion sweep covered agent spawn and list reconciliation while leaving the production web-terminal spawn path on its direct CAS.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR13-001-process-identity-lifecycle","causal_section_ids":["2.5"],"check_key":"edge-case-coverage","description":"`list_live_by_machine` includes pending rows, and `TerminalHostManager.handle_host_death` reaps their process groups solely from `row.process`. Round 13's `SET process = NULL` runs while a timed-out native attempt A can still own a live prepared or committed resource, so daemon death after B's bump and before A's callback settles it loses A's `pgid`/`start_time`; the callback disappears with the daemon and A can leak.","finding_id":"PR14-002-process-cleanup-erased","fix":"Under `settle_lock`, terminate and confirm the predecessor resource before bumping; if that cannot be confirmed, return a typed unsettled-retry refusal. Keep current-attempt `host_terminal_id` absent during the gap, and add a daemon-death-after-bump/before-callback acceptance proving A is reaped and B is untouched.","introduced_in_round":13,"location":"§2.5 `bump_attempt_generation` and host-death cleanup","prevention":"For every generation reset, inventory both match consumers and cleanup consumers of the cleared carrier, including daemon-crash recovery between reset and replacement.","principle":"Attempt identity invalidation must preserve predecessor cleanup evidence until that predecessor resource is durably settled.","root_cause":"Round 13 made one JSONB object serve as both the current exit discriminator and the only host-death process-group cleanup record, then cleared the whole object on retry.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR13-007-unused-tmux-exit-carriers","causal_section_ids":["2.5","2.9"],"check_key":"edge-case-coverage","description":"The existing tmux poll loop calls `embed.rs::emit_exit` on pane death and confirmed absence, and tmux observer slots use `locator_key` as `Identity.terminal_id`. Section 2.9 says `emit_exit` pushes control subscribers and the reader sends every `terminal_exited` to UUID-based `settle_exit`, so a tmux exit can route a non-UUID locator key into storage and terminate the event reader. This falsifies §2.5's claim that tmux never reaches `settle_exit`.","finding_id":"PR14-003-tmux-exit-routing","fix":"Create a distinct native durable-row control-event publisher and leave tmux `embed.rs::emit_exit` on its frame/observer path, or filter tmux identities before `settle_exit`. Add `embed.rs` to §2.9 Targets if it changes and add a subscribed-reader test proving confirmed tmux absence attempts no UUID settlement and leaves the reader healthy.","introduced_in_round":13,"location":"§2.9 control-event producer/reader and §2.5 native-only `settle_exit` claim","prevention":"Trace each pushed event from every producer call site through its identity construction and decoder before narrowing backend guards or dropping a regression test.","principle":"A control-event consumer must receive identities from exactly the backend domain its storage transition accepts.","root_cause":"Round 13 removed the tmux storage fallback based on a native-only assumption without checking the existing producer named by the plan.","section_id":"2.9","severity":"blocking"},{"category":"weak-testability","check_key":"acceptance-observability","description":"Section 6.3 says it adds `tests/e2e/test_terminal_client_stack.py::test_gclient_reaches_workspace` and claims that test in 6.3.5. Its close gate runs guard set G plus `cargo run ... --help`; G runs the Rust crate suite and selected Python groups, excluding this E2E module. Section 9.1 runs the file later, leaving 6.3 unable to prove its own acceptance when it closes.","finding_id":"PR14-004-gclient-e2e-close-gate","fix":"Either add a focused protected pytest invocation for `test_gclient_reaches_workspace` to 6.3's close gate with the required rebuilt binaries and isolated daemon, or move acceptance 6.3.5 and all ownership of that test to 9.1.","location":"§6.3 close gate and acceptance 6.3.5","prevention":"Resolve every acceptance test path against the leaf's explicit close command and shared guard groups before finalizing the section.","principle":"A leaf is independently closeable only when its close gate executes every acceptance artifact the leaf owns.","root_cause":"The section assigns one Python E2E test to 6.3 while relying on a downstream leaf to run the containing module.","section_id":"6.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"B-5 specifies `runtime.terminate(row, grace)` for a stale pending native row. `NativeTerminalRuntime` resolves kill identity only from `locator.host_terminal_id`, while a pending row has no locator and Round 13 stores the prepared id in `process.host_terminal_id`. The real runtime therefore cannot perform the termination that 2.5.5 and 9.1.9 claim; fake runtimes can hide the gap.","finding_id":"PR14-005-pending-native-termination","fix":"Have the reaper extract the current generation's `process.host_terminal_id` and pass it to the existing captured-resource `_kill_spawn_key` form, or define an equivalent typed pending-native termination operation. Keep the generation re-read under `settle_lock` and add a real-`NativeTerminalRuntime` case to 2.5.5 and 9.1.9.","location":"§2.5 B-5 reaper and acceptance 2.5.5","prevention":"For every cleanup call, execute the real backend implementation with the exact row state and identity shape the plan passes.","principle":"A cleanup path must carry the backend identity required by the concrete runtime operation it invokes.","root_cause":"The reaper calls native termination with a pending row even though the native runtime reads kill identity only from the live locator.","section_id":"2.5","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"PR13-004-preexec-row-proof","causal_section_ids":["2.7","2.9"],"check_key":"acceptance-observability","description":"Acceptance 2.9.13 says the three rows become `exited` through pushed `terminal_exited` and `settle_exit` alone with the stale-pending reaper disabled. The real daemon still runs the host health loop; its list reconciliation independently marks an absent dead live row `exited`. A broken event push or `settle_exit` can therefore pass the final-state assertion once reconciliation runs.","finding_id":"PR14-006-preexec-test-mask","fix":"Pause both stale-pending reaping and health/list reconciliation for the tested terminal, or instrument the event reader and require exactly one `settle_exit(terminal_id, host_terminal_id)` before recovery is re-enabled. Retain a final convergence assertion after re-enabling the backstops.","introduced_in_round":13,"location":"§2.9 acceptance 2.9.13 recovery isolation","prevention":"For every final-state oracle, enumerate all writers of that state and pause them or assert the intended writer fires first.","principle":"A regression test proves a primary transition only when every independent recovery path capable of producing the same final state is excluded or observed separately.","root_cause":"Round 13 disabled the stale-pending reaper while leaving health-loop inventory reconciliation able to settle the same row.","section_id":"2.9","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR13-004-preexec-row-proof","causal_section_ids":["2.7","2.9"],"check_key":"edge-case-coverage","description":"Acceptance 2.9.13 requires each abort, SIGKILL, and `/bin/true` residue to end `exited` and never `live`. Acceptance 2.5.10 explicitly permits promotion first, which performs `pending → live` before the later event performs `live → exited`. Commit reply and pushed-exit delivery are independent, so the two acceptance contracts cannot both hold.","finding_id":"PR14-007-never-live-contradiction","fix":"Change 2.9.13 to require an `exited` final state before any recovery sweep and no stale `live` state after exit delivery. When transient history matters, test the two valid schedules separately: exit-first skips `live`, while promotion-first permits `live → exited`.","introduced_in_round":13,"location":"§2.9 acceptance 2.9.13 versus §2.5 acceptance 2.5.10","prevention":"Cross-check new acceptance state assertions against every branch and ordering in the owning transition contract.","principle":"Acceptance criteria for one state machine must permit every ordering the plan explicitly declares valid.","root_cause":"Round 13 added a never-live oracle without reconciling it against the previously accepted promotion-first schedule.","section_id":"2.9","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"`terminal_control_result`, which §6.3 needs to settle the queue-once keystroke, is routed by `attachment_id` and carries no `request_id`. Section 6.2 gives `LiveDaemon` a general `request_id` map and a second `(attachment_id, client_write_seq)` map only for writes, so the control result has no sender path; other attachment-routed outcomes such as scroll application need the same inventory. Fencing only the write map on `terminal_attachment_finalized` can leave these waiters parked or misrouted.","finding_id":"PR14-008-attachment-result-correlation","fix":"Add the smallest explicit correlation path for each consumed attachment-routed reply—at minimum an attachment-scoped single-flight control waiter—then fail and remove those waiters on finalization, timeout, EOF, and generation replacement. Add interleaving, refusal, finalization-before-result, and late-result tests; classify any truly one-way verb through `notify`.","location":"§6.2 `LiveDaemon` reader maps and §6.3 control acquisition","prevention":"Inventory every outbound verb against its exact committed reply fields, then map registration, success, refusal, timeout, finalization, reconnect, and late-reply behavior.","principle":"Every reply that a state machine waits for needs a concrete correlation key, bounded waiter ownership, and finalization cleanup.","root_cause":"The data-plane design generalizes `*_result` routing by `request_id` and special-cases writes while omitting other attachment-routed replies.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"Section 6.2.2 says reconnect tombstones old ids, re-lists, attaches every shown pane afresh, and installs each new attachment id. Section 6.3.7 says a Detaching timeout invalidates that socket generation and, after the new generation re-lists, issues its own fresh proxy attach. Executing both contracts can mint two attachments for one pane, leak an observer or lease record, and let the last result overwrite the other identity.","finding_id":"PR14-009-reconnect-double-attach","fix":"Make `LiveDaemon` own socket replacement, subscribe-first reconciliation, and a generation-ready signal; make `Workspace` the sole owner of shown panes and issue exactly one fresh attach per pane with the desired transport. Rewrite 6.2.2, 6.3.7, and 6.3.9 around that boundary and assert one attach request/result per pane; retain 6.3.10 as the distinct same-socket path.","location":"§6.2.2 reconnect reattachment and §6.3.7 Detaching recovery","prevention":"For every reconnect transition, name the component that owns socket replacement, roster reconciliation, pane selection, attach issuance, and identity installation exactly once.","principle":"Each state transition needs one owner; transport recovery and pane reattachment must have a single handoff boundary.","root_cause":"The data-plane section and workspace section both claim responsibility for issuing the fresh attach after the same socket-generation replacement.","section_id":"6.3","severity":"blocking"}],"plan_hash":"66a2b50d366843b6662009c3893819448226a1333e2294f7c90a78b5cbdd2e4a","reviewer_session":"#11012","round":14,"round_number":14,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

**Round 15** `kind: verification`

- reviewer_run: 38524f83-7ccd-4492-8501-4b8b461958bd
- reviewer_session: #11013
- verdict: needs_review
- findings:
- PR15-001-final-e2e-order / blocking / bad-sequencing / 9.1 - the final E1 stack proof runs `gclient` without depending on 6.4 - accepted. Verified against the plan: 9.1 declares `(depends: P4, P8, 6.3)` while 6.4 (copy mode, paste, persistence, teardown, logging) declares `(depends: 6.3)`, so 6.4 is outside 9.1's closure even though acceptance 9.1.11 drives the real `gclient` through select -> `terminal_create` -> attach -> fallback -> `terminal_kill`. Repaired with the typed `add_dependency` repair (9.1 on 6.4) plus a close-gate amendment naming a rebuilt and reinstalled `gclient` alongside `gterm`.
- PR15-002-branch-target-qualification / blocking / gobby-format / 2.5 - bare Targets on symbol-bearing files that arrive through the 1.1 merge - accepted, remedy narrowed. The count is real: 181 unique bare source Targets in the plan, 142 of which exist in `wt-task-20255-m4` but not at the current project root. The proposed plan-wide qualification sweep is not implementable before the merge, because project-aware validation classifies a file absent from the root as new, permits only a bare path for it, and cannot resolve a `qualified_name` or a `::*` scope inside a file it has never indexed - so the sweep would fail the expansion-mode gate the plan must pass to hand off. Repaired by recording the constraint in `## Constraints` and pinning 1.1 as the gate, and by stating that post-merge leaves carry symbol precision in their bodies rather than in unresolvable Targets.
- PR15-003-stale-process-writer / blocking / unhandled-edge / 2.5 - fixer-induced (round 14, PR14-002-process-cleanup-erased) - a superseded attempt can rewrite the attempt discriminator after the bump cleared it - accepted. Verified on the branch: `spawn_executor._promote_prepared` calls `manager.record_process` at `src/gobby/agents/spawn_executor.py:452-456` before any CAS and outside `settle_lock`, and `record_process` (`src/gobby/storage/terminals.py:627`) guards only `state = 'pending' AND backend = 'native'`, so attempt A completing after attempt B's `bump_attempt_generation` writes A's `host_terminal_id` back onto B's row and A's late exit then satisfies `settle_exit`'s pending guard against B. `handle_spawn_prepared` (`host_manager.py:227`) and `reconcile_host_inventory` (`host_reconcile.py:134`) have the same unguarded shape. Repaired by making `record_process` the sole identity writer with a JSONB merge and an attempt CAS on `(attempt_generation, attempt_started_at)`, moving the two host-event writers onto an identity-free reap-record merge, and declaring the `{pgid, start_time}` pair a best-effort host-death record whose authoritative counterpart is the captured-`host_terminal_id` kill.
- PR15-004-native-exit-producer / blocking / unhandled-edge / 2.9 - fixer-induced (round 14, PR14-003-tmux-exit-routing) - round 14's native-only guard sits in a producer that cannot see native slots - accepted, and the refutation is correct. Verified on the branch: `crates/gterminal/src/host/embed.rs:632-640` selects the slot with `slot.locator.as_ref().is_some_and(|l| l.locator_key() == key)`, so a `locator: None` native slot is excluded by the lookup itself, and `emit_exit`'s only callers are the tmux poll loop at lines 370 and 407. Round 14's `slot.locator.is_none()` branch is therefore dead code and the native control push has no producer at all. Repaired by removing the dead guard and naming the real native child-exit hook: the `child.wait()` reaper thread at `crates/gterminal/src/pane/runtime.rs:358-375` already observes the exit status and discards it, so `PaneRuntime` exposes that completion, `HostState::commit` (`host/state.rs:483-511`) installs exactly one watcher per committed native slot, and that watcher publishes `{terminal_id, host_terminal_id, exit_code}` to control subscribers. `crates/gterminal/src/pane/runtime.rs` joins 2.9 Targets and 2.9.15 splits into a real-native positive case and the tmux negative case.
- PR15-005-web-create-error-carrier / blocking / traceability / 6.2 - the create refusal code never reaches the wire - accepted. Verified on the branch: `_handle_terminal_create` (`src/gobby/servers/websocket/terminal_ws.py:196-205`) emits `terminal_create_result` with `request_id`, `success`, `terminal_id`, and `backend` and discards `WebSpawnResult.error`, so 2.4's stable web-spawn refusal codes and 6.2's `Refused{code}` have no source. Repaired by giving 2.4 the handler change (bounded `code` on failure), 4.5 the refused create-result golden its corpus and 4.5.3 already enforce, and 6.2.5 the concrete decoded shape.
- PR15-006-control-timeout-correlation / blocking / unhandled-edge / 6.2 - fixer-induced (round 14, PR14-008-attachment-result-correlation) - the control deadline frees an attachment key that a late reply can still resolve - accepted, remedy narrowed. Verified against the plan: 6.2's single-flight waiter is keyed by `attachment_id` alone and its fence list names the request deadline alongside EOF, reconnect, and finalization; every other entry in that list also invalidates the key, while the deadline alone leaves the attachment and socket live and the key reusable, so a stale take reply can settle a fresh release waiter and the reverse. Repaired without a wire change: the control deadline retires the attachment and drives the replacement attach the fallback path already performs, which reuses the plan's existing invariant that a replacement attachment always carries a new daemon-minted `attachment_id`. A correlation token was rejected because 6.2 already declines to thread `request_id` through `terminal_input`/`terminal_write_outcome` for the same reason - it would edit 4.3's handler, 4.5's codec, and the committed corpus from a section that owns none of them.
- PR15-007-generation-ready-loss / blocking / unhandled-edge / 6.2 - fixer-induced (round 14, PR14-009-reconnect-double-attach) - the one-shot readiness signal rides a lossy bounded stream - accepted. Verified against the plan: 6.2 publishes generation-ready "on the same event stream `subscribe()` already serves", and that stream is specified as 256-entry bounded channels that close a slow receiver with `Lagged` and require it to re-list; re-listing restores roster data but never replays the attach trigger, so a late subscriber or a lag across the transition leaves every shown pane tombstoned. Repaired by making readiness sticky rather than eventful: `LiveDaemon` holds the current ready generation, exposes it at subscribe and after a `Lagged` recovery, and `Workspace` deduplicates by generation so exactly one attach per pane per generation survives either delivery.
- PR15-008-web-timeout-ownership / blocking / unhandled-edge / 2.5 - the web spawn abandons its shielded prepare task on every post-prepare failure - accepted. Verified on the branch: `spawn_web_terminal` (`src/gobby/terminals/web_spawn.py:80-132`) shields `prepare_task`, and on timeout calls `_kill(runtime, spawn_key, manager.get(terminal_id))` against a locator-less pending row - `NativeTerminalRuntime._host_id` raises `TerminalWriteError(stage="none")` for that row and `_kill` swallows it - then fails the row with no done callback, so a late native slot or tmux session survives with no row left for the reaper to find. The bind-failure and lost-CAS branches share the shape and `CommitSpawnRefusedError` kills nothing. Repaired by reusing 2.5's attempt-owned late-prepare settlement on the web ingress: the same `prepare_task.add_done_callback`, the same captured `host_terminal_id` kill, and the same row-gone fallback, across the timeout, cancellation, bind, commit, and lost-CAS branches.
- PR15-009-health-pause-seam / blocking / weak-testability / 2.9 - fixer-induced (round 14, PR14-006-preexec-test-mask) - 2.9.13 requires pausing a competing writer with no named seam - accepted. Verified on the branch: `TerminalHostManager._health_loop` (`host_manager.py:508-546`) calls `await self.reconcile()` on every tick and `reconcile()` is whole-host, so no terminal-scoped pause exists and the only producer-stop surface would also tear down the event reader the test is proving. Repaired without new mechanism by making the setup decision-complete: the isolated daemon's temporary config sets `health_interval_seconds` beyond the test horizon (the loop sleeps before its first reconcile, so a long interval suppresses it entirely), the stale-pending reaper is gated by its own switch, the event reader is instrumented for exactly one `settle_exit`, and reconciliation and reaping are then run explicitly after the assertion to prove backstop convergence.
- PR15-010-promotion-helper-target / blocking / traceability / 2.5 - fixer-induced (round 14, PR14-001-web-promotion-settlement) - the relocated settlement helper has no file owner - accepted, remedy narrowed. Verified on the branch: `src/gobby/terminals/web_spawn.py:9` already imports `derive_spawn_key` from `gobby.agents.spawn_executor`, so the caller-to-callee direction web_spawn -> spawn_executor exists and the reverse does not. Repaired by deleting round 14's "lives beside the shared spawn primitives" sentence and keeping the helper in `spawn_executor.py` as a public `settle_promotion` that `web_spawn.py` imports exactly as it already imports `derive_spawn_key`. A new backend-neutral spawn-support module was rejected as unjustified mechanism: it would move `derive_spawn_key` and both callers to buy an import direction that is already acyclic.
- PR15-011-native-reaper-call-contract / blocking / unhandled-edge / 2.5 - fixer-induced (round 14, PR14-005-pending-native-termination) - B-5 contradicts itself on native termination and leaves the unavailable branch undefined - accepted. Verified against the plan and the branch: B-5's round-14 paragraph terminates a pending native row by `process->>'host_terminal_id'`, and the very next sentence still says a native `commit_indeterminate` row "is terminated by `spawn_key` when the host reports it live"; `reap_stale_pending_terminals` (`src/gobby/agents/spawn_executor.py:598-626`) holds only `manager` and `runtime`, `NativeTerminalRuntime` exposes no kill-by-host-id today (`terminate` takes a `Terminal` and resolves `_host_id` from the locator), and `_ensure` raises `HostUnavailableError` under `settle_lock`. Repaired by deleting the contradictory sentence, naming the concrete operation the leaf adds (`NativeTerminalRuntime.terminate_host_id`), and defining the failure outcomes: an unavailable host leaves the row pending for the next sweep and the loop continues to the next row, and `HostManagerStopped` ends the sweep cleanly during shutdown.
- resolution_notes: All eleven findings accepted; four were applied with a narrower repair than proposed (PR15-002 documents an unimplementable-before-merge constraint instead of a 142-target sweep, PR15-006 reuses attachment replacement instead of a new wire token, PR15-010 keeps the helper on the existing acyclic import direction instead of adding a module, and PR15-001 pairs its typed dependency repair with a close-gate amendment). PR15-004 is a correct refutation of round 14's own repair and replaces it with a real producer. One typed repair rode on the findings (PR15-001 `add_dependency`) and was applied through `apply_plan_review_repairs`; the remaining ten are prose-only and were hand-applied. Seven of eleven findings are fixer-induced, so neither the approval nor the all-fixer-induced stop condition was met at the end of the granted four-round budget.

```json plan-review-round
{"evidence_id":"f2ecb2ea-854b-4160-baac-d2e2e29681b6","plan_hash":"273e1c7a35ae324509456d3a3823272f9b535f09c6531781f126d8d70f74ff40","round_number":15,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"6710ee075eecdf6cf3f6e288643da617a7f063b0c0330610fbd0d30203b38deb","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":11,"total":15},"evidence_id":"f2ecb2ea-854b-4160-baac-d2e2e29681b6","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":33,"manifest_digest":"ed97a9a2a9f848d39156060ea73983292cd3b5672536481f523e6da21e6f74d5","status":"valid"},"source_digest":"f6f6b8372245984d92c514a054808e87442f2a825ab679139a5e3c689f797aee","version":1},"evidence_id":"f2ecb2ea-854b-4160-baac-d2e2e29681b6","findings":[{"category":"bad-sequencing","check_key":"final-e2e-dependency-closure","description":"The derived 9.1 manifest depends on 6.3 but omits 6.4. It can therefore rebuild and exercise gclient before copy mode, persistence, teardown, logging, and the final library wiring land, even though 9.1 is the plan's final E1 stack proof.","finding_id":"PR15-001-final-e2e-order","fix":"Add `(depends: 6.4)` to 9.1 and make its close gate rebuild and reinstall both gclient and gterm from the epic worktree before running the full E2E module.","location":"§9.1 dependency header and close gate","prevention":"For each end-to-end leaf, compare its derived dependency closure with every production module exercised or claimed by the test.","principle":"A final assembled-system proof must run after every production leaf whose binary it claims to verify.","repairs":[{"kind":"add_dependency","on":["6.4"],"section_id":"9.1"}],"root_cause":"Section 9.1 depends on 6.3 while 6.4 is downstream of 6.3, so expansion can close the E1 stack proof before the client tail lands.","section_id":"9.1","severity":"blocking"},{"category":"gobby-format","check_key":"merged-branch-target-qualification","description":"A branch-byte inventory found 127 bare symbol-bearing source Targets across 29 deliverables, including round-14 additions such as `src/gobby/terminals/web_spawn.py`, `src/gobby/terminals/host_client.py`, `crates/gclient/src/startup.rs`, and `crates/gclient/src/app/mod.rs`. Draft validation passed only because the current project index cannot see the branch that 1.1 merges.","finding_id":"PR15-002-branch-target-qualification","fix":"Refresh or otherwise inventory the exact `wt-task-20255-m4` bytes, then replace every bare existing source Target with file-qualified symbols or one `::*` Target with a concrete scope reason. Apply the sweep plan-wide before resubmission.","location":"Targets blocks across 29 deliverables; representative changed sections §§2.5, 2.9, 6.2, 6.3","prevention":"After resolving the merge source, index or inventory that exact tree and classify every post-merge Target against the files that will exist at leaf start.","principle":"Files that exist when a leaf executes require exact qualified Targets or one justified file-wide scope.","root_cause":"Project-aware validation sees the current root, while 1.1 first merges the implementation branch; branch files were consequently treated as new bare paths although later leaves edit existing symbols.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR14-002-process-cleanup-erased","causal_section_ids":["2.5"],"check_key":"attempt-guarded-process-writers","description":"A timed-out attempt A can finish after attempt B bumps the row and write A's `host_terminal_id` back through `record_process`; `handle_spawn_prepared` and list reconciliation have the same unguarded shape. A delayed A exit can then satisfy B's pending guard and transition B to `exited`.","finding_id":"PR15-003-stale-process-writer","fix":"Make every process write an attempt-guarded CAS carrying the expected generation/start time, or serialize native retries until predecessor writers are impossible. Add A-timeout → B-bump → late-A-write → A-exit schedules for the direct, event, and reconciliation writers.","introduced_in_round":14,"location":"§2.5 `bump_attempt_generation`, `record_process`, and acceptance 2.5.12","prevention":"For each attempt-scoped carrier, enumerate every asynchronous writer and test a predecessor write after the successor generation begins.","principle":"Every write of an attempt-scoped discriminator must prove it belongs to the row's current attempt.","root_cause":"Round 14 removes only `process.host_terminal_id` at the bump, while direct, prepared-event, and list-reconcile writers can restore a predecessor id without an attempt guard.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR14-003-tmux-exit-routing","causal_section_ids":["2.9"],"check_key":"native-exit-producer-reachability","description":"`embed.rs::emit_exit` is called only by the tmux poll loop and finds slots by a present tmux locator. Native slots use `locator: None`, and PaneRuntime completion currently has no control-event publisher. The proposed `slot.locator.is_none()` branch therefore emits no native `terminal_exited`, while filtering out every reachable tmux call.","finding_id":"PR15-004-native-exit-producer","fix":"Name and target the actual native child-exit hook: expose completion/status from PaneRuntime or PreparedChild, install exactly one watcher per native slot, and publish `{terminal_id, host_terminal_id, exit_code}` there. Keep the tmux poll helper frame-only and extend the real-native and tmux-negative tests.","introduced_in_round":14,"location":"§2.9 E-5 producer paragraph and acceptances 2.9.2/2.9.13/2.9.15","prevention":"Trace each event from the OS/runtime observation site through every producer call and identity lookup before specifying a backend filter.","principle":"A consumed lifecycle event needs a reachable producer on the lifecycle path that actually observes the event.","root_cause":"Round 14 put a native-only predicate inside `embed.rs::emit_exit`, whose callers and slot lookup are exclusively tmux locator-present paths.","section_id":"2.9","severity":"blocking"},{"category":"traceability","check_key":"web-create-refusal-producer-consumer-parity","description":"Section 6.2 requires `LiveDaemon::spawn` to produce `Refused{code}`, and 2.4 requires stable web-spawn refusal codes. The production `terminal_create_result` currently emits only success, terminal id, and backend, so host absence, capacity, epoch, locator, and commit failures collapse into an undecodable refusal.","finding_id":"PR15-005-web-create-error-carrier","fix":"Add the production create handler to the outcome owner, carry bounded `code`/detail from `WebSpawnResult` on failure, add a byte-exact refused create-result golden driven through the real handler, and test `LiveDaemon` mapping that shape.","location":"§§2.4, 4.5, and 6.2 `terminal_create_result` contract","prevention":"For every typed adapter outcome, trace the discriminant from service result through handler encoding, goldens, decoder, and acceptance.","principle":"A typed consumer outcome requires the production producer and golden corpus to carry the same discriminant.","root_cause":"`spawn_web_terminal` returns an error code, while the WebSocket wrapper discards it and the create-result golden describes only the success carrier.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR14-008-attachment-result-correlation","causal_section_ids":["6.2"],"check_key":"sequential-control-reply-correlation","description":"Both take and release replies carry only `attachment_id`. After a request deadline, a new take or release can register under the same id; a delayed reply from the timed-out request then resolves the new waiter, including a take reply satisfying release or the reverse.","finding_id":"PR15-006-control-timeout-correlation","fix":"Add a control correlation token echoed by `terminal_control_result` and key the waiter by attachment plus token, updating handlers, goldens, web, and gclient together. The lower-wire-change alternative is to finalize the attachment or replace the socket generation on timeout before another control request is allowed.","introduced_in_round":14,"location":"§6.2 attachment-scoped control waiter and acceptance 6.2.10","prevention":"Test timeout → retry → late old reply → new reply for every request family that shares one response shape.","principle":"A correlation key cannot be reused until every reply from its prior operation is impossible or distinguishable.","root_cause":"Round 14's single-flight map prevents concurrent requests, but a deadline removes the waiter while retaining the attachment and socket, permitting sequential key reuse.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR14-009-reconnect-double-attach","causal_section_ids":["6.2","6.3"],"check_key":"sticky-generation-readiness","description":"A Workspace that subscribes after readiness, or re-subscribes after `Lagged` while the socket generation remains healthy, can miss the only generation-ready event. Re-listing converges roster data but does not recreate the signal, so shown panes remain tombstoned and detached.","finding_id":"PR15-007-generation-ready-loss","fix":"Expose the current ready generation through sticky/watch state or synchronously from subscribe/reconnect after the authoritative cut, and have Workspace deduplicate by generation before attaching. Add initial-late-subscribe and lag-across-ready schedules proving one attach per shown pane.","introduced_in_round":14,"location":"§§6.2–6.3 generation-ready handoff and acceptance 6.2.9","prevention":"For every one-shot state transition on a bounded stream, test late subscription and lag exactly across the transition.","principle":"A durable current-state handoff must be replayable to late or lag-recovering consumers.","root_cause":"Round 14 publishes readiness once on the same bounded lifecycle stream that closes slow receivers with `Lagged`.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"web-prepare-terminal-outcome-ownership","description":"On web timeout the kill can run before the resource exists, the row is failed, and the shielded prepare task continues without a done callback. A late tmux session or native slot can then survive outside pending reaping; native `_kill` also passes a locator-less row and swallows the identity failure. Adjacent post-prepare failure branches share the captured-resource gap.","finding_id":"PR15-008-web-timeout-ownership","fix":"Apply the agent path's attempt-owned late-prepare settlement to web spawn, passing the captured `host_terminal_id` for native cleanup and preserving the row-gone fallback. Cover timeout, cancellation, bind, commit, and lost-CAS schedules with focused web ingress tests.","location":"§2.5 web-spawn ownership and `spawn_web_terminal` failure paths","prevention":"For each spawn ingress, walk timeout, cancellation, bind failure, commit failure, and lost CAS after the resource may appear late.","principle":"Every shielded external operation must retain an owner until its eventual resource is settled.","root_cause":"The web path is added to shared promotion settlement while its timeout, cancellation, bind, and commit failures still use immediate locator-less cleanup and can abandon the shielded prepare task.","section_id":"2.5","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"PR14-006-preexec-test-mask","causal_section_ids":["2.9"],"check_key":"independent-reconcile-pause-seam","description":"The real-daemon test can remain maskable by list reconciliation, or disable the event/restart path it intends to prove. Reconciliation is whole-host, so a terminal-scoped pause is unavailable without new mechanism.","finding_id":"PR15-009-health-pause-seam","fix":"Make setup decision-complete: configure the health interval beyond the test horizon or inject a reconcile-only gate before start, gate stale reaping independently, instrument exactly one `settle_exit`, then explicitly run reconciliation and reaping after the event assertion to prove backstop convergence.","introduced_in_round":14,"location":"§2.9 acceptance 2.9.13","prevention":"For final-state tests with multiple writers, name how each competing writer is disabled before the stimulus and independently re-enabled after the assertion.","principle":"A sole-writer oracle needs an explicit test seam that disables competing writers while preserving the path under test.","root_cause":"Round 14 requires health/list reconciliation paused, while the existing producer-stop surface also tears down reconnect work and no independent pre-spawn gate is named.","section_id":"2.9","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR14-001-web-promotion-settlement","causal_section_ids":["2.5"],"check_key":"shared-helper-target-ownership","description":"Section 2.5 says `_settle_promotion` lives beside shared spawn primitives instead of `spawn_executor`'s private surface, but no Target owns that destination. Leaving it in `spawn_executor` violates the stated split; placing it in `web_spawn` and importing it back creates a cycle.","finding_id":"PR15-010-promotion-helper-target","fix":"Choose the exact shared module, add it to 2.5 Targets, and specify both callers' import direction. A small backend-neutral terminal spawn-support module that owns `derive_spawn_key` and promotion settlement is the direct form; avoid a registry or new abstraction layer.","introduced_in_round":14,"location":"§2.5 `_settle_promotion` placement paragraph and Targets","prevention":"For every extracted cross-module helper, name its destination Target and check both caller import directions before finalizing the plan.","principle":"A shared helper required outside a private module needs one explicit file owner and an acyclic import direction.","root_cause":"Round 14 relocated the helper in prose without naming its file; `web_spawn.py` already imports `derive_spawn_key` from `spawn_executor.py`.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR14-005-pending-native-termination","causal_section_ids":["2.5"],"check_key":"pending-native-kill-outcomes","description":"The detailed split correctly requires native kill by `process.host_terminal_id`, then the same paragraph says a `commit_indeterminate` native row is terminated by `spawn_key`. The reaper owns only a TerminalRuntime; a disconnected HostManagerControl can raise `HostUnavailable` or `HostManagerStopped`, yet the plan does not say whether to fail, retain pending, or continue reaping other rows.","finding_id":"PR15-011-native-reaper-call-contract","fix":"Name the existing captured-resource call with `host_terminal_id`, remove every native `spawn_key` termination statement, and define deterministic unavailable/stopped behavior that does not abort the reap loop or lose a potentially live resource. Align 2.5.1, 2.5.5, and 9.1.9 and add real-client failure schedules.","introduced_in_round":14,"location":"§2.5 B-5 backend split and acceptances 2.5.1/2.5.5/9.1.9","prevention":"Execute every cleanup description against the real runtime signature and enumerate success, missing identity, disconnected host, stopped manager, timeout, and refusal.","principle":"A cleanup span must name the concrete backend identity and every caller-visible failure outcome.","root_cause":"Round 14 added host-id termination but retained a later `spawn_key` instruction and omitted the unavailable/stopped client branch while the kill runs under `settle_lock`.","section_id":"2.5","severity":"blocking"}],"reviewer_session":"#11013","round":15,"round_number":15,"verdict":"needs_review"},"session_id":"27156037-1ce3-48e2-ac92-75441ba27bae"}
```

## Task Mapping
`kind: framing`

Bootstrap coverage ledger: `docs/contracts/plan-coverage.md` ("Bootstrap Ledger")
requires `.gobby/plans/herdr-terminal-client-qa-fixes.coverage-ledger.yaml`. The
ledger's header binds `plan_hash` (the approved artifact's sha256), `root_task_ref`
(the epic seq_num `gobby build` mints), and per-item `expected_leaves` (the titles of
the server-derived `## M1 Task Manifest` entries). Every one of those values exists at
approval except `root_task_ref`, and `gobby build` is the only command that mints it,
so the file cannot be written before that command runs. The handoff order is therefore
executable end to end:

1. `uv run gobby plans validate .gobby/plans/herdr-terminal-client-qa-fixes.md --mode expansion`.
2. `uv run gobby build .gobby/plans/herdr-terminal-client-qa-fixes.md --planning-seed-state approved --completed-plan-review-rounds <N> --quick`,
   recording the epic ref it prints. `--quick` runs one lifecycle action and then
   disables automation for the tree, so nothing is claimed while the ledger is written.
3. Generate the ledger from the approved plan and the sealed `## M1 Task Manifest`
   only — one `acceptance_items` entry per acceptance item of every `kind: deliverable`
   section (33 sections, every `N.N.M` id), each mapped to the manifest leaf whose
   `covers:herdr-terminal-client-qa-fixes:<section>:<item>` label names it, with
   `owner_agent` from the manifest's routing — plus the epic ref from step 2 and the
   approved plan's sha256. The generator never reads the created task tree, so
   `expected_leaves` stays an independent expectation rather than a transcript of what
   expansion produced; whether the step-2 tick has already dispatched expansion is
   therefore immaterial to the ledger's contents.
4. Assert the file exists and run the generator's consistency check (every manifest
   `covers:` label resolves to exactly one ledger row, every `N.N.M` id appears exactly
   once, and D1's rows carry empty `expected_leaves`). Stop the handoff on any mismatch.
5. `uv run gobby build resume <epic ref>` to let expansion finish creating the leaves
   and writing the coverage manifest.
6. Before the first leaf is claimed — `uv run gobby build stop <epic ref>` first if the
   dispatcher has already reached development — assert the ledger still exists and run
   `verify_bootstrap_ledger(db, <epic task id>)`, treating a missing file as a failure
   of the same severity as a mismatch, then resume. The handoff changelog entry records
   the ledger's path and plan hash.

Deferred rows. `_coverage_items` (`src/gobby/plans/coverage.py`) emits one coverage row
per entry of a deferred section's `original_acceptance_items`, and those rows carry
`deferral_target` with no `leaves`. D1 therefore contributes exactly two ledger rows,
`D1`/`5.3.1` and `D1`/`5.3.2`, each with an empty `expected_leaves` list and the
deferral task recorded as the row's `deferral_target`. Listing the deferral task as a
leaf would make `_leaf_mismatches` report an expected leaf the manifest cannot have.

Review and fail-closed ordering of the ledger. The ledger carries no authored
content: its rows are a deterministic projection of two reviewed inputs — the
approval round's sealed `manifest_entries` (the adversary's own derived output,
recorded in the V1 fence) and the plan's acceptance items — and its header fields are
the approved hash plus the ref `gobby build` minted. The approving adversary round is
therefore the ledger's review; no separate review stage is run on the generated file,
and no handoff-tooling deliverable is added to this epic: no production code path gates
expansion on the ledger (`verify_bootstrap_ledger` has no production caller), so new
tooling would carry no load that steps 4 and 6 do not already carry. Because
`verify_bootstrap_ledger` silently skips an absent companion, absence is made to fail
closed by those two steps, which treat a missing file exactly as a mismatch; a handoff
that reaches a first leaf claim without both of them passing is invalid. Changing
`verify_bootstrap_ledger`'s own absence semantics is out of this plan's scope: that
function governs every plan, and this plan's guarantee is carried by the explicit
checks above.

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

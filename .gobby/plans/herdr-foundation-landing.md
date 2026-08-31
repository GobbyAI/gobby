# Land the Herdr Terminal Foundation on 0.5.0

**Plan ID:** herdr-foundation-landing

## Overview
`kind: framing`

Epic #20255 (`.gobby/plans/herdr-terminal-client.md`) closed on 2026-08-21 on branch
`wt-task-20255-m4` (HEAD `518cec5c41`, 25 commits, merge-base with `0.5.0` =
`b89f371a15`) and never landed: `0.5.0` has no `crates/gterminal`, no
`src/gobby/terminals`, and no `terminals` table. The 2026-08-21 QA review found the
daemon-side runtime largely real and the client side largely not (ten red tests, a
`gclient` that probes health and exits, placebo acceptance tests, a fabricated
native-flip evidence artifact that flipped the shipped default). Its follow-up plan
`.gobby/plans/herdr-terminal-client-qa-fixes.md` was never built and assumed 69
commits of drift; `0.5.0` is now 661 commits ahead (migrations 399–407, spawn/lifecycle
and tmux-fidelity fixes, the Grok hook contract, worktree-principal fixes).

This plan lands the salvageable prefix of the original epic — P1 vendoring and the
herdr import, P2 `terminals` table + `TerminalRuntime` + tmux migration, §3.1/§3.2
`gterm` host and protocols, plus the real parts of P4 as explicit opt-in — on `0.5.0`
through a `0.5.0-test` worktree that runs the merged daemon before anything touches
`0.5.0`. Everything after the original plan's §3.3 join (gclient, native launches as
default, parity suites, the flip) is re-planned after landing, with the code index
live on the landed tree. The dry-run `git merge-tree --write-tree 0.5.0
wt-task-20255-m4` reports 34 conflicting files; 0.5.0 introduced no competing
terminal abstraction, so the worktree's design stands and the work is reconciliation.

## Constraints
`kind: framing`

**Decision Record (confirmed 2026-08-27).**

1. Strategy: merge once, land the foundation, re-plan the client on the landed tree.
   Discarding the worktree re-does ~200 mechanically verifiable acceptance items
   (P1/P2/§3.1/§3.2); executing the stale QA plan pays the merge cost and inherits
   specs written against a tree that no longer exists.
2. Hollow parts: strip the fakes (placebo acceptance tests, the fabricated flip
   evidence and its shaped checker, tautological E1 clauses) and revert the default
   backend to `tmux`; keep the real code (`gterm` host, `NativeTerminalRuntime`, web
   proxy, `gclient` crate skeleton) on-tree as explicit opt-in, documented as
   incomplete. Restraint rung 1: no new gate mechanism — `TerminalConfig.default_backend`
   and the existing "no silent backend switching" refusal already bound exposure.
3. Depth: Lightweight. This file is the artifact; no enhancement or adversary rounds.
4. Live check before landing: a `0.5.0-test` worktree branched from `0.5.0` receives
   the merge; the merged daemon runs from that worktree (`GOBBY_ALLOW_WORKTREE_DAEMON=1`,
   #21031) against a **cloned** hub database; rollback is stop → `uv run gobby install`
   → `uv run gobby start` from the main checkout on `0.5.0` with the original database.
   Landing onto `0.5.0` happens only after that loop is green.

**Branch and worktree topology.** `0.5.0-test` is created with
`gobby-worktrees:create_worktree(branch_name="0.5.0-test", base_branch="0.5.0")` at
`~/.gobby/worktrees/gobby/0.5.0-test` and is the shared worktree for every leaf.
`wt-task-20255-m4` (worktree id `2dda2b2f`, still `active`) is never modified; it is
the audit trail until L1 marks it merged and deletes it. The main checkout stays on
`0.5.0` throughout so rollback is a plain restart.

**Merge-resolution rules (leaf 1.1 executes; every other leaf inherits).**

- One `git merge wt-task-20255-m4` into `0.5.0-test`, never a rebase: 25 commits with
  34 conflicting files replay the same conflicts per commit under rebase, and the
  merge commit preserves the branch as provenance.
- 0.5.0's *behaviour* wins wherever the two sides changed the same tmux, spawn,
  resume, lifecycle, or websocket behaviour; the worktree's *structure* wins (the
  `TerminalRuntime` seam, `terminals` rows and `agent_runs.terminal_id`, backend-neutral
  WS/REST message shapes, `src/gobby/terminals/`). A 0.5.0 fix that lives in a file the
  worktree deleted or replaced is re-expressed in the replacing module, never dropped.
- Pins and manifests take 0.5.0's bytes and re-add the worktree's additions (crate
  registrations, ignore entries, guide sections); schema identity pins stay at 0.5.0's
  407 until leaf 1.2 advances them to 408.
- Tests take the union. An assertion that 0.5.0 added through a tmux seam the worktree
  renamed (`get_tmux_manager_for_context` → `manager_for_terminal_context`,
  `capture_pane` → `snapshot_lines`, `dispatch_keys`) is ported to the new seam; no
  0.5.0 assertion is deleted.

**Schema rule.** New DDL ships as a numbered migration only; `baseline.sql` keeps
0.5.0's bytes (`BASELINE_CHECKSUM` `ec222a7f…`). Fresh installs get every table from
the runner's baseline-plus-migrations apply
(`catalog_manifest_freshness.rs::embedded_runner_applies_fresh_and_idempotently`), and
0.5.0's baseline-lineage tripwire (`crates/gcore/src/schema/baseline_refresh.rs` with
the `predecessor`/`parent`/`worktree` fixtures in `runner_tests.rs`) admits only
allow-listed in-place baseline edits — commit `f8c4b926a2` (#20643, migration 399)
reverted exactly such an edit at the 398 hop. The merge left the worktree's
`terminals` hunk in `baseline.sql` (and swapped `agent_runs.tmux_session_name` for
`terminal_id`); leaf 1.2 restores `baseline.sql` to `e19caa9a9f`'s bytes and carries
that DDL in `408_terminals.sql`. Because `gdaemon schema verify` compares the whole
catalog manifest, a migrated hub must end identical to a fresh apply, so 408 also drops
`agent_runs.tmux_session_name` (no reader after the merge; daemon-owned metadata,
following migration 400's non-destructive precedent). `BASELINE_VERSION` stays 375;
migrations 376–407 are embedded in `crates/gcore/src/schema/assets.rs`; 403 is taken
(`403_interactive_overlay_principal.sql`), so the `terminals` DDL is
`408_terminals.sql`. Python delegates apply/verify to the installed identity-enforcing
`~/.gobby/bin/gdaemon` (`src/gobby/storage/schema_contract.py::_run_gdaemon`), and
`crates/gcore/src/schema/runner.rs` refuses a database newer than the binary
("database schema v{database_head} is newer than this runner"). That is why the live
window runs on a cloned database: applying 408 to the real hub would block a `0.5.0`
restart until a hand-written down-migration.

**Guard set G.** Every leaf's close gate runs, from the `0.5.0-test` root, with
`DATABASE_URL` pointed at the isolated test hub
(`postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`) and
`GOBBY_TEST_PROTECT=1`:

1. `uv run pytest tests/test_runner_lifecycle_restart_replay.py tests/agents/test_resume_executor.py tests/agents/test_spawn_executor.py tests/agents/test_tmux.py tests/agents/test_lifecycle_monitor.py tests/agents/test_capture_consumers.py tests/config/test_runtime_config_contract.py tests/config/test_terminals.py tests/cli/test_install_setup_gterm.py tests/gterminal/test_vendor_layer.py tests/mcp_proxy/tools/sessions/test_terminal.py tests/mcp_proxy/tools/sessions/test_terminal_clear.py tests/mcp_proxy/tools/test_spawn_agent_speed.py tests/servers/test_tmux_mixin.py tests/servers/test_admin_health.py tests/install/test_version_pins.py tests/install/test_distribution.py tests/tasks/test_validation_evidence.py`
2. `uv run pytest tests/terminals tests/storage/test_terminals.py tests/servers/test_terminal_ws_create.py tests/servers/test_terminal_ws_golden.py tests/servers/test_terminal_ws_lease.py tests/servers/test_terminal_ws_rename.py tests/servers/test_terminal_ws_viewport.py tests/servers/test_tmux_bridge_authority.py tests/servers/test_native_web_proxy.py tests/servers/test_attention_respond.py tests/mcp_proxy/test_sessions_terminal_tools.py` (DB-backed)
3. `cargo build -p gobby-terminal --release --features vt-engine && cargo clippy -p gobby-terminal -p gobby-client --all-targets -- -D warnings && cargo nextest run -p gobby-terminal -p gobby-client`
4. `cargo nextest run -p gobby-core -p gobby-daemon` (schema identity and grant pins)
5. `uv run ruff check src/ && uv run ruff format --check src/ && uv run mypy src/ && uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`
6. `cd web && npx vitest run src/hooks src/components/activity`
7. Host leak check: the set of `gterm host` PIDs after groups 2–3 equals the set before,
   and no surviving `gterm host` references a state directory the run created.

Carve-outs are explicit, cumulative, and end when their owner closes. From 1.1
close: group 2 and group 4's schema-identity tests (owner 1.2, until 1.2 closes);
the red tests named in 1.4 — `tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation`,
`tests/agents/test_resume_executor.py::test_codex_resume_delivers_prompt_via_composer_not_argv`,
`tests/terminals/test_no_direct_tmux_spawn.py`, `tests/terminals/test_no_direct_tmux_consumers.py`
(owner 1.4, until 1.4 closes); and
`tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`
(owner 1.3, until 1.3 closes — it passes on the merged tree, so it is carved out only
if the default revert in 1.3 turns it red). A carved-out test must fail for the
behavioural reason recorded at `518cec5c41` (an assertion or mock-call failure),
never at collection. Group 2 runs with `GOBBY_POSTGRES_TEST_DSN` exported.

**Binaries.** `crates/gcore` changes in this epic, so every managed binary is rebuilt
and installed via `uv run gobby install` from the tree being run (`ensure_gdaemon`
rebuilds `gdaemon` from the workspace whenever its embedded identity differs from the
tree's `schema_expected_identity.json`, and stages the file under a new inode). Zig
0.15.2 is installed at `/opt/homebrew/bin/zig`, so `gterm` builds locally with
`--features vt-engine`; `gterm` is installed to `~/.gobby/bin/gterm` by the same
new-inode rule (copy to a dotfile, `mv -f` over the name).

**Live window protocol (leaf 2.1).** Announce to active sessions via
`gobby-agents:send_message` and wait for a quiet window; no task-tracked work happens
during the window because its writes land on the clone and are discarded.
`uv run gobby stop --wait` from the main checkout. Clone the hub:
`createdb -h localhost -p 60891 -T gobby gobby_050test` (the 6.5 GB database copies
in about a minute once the daemon is stopped). Back up `~/.gobby/bootstrap.yaml` to
`~/.gobby/bootstrap.yaml.0.5.0` and point `database_url` at `gobby_050test`. From
`~/.gobby/worktrees/gobby/0.5.0-test`: `uv sync && uv run gobby install`, then
`GOBBY_ALLOW_WORKTREE_DAEMON=1 uv run gobby start --verbose`. Run the matrix in 2.1.
The window always ends by executing the rollback sequence — `uv run gobby stop`,
restore `bootstrap.yaml`, `uv run gobby install` and `uv run gobby start` from the
main checkout — which rehearses rollback on every pass. Startup sync from
`0.5.0-test` publishes that branch's templates to the shared DB; the branch is
`0.5.0` plus the herdr files, so the only template drift is the `terminals` block in
`config.yaml`, which 1.3 has already set to `tmux`. Memory vectors written to Qdrant
during the window are reconciled with `gcode vector cleanup-orphans` after rollback.

**No aliases, no compatibility.** `0.5.0` is unshipped; replaced symbols are deleted.

**Monolith ceiling.** Every hand-maintained production file stays under 1,000 lines at
every leaf close. Files near the ceiling on the merge path: `src/gobby/agents/tmux/session_manager.py`
(983 on 0.5.0, +12 on the worktree), `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`
(977), `src/gobby/runner_lifecycle_agents.py` (945), `src/gobby/agents/lifecycle_monitor.py`
(914). 1.1 and 1.4 name their splits.

**Lint policy.** No new blanket `allow` attributes. The existing
`#![allow(clippy::all)]` in `crates/gterminal/src/lib.rs` is not removed here (owner:
the follow-on epic's CI/packaging phase); clippy in group 3 still runs so regressions
in `gobby-client` are caught.

**Out of scope (follow-on epic, planned after L1 with the index live on the landed
tree).** A Full-depth plan, working title *herdr client completion*, supersedes
`.gobby/plans/herdr-terminal-client-qa-fixes.md` (left untouched as source material)
and owns: the `gclient` workspace and herdr UI parity (QA P6, P7); host backpressure
and real host-driven acceptance tests (QA P8, 2.6–2.9); the `WriteCoordinator`
composition graph, MCP `send_keys` and operator-write routing, WS pagination and
broadcast keying, deletion of the legacy tmux WS handlers, and the web input path
(QA P4); pending-row lifecycle, typed native failures, host respawn, stale-pending
reaper, and `SpawnResult` tmux-alias removal (QA 2.3–2.5); CI, packaging, installer
short-circuit, and the blanket-allow removal (QA P3); the honest flip gate and weekly
producer (QA P5, D1). Publishing `gterm`/`gclient` tags and Homebrew formulae remains
operator work.

**Named defaults.** Clone database name `gobby_050test`; bootstrap backup path
`~/.gobby/bootstrap.yaml.0.5.0`; merge commit subject
`[gobby-#<leaf>] merge: wt-task-20255-m4 into 0.5.0-test`; the replacing owner of the
tmux resize guard is whichever module handles `terminal_resize` for tmux rows after the
merge (`src/gobby/terminals/sync_bridge.py` or `src/gobby/servers/websocket/terminal_ws.py`
— 1.1 records which).

## P1: Merge and reconcile
`kind: framing`

**Goal**: `0.5.0-test` carries `0.5.0` HEAD plus every herdr commit, with 0.5.0's
behavioural fixes intact, the `terminals` schema shipped as migration 408, the
fabricated flip apparatus and placebo tests gone, and the ten red tests green.

### 1.1 Create `0.5.0-test` and merge `wt-task-20255-m4` into it [category: code]
`kind: deliverable`

Targets:
- `.gitignore`
- `crates/CLAUDE.md`
- `docs/guides/release-guide.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated manifest after the merge
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: merge conflict resolution, pins stay at 0.5.0's values
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: merge conflict resolution, pins stay at 0.5.0's values
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: merge conflict resolution, pins stay at 0.5.0's values
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: merge conflict resolution, pins stay at 0.5.0's values
- `src/gobby/agents/idle_check_handler.py::*` — scope-reason: merge conflict resolution across the terminal-runtime seam
- `src/gobby/agents/lifecycle_monitor.py::*` — scope-reason: merge conflict resolution across the terminal-runtime seam
- `src/gobby/agents/lifecycle_monitor_terminals.py`
- `src/gobby/agents/lifecycle_reconciliation.py::*` — scope-reason: merge conflict resolution across the terminal-runtime seam
- `src/gobby/agents/resume_executor.py::*` — scope-reason: merge conflict resolution across the terminal-runtime seam
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: merge conflict resolution across the terminal-runtime seam
- `src/gobby/agents/spawn_executor_support.py::*` — scope-reason: merge conflict resolution across the terminal-runtime seam
- `src/gobby/agents/tmux/pty_bridge.py::*` — scope-reason: deleted by the merge; its 0.5.0 guard moves to the replacing module
- `src/gobby/agents/tmux/session_manager.py::*` — scope-reason: auto-merged file at the size ceiling
- `src/gobby/agents/tmux/session_activation.py`
- `src/gobby/app_context.py::*` — scope-reason: merge conflict resolution of service-container wiring
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: merge conflict resolution
- `src/gobby/mcp_proxy/tools/agents_query_tools.py::*` — scope-reason: merge conflict resolution
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: merge conflict resolution
- `src/gobby/mcp_proxy/tools/spawn_agent/_terminal_backend.py`
- `src/gobby/runner_init/servers.py::*` — scope-reason: merge conflict resolution
- `src/gobby/servers/websocket/handlers/core.py::*` — scope-reason: merge conflict resolution
- `src/gobby/servers/websocket/tmux.py::*` — scope-reason: merge conflict resolution of the scrollback and attach-history path
- `src/gobby/servers/websocket/terminal_ws.py`
- `src/gobby/terminals/sync_bridge.py`
- `src/gobby/tasks/validation_evidence.py::*` — scope-reason: merge conflict resolution
- `tests/agents/test_capture_consumers.py::*` — scope-reason: union of both sides' assertions
- `tests/agents/test_resume_executor.py::*` — scope-reason: union of both sides' assertions
- `tests/agents/test_spawn_executor.py::*` — scope-reason: union of both sides' assertions
- `tests/agents/test_tmux.py::*` — scope-reason: union of both sides' assertions
- `tests/agents/test_verified_review_regressions.py::*` — scope-reason: union of both sides' assertions
- `tests/cli/test_cli_install.py::*` — scope-reason: union of both sides' assertions
- `tests/mcp_proxy/tools/test_spawn_agent_speed.py::*` — scope-reason: union of both sides' assertions
- `tests/servers/test_tmux_mixin.py::*` — scope-reason: union of both sides' assertions
- `tests/tasks/test_validation_evidence.py::*` — scope-reason: union of both sides' assertions
- `tests/test_runner_lifecycle_restart_replay.py::*` — scope-reason: union of both sides' assertions
- `web/src/hooks/__tests__/useTmuxSessions.test.ts::*` — scope-reason: union of both sides' assertions
- `web/src/hooks/useTmuxSessions.ts::*` — scope-reason: auto-merged hook carrying both sides' fixes
- `docs/guides/gterminal-development-guide.md`
- `docs/evidence/native-backend-flip.md`
- `.github/workflows/terminal-parity-weekly.yml`
- `tests/terminals/test_backend_selection.py`
- `tests/terminals/test_runtime_contract.py`

Create the worktree with `gobby-worktrees:create_worktree(branch_name="0.5.0-test",
base_branch="0.5.0", task_id=<this leaf>)`; the path is
`~/.gobby/worktrees/gobby/0.5.0-test`. Record the `0.5.0` SHA it started from. Run
`git merge --no-ff wt-task-20255-m4` there. The conflict set at planning time is the
34 files above minus the two auto-merged ones (`session_manager.py`,
`useTmuxSessions.ts`) and the three worktree-only replacing modules; re-run
`git merge-tree --write-tree 0.5.0 wt-task-20255-m4` first and treat any new conflict
under the same rules.

Resolution by file group:

- Pins and manifests (`.gitignore`, `crates/CLAUDE.md`, `docs/guides/release-guide.md`,
  `bundled_content_manifest.json`, `schema_expected_identity.json`, `assets.rs`,
  `bundle.rs`, `schema_contract.rs`): take 0.5.0's bytes, then re-add the worktree's
  additions — `gterminal`/`gclient` crate conventions in `crates/CLAUDE.md`, vendor and
  build ignores, the release-guide sections for `gterm`/`gclient`. Schema identity pins
  remain 0.5.0's (latest 407). The worktree's `baseline.sql` `terminals` hunk and its
  `catalog.manifest.json` entries auto-merge and stay; the resulting checksum mismatch
  is 1.2's carve-out. Regenerate `bundled_content_manifest.json` with the existing
  manifest tool rather than hand-merging hashes.
- Agents and spawn (`spawn_executor.py`, `spawn_executor_support.py`,
  `resume_executor.py`, `lifecycle_monitor.py`, `lifecycle_reconciliation.py`,
  `idle_check_handler.py`, `spawn_agent/_implementation.py`, `agents_query_tools.py`,
  `_session_start/flow.py`, `runner_init/servers.py`, `app_context.py`,
  `validation_evidence.py`): keep the worktree's `TerminalRuntime` call shape
  (`prepare_spawn`/`commit_spawn`, `terminal_id` on runs, `manager_for_terminal_context`)
  and carry every 0.5.0 hunk. The 0.5.0 hunks that must survive, by file:
  `spawn_executor.py` — `8e5660f3b6` persona/agent prompt split, `5032d6b3cd` Codex
  update-prompt suppression, `450b045160` worktree overlay binding;
  `spawn_executor_support.py` — `0c7ee0db18` fail the run when Codex prompt delivery
  fails; `lifecycle_monitor.py` — `0e84a3001e` draft grace in stagnation cleanup,
  `f17fdc177e` watchdogs off parked/mid-turn agents, `eb9c893f79` complete taskless
  idle runs; `lifecycle_reconciliation.py` — `cb07ec9aaf`; `resume_executor.py` — the
  vLLM config-override spawn, `api_base` normalisation, Codex suppression, and overlay
  binding commits; `spawn_agent/_implementation.py` — `bdf793939d` session-task claim
  link on auto-claim; `handlers/core.py` — `2367e0fb25` `terminal_input` must not parse
  a tmux id as an agent uuid. For each file, `git log --format='%h %s' b89f371a15..0.5.0 -- <file>`
  is the checklist and the leaf's evidence records one line per hunk: `kept`,
  `re-expressed in <module>`, or `superseded because <reason>`.
- Websocket tmux path (`servers/websocket/tmux.py`, `handlers/core.py`,
  `terminal_ws.py`, `sync_bridge.py`, `pty_bridge.py`): 0.5.0 reworked scrollback and
  attach history here (`a5324bf3c2`/`44bec18c41` #20720, `070f12a92e` #20798 attach
  deadline, `a9b54caa4b` #20793 alternate screen, `15bdfeed9c` #20815 history bound,
  `4445b2d835`/`88a401d6e7` #20805 no-op resize, `64e9ea1b42` #20841 tmux off the event
  loop, `7116385cb2` #20778 chunked writes, `621b0efef1` #21110 Enter retry). The
  worktree replaced `pty_bridge.py` with `sync_bridge.py` and added `terminal_ws.py`
  for backend-neutral messages. Keep the worktree's message names and routing; port
  each 0.5.0 behaviour into the module that now owns it. In particular the #20805
  guard — `BridgeInfo` records the geometry tmux is running the client at, `resize`
  returns `None` and repaints nothing when the requested size already matches, and the
  recorded size updates only after tmux was actually told — is re-expressed in the
  module that handles `terminal_resize` for tmux rows, with 0.5.0's regression test for
  it ported to that module's test file.
- `session_manager.py` auto-merges (0.5.0 moved activation subprocesses off the event
  loop, `64e9ea1b42`; the worktree added 12 lines). Its merged size is 983 + 12 = 995
  lines, one hunk from the ceiling, so this leaf splits `session_manager.py`: move the
  activation path (the off-loop subprocess helpers and the client-deadline bounding
  added by #20841) into the new `src/gobby/agents/tmux/session_activation.py`, keeping
  `TmuxSessionManager`'s public surface unchanged and updating imports in place.
- `lifecycle_monitor.py` (914 on 0.5.0) and `spawn_agent/_implementation.py` (977 on
  0.5.0, the worktree removed ~200 lines) are measured after the merge. If either
  reaches 1,000 lines, split it in this leaf: move the terminal-row consumers of
  `lifecycle_monitor.py` (capture, liveness, and cleanup calls that go through
  `TerminalRuntime`) into `src/gobby/agents/lifecycle_monitor_terminals.py`, and move
  the backend-selection and terminal-request construction of `_implementation.py` into
  `src/gobby/mcp_proxy/tools/spawn_agent/_terminal_backend.py`. If neither reaches the
  ceiling, record the measured sizes in evidence and leave both new paths uncreated.
- Tests and web: union of assertions. Port 0.5.0 assertions that go through renamed
  seams to the new seam (the 0.5.0 vLLM resume tests move to the worktree's fake-runtime
  fixture; `capture_pane`/`dispatch_keys` fakes become `snapshot_lines`/`write_key`
  fakes). `useTmuxSessions.ts` keeps 0.5.0's hook fixes and the worktree's
  backend-neutral message handling.
- Fabricated flip evidence (pulled forward from 1.3 after this leaf's third close
  review rejected on it): delete `docs/evidence/native-backend-flip.md` (its cited
  runs postdate their commits and W33 lacks the producer workflow) and
  `.github/workflows/terminal-parity-weekly.yml`; drop the committed-artifact
  assertion in `tests/terminals/test_backend_selection.py` and
  `tests/terminals/test_runtime_contract.py::test_weekly_workflow_is_the_parity_producer`;
  replace the guide's evidence pointer with a note that no repository-local
  evidence supports the native default and that 1.3 reverts it. The
  `check_native_backend_flip` gate and the native default themselves stay for 1.3.

After resolution: `uv sync`, `cargo build -p gobby-terminal --release --features
vt-engine`, `cargo build -p gobby-client --release`, `cd web && npm ci`, then guard
set G with the group 2 / schema-identity carve-out. Commit the merge as one merge
commit; follow-up resolution fixes may be separate commits on `0.5.0-test`. Mirror the
Guard set G bullet from `## Constraints` into
`docs/guides/gterminal-development-guide.md` under a "Guard set G" heading so later
leaves execute against the checked-in copy.

**Acceptance:**

- 1.1.1 - The `0.5.0-test` worktree exists at `~/.gobby/worktrees/gobby/0.5.0-test`, is registered active in gobby-worktrees, and its branch starts at the `0.5.0` HEAD recorded in evidence. behavior: "worktree registered" in `docs/guides/gterminal-development-guide.md`.
- 1.1.2 - `0.5.0-test` HEAD is a merge commit whose parents are `0.5.0` and `518cec5c41`; `git merge-base --is-ancestor wt-task-20255-m4 HEAD` and `git merge-base --is-ancestor <recorded 0.5.0 SHA> HEAD` both hold. behavior: "merge provenance" in `docs/guides/gterminal-development-guide.md`.
- 1.1.3 - Every 0.5.0 hunk in the agents, spawn, and websocket files listed above is kept, re-expressed, or explicitly superseded, with one evidence line per hunk. file: `src/gobby/agents/spawn_executor.py`.
- 1.1.4 - The #20805 no-op-resize guard lives in the module that handles `terminal_resize` for tmux rows, and 0.5.0's regression test for it passes against that module. file: `src/gobby/terminals/sync_bridge.py`.
- 1.1.5 - `terminal_input` on the websocket core handler never parses a tmux id as an agent uuid (`2367e0fb25` retained). file: `src/gobby/servers/websocket/handlers/core.py`.
- 1.1.6 - `session_manager.py` is split so that it and `session_activation.py` are each below 1,000 lines with `TmuxSessionManager`'s public surface unchanged. file: `src/gobby/agents/tmux/session_activation.py`.
- 1.1.7 - Both crates build and `cargo nextest run -p gobby-terminal -p gobby-client` is green; `uv run ruff check src/`, `uv run ruff format --check src/`, and `uv run mypy src/` are clean on the merged tree. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.
- 1.1.8 - Guard set G groups 1, 3, 5, 6, and 7 pass with only the schema-identity carve-out (owner 1.2) and the 1.4 red-test carve-out open; the evidence lists every carved-out test id and its behavioural failure reason. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 1.2 Ship the `terminals` DDL as migration 408 [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/408_terminals.sql`
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: embed migration 408 and advance the checksum pins
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog manifest for migration 408
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: regenerated grant bundle covering the terminals table
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: identity pins advance to 408
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: identity pins advance to 408
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated identity pin at 408
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: embedded migration count and head version
- `crates/gcore/src/grant/tests.rs::*` — scope-reason: bundle pin
- `crates/gcore/tests/catalog_manifest_freshness.rs::*` — scope-reason: freshness receipt
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: regenerated golden grant vector
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: regenerated golden grant vector
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: regenerated golden grant vector
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: regenerated golden grant vector
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: regenerated golden grant vector
- `tests/storage/test_terminals.py`

The merged `baseline.sql` carries the worktree's `CREATE TABLE terminals` (14 `CHECK`
constraints, five foreign keys, six indexes, one grant) and replaces
`agent_runs.tmux_session_name text` with `terminal_id uuid`; that hunk is what fails
the three baseline-lineage tests in group 4. Restore `baseline.sql` to 0.5.0's bytes
(`git checkout e19caa9a9f -- crates/gcore/assets/schema/baseline.sql`; `BASELINE_CHECKSUM`
stays `ec222a7f…`) and write `408_terminals.sql` carrying exactly that DDL for a hub at
407: `CREATE TABLE IF NOT EXISTS terminals (...)` with the same columns, named
constraints, and defaults; `ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS terminal_id uuid`
with the same foreign key, and `DROP COLUMN IF EXISTS tmux_session_name` (nothing reads
it after the merge; no `-- gobby:destructive` directive, per migration 400); the same
indexes (`IF NOT EXISTS`); the same `GRANT ... ON TABLE terminals TO gobby_daemon_runtime`.
Follow commit `ff4503c318` (#20899, migration 407) for the carriers: add the
`EmbeddedMigration { version: 408, filename: "408_terminals.sql", checksum: "<sha256 of the file>", sql: include_str!(...) }`
entry in `assets.rs`; advance the latest-version, latest-checksum, and root-hash pins in
`schema_contract.rs`, `cli_contract.rs`, `grant/tests.rs`; the embedded count (33) in
`runner_tests.rs` and `catalog_manifest_freshness.rs`; regenerate
`schema_expected_identity.json` with `scripts/generate_schema_expected_identity.py`
against the rebuilt `gdaemon`, and `catalog.manifest.json` with
`UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo nextest run -p gobby-core --features postgres --test catalog_manifest_freshness catalog_manifest_is_fresh_for_embedded_assets`
(`GOBBY_SCHEMA_TEST_DATABASE_URL` exported) — the merged manifest already lists the
`terminals` columns, so the regenerated file is expected to be byte-identical.
Regenerate the five golden vectors under `tests/runtime_grants/golden/` per the
docstring in `tests/runtime_grants/test_golden_vectors.py` (recompute each
`payload_checksum`, re-sign with `GOLDEN_SECRET`); `bundle.rs` changes only if its
non-postgres golden identity is touched, which it is not.

Add one real-DB test to `tests/storage/test_terminals.py`,
`test_migration_408_matches_baseline`: with `isolated_test_schema` (the
`tests/fixtures/postgres.py` helper) create two schemas and `apply_schema` both through
the installed `gdaemon`; rewind the second to the 407 shape (`DROP TABLE terminals
CASCADE`, `ALTER TABLE agent_runs DROP COLUMN terminal_id`, re-add
`tmux_session_name text`, delete the v408 `schema_migrations` receipt) and
`apply_schema` it again so the runner executes only 408; then assert that
`information_schema` columns, constraint definitions (`pg_get_constraintdef`), and
index definitions (`pg_indexes.indexdef`) for `terminals` and `agent_runs` are equal
between the two schemas, and that a third `apply_schema` leaves them unchanged
(idempotence). Pair it with a Rust twin in `runner_tests.rs` that applies
`with_migrations_for_test(&MIGRATIONS[..32])` (head 407) and then the full set,
asserting `migrations_applied == 1` and `catalog_manifest` equality against a fresh
full apply in a second schema.

The installed `~/.gobby/bin/` binaries stay at 407 until 2.1's live window: the
schema identity is compiled into every `gobby-core` consumer (`gdaemon`, `gcode`,
`ghook`, `gwiki`) and checked against the running daemon's grants, so a 408 install
next to the live `0.5.0` daemon breaks every other session's code-index calls.
Instead, `cargo build --release -p gobby-daemon` in the worktree, prove
`scripts/generate_schema_expected_identity.py --gdaemon target/release/gdaemon` reproduces
the checked-in `schema_expected_identity.json` byte-for-byte, and run every
`gdaemon`-backed gate with `GOBBY_NATIVE_BIN_DIR` pointed at a scratch directory whose
`gdaemon` links to `target/release/gdaemon` (link the installed `gcode`, `ghook`,
`gwiki` beside it; `src/gobby/utils/native_bin.py` honours that variable):
`target/release/gdaemon schema verify` on a fresh test schema, group 2, and the 408
tests. The schema-identity carve-out from 1.1 ends here; group 2 and group 4 run in
full. 2.1's `uv sync && uv run gobby install` from the worktree is where the 408
binaries reach `~/.gobby/bin/`.

**Acceptance:**

- 1.2.1 - `408_terminals.sql` exists and applies idempotently on a hub at 407, producing the `terminals` table and `agent_runs.terminal_id`. file: `crates/gcore/assets/schema/migrations/408_terminals.sql`.
- 1.2.2 - `assets.rs` embeds migration 408 with its checksum and every identity carrier reports latest version 408. file: `crates/gcore/src/schema/assets.rs`.
- 1.2.3 - A hub migrated from the 407 shape and a fresh apply have identical column, constraint, and index definitions for `terminals` and `agent_runs`, and a repeat apply changes nothing. test: `tests/storage/test_terminals.py::test_migration_408_matches_baseline`.
- 1.2.4 - `cargo nextest run -p gobby-core -p gobby-daemon` is green, including the schema contract, CLI contract, catalog freshness, and grant golden tests. file: `crates/gcore/tests/schema_contract.rs`.
- 1.2.5 - The worktree-built `gdaemon`'s probed identity reproduces `schema_expected_identity.json` byte-for-byte, and its `schema verify` passes on a fresh test schema through a `GOBBY_NATIVE_BIN_DIR` scratch directory; `~/.gobby/bin/` is untouched until 2.1's live window. file: `src/gobby/storage/schema_expected_identity.json`.
- 1.2.6 - Guard set G group 2 passes in full with `GOBBY_POSTGRES_TEST_DSN` exported. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 1.3 Revert the native default and strip the fabricated flip apparatus and placebo tests [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/config/terminals.py`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated contract after the default revert
- `src/gobby/install/shared/config/config.yaml::*` — scope-reason: `terminals.default_backend` reverts to tmux
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated manifest after the config edit
- `src/gobby/agents/spawn_models.py::*` — scope-reason: `resolve_terminal_backend` default follows the reverted config
- `src/gobby/servers/websocket/terminal_ws.py`
- `tests/config/test_terminals.py`
- `tests/config/test_runtime_config_contract.py::*` — scope-reason: contract regeneration check
- `tests/terminals/test_backend_selection.py`
- `docs/guides/gterminal-development-guide.md`
- `crates/gterminal/tests/control_protocol.rs`
- `crates/gterminal/tests/frame_protocol.rs`
- `tests/e2e/test_terminal_client_stack.py`

Commit `d07111cf2d` (#20284) flipped the shipped default on the strength of
`docs/evidence/native-backend-flip.md`, which cites run URLs for a workflow that had
not yet executed, and shaped `check_native_backend_flip` plus
`tests/terminals/test_backend_selection.py` to accept it. Undo the flip and delete the
apparatus:

- `TerminalConfig.default_backend` in `src/gobby/config/terminals.py` defaults to
  `"tmux"`; `terminals.default_backend: tmux` in `config.yaml`; regenerate
  `runtime_config_contract.json` and `bundled_content_manifest.json` with the existing
  generators so `test_checked_in_contract_matches_registry` is green. Delete
  `FlipGateResult`, `_Slot`, and `check_native_backend_flip` from `terminals.py`;
  `gcode grep -w check_native_backend_flip src tests` returns nothing afterwards.
  `resolve_terminal_backend` in `spawn_models.py` and the two `terminal_ws.py` lines
  the flip commit touched follow the config default with no native-specific branch.
- `docs/evidence/native-backend-flip.md` and
  `.github/workflows/terminal-parity-weekly.yml` (the producer of evidence that never
  ran) were already deleted in 1.1 — its third close review rejected on the
  artifact's fabricated run citations — together with the committed-artifact
  assertion in `test_backend_selection.py` and
  `test_runtime_contract.py::test_weekly_workflow_is_the_parity_producer`; confirm
  nothing outside `.gobby/plans/` references either path. Replace the "Default
  backend and rollback" section in `docs/guides/gterminal-development-guide.md`
  with a "Backend status" section: `tmux` is the default; `native` is explicit opt-in
  (`backend: native` on the spawn request or `terminals.default_backend`), requires an
  installed `gterm`, fails with a typed refusal when the host is unavailable, and is
  incomplete pending the follow-on epic (pending-row lifecycle, host respawn, write
  coordinator, gclient).
- `tests/terminals/test_backend_selection.py`: delete
  `test_flip_gate_rejects_every_nonconforming_artifact` and the artifact-shaping
  helpers (`_run_block`, `_bugs`, `_slot_pair`, `_conforming`); rewrite
  `test_flip_preserves_explicit_and_external` as
  `test_explicit_and_external_selection_under_tmux_default`, keeping its real
  assertions — an explicit `backend: native` request resolves to native, an external
  tmux session seeded through `seed_external_terminal` stays tmux, a native request
  with no host raises `HostCommandError`-derived refusal before fork with no tmux
  fallback, and a default request resolves to tmux. `tests/config/test_terminals.py`
  asserts the tmux default.
- `crates/gterminal/tests/control_protocol.rs` and `frame_protocol.rs`: delete every
  assertion of the form `format!("{}", <literal>) == "<literal>"` and any test whose
  body only asserts literals against themselves (eight in `control_protocol.rs`, two in
  `frame_protocol.rs` at planning time; sweep both files for the pattern). Every test
  that remains encodes or decodes a real message through the crate's public API; the
  evidence records, per file, one deliberate temporary breakage of an encoder or
  decoder that made a remaining test fail.
- `tests/e2e/test_terminal_client_stack.py`: delete clauses that cannot fail
  (`>= 0` on counts, `is not None` on values the fixture constructs, asserting an
  identifier equals itself). The isolated-daemon fixture and the clauses that assert
  real state — terminal rows and roster entries for a tmux spawn, one attention
  episode, run finalisation after kill — stay. Record each removed clause in evidence.

**Acceptance:**

- 1.3.1 - The shipped default backend is `tmux` in both the model and the bundled config, and the checked-in runtime config contract matches the registry. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 1.3.2 - `check_native_backend_flip`, `FlipGateResult`, and `_Slot` no longer exist and nothing references them. file: `src/gobby/config/terminals.py`.
- 1.3.3 - The fabricated evidence file and the weekly parity workflow (deleted in 1.1) stay absent and nothing outside `.gobby/plans/` references either path. behavior: "Backend status" in `docs/guides/gterminal-development-guide.md`.
- 1.3.4 - Explicit native selection, external tmux discovery, refusal before fork without a host, and the tmux default are proven under the reverted default. test: `tests/terminals/test_backend_selection.py::test_explicit_and_external_selection_under_tmux_default`.
- 1.3.5 - No literal-against-itself assertion remains in the gterminal acceptance tests and each remaining test fails under a recorded encoder/decoder breakage. file: `crates/gterminal/tests/control_protocol.rs`.
- 1.3.6 - The E1 stack test contains no tautological clause and still asserts terminal rows, roster, attention, and finalisation for a tmux spawn through the isolated daemon. file: `tests/e2e/test_terminal_client_stack.py`.
- 1.3.7 - The development guide's "Backend status" section documents tmux-default, native opt-in, the typed refusal, and what the follow-on epic owns. behavior: "Backend status" in `docs/guides/gterminal-development-guide.md`.

### 1.4 Turn the ten red tests green [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/runner_lifecycle_agents.py::*` — scope-reason: restart reconciliation of terminal-backed runs moves out
- `src/gobby/runner_lifecycle_reconcile.py`
- `src/gobby/agents/resume_executor.py::*` — scope-reason: Codex resume delivers through the composer
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: spawn paths go through the runtime only
- `src/gobby/agents/spawn_executor_support.py::*` — scope-reason: spawn paths go through the runtime only
- `src/gobby/terminals/runtime.py`
- `src/gobby/terminals/tmux_runtime.py`
- `src/gobby/terminals/native_runtime.py`
- `src/gobby/terminals/host_identity.py`
- `src/gobby/terminals/web_spawn.py`
- `src/gobby/storage/terminals.py`
- `tests/test_runner_lifecycle_restart_replay.py::*` — scope-reason: reconciliation assertions follow the moved function
- `tests/agents/test_resume_executor.py::*` — scope-reason: composer-delivery assertion
- `tests/terminals/test_no_direct_tmux_spawn.py`
- `tests/terminals/test_no_direct_tmux_consumers.py`

Group 1 at `518cec5c41` fails nine tests for behavioural reasons (the tenth,
`test_checked_in_contract_matches_registry`, is 1.3's). Make them green by fixing the
behaviour, never by loosening the tests:

- Restart reconciliation (`tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation::{test_provisional_resolution_skips_reconciliation_pending_run, test_reconcile_live_tmux_run_refreshes_pid_and_reader, test_reconcile_uses_configured_tmux_socket_for_live_agent, test_reconcile_missing_tmux_session_parks_and_resumes_run, test_reconcile_dead_tmux_pane_parks_and_resumes_run}`).
  `_reconcile_agent_runs_after_restart` (`src/gobby/runner_lifecycle_agents.py`, 945
  lines on 0.5.0) must: skip runs whose terminal row is still pending
  (`provisional` resolution); for a live tmux run, refresh the pid and reader through
  `TmuxTerminalRuntime.is_live`/`snapshot` using the configured socket; park and resume
  a run whose tmux session or pane is gone; treat a `None` backend as tmux for rows
  created before the migration; and route native rows through `NativeTerminalRuntime`
  (orphan the terminal, mark the run interrupted, hand to normal resume) instead of
  ignoring them. Move `_reconcile_agent_runs_after_restart` and its helpers into the
  new `src/gobby/runner_lifecycle_reconcile.py` — the split keeps
  `runner_lifecycle_agents.py` under the ceiling and gives the reconciliation its own
  module; `runner_lifecycle_agents.py` imports and calls it.
- Codex resume (`tests/agents/test_resume_executor.py::test_codex_resume_delivers_prompt_via_composer_not_argv`):
  the resume path builds the Codex command without the prompt in argv and delivers the
  prompt through the composer (`write_text(..., submit=True)` on the runtime), the same
  seam the spawn path uses after `0c7ee0db18`, and fails the run when delivery fails.
- No-direct-tmux guards (`tests/terminals/test_no_direct_tmux_spawn.py::{test_spawn_paths_use_runtime, test_identity_generation_absent_from_runtimes}`,
  `tests/terminals/test_no_direct_tmux_consumers.py::test_owned_consumers_are_backend_neutral`):
  remove every `TmuxSessionManager(` construction and `uuid4()` call from
  `src/gobby/terminals/*_runtime.py`, `web_spawn.py`, and `host_identity.py`; terminal
  identities are minted in `src/gobby/storage/terminals.py` (the `TerminalManager`
  insert path) and passed into `prepare_spawn`; spawn and resume paths reach tmux only
  through `TerminalRuntime`. Restore `_FIELD_SWEEP_ALLOWED` in the guard test to the
  narrow set that existed before it was widened at `518cec5c41`; the evidence shows the
  diff.

Each fix follows TDD: the named test's red run at the leaf's start (behavioural
failure, not collection), the minimal green, and the final full guard-set run.

**Acceptance:**

- 1.4.1 - A pending terminal row is skipped by restart reconciliation. test: `tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation::test_provisional_resolution_skips_reconciliation_pending_run`.
- 1.4.2 - A live tmux run has its pid and reader refreshed after restart. test: `tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation::test_reconcile_live_tmux_run_refreshes_pid_and_reader`.
- 1.4.3 - Reconciliation uses the configured tmux socket for a live agent. test: `tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation::test_reconcile_uses_configured_tmux_socket_for_live_agent`.
- 1.4.4 - A run whose tmux session is missing is parked and resumed. test: `tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation::test_reconcile_missing_tmux_session_parks_and_resumes_run`.
- 1.4.5 - A run whose tmux pane is dead is parked and resumed. test: `tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation::test_reconcile_dead_tmux_pane_parks_and_resumes_run`.
- 1.4.6 - Codex resume delivers the prompt through the composer, never argv. test: `tests/agents/test_resume_executor.py::test_codex_resume_delivers_prompt_via_composer_not_argv`.
- 1.4.7 - Spawn paths reach tmux only through `TerminalRuntime`. test: `tests/terminals/test_no_direct_tmux_spawn.py::test_spawn_paths_use_runtime`.
- 1.4.8 - No runtime module mints terminal identities. test: `tests/terminals/test_no_direct_tmux_spawn.py::test_identity_generation_absent_from_runtimes`.
- 1.4.9 - Owned consumers are backend-neutral. test: `tests/terminals/test_no_direct_tmux_consumers.py::test_owned_consumers_are_backend_neutral`.
- 1.4.10 - Restart reconciliation lives in its own module and `runner_lifecycle_agents.py` stays under 1,000 lines. file: `src/gobby/runner_lifecycle_reconcile.py`.
- 1.4.11 - Guard set G passes in full with no carve-out open. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

## P2: Prove and land
`kind: framing`

**Goal**: the merged daemon runs from `0.5.0-test` against a cloned hub, the tmux path
behaves at least as well as `0.5.0` HEAD in the real UI, and rollback is rehearsed.

### 2.1 Run the merged daemon from `0.5.0-test` and record the live matrix [category: test] (depends: P1)
`kind: deliverable`

Targets:
- `docs/guides/gterminal-development-guide.md`
- `docs/evidence/herdr-foundation-landing.md`

Before the window: `git merge 0.5.0` into `0.5.0-test` to absorb anything `0.5.0`
gained since 1.1 (resolve under the 1.1 rules; rerun guard set G if any file changed);
`git merge-base --is-ancestor 0.5.0 HEAD` must hold. Run the pre-push hook
(`lint/format/type/ts/frontend`) on `0.5.0-test`.

Execute the live window protocol from `## Constraints` verbatim (announce, stop,
clone `gobby_050test`, swap `bootstrap.yaml`, `uv sync && uv run gobby install`,
`GOBBY_ALLOW_WORKTREE_DAEMON=1 uv run gobby start --verbose`). Install the freshly
built `gterm` to `~/.gobby/bin/gterm` via a new inode before starting. Then run this
matrix, recording each row's outcome with the command or UI action and the observed
result in `docs/evidence/herdr-foundation-landing.md`:

1. Startup: the daemon starts, `gdaemon schema apply` applied 408 to `gobby_050test`,
   `/api/health` reports the `gterm` host state (running or absent with a reason), and
   `~/.gobby/logs/` shows no traceback in the first two minutes.
2. tmux spawn for Claude Code and Codex through `gobby-agents:spawn_agent` with no
   backend specified: `agent_runs.terminal_id` is set, the `terminals` row has
   `backend = 'tmux'`, the roster lists the run, and the agent reaches its first turn.
3. `gobby-sessions:send_keys` to each spawned agent delivers text and Enter (the
   #21110 retry path is exercised by sending during a busy turn).
4. Web terminal on each run: attach shows history up to the bound (#20815), scrolling
   is reliable, a resize that changes nothing does not repaint (#20805), a resize that
   changes size repaints once, a full-screen program (`less`) does not leak the
   alternate screen (#20793), a 20 KB paste arrives intact (#20778).
5. Attention: an agent prompt raises an attention episode and the web respond control
   answers it.
6. Daemon restart with both agents live: `uv run gobby restart` from the worktree with
   the override; both runs are reconciled live (not parked), and their terminals are
   still attachable.
7. Kill one agent through `gobby-agents:terminate_agent`: the tmux session is gone and
   the `terminals` row is finalised.
8. Explicit `backend: native` spawn with `gterm` installed: either a live native
   terminal with a frame stream reachable through the web proxy, or a typed refusal
   naming the host state — never a silent tmux fallback. Record which.
9. `gclient` starts from `~/.gobby/bin/gclient` (or `cargo run -p gobby-client`),
   probes daemon health, reports the host state, and exits cleanly.
10. Compare rows 2–7 against the same actions on the `0.5.0` daemon (from the rollback
    run): any row that behaves worse than `0.5.0` is a found bug fixed on `0.5.0-test`
    before this leaf closes, and the window is re-run.

End the window with the rollback sequence (stop, restore `bootstrap.yaml`,
`uv run gobby install` and `uv run gobby start` from the main checkout), confirm the
`0.5.0` daemon is healthy against the original database, and drop nothing yet — the
clone is dropped in L1. Add a "Landing status" section to
`docs/guides/gterminal-development-guide.md` naming what landed (P1, P2, §3.1/§3.2,
opt-in P4), what did not (gclient workspace, native default, parity suites, E1
assertions), the superseded QA plan path, and the follow-on epic's working title.

**Acceptance:**

- 2.1.1 - `0.5.0` is an ancestor of `0.5.0-test` HEAD and the pre-push hook passes there. behavior: "Landing status" in `docs/guides/gterminal-development-guide.md`.
- 2.1.2 - The merged daemon starts from the worktree against `gobby_050test`, applies 408, and reports the host state in `/api/health`. behavior: "row 1" in `docs/evidence/herdr-foundation-landing.md`.
- 2.1.3 - Matrix rows 2–9 are recorded with observed results, and every row that regressed against `0.5.0` was fixed and re-run before close. behavior: "rows 2–10" in `docs/evidence/herdr-foundation-landing.md`.
- 2.1.4 - The rollback sequence was executed at the end of the window and the `0.5.0` daemon came back healthy on the original database. behavior: "rollback" in `docs/evidence/herdr-foundation-landing.md`.
- 2.1.5 - The development guide carries the "Landing status" section. behavior: "Landing status" in `docs/guides/gterminal-development-guide.md`.

## L1 Landing into 0.5.0
`kind: verification`

Land only after 2.1 closes with a green window. From the main checkout on `0.5.0`:
`gobby-worktrees:merge_worktree` for the `0.5.0-test` worktree (a merge commit; never
a push). Then `uv run gobby install` and `uv run gobby restart` from the main checkout:
`gdaemon schema apply` applies 408 to the real hub. Watch the first ten minutes of
`~/.gobby/logs/` and one tmux spawn plus one web attach on the landed daemon. Then
`dropdb -h localhost -p 60891 gobby_050test`, `gcode vector cleanup-orphans`, and
`mark_worktree_merged` + `delete_worktree` for both `0.5.0-test` and
`wt-task-20255-m4` (the latter is merged by ancestry once `0.5.0` contains
`518cec5c41`). The next push to `0.5.0` is the first CI execution of `rust-ci.yml`
with the gterminal Zig build and the gclient jobs; a red job is found work if it blocks
`0.5.0`'s own CI, otherwise it is filed under the follow-on epic's CI phase.

## V1 Plan Changelog
`kind: verification`

**Draft** `kind: verification`

- source: investigation of `.gobby/plans/herdr-terminal-client.md`,
  `.gobby/plans/herdr-terminal-client-qa-fixes.md`, worktree `wt-task-20255-m4`, and
  `0.5.0` at `e19caa9a9f` (2026-08-27)
- decisions: merge-once/land-foundation/re-plan-client; strip fakes, keep real code
  opt-in; Lightweight depth; pre-landing live window from `0.5.0-test` on a cloned hub
- rounds: none (Lightweight)

## Task Mapping
`kind: framing`

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| epic | #21120 | open |
| 1.1 | #21121 | open |
| 1.2 | #21122 | open |
| 1.3 | #21123 | open |
| 1.4 | #21124 | open |
| 2.1 | #21125 | open |

Plan artifact: `.gobby/plans/runbooks.md`

# Runbooks: role-bound multi-agent tabs loaded from gclient

**Plan ID:** runbooks

## Context
`kind: framing`

Last night's overnight epic #22651 ran on a hand-built org chart: six gclient panes
(orchestrator, assistant, dispatcher, researcher, reviewer, janitor), each a persistent
interactive session launched by hand with a pasted prompt, wired together by a message
protocol and standing rules written in
`~/.claude/plans/daemon-crashed-hard-last-imperative-shannon.md`. Every seat ran as the
`default` agent: no role-scoped rules, no tool restrictions, no skill selection, and the
role lived only in the prompt, so a `/clear` or a CLI relaunch lost it.

This plan productizes that pattern as a **runbook**: a shell script of `gclient` verbs
that builds the tabs and split layout in the current workspace through the daemon's
existing workspace ops, waits for each pane's shell prompt, and types the pane's launch
command into it. gclient gains a command mode (`gclient <verb>`, decision 12) so such a
script needs nothing but the pane env. Each pane row remembers its role,
and every SessionStart in that pane activates the matching agent definition daemon-side,
so first launch, `/clear`, and a crash relaunch all rebind without agent cooperation.
Roles are separate agent definitions with rules that block code edits for the
non-coding seats and give the orchestrator one delegate-first nudge per context epoch.

Direction recorded, not acted on here: Josh intends runbooks to supersede `gobby build`
stage-manifest dispatch. This plan does not touch `src/gobby/build/`, the stage
registry, or `epic-reviewer`; the retirement is a deferred section.

Constraint that shaped the cut: ROADMAP.md moves the daemon to Rust family crates
(Stage 2) and makes the client carry operator verbs (S3.1). Stage 1 (#21543, #21551) is
open and unclaimed and `gdaemon` serves no routes yet, so the runbook logic lives in
gclient over the public WS API, and Python changes are limited to seams the client
cannot reach (schema, workspace ops, SessionStart, one MCP tool). No web UI, no
DB-backed runbook registry.

## Decision Record
`kind: framing`

Confirmed with Josh on 2026-09-21 during elicitation.

1. **Role binding.** New nullable `role` column on `workspace_panes` and nullable
   `runbook` on `workspace_tabs`. SessionStart resolves the session's bound terminal to
   its pane and passes `pane.role` as `agent_name_override`, activating the full
   definition (rules, blocked tools, skills, variables, persona prompt). The binding
   survives `move_pane`/`swap_panes` and is lost only on `close_pane`. Recovery and
   manual switching use a new `gobby-agents:apply_agent_definition` tool that applies
   the same full delta SessionStart uses (unlike `apply_persona`, which is prompt and
   skills only).
2. **Runbook store.** Superseded by decision 12 on 2026-09-22: a runbook is a shell
   script of `gclient` verbs. The bundled `orchestration-v1.sh` lives in
   `src/gobby/install/shared/workflows/runbooks/` and is run by path; user scripts live
   wherever the user keeps them. No YAML format in 1.x, no DB kind, no sync, no MCP
   CRUD, no web UI (deferred to a Stage 2 family crate).
3. **Noun.** "runbook" everywhere: menu item "Load runbook…", `runbooks/` directories,
   `workspace_tabs.runbook`, `workspace_panes.role`.
4. **Runbook shape.** Superseded by decision 12: a runbook is an ordered script of
   verbs (`new-tab`, `split`, `resize`, `title`, `wait-for-output`, `send-keys`). A
   pane with no launch line is a bare shell; a launch line without a role is a tool
   pane (nvim, a text browser); a role (bound with the `--role` flags once 1.2 lands)
   requires an agent CLI launch line. Commands stay literal shell strings (provider,
   model, effort, sandbox flags all live there), so no Rust port of the Python command
   builder. Launch waits for the shell prompt with `wait-for-output` and types the
   command through `pane.send_text {submit: true}`, leaving a live shell under the CLI
   to resume in.
5. **Python delta, thin seams only.** One gcore migration; `role`/`runbook` accepted on
   `pane.split`/`tab.create` plus a `pane.set_role` op; `role`, `runbook`, and the bound
   `session_ref` exposed on `get_workspace`, the WS snapshot, and events; SessionStart
   role resolution; `apply_agent_definition`. Nothing else in Python.
6. **Roster.** `gobby-workspaces:get_workspace` is the roster: agents find peers by
   role and message with `send_message(target="session", target_id=<session_ref>)`.
   The kickoff prompt carries the runbook name, the agent's own pane ref, and the tab
   refs.
7. **Restrictions as rules.** New rule group `runbook/`: a hard before_tool block on
   source-code writes for `agent_scope: [dispatcher, monitor, researcher, assistant]`
   (assistant keeps docs and plan paths), and a once-per-context-epoch orchestrator gate
   using `acknowledge_variable` with the standard clear/compact reset. Role definitions
   cherry-pick rule groups and skills; the default core-skill bootstrap is excluded for
   non-coding seats.
8. **Seats.** Superseded by decision 13 on 2026-09-23; the original text follows for
   the audit trail. Five persistent seats: `orchestrator`, `assistant`, `dispatcher`,
   `monitor` (the janitor, renamed), `researcher` (existing definition). No persistent
   reviewer: per-leaf review stays with `task-close-reviewer` (unchanged), and a new
   spawn-only `post-epic-reviewer` definition, derived from `epic-reviewer`, is spawned
   by the orchestrator once per sub-epic after its commits land. Provider, model, and
   effort live in the runbook's literal commands.
9. **Default agent untouched.** Runbook panes never activate `default`; its rules apply
   to a role only where that role's selectors include them.
10. **Bundled `orchestration-v1`.** Superseded by decision 14 on 2026-09-23; the original
    text follows for the audit trail. The live tab-0 layout with the reviewer pane
    removed: outer horizontal split (ratio 0.44); left column vertical split
    orchestrator over assistant (0.51); right column vertical split (0.35) with
    dispatcher | researcher on top (0.50) and monitor alone below. Reporting lines as
    they settled last night: dispatcher and monitor report to the assistant; the
    assistant and alarms reach the orchestrator.
11. **Deferred to the Rust port.** DB-backed runbooks (Stage 2 family crate); Rust
    activation and roster (S2.8, S2.11); `apply_agent_definition` in Rust (S2.12);
    retiring `gobby build` in favor of runbooks.
12. **Command mode replaces the loader.** The orchestrator's design, sent on Josh's
    word on 2026-09-22 ("send it to them, it's good"): `gclient <verb> [args]` with
    no TUI connects to the daemon the way the TUI does, sends one workspace op, prints
    the reply's `result`, and exits non-zero on a refused op. Every verb maps to an
    existing op, so the daemon gains nothing. Inside a gclient pane the defaults come
    from the pane env (`GOBBY_PANE_REF`, `GOBBY_WORKSPACE_ID`), so a runbook script
    needs no flags; the daemon URL comes from bootstrap through
    `gobby_core::daemon_url`, because the pane env deliberately carries no
    `GOBBY_DAEMON_URL`. The "Load runbook…" menu item, picker dialog, YAML parser,
    and replay state machine of the first draft are dropped. Auth is the local token,
    so remote targets wait for #20202 (S4.3). Work order: 1.4, then 2.1, then 1.1 to
    1.3, then P3.
13. **Roster and council flow** (supersedes 8; Program Director rulings Q1 and Q6 of
    2026-09-23, message `62c8f875`, applying #22691 criterion 10, Golden Path rulings 15,
    21, 22 and 23, and the flow Josh wrote into #22808). Eight persona definitions:
    `program-director`, `assistant`, `lane-manager`, `researcher`, `reviewer`, `archivist`,
    `log-monitor`, `elicitor`. The plan writer, enhancer and adversary are persona blocks on
    the existing `planner`, `plan-enhancer-taskless` and `plan-adversary-taskless`. No
    `post-epic-reviewer` (ruling 23: the Program Director reviews and lands every
    candidate) and no `plan-verifier`: its mechanical checks (`gobby plans validate`,
    `git diff --check`, the plan sha256, the coverage-contract checks) move into the
    adversary's finalization report. Council flow: the writer, enhancer and adversary
    debate to consensus with no round cap; the adversary finalizes to the Program
    Director, who may send it back; the assistant takes it to Josh; expansion waits for
    Josh. The fourth council pane is a read-only lookup helper on the `researcher`
    definition. Provider, model and effort stay in the launch lines (decision 4).
14. **Layout** (supersedes 10; ruling Q2 of the same message). Two bundled scripts on
    2.1's verbs, both mirroring the live workspace of 2026-09-23: `orchestration-v1.sh`
    builds a `control` tab (program director | assistant, horizontal 0.50) and a
    `monitors` tab (log monitor over archivist, vertical 0.50); `plan-council-v1.sh <plan>`
    builds one council tab named after the plan with four panes (writer on top, vertical
    0.50; below it enhancer | adversary | researcher at equal widths). Lane developer
    panes stay out of both scripts: the Program Director opens them per lane with
    `/goal` (ruling 23).
15. **Pending rulings Q3-Q5** (same message). #22713 folds as no block: a claiming
    session may amend its own task's `description`, `validation_criteria` and `labels`,
    the claim-time values must reach the close reviewer durably, and the disclosure order
    is injected at the edit; the durable carrier is chosen from the gobby#14332 lookup
    (an existing task history or audit surface first), and `task-close-reviewer.yaml`
    leaves the Non-goals only if the reviewer does not already receive that surface.
    Terminal backend (#22691 criteria 1-7, ruling 16): the 3.2 test that no definition
    names a backend and the registration text fold now; the runbook-script gterm
    assertion is dropped if workspace panes cannot fall back to tmux at all. #22695 stays
    2.1's leaf and keeps its gclient-lane slot: expansion adopts it if
    `start_expansion_run` can adopt an existing leaf, otherwise 2.1 cites it as existing
    work outside expansion.

## Non-goals
`kind: framing`

- No changes to `gobby build`, `src/gobby/dispatch/`, the stage registry,
  `epic-reviewer.yaml`, `task-close-reviewer.yaml`, or `default.yaml`.
- No web UI, no runbook CRUD over MCP or REST, no runbook rows in PostgreSQL.
- No pane placement for `spawn_agent` (workspaces decision record #10 stands).
- No change to the gclient direct-input keystroke path; the one-shot launch command
  uses the daemon's write coordinator.
- No multi-node runbooks; tabs load into the current node's current workspace.
- No YAML runbook format, picker dialog, or menu item in 1.x (decision 12).
- No `step_workflow` on the interactive roles: step programs only run for spawned
  sessions (`build_persona_changes` sets `step_workflow_complete` only when
  `is_spawned`), so the dispatcher loop stays a persona prompt.

## Constraints
`kind: framing`

- **Size guard.** Hand-maintained production files stay under 1,000 lines and the hook
  blocks threshold-crossing writes. Measured 2026-09-23: `src/gobby/terminals/workspace_ops.py`
  990, `src/gobby/storage/workspaces.py` 969, `crates/gclient/src/app/live_loop/menu.rs` 970,
  `crates/gclient/src/app/live_loop.rs` 938, `crates/gclient/src/app/mod.rs` 950,
  `crates/gclient/src/daemon/live.rs` 987, `crates/gclient/src/daemon/mod.rs` 989.
  `crates/gclient/src/app/live_loop/actions.rs` was 1,002 lines at `fff3192be0` and is 891
  after the #22780 merge `3467854a4d`, where `crates/gclient/tests/source_size.rs` passes
  (Program Director, 2026-09-23); the plan does not target that file. Deliverable 1.2 carries
  genuine splits; 2.1 lives in new `command` modules and touches `startup.rs` (691 lines)
  by two symbols and `daemon/workspace.rs` (391 lines) by one enum variant and two
  optional fields. gclient enforces the ceiling in `crates/gclient/tests/source_size.rs`.
- **Schema authority is gcore.** A migration is live only after the five derived
  carriers are refreshed (docs/contracts/plan-coverage.md derived-carriers row) and the
  binary set is rebuilt and promoted via `uv run gobby cutover` from the main checkout,
  announced with a `global` send_message first (memory f71268af).
- **WS op discovery.** `WORKSPACE_OPS` in `src/gobby/servers/websocket/workspace_ws.py`
  is derived by `inspect.getmembers(WorkspaceOps, inspect.iscoroutinefunction)` (inherited
  members included), so a mixin base class keeps every op name; new keyword params need
  plain class or `X | None` hints (`_fields` rejects anything else at import).
- **Daemon order of activation.** In `activate_materialized_session` the native
  terminal bind (`discover_and_bind_external_terminal`, then `retry_native_terminal_bind`)
  runs before `_activate_default_agent`, so a terminals-row lookup at activation time
  sees the deferred bind too.
- **Client couples through the public API only** (ROADMAP target architecture). gclient
  reads runbook files locally and issues ordinary workspace ops; nothing else.
- **Daemon `_place` semantics** (`src/gobby/storage/workspaces.py` `_place`,
  `_with_ratio`): `pane.split` replaces the `beside` leaf with `split(axis, 0.5,
  [beside, NEW])`, so the new pane is always the second child, and `pane.resize` sets
  the ratio of the split whose direct child is the named pane. Each resize must follow
  its split while both children are still leaves.
- **Validation commands.** Python: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <file>`,
  `uv run ruff check src/ tests/`, `uv run mypy src/`. Rust: `cargo nextest run -p gobby-client`,
  `cargo nextest run -p gobby-core -E 'test(schema)'` with `GOBBY_SCHEMA_TEST_DATABASE_URL`,
  `cargo clippy -p gobby-client`, `cargo fmt -p gobby-client -- --check`. Never the full
  pytest suite.

## P1: Daemon seams
`kind: framing`

Thin Python plus one gcore migration. 1.1 is schema only; 1.2 is the workspace rows and
ops; 1.3 is SessionStart activation; 1.4 is the manual tool. 1.4 is independent of the
others and lands first because 1.3 and 3.2 share its agents-guide edits.

### 1.1 Migration 450: pane role and tab runbook columns [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/450_add_runbook_roles.sql`
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated by the manifest freshness test
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: golden checksum, root hash, and latest_version literals move together
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: the identity literals move and the byte-limit test is added beside them
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated from the rebuilt gdaemon

Add migration 450: `ALTER TABLE workspace_panes ADD COLUMN role text` and
`ALTER TABLE workspace_tabs ADD COLUMN runbook text`, each with an `octet_length`
CHECK whose shape is copied from `workspace_panes_label_byte_limit` in migration 440
(`octet_length(label) <= 1024`, `440_add_workspaces.sql` 49-50). The limits themselves
are new, with no 128-byte precedent to copy: 128 bytes for `role`
(`workspace_panes_role_byte_limit`), 1024 for `runbook`
(`workspace_tabs_runbook_byte_limit`); `label` stays at 1024. No new GRANT: 440 already
granted DML on the three tables. The byte-limit test applies the embedded migrations
to the scratch database the way `catalog_manifest_freshness` does and inserts
over-limit rows.

Research context:
- Precedent commit `541320efc2` (migration 444) shows the carrier procedure; there is no
  single regenerate command. Order: (1) append an `EmbeddedMigration { version: 450,
  filename, checksum: <sha256 of the file bytes>, sql: include_str!(...) }` entry to
  `MIGRATIONS` in `crates/gcore/src/schema/assets.rs`; (2) regenerate the catalog
  manifest with the env-gated test
  `GOBBY_SCHEMA_TEST_DATABASE_URL=<scratch pg url> UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core --test catalog_manifest_freshness catalog_manifest_is_fresh_for_embedded_assets`;
  (3) `cargo build -p gobby-daemon` then `target/debug/gdaemon schema-identity --json`
  prints `latest_checksum` and `assets_root_hash` (`root_hash()` covers baseline,
  migrations, seed, and manifest, so it is final only after step 2); (4) update
  `GOLDEN_LATEST_CHECKSUM`, `GOLDEN_ASSETS_ROOT_HASH`, and `latest_version: 450` in
  `crates/gcore/src/grant/bundle.rs`, the literals in `schema_contract.rs`, and
  `latest_version` in `cli_contract.rs`; (5)
  `uv run python scripts/generate_schema_expected_identity.py --gdaemon target/debug/gdaemon`
  rewrites `src/gobby/storage/schema_expected_identity.json` (CI re-derives and compares).
- Latest existing migration is `449_workspace_default_project.sql` (adds
  `workspaces.default_project_id`); refs became zero-based in 442. On HEAD the
  `#[cfg(not(feature = "postgres"))]` fallback in `crates/gcore/src/grant/bundle.rs`
  carries `GOLDEN_LATEST_CHECKSUM` for 449 beside a stale `latest_version: 447` (#22618
  moved the checksum only); step (4) sets it to 450 and the stale literal goes with it.
- Live: the migration applies at the next `uv run gobby cutover`; `cutover` refuses
  uncommitted schema inputs, so commit the migration and carriers first.
- Rejected: reusing `label` as the role (display text is not a definition name);
  storing the role on the terminals row (a pane outlives CLI relaunches, a terminal row
  does not necessarily).

**Acceptance:**

- 1.1.1 - Migration 450 adds nullable `role` on `workspace_panes` and nullable `runbook`
  on `workspace_tabs`, with `octet_length` checks of 128 bytes on `role` and 1024 on
  `runbook`: a 129-byte role and a 1025-byte runbook are rejected, and `label` still
  accepts 1024. file: `crates/gcore/assets/schema/migrations/450_add_runbook_roles.sql`.
  test: `crates/gcore/tests/schema_contract.rs::migration_450_enforces_role_and_runbook_byte_limits`.
- 1.1.2 - `MIGRATIONS` embeds version 450 with its checksum and the catalog manifest
  lists both columns. symbol: `MIGRATIONS`. file: `crates/gcore/assets/schema/catalog.manifest.json`.
- 1.1.3 - The schema identity contract tests pass with `latest_version` 450. test:
  `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
  test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.
- 1.1.4 - `schema_expected_identity.json` matches the rebuilt gdaemon's identity.
  file: `src/gobby/storage/schema_expected_identity.json`.

### 1.2 Workspace rows and ops carry role, runbook, and session_ref [category: code] (depends: 1.1, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/workspaces.py::*` — scope-reason: dataclasses, inserts, the list/lookup queries, and a split of the layout helpers all change
- `src/gobby/storage/workspace_layout.py`
- `src/gobby/terminals/workspace_ops.py::*` — scope-reason: tab_create/pane_split params, new pane_set_role, role validation, and a split of the pane I/O methods
- `src/gobby/terminals/workspace_pane_io.py`
- `src/gobby/mcp_proxy/tools/workspaces/registry.py::*` — scope-reason: create_tab and split_pane gain params and set_pane_role is registered
- `src/gobby/cli/workspaces.py::*` — scope-reason: split --role, new panes set-role, pane line output
- `docs/contracts/gterm-protocols.md`
- `docs/guides/cli-commands.md`
- `docs/guides/gclient-user-guide.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/workspaces.md`
- `src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`
- `tests/storage/test_workspaces.py::*` — scope-reason: add role, runbook, and session_ref tests
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: add role and pane.set_role tests
- `tests/servers/test_workspace_ws.py::*` — scope-reason: extend the hand-enumerated OPS list and add the set_role round-trip
- `tests/mcp_proxy/test_workspaces_registry.py::*` — scope-reason: add set_pane_role and param-forwarding tests
- `tests/cli/test_workspaces.py::*` — scope-reason: add set-role and split --role tests

Storage: `WorkspaceTab` gains a trailing `runbook: str | None = None`; `WorkspacePane`
gains trailing `role: str | None = None` and a derived `session_ref: str | None = None`
(read from the row only when the key is present), so existing constructor sites,
including `tests/servers/test_terminal_ws_golden.py`, are untouched. `_insert_pane` and `WorkspaceManager.create_tab`
accept `role` (and `runbook` on the tab insert); `WorkspaceManager.add_pane` accepts
`role`; new `WorkspaceManager.set_pane_role(pane_id, role)` mirrors `rename_pane`
(strip, empty clears). `session_ref` is derived by one JOIN in the two read paths that
feed snapshots and activation, `list_panes` and `get_pane_for_terminal`:
`LEFT JOIN terminals tm ON tm.id = p.terminal_id AND tm.state IN ('pending','live')
LEFT JOIN sessions s ON s.id = tm.session_id LEFT JOIN projects pr ON pr.id = s.project_id`,
selecting `COALESCE(NULLIF(btrim(pr.name), ''), s.project_id::text) || '#' || s.seq_num::text`,
which mirrors `Session.ref` in `src/gobby/storage/session_models.py` including its
strip of `project_name`, so the roster ref is the one `send_message` resolves even when
`projects.name` carries surrounding whitespace. `RETURNING *` paths leave it None, which
is correct at insert. `to_dict()` stays `asdict` for the existing fields and drops
`role`, `runbook`, and `session_ref` when they are None (existing nulls such as
`title`, `terminal_id`, and `label` stay, because the fixtures already contain them),
so the golden corpus in `tests/fixtures/terminal_ws_golden/` is byte-identical and
every event, snapshot, and MCP payload carries the new fields only when set; gclient
ignores unknown keys on decode, so a set value still reaches the client. `move_pane`
and `swap_panes` are not rewritten to reinsert the pane row: `swap_panes` remaps
layout leaves and a cross-tab `move_pane` updates `tab_id` and `ref` only, so the role
column survives both (decision 1) and is lost only with the row on `remove_pane`. Split: move the layout helpers (`LayoutLeaf`,
`LayoutSplit`, `validate_layout`, `layout_pane_ids`, `_place`, `_with_ratio`,
`mint_pane_id`) out of `src/gobby/storage/workspaces.py` into the new
`src/gobby/storage/workspace_layout.py` and update importers, so the 969-line module
stays under the ceiling.

Ops: `WorkspaceEventKind` gains `pane.role_set`; `WorkspaceOps.tab_create` gains
kw-only `runbook: str | None = None, role: str | None = None`; `WorkspaceOps.pane_split`
gains `role: str | None = None`; new `WorkspaceOps.pane_set_role(actor, pane, role, *,
node=None)` copies `pane_rename` and emits `pane.role_set`. A private
`_require_persona_definition(role, project_id)` calls `resolve_agent` and
`supports_surface("persona")`, raising `WorkspaceOpError("invalid_op", ...)`, and runs
whenever a role is set: without it a typo yields a session with no activation at all
(1.3 failure mode). Split: move the pane I/O surface (`PaneOutputWait`,
`IDEMPOTENCY_KEY_PATTERN`, the `WAIT_CAPTURE_*` constants, `pane_send_text`,
`pane_send_keys`, `pane_read`, `pane_wait_for_output`, `_write`, `_pane_terminal`,
`_runtime`) out of `src/gobby/terminals/workspace_ops.py` into a mixin base class in
the new `src/gobby/terminals/workspace_pane_io.py`; `WORKSPACE_OPS` still discovers the
inherited coroutines. `PaneWrite` and `write_workspace_pane` already live in
`src/gobby/terminals/workspace_writes.py` (#22722) and stay there; `_write` keeps
delegating to them.

Surfaces: WS derives `pane.set_role` automatically (only the `OPS` list in
`tests/servers/test_workspace_ws.py` is enumerated by hand). MCP registry: `create_tab`
and `split_pane` forward the new params; new `set_pane_role(pane, role=None, node=None)`
registered after `rename_workspace_item` with a description that names the roster
contract (tabs carry `runbook`, panes carry `role` and `session_ref`). CLI:
`gobby panes split --role`, new `gobby panes set-role REF [ROLE]` (omit ROLE to clear),
and `_pane_line` prints `role` and `session_ref` when present; there is no `tabs create`
CLI, so `runbook` is WS/MCP-only and the docs say so. Docs: op vocabulary, snapshot row
fields, and the `pane.role_set` event in `docs/contracts/gterm-protocols.md`; the CLI
block in `docs/guides/cli-commands.md`; a "Tabs and panes" paragraph in
`docs/guides/gclient-user-guide.md`; the tool list in the gobby skill's
`references/sessions/workspaces.md`. The golden corpus fixtures are not regenerated:
rows without a role, runbook, or bound session serialize exactly as today, and
gclient's row structs tolerate the new keys when set (no `deny_unknown_fields`). Bind the seats
in the bundled runbook script: add `--runbook orchestration-v1` and `--role
orchestrator` to its `new-tab` line and `--role <seat>` to each `split` line; 2.1's
verbs forward those flags only when given.

Research context:
- Entry points: `WorkspaceOps.tab_create` (372-402), `pane_split` (460-490),
  `pane_rename` (541-548), `_fill` (823-873, returns the `set_pane_terminal` row, which
  already carries `role`), `_emit` (721-738); `WorkspaceManager.create_tab` (600-638),
  `add_pane` (722-735), `rename_pane` (817-822), `list_panes` (570-580),
  `get_pane_for_terminal` (888-893); `TerminalManager.bind_session` writes
  `terminals.session_id` (`src/gobby/storage/terminals.py` 663).
  `WorkspaceOps.workspace_snapshot` (332-368, now with a `project_id` filter) reads
  through `list_tabs`/`list_panes`, so snapshots carry the new fields without further
  edits; `workspace_list` (281-287) returns workspace rows only. Line numbers are
  2026-09-23 navigation hints only.
- Only external constructor site of the dataclasses: `tests/servers/test_terminal_ws_golden.py`
  (`WorkspaceTab(` / `WorkspacePane(` around 500-582); it compares `to_dict()` to the
  fixtures, and `crates/gclient/tests/ws_golden.rs` re-encodes the same files byte for
  byte, which is why the new keys are omitted when None rather than emitted as nulls:
  either leaf then closes without the other.
- `Session.ref` (`src/gobby/storage/session_models.py`) strips `project_name` before
  falling through to `project_id`; the SQL `btrim` mirrors that.
- `swap_panes` (`workspaces.py` 752-764) remaps layout leaves only; `move_pane`
  (766-805) updates `tab_id` and `ref`; `remove_pane` (737-750) deletes the row.
- Reusable: `resolve_agent` (`src/gobby/workflows/agent_resolver.py`) and
  `AgentDefinitionBody.supports_surface`.
- Rejected: a bind-time workspace event for `session_ref` (roster consumers read a
  snapshot; SessionStart binding emits no workspace event today and does not need to);
  a roster service or dedicated `gobby-runbooks` MCP server (get_workspace already is
  the roster).
- Planned checks: the five test files above plus
  `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_workspaces.py tests/terminals/test_workspace_ops.py tests/servers/test_workspace_ws.py tests/mcp_proxy/test_workspaces_registry.py tests/cli/test_workspaces.py tests/servers/test_terminal_ws_golden.py`,
  `wc -l` on both split modules under 1,000, ruff, mypy.

**Granularity:** more than six production Target files. Kept as one leaf because the
column, the op, the derived field, and the three surfaces are one wire contract that
cannot be verified independently; the two module splits are mechanical moves required
by the size guard, not separate outcomes.

**Acceptance:**

- 1.2.1 - `WorkspaceTab.runbook` and `WorkspacePane.role`/`session_ref` round-trip
  through `create_tab`, `add_pane`, and `set_pane_role`; `list_panes` derives
  `session_ref` as `<project>#<seq>` for a pane whose terminal is bound to a session,
  with the project name stripped and an empty name falling through to `project_id`;
  `to_dict()` includes a set role and runbook and omits the three keys when None; a
  role survives `swap_panes` and a cross-tab `move_pane` and is gone after `remove_pane`.
  test: `tests/storage/test_workspaces.py::test_role_runbook_round_trip_and_set_pane_role_clears`.
  test: `tests/storage/test_workspaces.py::test_list_panes_derives_session_ref_from_bound_terminal`.
  test: `tests/storage/test_workspaces.py::test_to_dict_omits_unset_role_runbook_and_session_ref`.
  test: `tests/storage/test_workspaces.py::test_role_survives_swap_and_move_and_dies_with_remove`.
- 1.2.2 - `tab.create` and `pane.split` accept `role` (and `runbook` on tab.create) and
  `pane.set_role` emits `pane.role_set`; a role that is not a persona-capable definition
  is refused with `invalid_op`. test:
  `tests/terminals/test_workspace_ops.py::test_tab_create_and_pane_split_carry_role_and_runbook`.
  test: `tests/terminals/test_workspace_ops.py::test_pane_set_role_emits_role_set_event`.
  test: `tests/terminals/test_workspace_ops.py::test_pane_set_role_rejects_non_persona_definition`.
- 1.2.3 - The WS op table and MCP registry expose `pane.set_role` / `set_pane_role` and
  the new params. test: `tests/servers/test_workspace_ws.py::test_pane_set_role_op_round_trips`.
  test: `tests/mcp_proxy/test_workspaces_registry.py::test_set_pane_role_tool_updates_pane`.
- 1.2.4 - `gobby panes set-role` and `gobby panes split --role` call the tools. test:
  `tests/cli/test_workspaces.py::test_panes_set_role_calls_tool`.
  test: `tests/cli/test_workspaces.py::test_panes_split_forwards_role`.
- 1.2.5 - Layout helpers live in `workspace_layout.py` and pane I/O in
  `workspace_pane_io.py`; both original modules stay under 1,000 lines and the golden
  corpus stays byte-identical with the fixtures unchanged. file: `src/gobby/storage/workspace_layout.py`.
  file: `src/gobby/terminals/workspace_pane_io.py`.
  test: `tests/servers/test_terminal_ws_golden.py::test_python_matches_terminal_ws_golden_corpus`.
- 1.2.6 - Protocol, CLI, and user-guide docs describe `role`, `runbook`, `session_ref`,
  `pane.set_role`, and `pane.role_set`. behavior: "Workspace messages" in
  `docs/contracts/gterm-protocols.md`.
- 1.2.7 - The bundled runbook script binds the tab's runbook and every seat's role
  through the `--runbook` and `--role` flags. file:
  `src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`.

### 1.3 SessionStart activates the pane's role [category: code] (depends: 1.2, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/terminal_runtime.py::*` — scope-reason: add the new pane_role_for_session helper beside the bind helpers
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::activate_materialized_session`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_pre_created_session`
- `docs/guides/agents.md`
- `docs/guides/sessions.md`
- `tests/hooks/test_pane_role_activation.py`
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: add the native-bind role activation test
- `tests/hooks/test_session_materialize.py::*` — scope-reason: add the retry-bind role activation test

New helper `pane_role_for_session(handler, session_id) -> str | None` (new symbol):
`handler.terminal_manager.get_live_for_session(session_id)` (one indexed query), then
`WorkspaceManager(terminal_manager.db).get_pane_for_terminal(terminal.id)`, returning
`pane.role`. At the two activation call sites the override becomes
`input_data.get("agent_name_override") or pane_role_for_session(handler, session_id)`,
so an explicit override (web-chat launch) still wins and the pane role beats an
existing `_agent_type` and the config default through `resolve_agent_name`. Both call
sites already sit after `retry_native_terminal_bind`, so the deferred-bind path is
covered; a session whose bind was refused twice stays unbound and resolves normally.
Document the known limitation that a tmux-inside-gterm session binds its tmux row, not
the pane's native row, so no role applies there.

Research context:
- `discover_and_bind_external_terminal` (`terminal_runtime.py` 98-134) returns a
  pending tuple only when the bind was refused and `None` on success, so it is not a
  "bound" signal; the terminals row is the cheapest correct source
  (`TerminalManager.get_live_for_session`, `src/gobby/storage/terminals.py` 624-635).
- Call sites: `activate_materialized_session` (`materialize.py` ~397-405) and
  `handle_pre_created_session` (`flow.py` ~665-673), both behind
  `skip_default_agent_activation`.
- `activate_default_agent` (`_session_start/agents.py` 96-241) resolves the name via
  `resolve_agent_name` (override, existing `_agent_type` if not default, config,
  default) and applies `build_persona_changes(is_spawned=False)`: `_agent_type`,
  `_active_rule_names`, `_active_skill_names`, `_skill_format`, definition variables,
  `_agent_blocked_tools`, `_agent_blocked_mcp_tools`. The persona prompt is prepended on
  the first before_agent by `_inject_agent_instructions_if_needed` (`_agent.py`
  256-348) using `prompts.persona`, so every role definition needs the `persona`
  surface (enforced by 1.2's validation).
- Failure mode motivating 1.2's validation: an unresolvable override makes
  `activate_default_agent` log and return None, merging nothing; reconcile later
  backfills `default` with no active rules.
- Compaction keeps the role (same row, same variables); `/clear` creates a successor
  in the same pane, whose SessionStart resolves the same pane row. No change to
  `_bind_clear_successor` or `get_handoff` is needed.
- Rejected: the single edit inside `resolve_agent_name` (would add two queries to every
  reconcile repair); stamping the role into the handoff markdown (agent-cooperative and
  lost on crash relaunch).
- Planned checks: the three test files with the isolated hub DSN; mypy.

**Acceptance:**

- 1.3.1 - A session bound to a pane with a role activates that definition; an explicit
  `agent_name_override` beats the pane role; an unbound session ignores it. symbol:
  `pane_role_for_session`. test: `tests/hooks/test_pane_role_activation.py::test_pane_role_is_used_as_override_when_none_given`.
  test: `tests/hooks/test_pane_role_activation.py::test_explicit_override_beats_pane_role`.
  test: `tests/hooks/test_pane_role_activation.py::test_unbound_session_ignores_pane_role`.
- 1.3.2 - End to end through the native bind: after `create_tab(role=...)` and a
  SessionStart carrying that terminal id, `_agent_type` equals the role. test:
  `tests/hooks/test_session_start_handlers.py::test_pane_role_activates_definition_after_native_bind`.
- 1.3.3 - The role also applies when the native bind lands on the retry path. test:
  `tests/hooks/test_session_materialize.py::test_pane_role_applies_on_retry_bind_path`.
- 1.3.4 - Guides describe pane-bound roles, the `/clear` and relaunch behavior, and the
  tmux limitation. behavior: "pane-bound roles" in `docs/guides/agents.md`.

### 1.4 gobby-agents:apply_agent_definition [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/apply_persona.py::*` — scope-reason: add the new apply_agent_definition_impl and ALWAYS_REAPPLY_KEYS beside apply_persona_impl
- `src/gobby/hooks/event_handlers/_session_start/agents.py::activate_default_agent`
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py::*` — scope-reason: one tool registration added beside apply_persona
- `docs/guides/agents.md`
- `docs/guides/mcp-tools.md`
- `src/gobby/install/shared/skills/gobby/references/agents/personas.md`
- `tests/mcp_proxy/tools/test_apply_agent_definition.py`

New `apply_agent_definition_impl(agent, db=None, session_id=None, variables=None,
cli_source=None)` (new symbol), structured like `apply_persona_impl`: resolve the
caller session; refuse spawned sessions (`agent_run_id`/`agent_depth` or
`is_spawned_agent`) because rules and blocked tools are rewritten; require the `persona`
surface with the same error text; `build_persona_changes(agent_body, session_id, db,
is_spawned=False)`; keep a change only if its key is in the always-reapply identity set,
is a definition-owned `workflows.variables` key, or is absent from the session (so live
state and task claims survive); add `_persona_name`, `_agent_identity_reinject: True`,
`_agent_context_injected: False` after the filter, so those three replace live values
(a session whose `_persona_name` still names the previous persona would otherwise keep
prompting as it, because `_inject_agent_instructions_if_needed` prefers a non-empty
`_persona_name` over `_agent_type`); run `colliding_persona_variable_error`; merge.
Never touches `agent_step_instances`. Extract the seven identity keys from
`activate_default_agent` into `ALWAYS_REAPPLY_KEYS` (new symbol) in `apply_persona.py`,
exactly `_agent_type`, `_active_rule_names`, `_active_skill_names`, `_skill_format`,
`_agent_blocked_tools`, `_agent_blocked_mcp_tools`, and `is_spawned_agent`, and import
it there so the two cannot drift. Register
`apply_agent_definition(agent: str, variables: dict | None = None)` next to
`apply_persona` and document it as the full lifecycle switch versus the narrow persona
switch.

Research context:
- `apply_persona_impl` (`apply_persona.py` 196-305) and `build_session_persona_changes`
  (120-139) write only `_persona_name`, `_active_skill_names`, `_skill_format`, and the
  reinject flags; `build_persona_changes` (40-117) is the full delta and also emits
  every enabled variable default not already in `changes` (~96-104), which is why the
  existing-variable filter is required. `activate_default_agent` applies that filter
  today (`agents.py` ~150-190).
- Registration precedent: `agents_spawn_tools.py` 107-122; `_session_is_spawned`
  (`src/gobby/hooks/session_activation.py` 693-698).
- Precedent tests: `tests/mcp_proxy/tools/test_apply_persona.py` `TestApplyPersonaImpl`
  (411-769, fixture `db` at 27-29) and `test_stepful_persona_preserves_lifecycle_and_enforcement_state`
  (551-637) for the step-instance invariant.
- Rejected: a `full=True` flag on `apply_persona` (two behaviors behind one name, and
  the narrow tool must keep refusing nothing for spawned sessions).

**Acceptance:**

- 1.4.1 - The tool sets `_agent_type`, active rules, blocked tools, skills, and the
  reinject flags on the caller session. symbol: `apply_agent_definition_impl`. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_happy_path_sets_agent_type_rules_blocked_tools_and_reinject_flags`.
- 1.4.2 - Unowned session variables and task claims survive; definition variables
  reapply; variable defaults do not clobber live values. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_preserves_unowned_session_variables_and_claims`.
  test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_definition_variables_reapply_but_defaults_do_not_clobber`.
- 1.4.3 - Spawned sessions and definitions without the persona surface are refused; step
  instances are never created or replaced. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_refuses_spawned_session`.
  test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_requires_persona_surface`.
  test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_never_touches_agent_step_instances`.
- 1.4.4 - `_persona_name` is set so the next-turn prompt matches `_agent_type`, starting
  from a caller whose `_persona_name` already names a different persona; and
  `activate_default_agent` imports `ALWAYS_REAPPLY_KEYS`, which holds exactly the seven
  identity keys. symbol: `ALWAYS_REAPPLY_KEYS`.
  test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_sets_persona_name_so_prompt_matches_agent_type`.
  test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_always_reapply_keys_are_the_seven_identity_keys`.
- 1.4.5 - The agents guide, MCP tool list, and personas reference document the tool.
  behavior: "apply_agent_definition" in `docs/guides/agents.md`.

## P2: gclient command mode
`kind: framing`

One Rust leaf. `gclient <verb>` runs without the TUI, sends one workspace op over the
same authenticated WebSocket the TUI uses, prints the reply, and exits. A runbook is a
shell script of these verbs, so the loader, picker, and YAML format of the first draft
are gone (decision 12). Rust conventions per `crates/AGENTS.md` (load the `rust` skill;
tests in `<module>/tests.rs`). Work order: after 1.4 and before 1.1.

### 2.1 gclient command mode and the bundled orchestration-v1 script [category: code]
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs`
- `crates/gclient/src/command/verbs.rs`
- `crates/gclient/src/command/tests.rs`
- `crates/gclient/src/lib.rs::*` — scope-reason: one `pub mod command;` line in a module list that has no indexed symbols
- `crates/gclient/src/startup.rs::run`
- `crates/gclient/src/startup.rs::USAGE`
- `crates/gclient/src/daemon/workspace.rs::*` — scope-reason: optional `runbook` on `TabCreate` and `role` on `PaneSplit`, skipped when None, beside the existing op fields; one `WorkspaceEventKind::PaneRoleSet` variant
- `crates/gclient/tests/workspace.rs::*` — scope-reason: one `pane.role_set` event fixture beside the existing `pane_added_event`
- `crates/gclient/tests/command_mode.rs`
- `crates/gclient/tests/mock_daemon/workspace.rs::*` — scope-reason: `WorkspaceSim::apply` answers `pane.read` and `pane.wait_for_output` for the script test
- `src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`
- `docs/guides/gclient-user-guide.md`

Entry. `startup::run` checks the first argument before `parse_args`: when it names a
verb, `command::dispatch(args, env)` runs it and `run` returns its exit code; otherwise
TUI startup is unchanged. `USAGE` gains one line pointing at `gclient help`. Connection
reuses the TUI's resolution: `gobby_core::daemon_url::daemon_url()` for the URL and
`~/.gobby/local_cli_token` through `gobby_core::local_token::read_local_cli_token` for
the token, with `--daemon-url` and `--token-file` overrides, then `LiveDaemon::connect`
and `Daemon::workspace_op` (both already public through the `daemon` module) for every
verb; the socket closes on exit. The pane env carries `GOBBY_NODE_ID`, `GOBBY_NODE_REF`,
`GOBBY_WORKSPACE_ID`, `GOBBY_TAB_ID`, `GOBBY_PANE_ID`, `GOBBY_PANE_REF`, and
`GOBBY_TERMINAL_ID` and, by design, no `GOBBY_DAEMON_URL` or `GOBBY_PROJECT_ID`
(`_identity_env` in `src/gobby/terminals/workspace_ops.py`; finding F4 of the
gclient-workspaces plan). Defaults therefore are: REF omitted means `$GOBBY_PANE_REF`,
`--workspace` omitted means `$GOBBY_WORKSPACE_ID`, and the daemon URL comes from
bootstrap. Refs use the daemon's grammar (`WorkspaceManager.resolve_reference`): a uuid,
or `w`, `n:w`, `n:w:t`, `n:w:t:p`; the client passes them through untouched and only
counts segments to choose the tab or pane form of a verb.

Event. `WorkspaceEventKind` in `daemon/workspace.rs` is a closed serde enum, and
`daemon_event` in `daemon/live_reader.rs` (372-414) turns an unknown kind into a
protocol error that `run_connection` (132) answers by dropping the socket. 1.2 adds
`pane.role_set`, so this leaf adds `WorkspaceEventKind::PaneRoleSet`
(`#[serde(rename = "pane.role_set")]`) ahead of it; no match arm changes, because
`WorkspaceModel::apply` already upserts the enclosed pane row through its wildcard arm.
Without the variant, V1 step 6's `gobby panes set-role` disconnects every attached
gclient.

Verbs. Each maps to one existing op; `--json` prints the reply's `result` verbatim,
which `workspace_ws.py` already returns synchronously, so the daemon gains nothing.
- `list [--workspace REF]` sends `workspace_attach` (the existing attach request; the
  snapshot is the roster and the subscription dies with the socket). Plain output is
  one line per tab (`ref  title`) and per pane (`ref  label  terminal_id`).
- `new-tab --project NAME|ID [--name TITLE] [--workspace REF] [--runbook NAME] [--role ROLE]`
  sends `tab.create`; a project name resolves through the REST lookup the TUI uses
  (`startup::resolve_project_at`). Plain output: `<tab ref> <first pane ref>`.
  `--runbook` and `--role` are forwarded only when given; the daemon accepts them once
  1.2 lands and refuses them with `invalid_op` before that.
- `split REF --right|--down [--role ROLE] [--cmd TEXT]` sends `pane.split` with the
  `LayoutAxis` spelling (`horizontal` for `--right`, `vertical` for `--down`); `--cmd`
  chains one `pane.send_text {submit: true}` to the new pane. Plain output: the new
  pane ref.
- `resize REF RATIO` sends `pane.resize` (the ratio of the split whose direct child is
  REF, per Constraints).
- `title REF TEXT` sends `pane.rename {label}` for a four-segment ref and
  `tab.rename {title}` for three.
- `select REF` sends `workspace.set_focus_hints {workspace, project_id: null, tab, pane}`,
  the seed the next gclient window focuses from; the TUI already sends this op.
- `send-keys REF TEXT [--enter]` sends `pane.send_text {text, submit}`.
- `capture-pane REF [--lines N]` sends `pane.read {lines}` (default 50) and prints the
  snapshot text. No `--ansi`: the runtime snapshot (`SnapshotResult`) is plain text.
- `wait-for-output REF --pattern REGEX [--timeout S] [--interval S]` sends
  `pane.wait_for_output {pattern, timeout_seconds, poll_interval_seconds}`; the daemon
  polls, so there is no client loop. The verb sets its own request deadline of the
  timeout plus a margin, because `LiveDaemon::request` applies a per-request deadline.
  Exit 0 on `matched`, 1 on `timeout`, 2 on `pane_lost`.
- `kill REF` sends `pane.close` (four segments) or `tab.close` (three).
- `help` prints the verb table.
Exit codes: 0 success; 1 refused op, printed as `code: reason` on stderr from the
`workspace_error` reply; 2 usage; 3 connection, token, or protocol failure.

Script. `orchestration-v1.sh` (`#!/usr/bin/env bash`, `set -euo pipefail`) reproduces
decision 10 with refs captured from plain output: `new-tab --project "${1:-gobby}"
--name orchestration` gives `$tab` and `$orch`; `split "$orch" --right` gives `$disp`,
then `resize "$orch" 0.44`; `split "$orch" --down` gives `$asst`, `resize "$orch" 0.51`;
`split "$disp" --down` gives `$mon`, `resize "$disp" 0.35`; `split "$disp" --right`
gives `$res`, `resize "$disp" 0.50`; five `title` lines; then for each seat
`wait-for-output REF --pattern "$PROMPT_PATTERN" --timeout 30` followed by
`send-keys REF "<launch line>" --enter`. `PROMPT_PATTERN` defaults to `'[%$#] *$'`.
The launch lines are the night runbook's commands (orchestrator `claude --model fable`,
assistant `claude --model opus --effort high`, dispatcher
`codex -m gpt-5.6-sol -c model_reasoning_effort=high`, researcher `claude`, monitor
`claude --model sonnet --effort medium`), each followed by a kickoff prompt that names
the runbook, the seat's own pane ref, and the tab ref; the user edits the script
freely. The `--runbook` and `--role` flags are added to the script by 1.2, which lands
after this leaf.

Research context:
- `startup::run` (`crates/gclient/src/startup.rs` 674-691) parses args, probes health,
  and starts the TUI; `resolve_probe_env` (450-468) holds the URL and token defaults to
  reuse. `LiveDaemon::connect` (`daemon/live.rs` 190), `Daemon::workspace_op`
  (`daemon/mod.rs` 459), `op_request` (`daemon/workspace.rs` 355-361), and
  `workspace_op_live` (`daemon/live_workspace.rs` 33) are the whole send path;
  `attach_request(node, workspace, project_id)` (`workspace.rs` 336-352) serves `list`
  with `project_id` None, since the verb always names the workspace. `WorkspaceOp`
  already has every variant the verbs need (`workspace.rs` 175-332); the newer
  `WorkspaceList` (`workspace.list`) returns workspace rows, not the tab/pane roster, so
  `list` stays on attach. `WorkspaceModel::apply` is at
  `crates/gclient/src/app/workspace_ops.rs` 47-83. Line numbers are 2026-09-23
  navigation hints only.
- Daemon side: `WORKSPACE_OPS` in `src/gobby/servers/websocket/workspace_ws.py`
  derives the op table and `_arguments` rejects unknown fields, which is why
  `--runbook` and `--role` are omitted when not given. Signatures in
  `src/gobby/terminals/workspace_ops.py`: `tab_create` 372, `pane_split` 460,
  `pane_resize` 532, `pane_rename` 541, `pane_close` 550, `pane_send_text` 571,
  `pane_read` 628, `pane_wait_for_output` 640, `workspace_set_focus_hints` 310.
- Tests: `crates/gclient/tests/mock_daemon/mod.rs` (`MockDaemon::start`, `requests()`,
  `enqueue_workspace_refusal`) and `mock_daemon/workspace.rs` (`WorkspaceSim::apply`
  simulates the layout ops with the daemon's `_place` semantics; `knows` accepts every
  other op) back `command_mode.rs`. Extend `WorkspaceSim::apply` to answer `pane.read`
  and `pane.wait_for_output` (matched) where it does not yet. The script test runs
  `bash` on the real script with `env!("CARGO_BIN_EXE_gclient")` first on `PATH` and
  `--daemon-url` pointing at the mock, then compares the simulator's final tree with
  decision 10. Pane-env defaults are tested by passing an explicit env map to
  `dispatch`, never by mutating the process env.
- Rejected: a client-side capture loop for `wait-for-output` (the daemon op exists);
  `--cwd` on `split` (`pane.split` spawns the tab's checkout shell; `--cmd 'cd DIR'`
  covers it); a REST route for one-shot ops (the WS reply already carries `result`);
  a parser crate (the TUI parses by hand and the verb table is small); a `GOBBY_DAEMON_URL`
  pane variable (removed deliberately by the workspaces plan).
- Planned checks: `cargo nextest run -p gobby-client -E 'test(command)'`,
  `cargo clippy -p gobby-client`, `cargo fmt -p gobby-client -- --check`,
  `bash -n src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`, and the
  size gate `crates/gclient/tests/source_size.rs`.

Consumers unchanged:
- `crates/gclient/src/main.rs` — no-edit-reason: calls `startup::run()` with the same signature; the verb check lives inside `run`.
- `crates/gclient/src/views/mod.rs` — no-edit-reason: its `run` wrapper (36-37) calls `crate::startup::run()` with the same signature; the verb check lives inside `startup::run`.

**Acceptance:**

- 2.1.1 - `gclient <verb>` runs without the TUI: each verb sends exactly its op with
  the documented fields, `--json` prints the reply `result` verbatim, and TUI argument
  parsing is unchanged. symbol: `run`. test:
  `crates/gclient/tests/command_mode.rs::each_verb_sends_its_workspace_op`.
  test: `crates/gclient/tests/command_mode.rs::json_prints_reply_result_verbatim`.
- 2.1.2 - Inside a pane, REF and `--workspace` default from `GOBBY_PANE_REF` and
  `GOBBY_WORKSPACE_ID`; outside one, omitting them is a usage error (exit 2). file:
  `crates/gclient/src/command.rs`. test:
  `crates/gclient/src/command/tests.rs::pane_env_supplies_default_refs`.
  test: `crates/gclient/src/command/tests.rs::missing_ref_outside_a_pane_is_usage_error`.
- 2.1.3 - A refused op prints `code: reason` on stderr and exits 1; `wait-for-output`
  exits 0, 1, or 2 on matched, timeout, or pane_lost; a connection or token failure
  exits 3. test:
  `crates/gclient/tests/command_mode.rs::refused_op_exits_one_with_code_and_reason`.
  test: `crates/gclient/tests/command_mode.rs::wait_for_output_exit_codes_follow_reason`.
- 2.1.4 - `new-tab --runbook`, `new-tab --role`, and `split --role` forward the fields
  only when given, so existing op payloads stay byte-identical. symbol: `WorkspaceOp`.
  test: `crates/gclient/tests/ws_golden.rs::corpus_replays_from_canonical_manifest`.
  test: `crates/gclient/src/command/tests.rs::optional_role_and_runbook_are_omitted_when_absent`.
- 2.1.5 - The bundled script is valid bash, and running it against the mock daemon
  reproduces decision 10's tree with five titled panes and five prompt-gated launches.
  file: `src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`. test:
  `crates/gclient/tests/command_mode.rs::orchestration_v1_script_reproduces_decision_10_layout`.
- 2.1.6 - The user guide documents command mode: the verb table, exit codes, pane-env
  defaults, and runbook scripts. behavior: "Command mode" in
  `docs/guides/gclient-user-guide.md`.
- 2.1.7 - A `workspace_event` whose kind is `pane.role_set` decodes as
  `WorkspaceEventKind::PaneRoleSet` and `WorkspaceModel::apply` upserts the enclosed
  pane row, so an attached client survives a role change. symbol: `WorkspaceEventKind`.
  test: `crates/gclient/tests/workspace.rs::pane_role_set_event_decodes_and_upserts_pane`.

## P3: Roles
`kind: framing`

YAML only. Templates sync to the DB registry on daemon start (`sync_bundled_agents`,
rule sync); the installed rows are the live definitions.

### 3.1 Rule group runbook: no-code-edits and the orchestrator delegate nudge [category: config]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/runbook/runbook-no-code-edits.yaml`
- `src/gobby/install/shared/workflows/rules/runbook/orchestrator-delegate-once.yaml`
- `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml::*` — scope-reason: declare one new variable default
- `docs/guides/workflow-rules.md`
- `tests/workflows/test_runbook_rules.py`

Group tags `[runbook, enforcement, gobby]`, deliberately without `default`, so only
roles that select `group:runbook` carry them. `runbook-no-code-edits`: `event:
before_tool`, `priority: 10` (ahead of the language skill gates at 30 so non-coders are
blocked, not nagged), `agent_scope: [dispatcher, monitor, researcher, assistant]`,
`when` on `event.data.get('canonical_tool_kind') == 'write' and
event.data.get('canonical_repo_mutation')`, with an assistant carve-out when every
`canonical_write_file_paths` entry ends in `.md` under `docs/` or `.gobby/plans/`
(absolute or repo-relative); one `block` effect whose reason says to route the change
through the orchestrator via the assistant. `orchestrator-delegate-once`: `before_tool`,
`priority: 12`, `agent_scope: [orchestrator]`, `when` not
`variables.get('orchestrator_delegate_nudge_fired')` and a non-markdown repo mutation;
one `block` with `delivery: on_receipt` and `acknowledge_variable:
orchestrator_delegate_nudge_fired` (copied from
`memory-lifecycle/guard-plan-memory-writes.yaml`). Its sibling
`reset-orchestrator-delegate-nudge-on-context-reset`: `session_start`, `priority: 8`,
the standard `source in ['clear','compact'] or (source == 'resume' and
pending_context_reset)` condition, `set_variable` false, and `agent_scope: [orchestrator]`
like the nudge rule. The scope is safe because `_agent_type` is settled by the time the
reset evaluates: for SessionStart, `HookManager._handle_after_daemon_ready`
(`src/gobby/hooks/hook_manager.py`, verified 2026-09-22) runs the handler first, which
activates the definition and writes `_agent_type` and `_active_rule_names`, then
`reconcile_session_activation`, then the workflow rules. Clear and compact differ: a
`/clear` successor rearms on its own because `_bind_clear_successor` copies task claims
only and the flag is absent; compact keeps the same session with the flag already true,
so the reset rule is what rearms compact. Declare
`orchestrator_delegate_nudge_fired: false` next to `plan_memory_write_nudge_fired`.
Tests use the engine harness under `tests/workflows/` to drive a before_tool event for
each role with `_agent_type` set; the source-write fixtures include a shell command that
`canonical_repo_mutation` classifies as a repo write, so the block is proven on the path
that `blocked_tools` could not see.

Research context:
- Canonical metadata comes from `src/gobby/hooks/_normalization_canonical.py`
  (`_build_canonical_tool_metadata` ~100-124 emits `canonical_tool_kind`,
  `canonical_repo_mutation`, `canonical_write_file_paths`; shell segments are classified
  the same way ~583-866, so `sed -i` and heredoc writes are caught with no `tools:`
  list). Generator expressions and `all()` are already used in
  `session-feedback/session-feedback.yaml` 47-51.
- `acknowledge_variable` is written only when the block is delivered
  (`src/gobby/workflows/engine/evaluation.py` 733-739); rule contract in
  `src/gobby/install/shared/workflows/rules/AGENTS.md` 66-80.
- No bundled rule carries `audience: interactive`; only
  `context-handoff/block-autonomous-clear-session.yaml` is `audience: autonomous`, which
  stays off for roles. Nothing to re-scope.
- Rejected: `blocked_tools` on the definitions as the primary block (must enumerate
  every provider spelling and cannot path-scope the assistant's docs allowance).
- Planned checks: `GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_runbook_rules.py`,
  `uv run gobby workflows` dry-run lint if available.

**Acceptance:**

- 3.1.1 - A source write by dispatcher, monitor, researcher, or assistant is blocked,
  through the Edit tool and through a shell command classified as a repo write, for each
  of the four roles; an assistant write to `docs/*.md` or `.gobby/plans/*.md` is allowed
  through the Edit tool and through a shell write of a single such path, and a write
  whose paths mix a markdown doc with a `.py` file is blocked. file:
  `src/gobby/install/shared/workflows/rules/runbook/runbook-no-code-edits.yaml`. test:
  `tests/workflows/test_runbook_rules.py::test_non_coding_roles_are_blocked_from_source_writes`.
  test: `tests/workflows/test_runbook_rules.py::test_assistant_may_write_docs_and_plans`.
- 3.1.2 - The orchestrator's first non-markdown write in an epoch is blocked once with
  the flag set on delivery; the second passes; a session_start with source `compact` on
  a session that already has the flag true and the orchestrator rule set active rearms
  it, and the scoped reset does not fire for another role. file: `src/gobby/install/shared/workflows/rules/runbook/orchestrator-delegate-once.yaml`.
  test: `tests/workflows/test_runbook_rules.py::test_orchestrator_nudge_fires_once_per_epoch`.
  test: `tests/workflows/test_runbook_rules.py::test_context_reset_rearms_orchestrator_nudge`.
- 3.1.3 - The flag has a bundled default and the rules guide documents the group.
  file: `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml`.
  behavior: "runbook" group in `docs/guides/workflow-rules.md`.

### 3.2 Role definitions: orchestrator, assistant, dispatcher, monitor, post-epic-reviewer, researcher [category: config] (depends: 3.1, 1.3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/orchestrator.yaml`
- `src/gobby/install/shared/workflows/agents/assistant.yaml`
- `src/gobby/install/shared/workflows/agents/dispatcher.yaml`
- `src/gobby/install/shared/workflows/agents/monitor.yaml`
- `src/gobby/install/shared/workflows/agents/post-epic-reviewer.yaml`
- `src/gobby/install/shared/workflows/agents/researcher.yaml::*` — scope-reason: expand the persona prompt and add the runbook selector
- `docs/guides/agents.md`
- `tests/agents/test_runbook_definitions.py`

Four new persona definitions (`surfaces: [persona]`, `isolation: none`; provider fields
left `inherit` since the runbook command chooses the CLI) plus one spawn-only reviewer
and two edits to `researcher`. Persona blocks condense the night runbook's org chart,
standing rules, and paste-ready prompts:
- `orchestrator`: final arbiter working by messages (gate verdicts, merges, restarts,
  lane commands, escalation); roster via `get_workspace` and `send_message(target=
  "session")`; delegation to lane workers through the dispatcher with the rare hands-on
  exception; every chore to the assistant; `OPEN/HOLD/STOP/RESUME <lane|all>`; restart
  protocol (zero live runs, global send before and after); escalation criteria; only
  the assistant and alarms inbound. Rules: include `["tag:default", "group:runbook"]`,
  exclude `["name:bootstrap-default-agent-core-skills"]`. No `blocked_tools`.
- `assistant`: inbound hub for dispatcher and monitor reports and every orchestrator
  chore (digest, delivery confirmation and re-wake, read-only diagnostics, found-work
  filing at rung 3, memory searches, planning-lane spawns); docs tasks end to end
  (claim, commit named files by explicit path, close with the sha, never push); never
  gate, merge, restart, command lanes, edit code, or claim non-docs tasks; `EVENT=DONE`
  / `EVENT=REPORT` upward. Rules: include `["tag:default", "group:runbook"]`, exclude
  `["name:bootstrap-default-agent-core-skills", "name:require-python-skill",
  "name:require-rust-skill"]` (both skill rules are tagged `default`, so only a name
  exclude removes them); `blocked_mcp_tools: ["gobby-agents:kill_agent"]`.
- `dispatcher`: per open lane `get_task` → `spawn_agent` → `wait_for_agent` on every
  outstanding run → end turn → report `LANE= EVENT=<CANDIDATE|FAILED|HOLDING|STOPPED|IDLE>
  TASK= RUN= WT= COMMIT= NOTE=` to the assistant; one worker per lane, three
  outstanding runs; lanes start CLOSED; the failure playbook; never claim, edit, merge,
  restart, install, spawn in a closed lane, or run tests. Rules: include `["tag:default",
  "group:runbook", "tag:worker-safety"]` (`tag:worker-safety` is not implied by
  `tag:default`; `researcher.yaml` includes both), exclude
  `["name:bootstrap-default-agent-core-skills"]`.
- `monitor`: read-only watcher on a timer (load, running-agent count, new errors.log
  lines, vector-sync failure delta, cache sizes, free pages); `EVENT=ALARM` with
  wake=true to assistant and orchestrator on thresholds; `EVENT=REPORT` every third
  tick; scheduled timers; never mutates. Rules: include `["group:runbook",
  "tag:context-handoff", "tag:memory-lifecycle", "tag:worker-safety"]` (no
  `tag:default`), exclude `[]`; `blocked_tools` copies
  `comms-agent.yaml`'s write list minus `Bash`.
- `post-epic-reviewer`: copy of `epic-reviewer.yaml` with `surfaces: [spawn]`, same
  provider/model/effort and `blocked_mcp_tools`; `prompts.agent` reviews a sub-epic's
  aggregate landed diff once (`get_task` the sub-epic passed as `task_id`, enumerate
  closed children, `git log`/`git diff` over their landing commits, apply the
  epic-reviewer checks), replies to the parent session with `EVENT=VERDICT TASK=#N
  LAND|BOUNCE` plus numbered file:line findings, then `end_agent_run`. The step program
  keeps `epic-reviewer`'s five steps (claim, load_skill, closed_review, review,
  terminate) and its exit condition `current_step == terminate`, with one change: the
  review step never offers `gobby-tasks-ops:complete_stage`, `fail_stage`, or
  `escalate_task` (a literal copy would complete the `epic_qa` stage); the effect that
  sets `review_complete` and transitions to terminate is the verdict `send_message` to
  the parent session, using the same `on_mcp_success` shape the source uses for
  `complete_stage`. terminate stays the `end_agent_run` step. Never edits, merges,
  closes, or spawns.
- `researcher`: expand the one-line `prompts.persona` to the read-only research role
  reporting to the assistant, and add `group:runbook` to `rule_selectors.include` so
  `runbook-no-code-edits` is selected. `prompts.agent` and the step workflow unchanged.
The agents guide gains a "Runbook roles" section listing the seats and their reporting
lines. Tests load each YAML through `AgentDefinitionBody` and assert surfaces, the exact
include and exclude selector lists above, and that `resolve_rules_for_agent` includes
the runbook rules.

Research context:
- Schema `AgentDefinitionBody` (`src/gobby/workflows/agent_models.py` 78-219):
  `require_surface_prompt_blocks` demands one prompt block per declared surface;
  `reject_legacy_step_keys` rejects `role`/`goal`/`personality`/`instructions`.
  Selectors (`src/gobby/workflows/selectors.py`): `tag:`, `group:` (the directory name
  set by sync), `name:`; exclude beats include. `default.yaml` includes `tag:default`.
- Precedents: `comms-agent.yaml` 39-56 (blocked_tools list), `epic-reviewer.yaml`
  (header 1-16, prompts 18-98, rule_selectors 99-104, step_workflow 105-292),
  `researcher.yaml` (surfaces line 9, persona 22-23, rule_selectors 69-73).
- Sync: `sync_bundled_agents` (`src/gobby/agents/sync.py` 129-299) writes
  `source="installed"`, preserves the user's enabled toggle.
- Rejected: a dispatcher `step_workflow` (inert on the persona surface, see Non-goals);
  reusing `epic-reviewer` directly (its prompt and step program are build-lifecycle
  bound and `gobby build` still owns it).
- Planned checks: `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_runbook_definitions.py`;
  after daemon restart, `gobby-workflows:list_agent_definitions` shows the six.

**Acceptance:**

- 3.2.1 - Each of the four new persona definitions parses, declares `persona`, and
  carries exactly the include and exclude selector lists written above. file: `src/gobby/install/shared/workflows/agents/orchestrator.yaml`.
  file: `src/gobby/install/shared/workflows/agents/assistant.yaml`.
  file: `src/gobby/install/shared/workflows/agents/dispatcher.yaml`.
  file: `src/gobby/install/shared/workflows/agents/monitor.yaml`.
  test: `tests/agents/test_runbook_definitions.py::test_runbook_personas_parse_and_select_runbook_rules`.
- 3.2.2 - `post-epic-reviewer` is spawn-only, carries the reviewer's tool blocks, its
  review step allows neither `gobby-tasks-ops:complete_stage` nor `fail_stage`, and a
  successful verdict `send_message` sets `review_complete` and transitions to terminate.
  file:
  `src/gobby/install/shared/workflows/agents/post-epic-reviewer.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_post_epic_reviewer_is_spawn_only_and_reports`.
- 3.2.3 - `researcher` selects the runbook group and its persona names the assistant as
  its reporting line. file: `src/gobby/install/shared/workflows/agents/researcher.yaml`.
  test: `tests/agents/test_runbook_definitions.py::test_researcher_selects_runbook_rules`.
- 3.2.4 - The agents guide documents the seats and reporting lines. behavior: "Runbook
  roles" in `docs/guides/agents.md`.

## V1 Verification
`kind: verification`

1. Unit and contract suites per deliverable (Constraints lists the commands); the gcore
   schema tests with a scratch database; `cargo nextest run -p gobby-client` including
   `source_size.rs`; ruff and mypy on `src/`.
2. Cutover from the main checkout after a `global` announcement: commit 1.1, then
   `uv run gobby cutover` (proves `gdaemon schema plan`, promotes the binary set, applies
   migration 450, restarts). Rebuild gclient
   (`cargo build --release -p gobby-client`) and promote it via
   `uv run gobby install --no-interactive` in the same window.
3. Live smoke: from a shell inside a gclient pane, run
   `bash src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`. Expect one
   new tab titled `orchestration` with five labeled panes in decision 10's split, each
   CLI running with its kickoff prompt submitted. From any session:
   `gobby-workspaces:get_workspace` shows the tab's `runbook` and each pane's `role` and
   `session_ref` once the CLIs have started.
4. Role activation: in the monitor pane, `gobby-workflows:get_variable _agent_type`
   (or the first prompt's injected persona) shows `monitor`; an Edit on a `.py` file is
   blocked by `runbook-no-code-edits`. In the orchestrator pane, the first `.py` edit
   is blocked once with the delegate nudge and the retry passes; `/clear` then a fresh
   `.py` edit is nudged again.
5. Persistence: `/clear` in the assistant pane; the successor's first prompt carries
   the assistant persona and `get_workspace` shows the new `session_ref`. Quit the CLI
   in the researcher pane and relaunch it by hand in the same shell; the new session is
   again the researcher.
6. Recovery: `gobby panes set-role 0:0:<tab>:<pane>` clears and re-sets a role; in a
   plain pane, `gobby-agents:apply_agent_definition(agent="researcher")` switches the
   session and the next prompt shows the researcher persona.
7. Failure path: a script line whose op is refused (a pane ref that does not exist)
   prints the daemon's `code: reason` on stderr and exits 1, so `set -e` stops the
   script with the panes created so far left in place; `wait-for-output` past its
   timeout exits 1.

**Enhancement round 1 of 1** (kind: enhancement). enhancer_run
`fa6aa37e-ab77-43cd-a1e5-1b9af49c8803` (plan-enhancer-taskless, grok/grok-4.7/high,
child session 65491917), round 1 of cap 1, converged: false, suggestions_presented: 11,
delivered 2026-09-22 06:43Z. Votes by Josh on 2026-09-22 ("fold as read"), folded by
gobby#14156 the same day; rb-1's 2.1 half and rb-7 addressed the YAML loader that
decision 12 had already removed.
- rb-1 (sequencing, 1.2 and 2.1): accept the 1.2 half, decline the 2.1 half. Omitting
  the three keys when None keeps the golden corpus byte-identical and lets either leaf
  close without the other; the 2.1 half targeted row fields the command-mode 2.1 no
  longer adds.
- rb-2 (testability, 2.1): accept. A closed event enum drops the socket on
  `pane.role_set`; one variant lands in the file 2.1 already targets.
- rb-3 (clarity, 3.2): accept. A literal copy of `epic-reviewer` would complete
  `epic_qa`, which the non-goals forbid.
- rb-4 (clarity, 1.4): accept. The seven keys were unnamed, and a stale `_persona_name`
  defeats the recovery path the tool exists for.
- rb-5 (clarity, 3.2): accept. Full selector lists are decision-complete and remove a
  guess the planned tests could not catch.
- rb-6 (clarity, 1.2): accept. `btrim` keeps the roster ref identical to what
  `send_message` resolves.
- rb-7 (clarity, 2.3): decline as moot. The replay state machine it fixed was removed
  by decision 12.
- rb-8 (testability, 3.1): accept. The shell path is the reason the rule uses canonical
  metadata, so the tests exercise it.
- rb-9 (clarity, 3.1): accept. The handler-then-rules order was verified in
  `hook_manager.py` on 2026-09-22; the plan's ordering claim was reversed.
- rb-10 (testability, 1.2): accept. Pins decision 1's survival guarantee in the section
  expansion copies.
- rb-11 (clarity, 1.1): accept. Removes an ambiguity that would have put 1024 on `role`.

## D1 Rust port of the runbook seams (depends: 1.2, 1.3, 1.4)
`kind: deferred`

```yaml
deferral:
  task_ref: "TBD-runbooks-rust-port"
  reason: "The workspace ops, hook ingress, and MCP seams move to Rust family crates at S2.8 (#21565), S2.11 (#21569), and S2.12 (#21570); runbook scripts can move to a DB-backed registry then, and the command-mode verbs move behind the family crate's API."
  owner: "gobby-1.0"
  original_acceptance_items:
    - 1.2.2
    - 1.3.1
    - 1.4.1
```

## D2 Retire gobby build in favor of runbooks
`kind: deferred`

```yaml
deferral:
  task_ref: "TBD-retire-gobby-build"
  reason: "Josh intends runbooks to supersede stage-manifest dispatch; the retirement needs its own planning pass (what replaces epic_qa and the build coordinator, and the memory that build runs autonomously) after orchestration-v1 has run a real night."
  owner: "josh"
  original_acceptance_items:
    - 3.2.2
```

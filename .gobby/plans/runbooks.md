Plan artifact: `.gobby/plans/runbooks.md`

# Runbooks: role-bound multi-agent tabs loaded from gclient

**Plan ID:** runbooks

## Context
`kind: framing`

Last night's overnight epic #22651 ran on a hand-built org chart: six gclient panes
(orchestrator, assistant, lane manager, researcher, reviewer, janitor; that file still
called the lane manager "dispatcher", a name Golden Path ruling 22 reserves for the
retired build dispatcher), each a persistent interactive session launched by hand with
a pasted prompt, wired together by a message protocol and standing rules written in
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
non-coding seats and give the Program Director one delegate-first nudge per context
epoch.

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
   (the seat names of 2026-09-21: ruling 22 renames the seat `lane-manager` and decision
   13 sets the roster and 3.1's scope; assistant keeps docs and plan paths), and a
   once-per-context-epoch orchestrator gate
   using `acknowledge_variable` with the standard clear/compact reset. Role definitions
   cherry-pick rule groups and skills; the default core-skill bootstrap is excluded for
   non-coding seats.
8. **Seats.** Superseded by decision 13 on 2026-09-23; the original text follows for
   the audit trail. Five persistent seats: `orchestrator`, `assistant`, `dispatcher`
   (now `lane-manager`, ruling 22), `monitor` (the janitor, renamed), `researcher`
   (existing definition). No persistent
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
    dispatcher (now the lane manager, ruling 22) | researcher on top (0.50) and monitor
    alone below. Reporting lines as they settled last night: the lane manager and
    monitor report to the assistant; the assistant and alarms reach the orchestrator.
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
    21, 22 and 23, and the flow Josh wrote into #22808). Its council part (the three
    persona blocks on existing definitions, the researcher lookup pane and the verifier
    checks in the adversary's report) is superseded by decision 18 on 2026-09-24; the
    text follows for the audit trail. Eight persona definitions:
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
    definition. Provider, model and effort stay in the launch lines (decision 4). 3.1's
    `agent_scope` follows this roster: every role except `program-director`, whose seat
    carries the delegate nudge; the assistant keeps its `docs/` and `.gobby/plans/`
    markdown carve-out and the planner and elicitor get a `.gobby/plans/` one.
14. **Layout** (supersedes 10; ruling Q2 of the same message). Two bundled scripts on
    2.1's verbs, both mirroring the live workspace of 2026-09-23: `orchestration-v1.sh`
    builds a `control` tab (program director | assistant, horizontal 0.50) and a
    `monitors` tab (log monitor over archivist, vertical 0.50); `plan-council-v1.sh <plan>`
    builds one council tab named after the plan with four panes (writer on top, vertical
    0.50; below it enhancer | adversary | mechanic at equal widths; the mechanic pane
    replaced the researcher lookup pane in decision 18). Lane developer
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
16. **Q4 and Q5 resolved by lookup** (gobby#14332, message `1e77d597`, 2026-09-23).
    Q4: workspace panes cannot fall back to tmux. `WorkspaceOps._pane_source` resolves
    the native gterm host unconditionally and a missing host is refused as
    `terminal_failed`; only `spawn_agent` resolves a backend. So no script asserts a
    backend, and criteria 1-7 map as the Constraints entry "Terminal backend" says.
    Q5: `apply_run` always mints children and cannot adopt an existing leaf, so 2.1 stays
    a deliverable; when expansion mints 2.1's task the Program Director closes #22695
    (gclient command mode) as superseded by it and the gclient-lane slot passes to the
    minted task. Writer position, named to the Program Director with the hash: the
    alternative, 2.1 as `kind: deferred` with `task_ref: "#22695"`, would drop 2.1's
    acceptance coverage. Q3: no durable surface records edits to a task's
    `description`, `validation_criteria` or `labels` (`task_lifecycle_events` holds state
    transitions, `task_validation_history` holds run outcomes) and the close reviewer
    receives none, so 1.1's migration adds the trigger-written
    `task_artifacts.claim_snapshot` and 3.3 surfaces it; `task-close-reviewer.yaml`
    leaves the Non-goals for that one step. Writer position, named with the hash: the
    column and trigger ride migration 451 rather than a 452 of their own, so V1 step 2
    stays one cutover.
17. **Execution model, as-is and to-be** (Josh, 2026-09-24 09:3x CDT, via the
    assistant; memory 556ec801; the D2 review that reopened #22808 as R5). Josh,
    verbatim: "Everything is an interactive session now unless it's a one-shot (like
    feature_low/mid/high). Everything is an agent running in a pane except one-shots.
    Therefore, every agent needs the ability to execute a step workflow." As-is: step
    programs run only for spawned sessions (`build_persona_changes` sets
    `step_workflow_complete` only when `is_spawned`; `activate_default_agent` and 1.4's
    `apply_agent_definition` both call it with `is_spawned=False`). To-be: every
    non-one-shot agent, the interactive roles included, executes a step workflow.
    Lifting the `is_spawned` gate is an engine-wide capability and belongs to a sibling
    plan (interactive step-workflow execution); this plan does not grow into it. The
    lane-manager and council roles ship as persona prompts in v1 and adopt step
    workflows when the sibling plan lands, so no role definition here assumes it stays
    persona-only. Plans keep current behavior (as-is) apart from target design (to-be):
    a code fact never stands as a non-goal or constraint when it contradicts the to-be
    model, which is why the former non-goal "No `step_workflow` on the interactive
    roles" is gone.
18. **Council definitions** (Josh, 2026-09-24 09:4x CDT, via the Program Director;
    supersedes the council part of 13 and the verifier part of ruling Q6). Josh,
    verbatim: "The runbooks plan should retire plan-enhancer-taskless and
    plan-adversary-taskless, or rename them. We need plan-writer, plan-enhancer,
    plan-adversary, and plan-mechanic." The council roster is exactly those four
    definitions, delivered by 3.4. On HEAD, `plan-enhancer.yaml` and `plan-adversary.yaml`
    already exist as gobby build's stage-native planning definitions (task-bound `claim`
    and `terminate` steps; `stages.yaml` line 38 names `plan-adversary` as the planning
    stage's reviewer), `planner` is that stage's `default_agent` (line 37), and
    `plan-writer` and `plan-mechanic` have no definition to build on (`plan-mechanic` is
    today only a folded skill name, `catalog.json` line 1041, that resolves to
    `references/plan/repair.md`). So `plan-writer` and `plan-mechanic` are new
    persona-surface definitions (the writer's body carries `planner`'s persona and the
    book's section 9; `planner` stays untouched for the spawned drafting runs and the
    build stage, Non-goal 1); the council enhancer and adversary are persona blocks on
    the existing `plan-enhancer` and `plan-adversary` (already `surfaces: [spawn,
    persona]`); and `plan-enhancer-taskless` and `plan-adversary-taskless` are retired,
    every reference migrated (3.4 lists them). `plan-mechanic` owns the mechanical
    checks that ruling Q6 had moved into the adversary's finalization report: `uv run
    gobby plans validate`, `git diff --check`, the plan sha256 against the committed
    blob, the coverage manifest and coverage-contract checks, and re-verifying citations
    and literals against HEAD; the adversary keeps the substantive attack and cites the
    mechanic's pass lines in its finalization. The mechanic takes the fourth council
    pane; `researcher` leaves the council and keeps its chart seat. To-be, deferred past
    v1 (Josh, verbatim: "mechanic could also be used for codebase research. that way we
    don't have four planning agents all doing codebase exploration for their parts. idea
    at least. we'll have time to optimize it after we get it working."): the mechanic
    produces one shared evidence pack (file and symbol map, line citations, current
    behavior) that the writer and enhancer consume; the Program Director's constraint,
    recorded with it: the adversary may use the pack but keeps independent code
    spot-checks, so a shared wrong premise cannot pass every role. Not built in v1.

## Non-goals
`kind: framing`

- No changes to `gobby build`, `src/gobby/dispatch/`, the stage registry,
  `epic-reviewer.yaml`, or `default.yaml`, except 3.4's deletion of the two retired
  taskless names from `dispatch/prompts.py` and `dispatch/spawn_artifacts.py` (decision
  18); `task-close-reviewer.yaml` changes only by 3.3's amendment step.
- No web UI, no runbook CRUD over MCP or REST, no runbook rows in PostgreSQL.
- No pane placement for `spawn_agent` (workspaces decision record #10 stands).
- No change to the gclient direct-input keystroke path; the one-shot launch command
  uses the daemon's write coordinator.
- No multi-node runbooks; tabs load into the current node's current workspace.
- No YAML runbook format, picker dialog, or menu item in 1.x (decision 12).

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
  `cargo nextest run -p gobby-core --features postgres -E 'test(schema)'` with
  `GOBBY_SCHEMA_TEST_DATABASE_URL` set to the test-hub DSN named in 1.1's carrier procedure
  (gcore's default features are empty and its schema tests are
  `#![cfg(feature = "postgres")]`, so without the feature the command compiles no test
  and passes vacuously),
  `cargo clippy -p gobby-client`, `cargo fmt -p gobby-client -- --check`. Never the full
  pytest suite.
- **Terminal backend (#22691 criteria 1-7).** Every fresh workspace pane is created by
  `WorkspaceOps._pane_source` (`src/gobby/terminals/workspace_ops.py` 754-790), which
  resolves the native gterm host unconditionally (771); when the host is absent, `_fill`
  (823-873) surfaces `WorkspaceOpError("terminal_failed", ...)` and nothing falls back
  (`docs/guides/gterminal-development-guide.md` 91-190). Only `spawn_agent` resolves a
  backend (`resolve_terminal_backend`, `src/gobby/agents/spawn_models.py` 26-38). So:
  criterion 1 is 3.2's test that no definition names a backend; 3 is 3.2's registration
  line reading 1.2's `backend` pane field; 5 holds by construction, because a persistent
  role's pane cannot come up on tmux and a refused `new-tab` or `split` prints
  `terminal_failed: <reason>` and exits 1 (2.1.3, V1 step 7); 2, 4, 6 and 7 describe
  `spawn_agent`'s fallback, which is #21565's (Golden Path ruling 16) and outside this
  plan. The one tmux path into a runbook pane is `terminal_id` adoption of a
  pre-existing terminal (`_adoptable`, 792), which the registration line reports. No
  script asserts a backend.

## P1: Daemon seams
`kind: framing`

Thin Python plus one gcore migration. 1.1 is schema only; 1.2 is the workspace rows and
ops; 1.3 is SessionStart activation; 1.4 is the manual tool. 1.4 is independent of the
others and lands first because 1.3 and 3.2 share its agents-guide edits.

### 1.1 Migration 451: pane role, tab runbook and claim snapshot columns [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/451_add_runbook_roles.sql`
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated by the manifest freshness test
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: golden checksum, root hash, and latest_version literals move together
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: the identity literals move and the byte-limit test is added beside them
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated from the rebuilt gdaemon
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: re-signed at identity 451 (`schema_identity`, `payload_checksum`, `signature`)
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: re-signed at identity 451
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: re-signed at identity 451
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: re-signed at identity 451; its unknown field stays outside the signed payload
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: re-signed at identity 451

Add migration 451: `ALTER TABLE workspace_panes ADD COLUMN role text` and
`ALTER TABLE workspace_tabs ADD COLUMN runbook text`, each with an `octet_length`
CHECK whose shape is copied from `workspace_panes_label_byte_limit` in migration 440
(`octet_length(label) <= 1024`, `440_add_workspaces.sql` 49-50). The limits themselves
are new, with no 128-byte precedent to copy: 128 bytes for `role`
(`workspace_panes_role_byte_limit`), 1024 for `runbook`
(`workspace_tabs_runbook_byte_limit`); `label` stays at 1024. No new GRANT: 440 already
granted DML on the three tables. The byte-limit test applies the embedded migrations
to the scratch database the way `catalog_manifest_freshness` does and inserts
over-limit rows.

The same migration carries 3.3's #22713 carrier: `ALTER TABLE task_artifacts ADD COLUMN
claim_snapshot text` (no CHECK; the value is trigger-written JSON) and the trigger pair
`tasks_claim_snapshot_ai AFTER INSERT ON tasks FOR EACH ROW WHEN
(NEW.claimed_by_session_id IS NOT NULL)` and `tasks_claim_snapshot_au AFTER UPDATE OF
claimed_by_session_id ON tasks FOR EACH ROW WHEN (NEW.claimed_by_session_id IS NOT NULL
AND NEW.claimed_by_session_id IS DISTINCT FROM OLD.claimed_by_session_id)`, both
executing the new `snapshot_task_claim()` (`LANGUAGE plpgsql`, the shape of
`refresh_task_state_bucket_from_task` and its `tasks_state_bucket_ai`/`_au` pair,
baseline.sql 5338-5340): `INSERT INTO task_artifacts (task_id, claim_snapshot) VALUES
(NEW.id, jsonb_build_object('claimed_at', now(), 'session_id', NEW.claimed_by_session_id,
'description', NEW.description, 'validation_criteria', NEW.validation_criteria,
'labels', NEW.labels)::text) ON CONFLICT (task_id) DO UPDATE SET claim_snapshot =
EXCLUDED.claim_snapshot, updated_at = now()`. A row of only `task_id` and
`claim_snapshot` satisfies `task_artifacts_check` (every isolation pair NULL). The
trigger covers every claim path with no Python call site: the insert with a claimant
(`src/gobby/storage/tasks/_creation.py`), `claim_task` and `claim_task_for_agent`
(`_transitions.py` 177-251), `update_task`'s `claimed_by_session_id` parameter
(`_manager.py` 348), and the stage transitions (`_stage_state_transitions.py`
638-650). It rides this migration so V1 step 2 stays one cutover (decision 16).

Research context:
- Precedent commit `541320efc2` (migration 444) shows the carrier procedure; there is no
  single regenerate command. Order: (1) append an `EmbeddedMigration { version: 451,
  filename, checksum: <sha256 of the file bytes>, sql: include_str!(...) }` entry to
  `MIGRATIONS` in `crates/gcore/src/schema/assets.rs`; (2) regenerate the catalog
  manifest with the env-gated test
  `GOBBY_SCHEMA_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core --features postgres --test catalog_manifest_freshness catalog_manifest_is_fresh_for_embedded_assets`
  (the test creates a scratch database on that server, applies baseline plus migrations,
  writes `catalog.manifest.json` and drops the database; without the DSN it skips with
  an eprintln, and without `--features postgres` it compiles no test at all and the
  manifest is never rewritten, because gcore's default features are empty
  (`crates/gcore/Cargo.toml`), `pub mod schema` is gated on `postgres` (`lib.rs` line
  46), and `schema_contract.rs` and `catalog_manifest_freshness.rs` are
  `#![cfg(feature = "postgres")]`);
  (3) `cargo build -p gobby-daemon` then `target/debug/gdaemon schema version --json`
  (the `schema` subcommands are `apply`, `plan`, `sweep-test-schemas`, `verify` and
  `version`; `schema-identity` is gcode's)
  prints `latest_checksum` and `assets_root_hash` (`root_hash()` covers baseline,
  migrations, seed, and manifest, so it is final only after step 2); (4) update
  `GOLDEN_LATEST_CHECKSUM`, `GOLDEN_ASSETS_ROOT_HASH`, and `latest_version: 451` in
  `crates/gcore/src/grant/bundle.rs`, the literals in `schema_contract.rs`, and
  `latest_version` in `cli_contract.rs`; (5)
  `uv run python scripts/generate_schema_expected_identity.py --gdaemon target/debug/gdaemon`
  rewrites `src/gobby/storage/schema_expected_identity.json` (CI re-derives and compares);
  (6) re-sign the five golden grant vectors under `tests/runtime_grants/golden/`: they
  embed the signed schema identity, so every identity bump regenerates them
  (`tests/runtime_grants/test_golden_vectors.py` lines 3-6; precedents `2b7cf60e79` for
  449 and `ec61574731` for 450, one line in each of the five files). For each file set
  `schema_identity` to the new identity, recompute `payload_checksum` and re-sign with
  `sign_grant(..., GOLDEN_SECRET)` (`gobby.runtime_grants`; `GOLDEN_SECRET` is
  `tests/runtime_grants/support.py` line 13); only the identity and those two fields
  change, and the skew vector's unknown field (`future_capability_probe`, the one
  `NEGATIVE_GOLDENS` entry) stays outside the signed payload.
- The newest committed migration on 0.5.0 is `450_drop_session_heuristic_title.sql`
  (#22740, drops `sessions.heuristic_title`; `b9303f8183`, merged in `8de59a666f` on
  2026-09-24); 449 added `workspaces.default_project_id` and refs became zero-based in
  442. This plan takes 451 (Program Director, 2026-09-24). On HEAD the
  `#[cfg(not(feature = "postgres"))]` fallback in `crates/gcore/src/grant/bundle.rs`
  carries `latest_version: 450` (line 218, set by #22740 in `b9303f8183`) beside the 450
  `GOLDEN_LATEST_CHECKSUM` and `GOLDEN_ASSETS_ROOT_HASH` (lines 15-18); step (4) moves
  the three together from 450 to 451, and `schema_contract.rs` moves four (the version,
  the newest file name, the latest checksum and the assets root hash). Its guard test
  `grant::tests::expected_schema_identity_tracks_catalog_head` exercises the fallback only
  under gcore's default features, so V1 step 1 runs it without `--features postgres`
  (memory 24090e86).
- Live: the migration applies at the next `uv run gobby cutover`; `cutover` refuses
  uncommitted schema inputs, so commit the migration and carriers first.
- `task_artifacts` is keyed by `task_id` (`task_artifacts_pkey`, baseline.sql
  4617-4618) with an `ON DELETE CASCADE` foreign key to `tasks` (5709-5710) and
  `SELECT, INSERT, DELETE, UPDATE` granted to `gobby_daemon_runtime` (6460), so the
  trigger's upsert needs no new grant. `tasks.labels` is `jsonb`, `description` and
  `validation_criteria` are `text`, and there is no `claimed_at` column, so the snapshot
  stamps `now()`.
- Rejected: reusing `label` as the role (display text is not a definition name);
  storing the role on the terminals row (a pane outlives CLI relaunches, a terminal row
  does not necessarily).

**Acceptance:**

- 1.1.1 - Migration 451 adds nullable `role` on `workspace_panes` and nullable `runbook`
  on `workspace_tabs`, with `octet_length` checks of 128 bytes on `role` and 1024 on
  `runbook`: a 129-byte role and a 1025-byte runbook are rejected, and `label` still
  accepts 1024. file: `crates/gcore/assets/schema/migrations/451_add_runbook_roles.sql`.
  test: `crates/gcore/tests/schema_contract.rs::migration_451_enforces_role_and_runbook_byte_limits`.
- 1.1.2 - `MIGRATIONS` embeds version 451 with its checksum and the catalog manifest
  lists the three columns and the trigger function. symbol: `MIGRATIONS`. file: `crates/gcore/assets/schema/catalog.manifest.json`.
- 1.1.3 - The schema identity contract tests pass with `latest_version` 451. test:
  `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
  test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.
- 1.1.4 - `schema_expected_identity.json` matches the rebuilt gdaemon's identity.
  file: `src/gobby/storage/schema_expected_identity.json`.
- 1.1.5 - Claiming a task writes `task_artifacts.claim_snapshot` holding the claim-time
  `description`, `validation_criteria` and `labels` with the claiming session id; a
  claim by another session replaces it; an update that changes `description` without
  changing the claimant leaves it untouched; an unclaimed insert writes no row. file:
  `crates/gcore/assets/schema/migrations/451_add_runbook_roles.sql`. test:
  `crates/gcore/tests/schema_contract.rs::migration_451_snapshots_task_fields_on_claim`.
- 1.1.6 - The five golden grant vectors carry identity 451 and verify against
  `GOLDEN_SECRET`. test: `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.
  test: `tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed`.

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
- `src/gobby/install/shared/workflows/runbooks/plan-council-v1.sh`
- `tests/storage/test_workspaces.py::*` — scope-reason: add role, runbook, and session_ref tests
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: add role and pane.set_role tests
- `tests/servers/test_workspace_ws.py::*` — scope-reason: extend the hand-enumerated OPS list and add the set_role round-trip
- `tests/mcp_proxy/test_workspaces_registry.py::*` — scope-reason: add set_pane_role and param-forwarding tests
- `tests/cli/test_workspaces.py::*` — scope-reason: add set-role and split --role tests

Storage: `WorkspaceTab` gains a trailing `runbook: str | None = None`; `WorkspacePane`
gains trailing `role: str | None = None` and two derived fields, `session_ref: str |
None = None` and `backend: str | None = None` (read from the row only when the keys
are present), so existing constructor sites,
including `tests/servers/test_terminal_ws_golden.py`, are untouched. `_insert_pane` and `WorkspaceManager.create_tab`
accept `role` (and `runbook` on the tab insert); `WorkspaceManager.add_pane` accepts
`role`; new `WorkspaceManager.set_pane_role(pane_id, role)` mirrors `rename_pane`
(strip, empty clears). `session_ref` and `backend` are derived by one JOIN in the two read paths that
feed snapshots and activation, `list_panes` and `get_pane_for_terminal`:
`LEFT JOIN terminals tm ON tm.id = p.terminal_id AND tm.state IN ('pending','live')
LEFT JOIN sessions s ON s.id = tm.session_id LEFT JOIN projects pr ON pr.id = s.project_id`,
selecting `COALESCE(NULLIF(btrim(pr.name), ''), s.project_id::text) || '#' || s.seq_num::text`,
which mirrors `Session.ref` in `src/gobby/storage/session_models.py` including its
strip of `project_name`, so the roster ref is the one `send_message` resolves even when
`projects.name` carries surrounding whitespace. The same JOIN selects `tm.backend` as
`backend`, so the roster shows which terminal backend each seat got (#22691 criterion
3) without any definition naming one (criterion 1). `RETURNING *` paths leave it None, which
is correct at insert. `to_dict()` stays `asdict` for the existing fields and drops
`role`, `runbook`, `session_ref` and `backend` when they are None (existing nulls such as
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
contract (tabs carry `runbook`, panes carry `role`, `session_ref` and `backend`). CLI:
`gobby panes split --role`, new `gobby panes set-role REF [ROLE]` (omit ROLE to clear),
and `_pane_line` prints `role`, `session_ref` and `backend` when present; there is no `tabs create`
CLI, so `runbook` is WS/MCP-only and the docs say so. `pane.set_role` and `set-role`
write the binding only: the session already running in that pane keeps its active
definition until its next SessionStart (`/clear` or a relaunch, 1.3); an immediate switch
is `apply_agent_definition` (1.4) called from that session, and the tool description and
the docs say both. Docs: op vocabulary, snapshot row
fields, and the `pane.role_set` event in `docs/contracts/gterm-protocols.md`; the CLI
block in `docs/guides/cli-commands.md`; a "Tabs and panes" paragraph in
`docs/guides/gclient-user-guide.md`; the tool list in the gobby skill's
`references/sessions/workspaces.md`. The golden corpus fixtures are not regenerated:
rows without a role, runbook, or bound session serialize exactly as today, and
gclient's row structs tolerate the new keys when set (no `deny_unknown_fields`). Bind the seats
in both bundled runbook scripts: `orchestration-v1.sh` adds `--runbook orchestration-v1`
to its two `new-tab` lines, with `--role program-director` and `--role log-monitor`, and
`plan-council-v1.sh` adds `--runbook plan-council-v1 --role plan-writer` to its `new-tab`
line; every `split` line gets `--role <seat>`; 2.1's verbs forward those flags only when
given.

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
  with the project name stripped and an empty name falling through to `project_id`,
  and `backend` from the same terminal row; `to_dict()` includes a set role and
  runbook and omits the four keys when None; a
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
  `backend`, `pane.set_role` (a binding for the next SessionStart, never a live switch),
  and `pane.role_set`. behavior: "Workspace messages" in
  `docs/contracts/gterm-protocols.md`.
- 1.2.7 - Both bundled runbook scripts bind each tab's runbook and every seat's role
  through the `--runbook` and `--role` flags. file:
  `src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`. file:
  `src/gobby/install/shared/workflows/runbooks/plan-council-v1.sh`.

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
- 1.3.4 - Guides describe pane-bound roles, the `/clear` and relaunch behavior, that a
  role set on a pane reaches its running session only at that session's next
  SessionStart while `apply_agent_definition` switches it now, and the tmux limitation.
  behavior: "pane-bound roles" in `docs/guides/agents.md`.

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
Never touches `agent_step_instances` in v1 (decision 17: the sibling plan decides where
an interactive session's step program is instantiated). Extract the seven identity keys
from
`activate_default_agent` into `ALWAYS_REAPPLY_KEYS` (new symbol) in `apply_persona.py`,
exactly `_agent_type`, `_active_rule_names`, `_active_skill_names`, `_skill_format`,
`_agent_blocked_tools`, `_agent_blocked_mcp_tools`, and `is_spawned_agent`, and import
it there so the two cannot drift. Register
`apply_agent_definition(agent: str, variables: dict | None = None)` next to
`apply_persona` and document it as the full lifecycle switch versus the narrow persona
switch, and as the one live switch: `pane.set_role` (1.2) binds the next SessionStart
and leaves the running session alone.

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
  instances are not created or replaced in v1 (decision 17). test:
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

### 2.1 gclient command mode and the bundled runbook scripts [category: code]
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
- `src/gobby/install/shared/workflows/runbooks/plan-council-v1.sh`
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

Scripts. Two bundled scripts (`#!/usr/bin/env bash`, `set -euo pipefail`) reproduce
decision 14 with refs captured from plain output. Each successful `new-tab` is followed
by `echo "CREATED_TAB=<tab ref>" >&2`, so when a later op is refused (the script stops
under `set -e` with the panes created so far in place, 2.1.3) stderr names the tabs to
remove with `gclient kill <tab ref>`; there is no rollback. `orchestration-v1.sh [project]`:
`new-tab --project "${1:-gobby}" --name control` gives `$ctl` and `$pd`; `split "$pd"
--right` gives `$asst`, `resize "$pd" 0.50`; `new-tab --project "${1:-gobby}" --name
monitors` gives `$mon` and `$logmon`; `split "$logmon" --down` gives `$arch`, `resize
"$logmon" 0.50`; four `title` lines (`program-director`, `assistant`, `log-monitor`,
`archivist`). `plan-council-v1.sh <plan> [project]`: `new-tab --project "${2:-gobby}"
--name "$(basename "$1" .md)"` gives `$tab` and `$writer`; `split "$writer" --down`
gives `$enh`, `resize "$writer" 0.50`; `split "$enh" --right` gives `$adv`, `resize
"$enh" 0.3333`; `split "$adv" --right` gives `$mech`, `resize "$adv" 0.50` (each resize
follows its split while both children are leaves, per Constraints, so the three lower
panes come out at equal widths); four `title` lines (`plan-writer`, `plan-enhancer`,
`plan-adversary`, `plan-mechanic`; decision 18). Both scripts then run, for each seat,
`wait-for-output REF
--pattern "$PROMPT_PATTERN" --timeout 30`, `send-keys REF "<launch line>" --enter`,
`wait-for-output REF --pattern "$READY_<cli>" --timeout 60`, then `send-keys REF
"<kickoff prompt>" --enter`; a timeout on either wait exits 1 like a refused op.
`PROMPT_PATTERN` defaults to `'[%$#] *$'`; the ready patterns are each CLI's idle
composer as captured from the live council panes on 2026-09-23: `READY_claude='bypass
permissions on'` (the status footer Claude Code prints only once its composer is up
under `--dangerously-skip-permissions`), `READY_codex='› Ask Codex'` (the empty
composer's placeholder), `READY_grok='Ctrl\+x:shortcuts'` (the hint bar under the idle
composer; every `READY_*` value is a regex, so metacharacters are escaped). No
`READY_*` value may match its seat's launch line, because
`wait-for-output` matches text already on screen, including the launch line just
typed; V1 step 3 verifies the patterns live. Launch lines follow the prompt
book's provider, model and effort per role and the flag spellings of
`~/Desktop/gobby-team-resume-2026-09-22.md`: program director and plan writer `claude
--dangerously-skip-permissions --model 'claude-fable-5-1[1m]'`; assistant and plan
mechanic `claude --dangerously-skip-permissions --model 'claude-opus-5[1m]'` (the book
has no mechanic section; the mechanic takes the lookup pane's line, decision 18); log
monitor `codex --dangerously-bypass-approvals-and-sandbox -m gpt-5.6-luna -c
model_reasoning_effort=medium`; archivist the same with `gpt-5.6-terra`; enhancer the
same with `gpt-5.6-sol` and `xhigh`; adversary `grok -m grok-4.7 --reasoning-effort
xhigh --always-approve` (the book names no provider or effort for the adversary: section
11, lines 645-681, and "The plan council", 519-535, carry none; `xhigh` is the adversary
definition's `reasoning_effort` in 3.2 and the live council seat's). The kickoff prompt
sent after the ready wait names the
runbook, the seat's own pane ref and the tab ref, and, for the council, the plan path;
the user edits the scripts freely. The `--runbook` and `--role` flags are added to both
scripts by 1.2, which lands after this leaf.

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
  decision 14. Pane-env defaults are tested by passing an explicit env map to
  `dispatch`, never by mutating the process env.
- Existing leaf #22695 (gclient command mode) is this deliverable's prior filing.
  `apply_run` (`src/gobby/tasks/expansion/_apply.py` 91-) always mints children and
  cannot adopt an existing leaf (gobby#14332 lookup, 2026-09-23), so 2.1 stays a
  deliverable and #22695 is closed by the Program Director as superseded by the minted
  task, which inherits its gclient-lane slot (decision 16).
- Rejected: a client-side capture loop for `wait-for-output` (the daemon op exists);
  `--cwd` on `split` (`pane.split` spawns the tab's checkout shell; `--cmd 'cd DIR'`
  covers it); a REST route for one-shot ops (the WS reply already carries `result`);
  a parser crate (the TUI parses by hand and the verb table is small); a `GOBBY_DAEMON_URL`
  pane variable (removed deliberately by the workspaces plan).
- Planned checks: `cargo nextest run -p gobby-client -E 'test(command)'`,
  `cargo clippy -p gobby-client`, `cargo fmt -p gobby-client -- --check`,
  `bash -n` on both scripts under `src/gobby/install/shared/workflows/runbooks/`, and the
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
- 2.1.5 - `orchestration-v1.sh` is valid bash, and running it against the mock daemon
  reproduces decision 14's `control` and `monitors` tabs with four titled panes and
  four launches, each kickoff `send-keys` sent only after the seat's ready-pattern wait
  matched, and every seat's `READY_*` regex matching its idle-composer text and no launch
  line. file:
  `src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`. test:
  `crates/gclient/tests/command_mode.rs::orchestration_v1_script_reproduces_decision_14_layout`.
- 2.1.6 - The user guide documents command mode: the verb table, exit codes, pane-env
  defaults, the runbook scripts, and their failure path (`CREATED_TAB=` lines on stderr,
  cleanup with `kill`). behavior: "Command mode" in
  `docs/guides/gclient-user-guide.md`.
- 2.1.7 - A `workspace_event` whose kind is `pane.role_set` decodes as
  `WorkspaceEventKind::PaneRoleSet` and `WorkspaceModel::apply` upserts the enclosed
  pane row, so an attached client survives a role change. symbol: `WorkspaceEventKind`.
  test: `crates/gclient/tests/workspace.rs::pane_role_set_event_decodes_and_upserts_pane`.
- 2.1.8 - `plan-council-v1.sh <plan>` is valid bash, and running it against the mock
  daemon reproduces decision 14's council tab named after the plan, with the writer
  over three equal-width panes, four titles, and four launches whose kickoff `send-keys`
  follow the ready-pattern wait and carry the plan path, with every seat's `READY_*`
  regex matching its captured idle-composer text (`Ctrl\+x:shortcuts` matches the literal
  hint `Ctrl+x:shortcuts`) and matching no launch line. file:
  `src/gobby/install/shared/workflows/runbooks/plan-council-v1.sh`. test:
  `crates/gclient/tests/command_mode.rs::plan_council_v1_script_reproduces_decision_14_layout`.
- 2.1.9 - Against a mock daemon that refuses the first `split` after the second
  `new-tab`, `orchestration-v1.sh` exits nonzero, its stderr carries the daemon's
  `code: reason` and one `CREATED_TAB=<ref>` line per tab created, and no further op is
  sent. file: `src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`. test:
  `crates/gclient/tests/command_mode.rs::script_stops_at_refused_op_and_names_created_tabs`.

## P3: Roles
`kind: framing`

YAML, plus 3.4's edits to two planning references, two docs and two dispatch name maps.
Templates sync to the DB registry on daemon start (`sync_bundled_agents`, rule sync);
the installed rows are the live definitions.

### 3.1 Rule group runbook: no-code-edits and the program-director delegate nudge [category: config]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/runbook/runbook-no-code-edits.yaml`
- `src/gobby/install/shared/workflows/rules/runbook/program-director-delegate-once.yaml`
- `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml::*` — scope-reason: declare one new variable default
- `docs/guides/workflow-rules.md`
- `tests/workflows/test_runbook_rules.py`

Group tags `[runbook, enforcement, gobby]`, deliberately without `default`, so only
roles that select `group:runbook` carry them. The group is a rule change under standing
instruction 4: the Program Director's gate on this plan is that approval, and any later
change to its text is disclosed to the Program Director before it lands.
`runbook-no-code-edits`: `event:
before_tool`, `priority: 10` (ahead of the language skill gates at 30 so non-coders are
blocked, not nagged), `agent_scope: [assistant, lane-manager, researcher, reviewer,
archivist, log-monitor, elicitor, plan-writer, plan-enhancer, plan-adversary,
plan-mechanic]` (every role of decisions 13 and 18 except `program-director`), `when`
on `event.data.get('canonical_tool_kind') == 'write' and
event.data.get('canonical_repo_mutation')`, with two carve-outs: `assistant` when every
`canonical_write_file_paths` entry ends in `.md` under `docs/` or `.gobby/plans/`, and
`plan-writer` or `elicitor` when every entry ends in `.md` under `.gobby/plans/` (absolute
or repo-relative); one `block` effect whose reason says to route the change through the
Program Director via the assistant. `program-director-delegate-once`: `before_tool`,
`priority: 12`, `agent_scope: [program-director]`, `when` not
`variables.get('program_director_delegate_nudge_fired')` and some
`canonical_write_file_paths` entry does not end in `.md` (a repo mutation with no write
paths, such as `git merge`, is the Program Director's landing work and never nudges);
one `block` with `delivery: on_receipt` and `acknowledge_variable:
program_director_delegate_nudge_fired` (copied from
`memory-lifecycle/guard-plan-memory-writes.yaml`). Its sibling
`reset-program-director-delegate-nudge-on-context-reset`: `session_start`, `priority: 8`,
the standard `source in ['clear','compact'] or (source == 'resume' and
pending_context_reset)` condition, `set_variable` false, and `agent_scope:
[program-director]` like the nudge rule. The scope is safe because `_agent_type` is settled by the time the
reset evaluates: for SessionStart, `HookManager._handle_after_daemon_ready`
(`src/gobby/hooks/hook_manager.py`, verified 2026-09-22) runs the handler first, which
activates the definition and writes `_agent_type` and `_active_rule_names`, then
`reconcile_session_activation`, then the workflow rules. Clear and compact differ: a
`/clear` successor rearms on its own because `_bind_clear_successor` copies task claims
only and the flag is absent; compact keeps the same session with the flag already true,
so the reset rule is what rearms compact. Declare
`program_director_delegate_nudge_fired: false` next to `plan_memory_write_nudge_fired`.
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

- 3.1.1 - A source write by each of the eleven scoped roles is blocked, through the Edit
  tool and through a shell command classified as a repo write; an assistant write to
  `docs/*.md` or `.gobby/plans/*.md` and a plan-writer or elicitor write to
  `.gobby/plans/*.md` are allowed through the Edit tool and through a shell write of a
  single such path; a plan-writer write to `docs/*.md` and a write whose paths mix a
  markdown doc with a `.py` file are blocked. file:
  `src/gobby/install/shared/workflows/rules/runbook/runbook-no-code-edits.yaml`. test:
  `tests/workflows/test_runbook_rules.py::test_non_coding_roles_are_blocked_from_source_writes`.
  test: `tests/workflows/test_runbook_rules.py::test_assistant_may_write_docs_and_plans`.
  test: `tests/workflows/test_runbook_rules.py::test_plan_writer_and_elicitor_may_write_plans_only`.
- 3.1.2 - The Program Director's first non-markdown write in an epoch is blocked once
  with the flag set on delivery; the second passes; a shell repo mutation with no write
  paths never fires it; a session_start with source `compact` on a session that already
  has the flag true and the program-director rule set active rearms it, and the scoped
  reset does not fire for another role. file:
  `src/gobby/install/shared/workflows/rules/runbook/program-director-delegate-once.yaml`.
  test: `tests/workflows/test_runbook_rules.py::test_program_director_nudge_fires_once_per_epoch`.
  test: `tests/workflows/test_runbook_rules.py::test_context_reset_rearms_program_director_nudge`.
- 3.1.3 - The flag has a bundled default and the rules guide documents the group.
  file: `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml`.
  behavior: "runbook" group in `docs/guides/workflow-rules.md`.

### 3.2 Role definitions: eight runbook personas [category: config] (depends: 3.1, 1.3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/program-director.yaml`
- `src/gobby/install/shared/workflows/agents/assistant.yaml`
- `src/gobby/install/shared/workflows/agents/lane-manager.yaml`
- `src/gobby/install/shared/workflows/agents/reviewer.yaml`
- `src/gobby/install/shared/workflows/agents/archivist.yaml`
- `src/gobby/install/shared/workflows/agents/log-monitor.yaml`
- `src/gobby/install/shared/workflows/agents/elicitor.yaml`
- `src/gobby/install/shared/workflows/agents/researcher.yaml::*` — scope-reason: replace the persona block and add one selector; `prompts.agent` and the step workflow stay
- `docs/guides/agents.md`
- `tests/agents/runbook_text.py`
- `tests/agents/test_runbook_definitions.py`

Seven new persona definitions (`surfaces: [persona]`, `isolation: none`; provider, model
and effort left `inherit` because the launch line chooses the CLI, decision 13) and a
persona edit to one existing definition, `researcher`: the chart roster of decision 13
(the four council definitions are 3.4's, decision 18). Every one of the
eight `prompts.persona` blocks opens with the same standing text, in this order: Josh's
five standing instructions (prompt book lines 72-89, verbatim), the durable shared tail
(91-113 verbatim except its two session addresses: gobby#14069 with its UUID and
gobby#14018 are the 2026-09-22 chart, so the tail names the assistant and the Program
Director by role and resolves each through `gobby-workspaces:get_workspace`, the pane
whose `role` is `assistant` or `program-director` and its `session_ref`, falling back
to a `project` send that names the missing role; the tail states this divergence
itself), and the conditional sign-off rule the book omits, quoted from #22691's
description item 5 (the "Reply in the terminal, not here (#22670)" line applies only
while Josh is at the desk; while he is mobile it stays suppressed until he says he is
back). That is criterion 8. `tests/agents/runbook_text.py` (a test-support module with no
tests, imported by `tests/agents/test_runbook_definitions.py` and 3.4's test) carries
the three texts as constants `STANDING_INSTRUCTIONS`, `SHARED_TAIL` (the durable text)
and `CONDITIONAL_SIGN_OFF`, copied from the book and the epic when the definitions are
written, because a test cannot read `~/Desktop`; the book stays the input of record
(criterion 10), and a later book change is a definition change disclosed under standing
instruction 4.

After the standing text every persona carries three fixed lines:
- `Diverges from the prompt book: <reason>` or `Diverges from the prompt book: none`
  (criterion 10). The reasons are fixed per role below; the tail's address divergence
  is stated inside the tail and shared by all eleven; nothing else may diverge.
- Registration (criterion 3): the first message goes to the assistant, resolved by role
  as the tail says, and names the role, the model, `GOBBY_PANE_REF`, and the terminal
  backend that `gobby-workspaces:get_workspace` reports for that pane (the `backend` field 1.2 adds
  to the pane view from the pane's terminal row); a session with no `GOBBY_PANE_ID` is
  not in a runbook pane and registers as such. The definition itself never names a
  backend (criterion 1): the daemon's pane view is the only source, so a pane that
  adopted a pre-existing tmux terminal (`_adoptable`,
  `src/gobby/terminals/workspace_ops.py:792`) reports that with no special text.
- Creation (criterion 11): persistent and semi-persistent roles are panes the assistant
  creates and releases; no definition asks anyone to `spawn_agent` a persistent or
  semi-persistent role.

Then the role text, condensed from the book section named, keeping every rule and
format line listed here:
- `program-director` (book section 2, lines 153-185; Golden Path rulings 22 and 23):
  keeps Josh on the Golden Path and says which ideas go on or off it; orders the queue
  (the lane manager rate-limits under its load instructions; Josh re-prioritizes through
  the assistant, never around it); last-mile delivery and the final gate: every closed
  task reaches it as a candidate, it merges, validates and says "landed", and nothing
  reaches Josh's review without passing it; owns the restart sequence including the
  template registry sync; rules on found work, duplicate consolidation and track order;
  holds the three approvals (new agent role, rule change, sandbox change) and sends
  Josh FYIs on his web link; confirms a task is open before ordering work against it;
  opens lane developer panes with `/goal` and gives the lane manager only bounded runs.
  Rules: include `["tag:default", "group:runbook"]`, exclude
  `["name:bootstrap-default-agent-core-skills"]`; no `blocked_tools`, because it
  merges and validates by hand (3.1's nudge covers non-markdown writes). Divergence:
  the lane manager no longer spawns lane workers on its own; the Program Director
  opens lane panes with `/goal` and lands every candidate itself (ruling 23).
- `assistant` (book section 1, lines 114-152; the communications coordinator of
  criterion 9): Josh's assistant and comms hub. What reaches Josh (program changes,
  product-direction decisions, stoppages, judgment calls, things he asked for) and how
  (hourly Telegram summary of at most eight lines, one line when nothing changed). The
  persona explicitly requires criterion 9's four items as a numbered list: (1) routine
  successful loads are reported only as "Systems nominal."; (2) every task reference
  carries the task number and the task name; (3) decisions that need operator input go
  out as a web link; (4) daemon restarts need no pre-approval and get an alert
  immediately before and immediately after each restart, the after alert stating the
  outcome; the restart itself stays the Program Director's (book section 2) and the
  assistant sends both alerts. A fifth requirement is Josh's (2026-09-23, relayed by
  the Program Director, verbatim: "add to your runbook instructions that I don't need
  you to repeat what you send on Telegram here. It's already in your context."): after
  a Telegram send, the terminal reply confirms the send in at most one line and never
  restates the content. Never: spawn, research, claim non-docs tasks, edit code,
  merge, push, restart, run lane commands, mutate the database (psql is read-only),
  touch another session's untracked files, or publish the decision docket before Josh
  says so. Owns: creating and releasing the panes of every persistent and
  semi-persistent role (criterion 11), relaying verdicts, candidates and alarms between
  the reviewer, lane manager and Program Director, the restart alerts, its own task rows
  (filing, consolidating, amending description, criteria and labels), corrections sent
  in the same message as the error, and the live roster from registrations, handed to
  the Program Director on request. Rules: include `["tag:default", "group:runbook"]`,
  exclude `["name:bootstrap-default-agent-core-skills", "name:require-python-skill",
  "name:require-rust-skill"]` (both skill rules are tagged `default`, so only a name
  exclude removes them); `blocked_mcp_tools: ["gobby-agents:kill_agent"]`. Divergence:
  the assistant creates every persistent and semi-persistent pane, so it no longer
  hands TTLs to the lane manager (criterion 11, ruling 23); a Telegram send is confirmed
  in the terminal in one line and never restated (Josh, 2026-09-23; the book is
  silent on it).
- `lane-manager` (book section 3, lines 186-214; rulings 22 and 23): the build-stage
  router and load balancer. The Program Director orders the queue; the lane manager
  decides when under the Director's load ceilings, never reorders against it, never
  launches past a ceiling because a lane is idle, and after restart 8 stands down: it
  holds the seat, spawns only the bounded worker runs the Director orders, and
  otherwise reports. Event lines to the assistant, unprompted: `LANE= EVENT=STARTED|
  CANDIDATE|BOUNCE|CLOSED TASK=#NNNNN TASK_TITLE= RUN= WT= COMMIT= NOTE=`, always with
  the task title; verdict-class blockers to the Program Director; found work it cannot
  place to the assistant with the failing command, diagnostics, paths and impact. Never:
  claim, edit, merge, restart, install, spawn in a closed lane, run tests. Criterion 11
  sentence, verbatim in the persona: "Never call `spawn_agent` for a persistent or
  semi-persistent role, and never pass `terminal_backend`; the assistant creates those
  panes." Rules: include `["tag:default", "group:runbook", "tag:worker-safety"]`
  (`tag:worker-safety` is not implied by `tag:default`; `researcher.yaml` includes
  both), exclude `["name:bootstrap-default-agent-core-skills"]`. Divergence: named
  `lane-manager` because "dispatcher" is the build-stage dispatcher (ruling 22); no
  persistent or semi-persistent spawns and no TTLs; stands down after restart 8
  (ruling 23).
- `researcher` (book section 4, lines 215-248; decision 13): replace the one-line
  persona (22-23) with the read-only research role: answers questions from the
  assistant; every claim
  marked VERIFIED with file and line, INFERRED with its basis, or unproven; refutes a
  false premise in the first line; reads the signed sources before concluding and names
  a conflict rather than overriding it; `EVENT=REPORT NOTE=<one-line recommendation>`
  first, then the evidence; semi-persistent: waits for follow-ups and ends only when the
  assistant asks. Add `"group:runbook"` to `rule_selectors.include` (69-73) so
  `runbook-no-code-edits` is selected; `prompts.agent`, provider fields and the step
  workflow are unchanged. Divergence: none beyond the standing text (the council
  lookup duty of decision 13 passed to the plan mechanic, decision 18).
- `reviewer` (book section 5, lines 249-277; ruling 23): returns a verdict on every
  candidate commit before the Program Director lands it; a closed task row is not a
  verdict and it says so; checks that the commit does what the criteria demand, that
  scope is confined to the task's files with no epic spillover, that criteria,
  description and labels were not loosened to make the close pass (3.3's
  `claim_snapshot`, read through `gobby-tasks-artifacts-ops:get_artifacts`, is the
  claim-time reference), and that validation ran and is visible; verdict line to the assistant `EVENT=CANDIDATE_VERDICT
  TASK=#NNNNN TASK_TITLE= VERDICT=LAND|BOUNCE COMMIT=` followed by SCOPE, BEHAVIOR,
  VALIDATION and any LIVE_NOTE (landing alone does not change runtime behavior, for
  example a rule template that needs the registry sync); found work is surfaced to the
  assistant with the reproduction, never fixed; a provider refusal ends in a handoff that
  says what was refused. Never edits, merges, closes or spawns. Rules: include
  `["tag:default", "group:runbook"]`, exclude `["name:bootstrap-default-agent-core-skills",
  "name:require-python-skill", "name:require-rust-skill"]`; `blocked_mcp_tools:
  ["gobby-agents:kill_agent", "gobby-tasks:close_task", "gobby-tasks:update_task"]`.
  Divergence: none.
- `archivist` (book section 6, lines 278-410, the Program Director's definition of
  record): read-only against the repository, writes only under `~/Desktop/gobby-*.md`;
  owns the live digest (status line with CDT time, lanes and running runs; landed with
  task, worktree, merge sha, rollback sha; in flight; bounced; alarms; restarts;
  decisions to ratify; escalations; morning TODO), technical writing on request, and
  historical questions answered from the digest, inter-session message history, task
  records, session transcripts and git log, saying when a fact is inferred; inputs
  `EVENT=RECORD` lines from the assistant (batches allowed), `DIGEST` from the Program
  Director for a full refresh, questions from any session answered with `wake=false`
  unless asked with `wake=true`; refreshes the status line hourly from
  `list_running_agents` and the message history; style: facts with CDT times, ids and
  shas, brevity, no speculation. Never: edits code or task rows, files tasks, runs
  psql, messages workers, the lane manager or the reviewer, restarts anything; talks to
  the Program Director and the assistant and answers whoever asks; persistent, never
  calls `end_agent_run`. Divergence: none.
- `log-monitor` (book section 7, lines 411-518, the prompt of record): read-only log
  monitor for the daemon, persistent between ticks, parent the Program Director, filing
  through the assistant. First turn: `date`, `uptime`, a 30-minute baseline of
  `~/.gobby/logs/errors.log` and `daemon.log` with byte offsets, the state file
  `$TMPDIR/log-monitor-state.json` rewritten every tick and reloaded after compaction,
  `EVENT=ACK` to the parent. Each `TICK`: `uptime`, `list_running_agents`, the new bytes
  only, WARNING and ERROR lines plus tracebacks grouped into signature families with
  per-window counts, families mapped to tasks by the table (unknown families searched
  with `search_tasks` first), one report, end of turn. Nominal line
  `Systems nominal | window HH:MM-HH:MM | <N> warnings in <K> families, all mapped` with
  `wake=false` (no load or run figures: standing instruction 1 and memory 33cb3885, the
  number is the news only when it breaches); `EVENT=ALARM` with `wake=true` on
  the book's thresholds (unmapped family at 3 lines; mapped family at 3x the previous
  window and at least 20 lines; any traceback, `pool acquisition failed`,
  `DatabaseExecutor is shut down`, `HostEpochChangedError` or `spawn_rollback`; 1-minute
  load above 24; running runs at or above 8); tracebacks alarm on first sight or a
  doubling; hook-saturation lines are nominal below 3x the previous window;
  `EVENT=LOG_FINDING` to the assistant for an unmapped family at 3 lines, the assistant
  files it. The family table is seeded from the book and replaced by the parent's
  `TABLE` messages; `STOP` ends with a structured handoff; anything else is ignored.
  Never: edits repository files, creates or edits tasks, runs psql, restarts or installs,
  spawns, types into interactive shells, sleeps or polls. Divergence: the routine
  nominal line drops the book's `load <1m>/<5m>/<15m> | runs <R>` fields (book line 460)
  because standing instruction 1 forbids routine load numbers; alarms still cite the
  breached figure.
  The two monitors-tab seats share one selector list: include `["group:runbook",
  "tag:context-handoff", "tag:memory-lifecycle", "tag:worker-safety"]` (no
  `tag:default`), exclude `[]`; `blocked_tools` is the write tools only, `["Edit",
  "KillShell", "MultiEdit", "NotebookEdit", "Write", "apply_patch", "edit_file",
  "notebook_edit", "replace", "write_file"]` (the comms agent's list minus its shell
  spellings `Bash`, `BashOutput`, `shell` and `run_shell_command`: both seats launch
  Codex and need a shell for `date`, `uptime`, the log reads, the state file under
  `$TMPDIR` and the digest under `~/Desktop`, all outside the repository, which
  `runbook-no-code-edits` leaves alone); `blocked_mcp_tools: ["gobby-agents:kill_agent", "gobby-tasks:create_task",
  "gobby-tasks:update_task", "gobby-tasks:close_task", "gobby-tasks:claim_task"]`.
- `elicitor` (book section 8, lines 536-576): runs before drafting; its deliverable is
  the decision-complete problem statement at `.gobby/plans/<plan>.problem.md`, left
  uncommitted for the plan writer to commit with the plan once the Program Director
  approves it, with the five parts (intent, constraints, success criteria, explicit
  non-goals, the signed-source inventory naming `ROADMAP.md`, the Golden Path,
  `.impeccable.md` and every signed canvas, approved plan and Josh ruling that bears on
  the work); closes open questions itself from the signed sources, the Golden Path and
  the code, then sends the Program Director one batched question list per round, with
  Josh's questions routed through the assistant in the same batch; never proposes a
  solution. Semi-persistent: released by the assistant when the Program Director
  approves the statement or 90 minutes pass. Rules: include `["tag:default",
  "group:runbook"]`, exclude `["name:bootstrap-default-agent-core-skills",
  "name:require-python-skill", "name:require-rust-skill"]`. Divergence: the pane is
  created and released by the assistant; there is no lane-manager TTL (criterion 11,
  ruling 23).
- Council definitions: 3.4's (decision 18); their persona blocks are written there.
The agents guide gains a "Runbook roles" section listing the eight seats, the four
council definitions of 3.4 (decision 18), the reporting lines (lane manager, log monitor, reviewer and archivist to the
assistant; the assistant, alarms and verdict-class items to the Program Director; the
council to the Program Director through the adversary) and the standing text. Tests load
each YAML through `AgentDefinitionBody` and assert surfaces, the exact selector lists and
tool blocks above, the standing text, the fixed lines, and that
`resolve_rules_for_agent` includes the runbook rules.

Research context:
- Spawn-surface reach of `group:runbook` (Program Director, 2026-09-24):
  `_filter_by_agent_scope` (`src/gobby/workflows/engine/core.py` 838-855) keeps a scoped
  rule when `variables["_agent_type"]` is in `agent_scope`, on any surface, so
  `researcher`'s spawned runs are blocked from non-markdown writes exactly as its pane
  is; that is intended. The council definitions' spawn-surface analysis is 3.4's.
  Coverage YAML is written by the daemon's `regenerate_coverage_manifest`
  (`src/gobby/mcp_proxy/tools/plans/__init__.py` 224-255; `coverage_manifest_path` in
  `src/gobby/plans/coverage_manifest.py` 57) and by `expansion-qa` (`expansion-qa.yaml`
  31), which is outside the rule's `agent_scope`; no runbook or council session writes
  it through Write.
- Schema `AgentDefinitionBody` (`src/gobby/workflows/agent_models.py` 78-219):
  `require_surface_prompt_blocks` demands one prompt block per declared surface;
  `reject_legacy_step_keys` rejects `role`/`goal`/`personality`/`instructions`; no
  field names a terminal backend, so criterion 1 is a prose check (no key `backend` or
  `terminal_backend` anywhere in a loaded tree, no word `tmux` or `gterm` in any string
  under `prompts` once backtick-quoted spans are removed, since the log-monitor family
  table quotes a `gterm` log signature; `native` is not checked because the shared tail says "native
  subagents"). Selectors (`src/gobby/workflows/selectors.py`): `tag:`, `group:` (the
  directory name set by sync), `name:`; exclude beats include. `default.yaml` includes
  `tag:default`.
- Precedents: `comms-agent.yaml` 39-56 (blocked_tools list); `researcher.yaml`
  (surfaces line 9, persona 22-23, rule_selectors 69-73).
- Sync: `sync_bundled_agents` (`src/gobby/agents/sync.py` 129-299) writes
  `source="installed"`, preserves the user's enabled toggle.
- Standing-instruction 4: the eight roles are on the chart or planned in #22691 and the
  roster is decision 13, so the definitions need disclosure, not approval; the rule
  group is 3.1's and its change is under Program Director approval.
- Rejected: `post-epic-reviewer` and `plan-verifier` (decision 13; the verifier's checks
  are the plan mechanic's, decision 18); a `spawn` surface on the seven new definitions
  (criterion 11: panes, not spawns); reusing `epic-reviewer` (build-lifecycle bound and
  still owned by `gobby build`).
- Deferred, not rejected: a `step_workflow` on the lane-manager and council definitions.
  They ship as persona prompts in v1 and adopt step workflows when decision 17's sibling
  plan lifts the `is_spawned` gate; nothing in their definitions assumes persona-only.
- Planned checks: `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_runbook_definitions.py`;
  after daemon restart, `gobby-workflows:list_agent_definitions` shows the seven new
  names and the edited `researcher`.

**Acceptance:**

- 3.2.1 - Each of the seven new persona definitions parses, declares only `persona`,
  and carries exactly the selector lists and tool blocks written above (the monitors-tab
  `blocked_tools` is the write-only list; no shell spelling appears in it). file:
  `src/gobby/install/shared/workflows/agents/program-director.yaml`. file:
  `src/gobby/install/shared/workflows/agents/assistant.yaml`. file:
  `src/gobby/install/shared/workflows/agents/lane-manager.yaml`. file:
  `src/gobby/install/shared/workflows/agents/reviewer.yaml`. file:
  `src/gobby/install/shared/workflows/agents/archivist.yaml`. file:
  `src/gobby/install/shared/workflows/agents/log-monitor.yaml`. file:
  `src/gobby/install/shared/workflows/agents/elicitor.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_runbook_personas_parse_and_select_runbook_rules`.
- 3.2.2 - Every one of the eight personas embeds the five standing instructions, the
  durable shared tail and the conditional sign-off rule verbatim, in that order, followed
  by a `Diverges from the prompt book:` line; no persona's routine nominal template
  carries `load <` or `runs <` (the log-monitor line); the edited `researcher` keeps
  `surfaces: [spawn, persona]` and selects `group:runbook`. file:
  `src/gobby/install/shared/workflows/agents/researcher.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_every_runbook_persona_carries_the_standing_text`.
- 3.2.3 - No definition names a backend: no key `backend` or `terminal_backend` in any
  loaded tree and no `tmux` or `gterm` in any prompt string outside a backtick-quoted
  log signature (the log-monitor family table quotes the book's
  `terminals.host_manager._health_loop - gterm control probe failed` row, book line 495;
  a quoted signature selects nothing); every persona's registration line names
  `GOBBY_PANE_REF` and the pane view's backend. test:
  `tests/agents/test_runbook_definitions.py::test_no_runbook_definition_names_a_backend`.
- 3.2.4 - The assistant persona requires criterion 9's four items and Josh's Telegram
  rule (a send is confirmed in at most one line, never restated) by their fixed
  phrases. file: `src/gobby/install/shared/workflows/agents/assistant.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_assistant_requires_the_four_comms_items`.
- 3.2.5 - The reviewer is a persona definition that blocks `close_task` and
  `update_task`, carries the verdict line format, and says a closed row is not a
  verdict. file: `src/gobby/install/shared/workflows/agents/reviewer.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_reviewer_is_verdict_only`.
- 3.2.6 - The lane-manager persona carries the criterion 11 sentence verbatim (the
  sentence itself names the `terminal_backend` parameter it forbids) and its loaded tree
  has no mapping key `backend` or `terminal_backend`. file:
  `src/gobby/install/shared/workflows/agents/lane-manager.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_lane_manager_never_spawns_persistent_roles`.
- 3.2.7 - `researcher` selects the runbook group and its persona names the assistant
  as its reporting line. file:
  `src/gobby/install/shared/workflows/agents/researcher.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_researcher_selects_runbook_rules`.
- 3.2.8 - The agents guide documents the seats, the four council definitions and the reporting
  lines. behavior: "Runbook roles" in `docs/guides/agents.md`.

### 3.3 Task amendment disclosure (#22713) [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/tasks/_artifacts.py::*` — scope-reason: `TaskArtifacts` gains the trigger-owned field and `from_row` reads it
- `src/gobby/mcp_proxy/tools/tasks/_artifacts.py::*` — scope-reason: `_artifact_payload` adds the read-only field beside its `_ARTIFACT_MUTATION_FIELDS` filter
- `src/gobby/install/shared/workflows/rules/task-enforcement/disclose-task-amendment.yaml`
- `src/gobby/install/shared/workflows/agents/task-close-reviewer.yaml::*` — scope-reason: the review workflow gains the amendment step and one allowed tool
- `docs/guides/tasks.md`
- `tests/mcp_proxy/tools/tasks/test_get_artifacts_claim_snapshot.py`
- `tests/workflows/test_disclose_task_amendment.py`
- `tests/agents/test_task_close_reviewer_amendment.py`

Ruling B1 on #22713: a claiming session may amend its own task's `description`,
`validation_criteria` and `labels`; the amendment requires pre-close review, disclosed
to the close reviewer with the diff and judged against the task's intent; enforcement
lives in the agent definitions. 1.1's trigger keeps the claim-time values in
`task_artifacts.claim_snapshot`; this leaf surfaces them and closes the loop.
- Storage: `TaskArtifacts` gains `claim_snapshot: str | None = None`, read by
  `from_row`. It stays out of `_ARTIFACT_FIELDS`, so `set_artifact` and
  `set_artifacts_atomic` refuse it through `_validate_field_names` and only the trigger
  writes it; `get_artifacts` returns the JSON text unchanged.
- MCP: `gobby-tasks-artifacts-ops:get_artifacts` returns `claim_snapshot` (the JSON
  text, or None) because `_artifact_payload` (`mcp_proxy/tools/tasks/_artifacts.py`
  47-51) adds it beside its `_ARTIFACT_MUTATION_FIELDS` filter; the mutation set is
  unchanged, so `set_artifact` keeps refusing the field at both layers. `get_task` is
  not touched: `_crud.py` sits at 996 lines.
- Rule `task-enforcement/disclose-task-amendment.yaml` (tags `[task-enforcement,
  enforcement, tasks, gobby, default]` like its siblings): `event: after_tool`,
  `priority: 30`, `when` copied from `block-reopen-task.yaml` 10-19 for `gobby-tasks`
  `update_task` with the `claimed_tasks` match on the UUID key, the `#N` value or the
  bare number, and any of `description`, `validation_criteria` or `labels` present in
  `tool_input`; one `inject_context` effect whose template is ruling B1's standing
  order: the amendment is reviewed before close; name it and why in the close summary;
  keep the task's intent. A path-form `task_id` is not matched (the variable holds
  UUIDs and `#N`); the reviewer's diff below catches that case regardless.
- Reviewer: `task-close-reviewer.yaml` leaves the Non-goals for one allowed tool,
  `gobby-tasks-artifacts-ops:get_artifacts`, and one step in `prompts.agent` after the
  `get_task` read: call `get_artifacts(task_id)`; when it carries `claim_snapshot`,
  diff its `description`, `validation_criteria` and `labels` against
  the current values (`get_task_diff` is the code diff and does not help here); a
  non-empty diff is an amendment. The reviewer judges it against the intent the
  snapshot captured and passes it only when the closure summary names it; an unnamed
  or loosening amendment is a blocking finding in `submit_close_review`, so the close
  bounces. The blocked list is unchanged; `get_artifacts` is read-only. The persistent
  `reviewer` persona (3.2) reads the same field.
- Docs: `docs/guides/tasks.md` gains "Amending a claimed task": what the snapshot
  holds, when it is taken, how the disclosure order arrives, what the close reviewer
  does with it.
The `needs-decision` label on #22713 is the documented gate workaround (defect #22726);
this leaf is the follow-up implementation task its criterion names, with these tests.

Research context:
- Lookup L3 (gobby#14332, message `1e77d597`, 2026-09-23): no durable surface records
  task-field edits; `task_lifecycle_events` (baseline.sql 3742-3752) holds state
  transitions with a text reason; `task_validation_history` (3845-3858) holds run
  outcomes; the close reviewer's allowed tools are `get_task`, `get_task_diff`,
  `list_tasks`, `submit_close_review` and read-only memory and rule lookups
  (`task-close-reviewer.yaml` 238-246), and `get_task(brief=false)` returns no
  artifacts today.
- Rule engine: `tool_input.server_name`/`tool_name` plus the promoted nested args
  (`src/gobby/workflows/engine/enforcement_checks.py` 930-940); `claimed_tasks` is
  `{uuid: '#N'}` written by the `detect_task_claim` observer; effects available are
  set_variable, inject_context, observe, mcp_call, rewrite_input, load_skill,
  run_command and block (no warn), so the order is an `inject_context`; `after_tool`
  precedents `reviewer-lifecycle/terminal-verdict-after-validation.yaml` 33 and
  `error-recovery/inject-tool-error-recovery.yaml` 6; `inject_context` template
  precedent `task-enforcement/track-task-claim.yaml` 38-42.
- `_artifacts.py`: `_ARTIFACT_FIELDS` 13-32, `TaskArtifacts.from_row` 91-111,
  `_validate_field_names` 114-119, `TaskArtifactManager.get_artifacts` 274-278;
  `clear_artifacts` has no caller outside the package export, so the snapshot lives as
  long as the task row. MCP precedent `gobby-tasks-artifacts-ops:get_artifacts`
  (`src/gobby/mcp_proxy/tools/tasks/_artifacts.py` 298). `get_task` (`_crud.py`
  489-525) builds the full record from `task.to_dict()` plus dependencies.
- Rejected: a session-variable carrier (dies with the session; the reviewer never sees
  it); a `block` on the three fields (Josh rejected option a); a `task_amendments`
  table (the artifact row already is the per-task 1:1 carrier); Python writes in the
  four claim paths (the trigger covers all four with no call site); extending
  `get_task(brief=false)` (`_crud.py` is at 996 lines and `get_artifacts` already
  serves the artifact row).
- Planned checks: the three test files with the isolated hub DSN; `uv run mypy src/`;
  `uv run gobby workflows` dry-run lint if available.

**Acceptance:**

- 3.3.1 - `TaskArtifacts` carries `claim_snapshot` read from the row and refuses it as a
  writable field; `get_artifacts` returns it and `set_artifact` refuses it. symbol:
  `_artifact_payload`. file: `src/gobby/storage/tasks/_artifacts.py`.
  test: `tests/mcp_proxy/tools/tasks/test_get_artifacts_claim_snapshot.py::test_get_artifacts_carries_claim_snapshot`.
  test: `tests/mcp_proxy/tools/tasks/test_get_artifacts_claim_snapshot.py::test_set_artifact_refuses_claim_snapshot`.
- 3.3.2 - After a successful `update_task` that touches any of the three fields on a
  claimed task, matched by UUID, `#N` or bare number, the disclosure order is
  injected; other fields, other tasks and other tools stay silent. file:
  `src/gobby/install/shared/workflows/rules/task-enforcement/disclose-task-amendment.yaml`.
  test: `tests/workflows/test_disclose_task_amendment.py::test_amendment_on_claimed_task_injects_disclosure_order`.
  test: `tests/workflows/test_disclose_task_amendment.py::test_other_fields_tasks_and_tools_stay_silent`.
- 3.3.3 - The close reviewer's agent prompt names the snapshot diff step and the bounce
  condition; its allowed list gains only `gobby-tasks-artifacts-ops:get_artifacts` and
  its blocked list is unchanged. file:
  `src/gobby/install/shared/workflows/agents/task-close-reviewer.yaml`. test:
  `tests/agents/test_task_close_reviewer_amendment.py::test_close_reviewer_checks_claim_snapshot_and_allows_get_artifacts`.
- 3.3.4 - The tasks guide documents amending a claimed task. behavior: "Amending a
  claimed task" in `docs/guides/tasks.md`.

### 3.4 Council definitions: plan-writer, plan-enhancer, plan-adversary and plan-mechanic [category: code] (depends: 3.1, 3.2, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/plan-writer.yaml`
- `src/gobby/install/shared/workflows/agents/plan-mechanic.yaml`
- `src/gobby/install/shared/workflows/agents/plan-enhancer.yaml::*` — scope-reason: replace the one-line persona (26-27 today) and add one selector; `prompts.agent`, the tool blocks and the step workflow stay
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: replace the one-line persona (21-22 today) and add one selector; `prompts.agent`, the tool blocks and the step workflow stay
- `src/gobby/install/shared/workflows/agents/plan-enhancer-taskless.yaml::*` — scope-reason: deleted (decision 18)
- `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml::*` — scope-reason: deleted (decision 18)
- `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-enhancer-lessons.yaml::*` — scope-reason: `agent_scope` (line 10) drops the retired name
- `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-reviewer-lessons.yaml::*` — scope-reason: `agent_scope` (line 10) drops the retired name
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/guard-plan-memory-writes.yaml::*` — scope-reason: its agent list (lines 16 and 18) drops the two retired names
- `src/gobby/dispatch/prompts.py::PROMPT_BUILDERS`
- `src/gobby/dispatch/spawn_artifacts.py::_TASKLESS_MAIN_CONTEXT_AGENT_SLUGS`
- `src/gobby/install/shared/skills/gobby/references/plan/enhancement.md`
- `src/gobby/install/shared/skills/gobby/references/plan/review.md`
- `docs/contracts/plan-coverage.md::*` — scope-reason: the taskless-reviewer sentences name the council adversary
- `docs/guides/plans-and-plan-mode.md::*` — scope-reason: the interactive enhancement and review paragraphs name the council
- `tests/agents/test_council_definitions.py`
- `tests/agents/test_plan_adversary_taskless_definition.py::*` — scope-reason: deleted with its definition
- `tests/agents/test_plan_enhancer_agents.py::*` — scope-reason: the taskless half is dropped
- `tests/agents/test_plan_adversary_internal_research_definition.py::*` — scope-reason: the name tuples (16-17) drop the retired names
- `tests/agents/test_agents_sync.py::*` — scope-reason: line 86 names the retired definition
- `tests/agents/watchdog/test_completed_turn_mcp_gate.py::*` — scope-reason: the fixture (42, 47) names `plan-adversary` instead
- `tests/dispatch/test_dispatch_prompts.py::*` — scope-reason: the alias rows and assertions (38-40, 75-77) go
- `tests/dispatch/test_spawn_isolation.py::*` — scope-reason: the taskless cases (70, 84, 103) go with the branch
- `tests/workflows/test_step_enforcement.py::*` — scope-reason: the fixture (631, 643) names `plan-adversary` instead
- `tests/workflows/test_planner_grammar_prompt.py::*` — scope-reason: the retired rows (65, 67) go
- `tests/workflows/test_review_learning_rules.py::*` — scope-reason: the expected scope lists (359, 365) shrink
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: the provider table (152, 154) drops the retired rows
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: the agent lists (799, 801, 852) drop the retired names
- `tests/skills/test_plan_review_skill.py::*` — scope-reason: the parametrize (82) keeps `plan-adversary` only
- `tests/skills/test_plan_skill_delegated_mode.py::*` — scope-reason: names the retired definition
- `tests/skills/test_review_learning_skill.py::*` — scope-reason: names the retired definition
- `tests/mcp_proxy/test_stage_review_schema.py::*` — scope-reason: names the retired definition

Decision 18's roster: two new persona-surface definitions and persona blocks on two
existing ones, all four carrying 3.2's standing text (criterion 8), the registration line
(criterion 3) and the council sentence (wait between rounds rather than looking for work;
the assistant releases the pane); the two taskless definitions are deleted and every live
reference migrated.
- `plan-writer` (new; `surfaces: [persona]`, `isolation: none`; provider, model and
  effort `inherit`; book section 9, lines 577-618): built on `planner`'s persona (18-19),
  `blocked_mcp_tools` (14) and `rule_selectors` (127) plus `"group:runbook"`; `planner`
  itself is untouched (gobby build's planning-stage `default_agent`, `stages.yaml` line
  37, and the spawned drafting definition). The persona: owns the plan file and its hash
  and is the only member that edits it; claims a real planning task first; reads
  `docs/contracts/plan-coverage.md` and `references/plan/drafting.md` before drafting;
  drafts against the approved problem statement, or against the Decision Record when the
  Program Director waives the elicitor, and says when a statement does not cover
  something; `uv run gobby plans validate` passes before review opens; commits by
  explicit path only, the plan and the problem statement together; answers every finding
  with Fold (say what changed) or Contest (reasoning; "out of scope" only by pointing at
  a non-goal); a signed design artifact outranks the adversary; records every position
  and move in the council log; hands each published hash to the mechanic and reads its
  pass lines before sending the hash on. Divergence: no round cap; the council debates
  to consensus and the adversary finalizes to the Program Director, who may send it back
  (decision 13, #22808).
- `plan-enhancer` (existing, `surfaces: [spawn, persona]`; book section 10, lines
  619-644): the one-line persona (26-27) becomes the enhancer block and `"group:runbook"`
  joins `rule_selectors.include`; `prompts.agent`, the `Edit`/`Write` block (18-20),
  `blocked_mcp_tools` (21-23) and the stage-native step workflow stay, so gobby build's
  pre-adversary sub-loop is unchanged. The persona: reviews only the hash it was given
  and stops if the file differs; numbered `cr-N` items, each stating change, benefit and
  cost, ranked; looks for a cheaper mechanism, a missing edge state, a sequencing change
  or an existing pattern being reinvented, never style, non-goal scope or a contradiction
  of a signed source. Divergence: none beyond the council sentence.
- `plan-adversary` (existing, `surfaces: [spawn, persona]`; book section 11, lines
  645-681): the one-line persona (21-22) becomes the adversary block and
  `"group:runbook"` joins `rule_selectors.include`; `prompts.agent`, `blocked_mcp_tools`
  (15-18) and the stage-native step workflow stay, so the planning stage's reviewer
  (`stages.yaml` line 38) is unchanged. The persona: attacks only the hash it was given;
  every finding carries `check_keys`; a blocking defect is a step that cannot work, a
  missing dependency, a vacuous criterion, a contract violation, an unhandled expensive
  failure mode or work a lane cannot do, never a preference or an excluded scope; a
  signed design artifact outranks it (the chrome Edit-menu case is quoted); it withdraws
  or holds each contested finding; it may use the mechanic's evidence but keeps its own
  code spot-checks (decision 18); and it finalizes to the Program Director citing the
  mechanic's pass lines for the final hash (validate, `git diff --check`, sha256,
  coverage) instead of running those checks itself. Divergence: no round cap and the
  finalization report (decisions 13 and 18).
- `plan-mechanic` (new; `surfaces: [persona]`, `isolation: none`; provider, model and
  effort `inherit`; the book has no section for it, so the persona is written here): the
  council's mechanical checker and its fourth pane. On every hash the writer publishes,
  and on request from any member: `uv run gobby plans validate <plan>` (both modes when
  a manifest is present), `git diff --check`, the plan's sha256 against the committed
  blob (`git show <commit>:<path> | sha256sum`), the coverage manifest of a registered
  plan (`gobby-plans:regenerate_coverage_manifest`, or `update_plan_hash` when the row's
  hash is stale; memory 5538e963) with the coverage-contract checks of
  `docs/contracts/plan-coverage.md`, and re-verification of citations and literals
  (paths, symbols, line numbers, migration numbers, commit shas) against HEAD with
  `gcode`. It reports `CHECK=<name> RESULT=PASS|FAIL HASH=<sha256> NOTE=` lines to the
  requester and, for a final hash, to the adversary and the writer. Validator residue is
  reported, never repaired: the writer is the only editor, and
  `gobby-plans:apply_plan_review_repairs` and `gobby-plans:apply_plan_handoff_manifest`
  sit in `blocked_mcp_tools` beside `gobby-agents:spawn_agent` and `kill_agent`. It loads
  `gobby:references/plan/repair.md` (the `plan-mechanic` skill name resolves to it,
  `catalog.json` line 1041) and `references/plan/coverage.md` before its first check.
  Rules: include `["tag:default", "group:runbook"]`, exclude
  `["name:bootstrap-default-agent-core-skills", "name:require-python-skill",
  "name:require-rust-skill"]`. Semi-persistent: released by the assistant with the
  council. To-be, not built here: decision 18's shared evidence pack.
- Retirement: `plan-enhancer-taskless.yaml` and `plan-adversary-taskless.yaml` are
  deleted; `sync_bundled_agents`'s orphan sweep soft-deletes their registry rows at the
  next start (`src/gobby/agents/sync.py` 5-6 and 135-136). `PROMPT_BUILDERS`
  (`src/gobby/dispatch/prompts.py` 356 and 358) loses the two alias keys;
  `_TASKLESS_MAIN_CONTEXT_AGENT_SLUGS` (`spawn_artifacts.py` 43-45) and its branch
  (508-509) go, because no slug is left in the set. The three rules drop the names from
  `agent_scope` and the memory-guard list (a rule-template change, disclosed to the
  Program Director under standing instruction 4). `enhancement.md` line 6 and
  `review.md` line 6 stop spawning a taskless agent: an interactive round runs in the
  council tab (`plan-council-v1.sh <plan>`, decision 14), opens when the writer publishes
  a hash to the enhancer or adversary pane, and returns by `send_message` into the
  council log; `prepare_plan_review_round` and the review-evidence store stay for the
  stage-native flow. The two docs say the same. Historical mentions in `docs/research/`,
  `docs/plans/`, `.gobby/plans/completed/` and the `tests/adapters/test_claude_code_adapter.py`
  comment (line 1058) stay.
- Tests: `tests/agents/test_council_definitions.py` loads the four YAMLs through
  `AgentDefinitionBody`, imports 3.2's `runbook_text` constants, and asserts surfaces,
  selectors, tool blocks, the standing text, the fixed lines, that no definition names
  a backend (3.2.3's scan) and that `resolve_rules_for_agent` includes the runbook
  rules; the listed existing tests lose their taskless rows and cases.

Research context:
- Names on HEAD (2026-09-24): `plan-enhancer.yaml` (281 lines; `claim` and `terminate`
  steps; `record_plan_enhancement`) and `plan-adversary.yaml` (464 lines; `claim` and
  `terminate`; the stage review verbs) are gobby build's; `plan-enhancer-taskless.yaml`
  (208) and `plan-adversary-taskless.yaml` (296) were the `/gobby plan` spawn variants
  (`send_message` then `end_agent_run`; `tests/agents/test_plan_enhancer_agents.py` 1-14
  states the split). `plan-writer` and `plan-mechanic` exist nowhere; `plan-verifier`
  never was a definition.
- Spawn-surface reach of `group:runbook` on the two existing definitions
  (`_filter_by_agent_scope`, `src/gobby/workflows/engine/core.py` 838-855, matches
  `_agent_type` on any surface): the spawned `plan-enhancer` has `Edit` and `Write`
  blocked (18-20); the spawned `plan-adversary` never edits the artifact
  (`tests/agents/test_plan_adversary_no_edits_on_reject.py`) and records findings through
  the stage review tools; so the group blocks nothing either run does. `planner` leaves
  the scope (it is not a council definition), so the spawned drafting run is not
  rule-scoped at all; the `.gobby/plans/` carve-out names `plan-writer`.
- Rejected: a taskless mode on `plan-enhancer` and `plan-adversary` (a second transition
  out of `claim` and a second completion path in `enhance` and `review`) to keep the
  spawned interactive rounds alive under the surviving names, because it merges two step
  workflows and changes gobby build's reviewer surface (Non-goal 1); renaming `planner`
  to `plan-writer`, because the stage registry names it (Non-goal 1); renaming the
  stage-native pair out of the way, for the same reason.
- Planned checks: `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_council_definitions.py tests/agents/test_plan_enhancer_agents.py tests/dispatch/test_dispatch_prompts.py tests/dispatch/test_spawn_isolation.py`;
  a repository grep for the two retired names over `src/` and `tests/` is empty; after
  restart, `gobby-workflows:list_agent_definitions` shows `plan-writer` and
  `plan-mechanic` and neither taskless name.

Consumers unchanged:
- `src/gobby/dispatch/_planning_enhancement.py` — no-edit-reason: it looks up `PROMPT_BUILDERS` by the stage-native `plan-enhancer` key, which stays.
- `src/gobby/dispatch/_rule_actions.py` — no-edit-reason: it resolves builders by the spawned action's slug; no action names a retired slug.
- `src/gobby/dispatch/_rule_state.py` — no-edit-reason: same lookup by live slug; the two deleted keys had no stage-native caller.
- `src/gobby/dispatch/rules.py` — no-edit-reason: same; the mapping's remaining keys and signature are unchanged.

**Acceptance:**

- 3.4.1 - `plan-writer` and `plan-mechanic` parse, declare only `persona`, and carry the
  standing text, the selector lists and the tool blocks written above; `plan-enhancer`
  and `plan-adversary` keep `surfaces: [spawn, persona]`, their tool blocks and their
  step workflows, and select `group:runbook`; none of the four names a backend. file:
  `src/gobby/install/shared/workflows/agents/plan-writer.yaml`. file:
  `src/gobby/install/shared/workflows/agents/plan-mechanic.yaml`. file:
  `src/gobby/install/shared/workflows/agents/plan-enhancer.yaml`. file:
  `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`. test:
  `tests/agents/test_council_definitions.py::test_council_definitions_parse_and_select_runbook_rules`.
- 3.4.2 - The mechanic persona names its checks by their fixed phrases (`uv run gobby
  plans validate`, `git diff --check`, `sha256`, `regenerate_coverage_manifest`, `against
  HEAD`) and the `CHECK=` report line, says residue is reported and never repaired, and
  its loaded tree blocks `gobby-plans:apply_plan_review_repairs`. file:
  `src/gobby/install/shared/workflows/agents/plan-mechanic.yaml`. test:
  `tests/agents/test_council_definitions.py::test_mechanic_owns_the_mechanical_checks_and_never_repairs`.
- 3.4.3 - The adversary persona's finalization sentence cites the mechanic's pass lines
  and its spot-check sentence keeps independent code checks (fixed phrases). file:
  `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`. test:
  `tests/agents/test_council_definitions.py::test_adversary_finalizes_on_the_mechanics_pass_lines`.
- 3.4.4 - Neither taskless file exists; no bundled rule, skill reference, source module
  or test under `src/` and `tests/` names `plan-enhancer-taskless` or
  `plan-adversary-taskless`; and the two planning references describe the council round
  (`plan-council-v1.sh`) with no spawn of a taskless agent. file:
  `src/gobby/install/shared/skills/gobby/references/plan/enhancement.md`. file:
  `src/gobby/install/shared/skills/gobby/references/plan/review.md`. test:
  `tests/agents/test_council_definitions.py::test_taskless_definitions_are_retired_everywhere`.

## V1 Verification
`kind: verification`

1. Unit and contract suites per deliverable (Constraints lists the commands); the gcore
   schema tests with `--features postgres` and a scratch database
   (`GOBBY_SCHEMA_TEST_DATABASE_URL`; without the feature they compile to nothing) and
   `cargo test -p gobby-core --lib grant::tests` without `--features postgres` (the
   no-postgres identity guard, memory 24090e86);
   `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_golden_vectors.py`
   (the five re-signed goldens verify against `GOLDEN_SECRET`, 1.1.6);
   `cargo nextest run -p gobby-client` including `source_size.rs`; ruff and mypy on
   `src/`.
2. Cutover from the main checkout after a `global` announcement: commit 1.1, then
   `uv run gobby cutover` (proves `gdaemon schema plan`, promotes the binary set, applies
   migration 451, restarts). Rebuild gclient
   (`cargo build --release -p gobby-client`) and promote it via
   `uv run gobby install --no-interactive` in the same window.
3. Live smoke: from a shell inside a gclient pane, run
   `bash src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`. Expect two
   new tabs, `control` (program director | assistant) and `monitors` (log monitor over
   archivist), four labeled panes in decision 14's splits, each CLI running with its
   kickoff prompt submitted into the CLI's composer, not typed at the shell (the
   `READY_*` waits). Then
   `bash src/gobby/install/shared/workflows/runbooks/plan-council-v1.sh .gobby/plans/runbooks.md`:
   one tab named `runbooks` with the plan writer over enhancer | adversary | mechanic.
   From any session: `gobby-workspaces:get_workspace` shows each tab's `runbook` and
   each pane's `role`, `session_ref` and `backend` once the CLIs have started, and the
   assistant has one registration message per seat naming its pane ref and backend
   (criterion 3).
4. Role activation: in the log-monitor pane, `gobby-workflows:get_variable _agent_type`
   (or the first prompt's injected persona) shows `log-monitor`; an Edit on a `.py` file
   is blocked by `runbook-no-code-edits`. In the plan-writer pane an Edit on
   `.gobby/plans/runbooks.md` passes and one on `docs/guides/agents.md` is blocked. In
   the program-director pane, the first `.py` edit is blocked once with the delegate
   nudge and the retry passes; a `git merge` is never nudged; `/clear` then a fresh
   `.py` edit is nudged again.
5. Persistence: `/clear` in the assistant pane; the successor's first prompt carries
   the assistant persona and `get_workspace` shows the new `session_ref`. Quit the CLI
   in the council's mechanic pane and relaunch it by hand in the same shell; the new
   session is again `plan-mechanic`.
6. Recovery: in the log-monitor pane, `gobby panes set-role 0:0:<tab>:<pane>` clears
   the role and re-sets it as `researcher`; `gobby-workflows:get_variable _agent_type`
   in that session still shows `log-monitor` (set-role binds the next SessionStart
   only). `gobby-agents:apply_agent_definition(agent="researcher")` there flips
   `_agent_type` to `researcher` and the next prompt shows the researcher persona; a
   `/clear` in the pane comes back as `researcher` from the pane row.
7. Failure path: a script line whose op is refused (a pane ref that does not exist)
   prints the daemon's `code: reason` on stderr and exits 1, so `set -e` stops the
   script with the panes created so far left in place and the `CREATED_TAB=` lines on
   stderr name the tabs to remove with `gclient kill`; `wait-for-output` past its
   timeout exits 1. With the gterm host stopped, `orchestration-v1.sh` is refused at its
   first `new-tab` with `terminal_failed`, exits 1, and no role comes up on another
   backend (criterion 5).
8. Amendment disclosure (3.3): claim a scratch task and `update_task` its
   `validation_criteria`; the next turn carries the disclosure order;
   `gobby-tasks-artifacts-ops:get_artifacts` shows `claim_snapshot` with the claim-time
   text; a
   `close_task` whose summary names the amendment passes the close review, and one
   that omits it is bounced with the amendment as the finding.

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

The retirement's own criteria, carried into the deferred task's validation criteria at
expansion (no live deliverable of this plan delivers them; ruling 23 dropped the
post-epic-reviewer that 3.2 once carried for `epic_qa`):
- D2.1 - A planning pass, after orchestration-v1 has run at least one full night (V1
  step 3 and the archivist's record), names what replaces `epic_qa` and the build
  coordinator for stage dispatch under runbooks, or keeps them, as a plan of its own.
- D2.2 - `gobby build` and stage-manifest dispatch are retired or re-scoped by that
  plan, and the memory that build runs autonomously is retired or rewritten to match.

```yaml
deferral:
  task_ref: "TBD-retire-gobby-build"
  reason: "Josh intends runbooks to supersede stage-manifest dispatch; the retirement needs its own planning pass (what replaces epic_qa and the build coordinator, and the memory that build runs autonomously) after orchestration-v1 has run a real night."
  owner: "josh"
  original_acceptance_items:
    - D2.1
    - D2.2
```

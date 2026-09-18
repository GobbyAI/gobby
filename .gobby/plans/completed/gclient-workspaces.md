Plan artifact: `.gobby/plans/gclient-workspaces.md`

# gclient workspaces: daemon-owned layout, pane identity, and pane API

**Plan ID:** gclient-workspaces

## Status
`kind: framing`

Approved narrative, drafted 2026-09-16 by coordinator gobby#12967 from the
plan-mode session with the user (Decision Record below, each item confirmed
in turn). Registered as a child epic of Stage 0 #21334 once expanded; #22202
(retire the tmux wrapper from the Ghostty config) is re-parented under P5 and
becomes its closing chore (deferred section D1). Durable decision memory:
be35449d. Revised 2026-09-17 after the round 1 and round 2 adversaries'
draft findings (both rounds ended in a protocol failure and their evidence
was expired, so no round is recorded); every draft finding was verified
against source and accepted by the coordinator under unattended authority
and folded in below. Revised again after round 3 (the first round to
complete the protocol; recorded under V1 Plan Changelog): all five
blocking findings and both nits were accepted and folded in.

## Context
`kind: framing`

Task #22202 asked to retire the tmux wrapper from the Ghostty config once
native tabs were daemon-visible. Planning found that its preconditions were
never built (no attach client, session seeding still keys on `TMUX_PANE`,
no tab-close policy), and the user re-scoped: Ghostty tabs return to plain
zsh, gclient is where daemon-driven work happens, and gclient must reach
herdr parity on the parts the original client plan left out on purpose
(`.gobby/plans/herdr-terminal-client.md` imported the chrome and inverted
the seam, and skipped herdr's session persistence, JSON API, and
orchestration).

What herdr has that Gobby lacks, unbranded:

| herdr | Gobby today | Gap |
| --- | --- | --- |
| Named sessions: bare launch attaches `default`; `session list/attach/stop/delete`; `--session` | One client-side snapshot file per project under `~/.gobby/client/` (`crates/gclient/src/persist.rs`) | Layout has no owner but the client and no name |
| Several clients view one session with per-viewer presentation state | Two gclients on one project overwrite the same file (`persist_if_changed` in `crates/gclient/src/app/live_loop/projects.rs`) | Same |
| Pane identity in every pane's env: `*_ENV`, `*_SOCKET_PATH`, `*_WORKSPACE_ID`, `*_TAB_ID`, `*_PANE_ID` | `GTERM_ENV=1` only (`crates/gterminal/src/host/spawn.rs`) | Everything else, plus a terminal id |
| Socket API on ids: workspace/tab/pane methods, `pane.read`, `pane.send_text`, `pane.wait_for_output`, `events.subscribe`, `session.snapshot` | WS `terminal_create/attach/input/list` (`docs/contracts/gterm-protocols.md`), REST lists, MCP `send_keys`/`capture_output` keyed by Gobby session | No layout operations, no pane addressing, no `gobby` CLI for any of it |
| `--remote <host>` thin client | #20202, Stage 4 | Stays there |
| Pane zoom, splits, tabs, agent state | Have | None |

A user-started CLI in a gclient pane has no `terminals` row binding today
because only agent spawns pre-bind a session through `GOBBY_SESSION_ID`
(`src/gobby/hooks/event_handlers/_session_start/flow.py`); that is the
concrete cost the identity deliverable (P3) removes.

## Decision Record
`kind: framing`

Confirmed with the user on 2026-09-16.

1. Ghostty tabs are plain zsh. No single-pane attach client, no tab-close
   policy. The tmux wrapper line leaves the Ghostty config.
2. Anything the daemon must drive starts in gclient or in a tmux the user
   starts by hand; a bare tab cannot be adopted later, only resumed inside a
   gclient pane.
3. Closing a gclient window must not lose terminals; relaunching reattaches.
4. The daemon owns the layout. Named workspaces with tabs and panes are hub
   rows served over WS, MCP, and the `gobby` CLI; gclient is a viewer that
   mutates through the daemon; gterm stays PTY plus frames and exports the
   identity into each pane's environment at spawn. Rejected: gterm owning
   the layout (it would grow a second session model beside the daemon's
   terminals rows and every other surface would have to query the host).
5. Noun: **workspace** (`gclient --workspace`, `gobby workspaces`,
   `GOBBY_WORKSPACE_ID`). Projects stay projects inside a workspace. The
   existing `worktrees.workspace_role` and `integration_workspace_mutex`
   mean "isolation worktree" and are untouched.
6. Refs: `n#:w#:t#:p#`, lowest free number per scope, reused after release
   (unlike task and session `#N`). `n#` is the node (machine), `w#` the
   workspace, `t#` a tab inside it, `p#` a pane inside the tab. Rows keep
   UUID keys; env and APIs carry both. A workspace belongs to a node
   (`workspaces.machine_id`, `ref` unique per node). `gclient`, `gobby
   workspaces`, and `gobby panes` take `--node <ref|id|hostname|label>`
   defaulting to the local node, and a full ref (`n2:w1`) overrides it.
   "No node given" is resolved by the daemon that receives the call (its own
   `require_machine_id()`), never guessed by a client; every reply carries
   the resolved `n#`. Attaching another node's workspace becomes possible
   when #20202 supplies the transport; the addressing does not change.
7. Several gclient windows may attach the same workspace at once, each with
   its own focus, zoom, and active tab.
8. No client session state file. The node's workspaces, tabs, and panes,
   including focus hints, live in hub rows and persist across daemon
   restarts (PostgreSQL keeps the rows, the host keeps the PTYs, the restart
   sweep drops panes whose terminals died). Transient per-window state (live
   focus, zoom, scroll, copy mode) is memory only and re-seeds from the row
   hints on launch. User preferences (sidebar width and collapse, project
   order and labels, theme, keymap) live in `prefs.toml`.
9. The client stays `gclient`; `gterm` stays the host.
10. Not in scope: `--remote` (#20202), herdr `--handoff` (its update-time
    live handoff), graphics, popups, plugins (#20201), web UI parity for
    workspaces (the web terminal tab keeps its terminal-id model), and pane
    placement for `spawn_agent` (an agent terminal later placed into a pane
    keeps its spawn-time env).

## Constraints
`kind: framing`

- Schema authority is Rust gcore: a new migration is
  `crates/gcore/assets/schema/migrations/440_add_workspaces.sql` plus the
  pins the derived-carrier rule names; `baseline.sql` is not regenerated;
  `src/gobby/storage/migrations/` is retired.
- Every op names explicit ids or refs, never "the focused pane". Focus is a
  per-window hint stored on rows only as the seed for the next window.
- Pane terminals spawned by a workspace op are always `native` rows
  spawned through the gterm host, resolved explicitly and independent of
  `terminals.default_backend`; the tmux backend never carries pane
  identity (a tmux started by hand in a pane is an ordinary external tmux
  terminal). An adopted terminal keeps whatever backend its row has.
- gclient file ceilings: `crates/gclient/src/app/mod.rs`,
  `crates/gclient/src/daemon/mod.rs`, `crates/gclient/src/daemon/live.rs`,
  and `crates/gclient/src/app/live_loop/menu.rs` are within 100 lines of the
  1000-line guard (`crates/gclient/tests/source_size.rs`). New client code
  goes in `crates/gclient/src/daemon/workspace.rs` and
  `crates/gclient/src/app/workspace_ops.rs` from the start.
- Lifecycle publication fans each event to every client
  (`src/gobby/servers/websocket/broadcast.py`); `workspace_event` is gated by
  the parametric subscription `workspace_event:workspace_id=<id>` from day
  one, and `terminal_output` is untouched.
- Rust CI: `cargo fmt`, `cargo clippy --all-targets -- -D warnings`, and
  `cargo nextest run` per touched crate; gclient builds need
  `ZIG=/Users/josh/.gobby/cache/zig/zig-aarch64-macos-0.16.0/zig`.
- Python: `uv run ruff format src/ tests/`, `uv run ruff check src/ tests/`,
  `uv run mypy src/`, focused pytest under
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`,
  `uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`.
- Schema and crate changes ship through an announced
  `uv run gobby cutover --path .` (global `send_message`, quiet window with
  no live spawned worker or close validator) from a clean schema-input tree.
- Implementation workers: Claude Opus `xhigh`, `backend-developer`, worktree
  isolation, one per leaf; P4 (the client) is the coordinator's own UI leaf.
  Codex only for validators. No Grok.
- Found work outside this plan, already handled separately: the gclient
  launch crash on a null `branch_name` worktree row is #22446; the gterm
  host pidfile ordering is #22425.
- Candidates recorded, not built here: worktree rows whose path vanished
  stay `active`; `GET /api/agents/runs?limit=200` returns 21.5 MB for the
  gobby project because runs carry `result`, `prompt`, and
  `resume_metadata_json`, and gclient fetches it per project on every sidebar
  refetch for 13 scalar fields.

## P1: Hub model and refs
`kind: framing`

**Goal:** the daemon stores workspaces, tabs, and panes as node-scoped hub
rows with reusable `n#:w#:t#:p#` refs, and survives a restart with them.

### 1.1 Add the workspaces schema and node refs [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/440_add_workspaces.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: the MIGRATIONS table gains the 440 entry and its sha256 pin
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog manifest for migration 440
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: the expected schema identity pins move with the new migration
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: latest-migration and identity assertions move
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: runner coverage for migration 440
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: the schema identity literal the CLI contract asserts moves
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated expected schema identity
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: re-signed schema identity for migration 440
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: re-signed schema identity for migration 440
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: re-signed schema identity for migration 440
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: re-signed schema identity for migration 440
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: re-signed schema identity for migration 440

Migration `440_add_workspaces.sql` follows the shape of
`431_add_coordination_waits.sql` (`CREATE TABLE`, indexes, then the mandatory
`GRANT SELECT, INSERT, UPDATE, DELETE ON <table> TO gobby_daemon_runtime`,
as `baseline.sql` does for `terminals`). It creates:

- `workspaces`: `id uuid PRIMARY KEY`, `machine_id` referencing `machines`,
  `ref integer NOT NULL`, `name text NOT NULL`, `focused_project_id`
  nullable referencing `projects` `ON DELETE SET NULL DEFERRABLE` (the
  `terminals` foreign-key style), `focused_tab_id uuid`
  nullable, `created_at`/`updated_at timestamptz`, `UNIQUE (machine_id,
  ref)`, `UNIQUE (machine_id, name)`.
- `workspace_tabs`: `id uuid PRIMARY KEY`, `workspace_id` referencing
  `workspaces ON DELETE CASCADE`, `ref integer NOT NULL`, `title text`,
  `project_id uuid NOT NULL` referencing `projects ON DELETE CASCADE` (a
  tab always has a project: `terminals.project_id` is `NOT NULL`, so every
  pane spawn needs one, and gclient groups tabs by project), `worktree_id`
  nullable referencing `worktrees ON DELETE SET NULL DEFERRABLE`, `position integer NOT
  NULL`, `focused_pane_id uuid` nullable, `layout jsonb NOT NULL` (the split
  tree: `{"kind":"pane","pane_id":...}` leaves and
  `{"kind":"split","axis":"horizontal|vertical","ratio":<f32>,"children":[..]}`
  nodes, mirroring the client's `LayoutNode`), timestamps, `UNIQUE
  (workspace_id, ref)`.
- `workspace_panes`: `id uuid PRIMARY KEY`, `tab_id` referencing
  `workspace_tabs ON DELETE CASCADE`, `ref integer NOT NULL`, `terminal_id
  uuid` nullable referencing `terminals ON DELETE SET NULL DEFERRABLE`
  (`terminals.id` is `uuid`; the column is `NULL` while a split's spawn is
  in flight and after the terminal row is removed, and 1.2's sweep treats
  both as dead), `owns_terminal boolean NOT NULL` (true when the split or
  tab spawned the terminal, false when the pane adopted an existing one;
  2.1's `pane.close` kills only owned terminals), `label text` with a
  byte-limit `CHECK` in the style of `terminals_title_byte_limit`,
  timestamps, `UNIQUE (tab_id, ref)`, and a partial unique index on
  `(terminal_id) WHERE terminal_id IS NOT NULL` so one terminal sits in at
  most one pane even when two adopts race (2.1 replies `busy`).
- `ALTER TABLE machines ADD COLUMN ref integer` with a partial unique index
  on `(owner_user_id, ref)`; existing rows get refs assigned by 1.2's
  allocator on their next `upsert_seen`, so the column is nullable.

The pins that move in the same commit (recipe recorded in
`.gobby/plans/agy-full-integration.md` under "schema pins"): the
`MIGRATIONS` entry in `crates/gcore/src/schema/assets.rs` with the file's
sha256; `catalog.manifest.json` regenerated through
`gcore::schema::render_catalog_manifest` against a hub with 440 applied;
`src/gobby/storage/schema_expected_identity.json` regenerated by
`scripts/generate_schema_expected_identity.py` (CI byte-compares it);
`expected_schema_identity` in `crates/gcore/src/grant/bundle.rs`; the
identity literals in `crates/gcore/tests/schema_contract.rs` and
`crates/gdaemon/tests/cli_contract.rs`; runner coverage in
`crates/gcore/src/schema/runner_tests.rs`; and the five signed golden grant
vectors under `tests/runtime_grants/golden/` (the set commit 9dcff7393c
re-signed), re-signed by the procedure in
`tests/runtime_grants/test_golden_vectors.py`. `baseline.sql` is not
regenerated. The migration is the only schema input, so `uv run gobby
cutover --path .` accepts it from a clean tree.

**Research context:**

- Schema authority is Rust gcore (`crates/gcore/src/schema/assets.rs`
  embeds every migration; `src/gobby/storage/migrations/` is retired).
  Latest migration today: `439_retire_linear_github_issue_bridge.sql`.
- The `machines` table (`baseline.sql`, `CREATE TABLE machines`) has `id,
  hostname, os, label, tailscale_name, owner_user_id, first_seen,
  last_seen` and no ref. `LocalMachineManager.upsert_seen`
  (`src/gobby/storage/machines.py`) is the only writer.
- `terminals` foreign keys use `ON DELETE SET NULL DEFERRABLE` (not
  `INITIALLY DEFERRED`); `terminals.id` is `uuid`; the title byte limit is
  the `terminals_title_byte_limit` constraint. All three are the model for
  the new tables.
- Rejected: storing the whole layout as one jsonb blob on `workspaces`.
  Pane rows need their own identity for refs, env, and the terminal foreign
  key; the split tree is the only part that is naturally a document.
- Verification planned: `cargo nextest run -p gobby-core`, `cargo nextest
  run -p gobby-daemon --test cli_contract`, `uv run pytest
  tests/runtime_grants/test_golden_vectors.py`, and a read-only `gdaemon
  schema plan` against the dev hub reporting exactly one pending migration.

**Acceptance:**

- 1.1.1 - Migration 440 creates `workspaces`, `workspace_tabs`,
  `workspace_panes`, adds `machines.ref`, and grants the runtime role. file:
  `crates/gcore/assets/schema/migrations/440_add_workspaces.sql`.
- 1.1.2 - The embedded migration table, catalog manifest, grant identity
  pins, contract tests, and expected identity file agree on the new schema
  identity. test:
  `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
- 1.1.3 - The signed golden grant vectors verify against the new identity.
  test: `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.

### 1.2 Add WorkspaceManager with ref allocation and the dead-pane sweep [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/workspaces.py`
- `src/gobby/storage/machines.py::*` — scope-reason: Machine gains ref and upsert_seen allocates the lowest free node ref
- `src/gobby/runner_init/terminal_wiring.py::*` — scope-reason: the wiring constructs the WorkspaceManager beside the terminal services
- `src/gobby/runner_init/servers.py::*` — scope-reason: the configure_terminals call site and the ServiceContainer construction pass the WorkspaceManager
- `src/gobby/app_context.py::*` — scope-reason: ServiceContainer carries the WorkspaceManager for the MCP registries
- `src/gobby/servers/websocket/server.py::*` — scope-reason: configure_terminals accepts an optional WorkspaceManager keyword
- `src/gobby/guard_set_g.py::*` — scope-reason: the guard group listing gains the new storage and wiring tests
- `tests/storage/test_workspaces.py`
- `tests/storage/test_machines.py::*` — scope-reason: ref allocation and node resolution cases
- `tests/terminals/test_composition_roots.py::*` — scope-reason: the wiring source scan moves with the new manager injection

`src/gobby/storage/workspaces.py` defines `Workspace`, `WorkspaceTab`, and
`WorkspacePane` dataclasses with `from_row` and `to_dict` (the `Terminal`
and `Worktree` shape), a `LayoutNode` validator for the jsonb tree, and
`WorkspaceManager(db)` in the `TerminalManager`/`LocalWorktreeManager`
style: `resolve_node` (uuid, `n#`, hostname, or label through
`LocalMachineManager`; `None` means `require_machine_id()`),
`resolve_reference` (uuid, name, or `n#:w#` ref for a workspace; `n#:w#:t#`
and `n#:w#:t#:p#` for tabs and panes, the `LocalWorktreeManager.resolve_reference`
shape), `create` (auto-creates `default` on first attach), `list_for_node`,
`get`, `rename`, `close`, `create_tab`, `rename_tab`, `move_tab`,
`close_tab`, `add_pane` (inserts a leaf beside a sibling with an axis),
`remove_pane` (collapses the split to the survivor), `swap_panes`,
`move_pane`, `set_ratio`, `rename_pane`, `set_focus_hints`,
`set_pane_terminal`, `sweep_dead_panes`, and the in-flight spawn guard
`mark_spawn_in_flight(pane_id)` / `clear_spawn_in_flight(pane_id)`: a
process-local set owned by this one manager (1.2.5), so every surface
that runs an op shares it. `add_pane` takes the caller-minted pane uuid,
and 2.1 registers the id before the insert transaction, so no other
thread can observe the committed `NULL` row unregistered. Ref allocation
is `SELECT ... FOR UPDATE` on the parent row inside
`with self.db.transaction():` picking the lowest positive integer absent
from the sibling set, so a closed pane's number is reused. Every
multi-row mutation is one transaction, and the parent-row lock is the
only serialization: no `asyncio` locks beside it. A mutation that spans
two parent rows (`move_pane` across tabs, `move_tab`) locks them in
ascending id order inside that one transaction, so opposite concurrent
moves serialize instead of deadlocking.

`LocalMachineManager.upsert_seen` allocates `machines.ref` for the owner the
same way on first sight of a machine without one, and `Machine.to_dict`
carries `ref`; `LocalMachineManager.get` and `list_for_user` resolve `n#`.

`init_terminal_wiring` constructs the manager beside the terminal
services and stores it on the runner; `src/gobby/runner_init/servers.py`
passes it to `configure_terminals` at the existing call site (the new
keyword defaults to `None` so the five tests that call
`configure_terminals` today keep passing) and into the `ServiceContainer`
(`src/gobby/app_context.py`), which is how `HTTPServer._init_mcp_subsystems`
hands it to the MCP registries in 2.3.

`sweep_dead_panes(workspace_id)` is a read-time and event-time derivation,
never a startup hook and never a settlement hook: 2.1 runs it at the start
of every op, 2.2 at every `workspace_attach` and `workspace_snapshot`, and a
viewer that receives `terminal_exited` for a pane's terminal requests a
snapshot (4.2), so the exit is pruned and published without a hook in
`settle_exit`. A pane is dead when its terminal row is not
`pending` or `live`, or when `terminal_id` is `NULL` and the pane is not
in the manager's own in-flight set, which the sweep reads after taking the
parent-row lock. Dead panes
leave their tab's layout (the split collapses to the survivor), a tab with
no panes is removed, an empty workspace survives so a new window respawns a
shell into it, and every removal publishes a `pane.removed` or
`tab.removed` workspace event (2.1). Every writer of `terminals.state`
(`settle_exit`, `reconcile_host_inventory`, `fail_pending_attempt`, host
death, event-gap recovery, the tmux sweep) is therefore covered by the next
read without a hook of its own, and a restart that killed terminals prunes
on the first attach after it.

**Granularity:** one storage manager plus its wiring: the composition-root
test scans one source file, so the manager and its injection land
together or the scan asserts a wiring that does not exist yet.

**Research context:**

- `TerminalManager` (`src/gobby/storage/terminals.py`) and
  `LocalWorktreeManager` (`src/gobby/storage/worktrees.py`, with
  `resolve_reference` accepting id, path, or branch) are the pattern:
  dataclass rows, `from_row`, hub transaction boundary with psycopg `%s`
  placeholders.
- `init_terminal_wiring` (`src/gobby/runner_init/terminal_wiring.py`)
  builds the lease registry, runtimes, and coordinator and calls
  `clear_orphaned_attachment_writes`; `tests/terminals/test_composition_roots.py`
  scans that source text, so the new injection must keep its assertions
  true or move them.
- `ensure_machine_identity` (`src/gobby/runner_init/helpers.py`) calls
  `LocalMachineManager.upsert_seen` at daemon start with the id from
  `require_machine_id` (`src/gobby/utils/machine_id.py`), which is how the
  daemon knows its own node; gclient reads the same file through
  `gobby_core::machine::read_local_machine_id`.
- Rejected: a client-side node guess. Only the daemon resolves "no node
  given", so every reply carries the resolved `n#` and a remote client
  (#20202) needs no change to addressing.
- Verification planned: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run
  pytest tests/storage/test_workspaces.py tests/storage/test_machines.py
  tests/terminals/test_composition_roots.py`, plus `uv run mypy src/`.

**Acceptance:**

- 1.2.1 - `WorkspaceManager` creates, resolves (uuid, name, every ref
  form), mutates, and closes workspaces, tabs, and panes with reusable
  lowest-free refs inside single transactions. symbol: `WorkspaceManager`.
  file: `src/gobby/storage/workspaces.py`.
- 1.2.2 - A closed pane's ref is reused by the next split and a closed
  workspace's `w#` by the next create. test:
  `tests/storage/test_workspaces.py::test_refs_are_lowest_free_and_reused`.
- 1.2.3 - Node resolution accepts `n#`, uuid, hostname, and label, and
  `None` resolves to the daemon's own machine id; `machines.ref` is
  allocated on `upsert_seen`. test:
  `tests/storage/test_machines.py::test_upsert_seen_allocates_lowest_free_ref`.
- 1.2.4 - The sweep drops panes whose terminal was transitioned by any
  writer (`mark_exited`, `mark_orphaned`, `fail_pending_attempt`, a deleted
  row), keeps a `NULL` pane while `mark_spawn_in_flight` holds its id and
  prunes it once `clear_spawn_in_flight` releases it, removes empty tabs,
  and keeps empty workspaces. test:
  `tests/storage/test_workspaces.py::test_sweep_dead_panes_prunes_layouts`.
- 1.2.5 - The daemon composition root constructs one `WorkspaceManager`
  and hands it to both the WebSocket server and the `ServiceContainer`.
  test: `tests/terminals/test_composition_roots.py::test_wiring_hands_one_workspace_manager_to_both_servers`.

## P2: Daemon API
`kind: framing`

**Goal:** every layout operation is addressable over the WebSocket by ref
or id, MCP and the `gobby` CLI carry the per-surface subsets that 2.3 and
2.4 list (Decision 4 names those three surfaces; no HTTP route has a
consumer), and pane events share the terminal lifecycle ordering.

### 2.1 Execute workspace ops through a shared module with actor scope [category: code] (depends: 1.2, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/terminals/workspace_ops.py`
- `src/gobby/terminals/actor_scope.py`
- `src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py::*` — scope-reason: its caller and target scope policy moves into actor_scope and send_keys calls the shared function
- `src/gobby/terminals/web_spawn.py::*` — scope-reason: spawn_web_terminal takes a caller env for the pane identity
- `src/gobby/servers/websocket/terminal_ws_create.py::*` — scope-reason: the terminal_kill body becomes a shared helper that pane close and the mid-spawn rollback call
- `tests/terminals/test_workspace_ops.py`

`src/gobby/terminals/workspace_ops.py` executes every op for every surface:
2.2 serves it over the WebSocket, 2.3 over MCP, and 2.4 through the CLI.
`WorkspaceOps` is constructed with the `WorkspaceManager`, the terminal
runtime registry and `WriteCoordinator`, the session manager, and a
`publish` callable; 2.2 injects `broadcast_workspace_event` and the tests
pass a recorder, so every op is testable without a socket. Each surface
may build its own `WorkspaceOps` (the WebSocket server is constructed
after the HTTP server's MCP registries in
`src/gobby/runner_init/servers.py`), because the only process-local
state, the in-flight spawn guard, lives on the one shared
`WorkspaceManager` (1.2), not on the ops object. Spawning ops resolve
the native runtime explicitly (`registry.resolve("native")`), never
`terminals.default_backend`, and raise `terminal_failed` when the native
runtime is unavailable; ops on an adopted terminal resolve its runtime
from the row's `backend`, so an adopted tmux agent terminal is read,
written, and released through the tmux runtime. Every entry point runs
`sweep_dead_panes` for the workspace first (1.2). The op vocabulary is
`workspace.create|rename|close`, `tab.create|rename|move|close`,
`pane.split|swap|move|resize|rename|close`,
`pane.send_text|send_keys|read|wait_for_output`, and
`workspace.set_focus_hints`; every op names explicit ids or refs. Failures
are typed exceptions carrying one of `not_found`, `invalid_ref`,
`invalid_op`, `terminal_failed`, `busy`, or `forbidden`, which each surface
maps to its own error shape. Every mutation publishes a `workspace_event`
payload with `kind` in `workspace.created|renamed|closed`,
`tab.created|renamed|moved|closed|removed`,
`pane.added|swapped|moved|resized|renamed|closed|removed`, or
`focus_hints`, carrying the rows it changed.

The op payloads:

- `tab.create {workspace, project_id, worktree_id?, title?, terminal_id?}`
  makes the tab and its first pane: it spawns a shell into the project's
  checkout (or the worktree's path) unless `terminal_id` names a live
  terminal to adopt (`open_agent_in_new_tab` in gclient adopts an agent
  terminal this way; the adopted terminal keeps its spawn-time env).
  Adopting a terminal that another pane already holds raises `busy` naming
  the existing pane's ref; the partial unique index from 1.1 makes the
  second `set_pane_terminal` fail even when two adopts race.
- `pane.split {pane, axis, terminal_id?}` inserts the new pane beside the
  named one. Order: the op mints the pane uuid and calls
  `mark_spawn_in_flight` on the manager; one transaction then inserts the
  pane row with `terminal_id NULL`, allocates its ref, and updates the
  layout; then, unless `terminal_id` adopts, `spawn_web_terminal` runs on
  the native runtime with the identity env (`GOBBY_NODE_ID`,
  `GOBBY_NODE_REF`, `GOBBY_WORKSPACE_ID`, `GOBBY_TAB_ID`, `GOBBY_PANE_ID`,
  `GOBBY_PANE_REF` from 3.1's names; `GOBBY_TERMINAL_ID` comes from the
  runtime; no `GOBBY_PROJECT_ID` or `GOBBY_DAEMON_URL`, because
  `read_project_id` in `src/gobby/mcp_proxy/stdio_proxy.py` prefers the
  env over the cwd and a CLI started after `cd` into another checkout
  would route to the pane's spawn-time project; the pane's project is on
  its tab and terminals rows) and the tab's checkout as cwd;
  `set_pane_terminal` then records the minted id and `owns_terminal`, and
  reports whether the pane row still exists. If the spawn raises or the
  runtime reports failure, the op deletes the pane row, restores the
  layout in one transaction, publishes `pane.removed`, and raises
  `terminal_failed`. If the pane row was removed while the spawn ran (a
  cascade such as a project delete; the close ops refuse in-flight panes
  with `busy`), the op kills the minted terminal through the shared
  `terminal_kill` helper and raises `not_found`. `clear_spawn_in_flight`
  runs on every outcome, so a concurrent sweep never prunes a pane whose
  spawn is still running, and a daemon that died mid-split leaves a
  `NULL` pane the next attach prunes.
  `spawn_web_terminal` keeps minting the terminal id and creating the
  pending row itself; `TerminalSpawnRequest` already carries `env`, so only
  `spawn_web_terminal` gains a caller `env` parameter, merged under the
  runtime's own variables so nothing shadows `GOBBY_TERMINAL_ID`.
- `pane.close {pane}` removes the pane row and collapses the split in one
  transaction and publishes `pane.removed`; when `owns_terminal` is true and
  the terminal is `pending` or `live` it also runs the shared
  `terminal_kill` helper (the pane row is already gone, so the later exit
  event has nothing to sweep). An adopted terminal (agent or external) is
  released, never killed, matching what gclient does today for external
  panes. An exited or orphaned terminal settles nothing: the row removal is
  the whole op. A pane whose split is still in flight (`terminal_id NULL`
  and held by the manager's in-flight set) raises `busy` instead of
  racing the spawn; the caller retries after the split replies.
- `pane.move {pane, tab, beside?, axis?}` moves a pane across tabs: it
  leaves its source split (collapsed to the survivor), takes the lowest
  free ref in the destination tab, and both tabs publish; the two tab
  rows are locked in ascending id order in one transaction (1.2), so
  opposite moves from two windows serialize. `GOBBY_PANE_REF`
  in the pane's environment is spawn-time identity, like every identity
  variable (Decision 10); `GOBBY_PANE_ID` stays the stable key.
- `pane.swap`, `pane.resize {pane, ratio}`, `pane.rename`, `tab.rename`,
  `tab.move`, `tab.close` (closes each pane as `pane.close` does, and
  raises `busy` before touching any row while a child pane's split is in
  flight), `workspace.create|rename|close` (close closes every tab, with
  the same `busy` guard), and `workspace.set_focus_hints` are row
  mutations with one transaction each.
- `pane.send_text`, `pane.send_keys`, `pane.read`, and
  `pane.wait_for_output` reuse the `WriteCoordinator.write(WriteRequest(...,
  origin="daemon"))` body of `send_keys` in
  `src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py` with an
  `action_key` of `workspace-pane-send:<pane>:<key>`, the `runtime.snapshot`
  body of `capture_output`, and the matcher loop of `wait_for_output` in
  `src/gobby/mcp_proxy/tools/agents_query_tools.py`.

Authorization keeps the effect's existing policy. Every op takes an
`actor`, `operator` or `session:<id>`; the surfaces derive it (2.2 for the
socket, 2.3 for MCP and the CLI's calls through it) and the ops module
never inspects a token. `src/gobby/terminals/actor_scope.py` holds
`resolve_actor_scope(session_manager, actor)`, the policy now inside
`_authorize_send_keys_target`: an autonomous agent-run session actor is
refused, a target terminal is in scope for a session actor when its project
is the caller's project or its bound session is in the caller's agent tree,
and the `operator` actor is in scope everywhere. `send_keys` calls the
shared function. The scope applies to every op that kills, spawns into, or
adopts a terminal: the four pane terminal ops check the pane's terminal;
`pane.split` and `tab.create` check the tab's project for a spawn and the
adopted terminal for an adopt; `pane.close`, `tab.close`, and
`workspace.close` check every owned live terminal they would kill. Row-only
ops (`rename`, `move`, `swap`, `resize`, `set_focus_hints`,
`workspace.create|rename`) are open to any actor that reached the surface.
An out-of-scope call raises `forbidden`.

There is no exit hook: `settle_exit` is a storage update run from
`_apply_host_event` with no manager or broadcaster in reach. A viewer
learns that a pane's terminal exited from the `terminal_exited` lifecycle
event it already receives and requests `workspace_snapshot` (4.2), whose
sweep publishes `pane.removed` to every attached viewer; every other reader
prunes at its next op.

**Research context:**

- `_authorize_send_keys_target` in
  `src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py` requires a
  `SessionContext`, refuses callers with an `agent_run_id`, and admits a
  target in the same project or in the caller's agent tree (`is_ancestor`);
  that is the policy `actor_scope.py` lifts.
- `spawn_web_terminal` (`src/gobby/terminals/web_spawn.py`) builds a
  `TerminalSpawnRequest` without env today; the request's `env` field
  exists (`src/gobby/terminals/runtime.py`) and flows to
  `NativeTerminalRuntime.prepare_spawn` and the host's `spawn_prepared`.
- The `terminal_kill` body lives in
  `src/gobby/servers/websocket/terminal_ws_create.py`; `settle_exit` in
  `src/gobby/storage/terminal_settlement.py` is called from
  `host_manager._apply_host_event` through `asyncio.to_thread` with only
  the storage manager in scope.
- Rejected: an `asyncio` lock per pane for the mid-spawn race; the row's
  existence check in `set_pane_terminal` and the in-flight set are enough,
  and the parent-row lock (1.2) already serializes layout writes.
- Rejected: a settlement hook that sweeps the exited terminal's tab; it
  would need the manager and the broadcaster threaded into storage code.
- Verification planned: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run
  pytest tests/terminals/test_workspace_ops.py` plus the existing
  `send_keys` tests under `tests/mcp_proxy/`, and `uv run mypy src/`.

**Acceptance:**

- 2.1.1 - `pane.split` inserts the pane before spawning, spawns on the
  native runtime even when `default_backend` is `tmux`, with the identity
  env (and without `GOBBY_PROJECT_ID` or `GOBBY_DAEMON_URL`) and the tab's
  checkout as cwd, records the terminal id, and on spawn failure or an
  unavailable native runtime removes the row, restores the layout, and
  raises `terminal_failed`; a sweep during the spawn leaves the pane
  alone; a pane whose row a cascade removed mid-spawn has its minted
  terminal killed and the op raises `not_found`; `pane.close`,
  `tab.close`, and `workspace.close` on an in-flight pane raise `busy`. test:
  `tests/terminals/test_workspace_ops.py::test_split_spawns_with_pane_identity_env_and_rolls_back`.
- 2.1.2 - `tab.create` and `pane.split` adopt a named live terminal of
  either backend without spawning and raise `busy` with the holder's ref
  when it is already paned; `pane.read` and `pane.send_keys` on an adopted
  tmux terminal go through the tmux runtime resolved from the row's
  `backend`; `pane.close` kills only an owned live terminal, releases an
  adopted one,
  and removes the row of an exited one immediately; `pane.move` across tabs
  collapses the source split and allocates a destination ref. test:
  `tests/terminals/test_workspace_ops.py::test_adopt_close_and_move_semantics`.
- 2.1.3 - A session actor outside the target's project and agent tree, or
  an autonomous agent-run session, gets `forbidden` from the pane terminal
  ops, from a spawn or adopt, and from `pane.close`, `tab.close`, and
  `workspace.close` over an owned live terminal, while the operator actor
  and row-only ops are admitted; `send_keys` keeps its behavior through the
  shared policy. test:
  `tests/terminals/test_workspace_ops.py::test_actor_scope_guards_kill_spawn_and_adopt`.
- 2.1.4 - Every mutation publishes its `workspace_event` through the
  injected callable and every failure is a typed exception with one of the
  six codes. test:
  `tests/terminals/test_workspace_ops.py::test_ops_publish_events_and_raise_typed_errors`.

### 2.2 Serve workspace messages and events over the terminal WebSocket [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/workspace_ws.py`
- `src/gobby/servers/websocket/server.py::*` — scope-reason: the mixin joins the server bases and the dispatch table gains the workspace messages
- `src/gobby/servers/websocket/broadcast.py::*` — scope-reason: broadcast_workspace_event, the gated event_types set, and the parametric filter
- `crates/gclient/src/daemon/ws.rs::*` — scope-reason: GOLDEN_NAMES gains the workspace fixtures the corpus assert requires
- `crates/gclient/tests/ws_golden.rs::*` — scope-reason: corpus count and exact-set assertions move
- `web/src/hooks/__tests__/useTmuxSessions.test.ts::*` — scope-reason: the web corpus count and directory-set assertions move with the grown corpus
- `tests/fixtures/terminal_ws_golden/manifest.json::*` — scope-reason: the corpus manifest lists the five workspace fixtures
- `tests/fixtures/terminal_ws_golden/workspace_attach.json`
- `tests/fixtures/terminal_ws_golden/workspace_snapshot.json`
- `tests/fixtures/terminal_ws_golden/workspace_op.json`
- `tests/fixtures/terminal_ws_golden/workspace_event.json`
- `tests/fixtures/terminal_ws_golden/workspace_error.json`
- `tests/servers/test_terminal_ws_golden.py::*` — scope-reason: fixture count literals and emitter coverage move
- `tests/servers/test_workspace_ws.py`
- `docs/contracts/gterm-protocols.md`

`WorkspaceWsMixin` (`src/gobby/servers/websocket/workspace_ws.py`) is
shaped like `TerminalCreateMixin`: it joins `WebSocketServer`'s bases and
adds `workspace_attach`, `workspace_snapshot`, and `workspace_op` to
`_dispatch_table`, each executing through 2.1's `WorkspaceOps` with the
`operator` actor (the socket is authenticated as the local user).
`workspace_attach` takes `{node?, workspace?}` (ref, uuid, or name; absent
means the daemon's node and `default`, created on first attach), registers
the connection on the parametric subscription
`workspace_event:workspace_id=<id>`, and replies with the full snapshot
(workspace row with resolved `n#`, tabs with layouts, panes with their
terminal ids and refs) plus `snapshot: {daemon_epoch, seq}` taken from
`lease_registry.lifecycle_snapshot()` exactly as `_handle_terminal_list`
does. `workspace_snapshot` re-serves it; both run the sweep through the ops
module first (1.2). `workspace_op` is one envelope `{request_id, op, ...}`
carrying 2.1's vocabulary. A typed exception from the ops module becomes a
`workspace_error` whose `code` passes through `_bounded_code`. Replies
correlate on `request_id` like every terminal message, and events are
`workspace_event` messages carrying the payloads 2.1 publishes.

`BroadcastMixin.broadcast_workspace_event` sits beside
`broadcast_worktree_event`, is the `publish` callable the composition root
hands to `WorkspaceOps`, and publishes through
`lease_registry.publish_lifecycle` so pane events and the terminal events
for their rows share one `daemon_epoch`/`seq` order. `workspace_id` and
`project_id` sit at the top level of the message so the parametric filter
narrows fan-out, and `workspace_event` joins the gated `event_types` set.
Payloads above the page bound use `fragment_event`.

Goldens: the five fixtures join `tests/fixtures/terminal_ws_golden/` and
`manifest.json`; the count literals in `tests/servers/test_terminal_ws_golden.py`
and `crates/gclient/tests/ws_golden.rs` move, `GOLDEN_NAMES` in
`crates/gclient/src/daemon/ws.rs` gains the five names, or the Rust
exact-set assertion fails on the grown corpus, and the web replay in
`web/src/hooks/__tests__/useTmuxSessions.test.ts` (its `toHaveLength`
corpus count and the directory-set assertions) moves with the manifest, or
`vitest` fails on the grown corpus. The message table in
`docs/contracts/gterm-protocols.md` gains the four messages and the six
error codes.

**Research context:**

- `WebSocketServer` (`src/gobby/servers/websocket/server.py`) composes
  `TerminalWsMixin`, `TerminalCreateMixin`, and `BroadcastMixin`;
  `_handle_message` routes by `_dispatch_table`. `_bounded_code` lives in
  `terminal_ws_create.py`. `_handle_terminal_list` (`terminal_ws.py`)
  attaches the lifecycle watermark.
- `BroadcastMixin._is_subscribed` gates by `event_types`; the parametric
  filter accepts `<event>:<field>=<value>` subscriptions. `publish_lifecycle`
  (`src/gobby/terminals/leases.py`) is the single ordered publisher.
- The golden corpus has three consumers: the Python emitter test, the
  Rust replay, and the web hook test, which asserts the manifest length
  and the fixture directory set.
- Rejected: a second WebSocket endpoint. One socket keeps the watermark
  and the client's reconnect path unchanged.
- Rejected: optimistic client apply. The daemon is authoritative; the only
  local apply is the split-ratio drag (P4), sent on drop.
- Verification planned: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run
  pytest tests/servers/test_workspace_ws.py
  tests/servers/test_terminal_ws_golden.py`, `cargo nextest run -p
  gobby-client --test ws_golden`, and `npm test --
  src/hooks/__tests__/useTmuxSessions.test.ts` under `web/`.

**Acceptance:**

- 2.2.1 - `workspace_attach` without a node or workspace attaches the
  daemon node's `default`, creating it on first use, and its reply carries
  the resolved `n#` and a lifecycle watermark. test:
  `tests/servers/test_workspace_ws.py::test_attach_creates_default_on_the_local_node`.
- 2.2.2 - Every `workspace_op` round-trips with its `request_id`, and each
  typed ops exception yields a `workspace_error` with its code. test:
  `tests/servers/test_workspace_ws.py::test_ops_round_trip_and_errors_are_typed`.
- 2.2.3 - A `workspace_event` and the terminal event for the same pane
  share the lifecycle order, and only connections subscribed to that
  workspace receive it. test:
  `tests/servers/test_workspace_ws.py::test_workspace_events_share_lifecycle_order_and_filter`.
- 2.2.4 - The golden corpus gains the five workspace fixtures and both
  emitters match them. test:
  `tests/servers/test_terminal_ws_golden.py::test_python_matches_terminal_ws_golden_corpus`.
- 2.2.5 - The Rust replay of the corpus accepts the grown manifest. test:
  `crates/gclient/tests/ws_golden.rs::corpus_replays_from_canonical_manifest`.
- 2.2.6 - The web replay of the corpus accepts the grown manifest. test:
  `web/src/hooks/__tests__/useTmuxSessions.test.ts::replays every canonical terminal WS fixture from the manifest`.
- 2.2.7 - The protocol contract documents the four messages and their
  error codes. behavior: "workspace messages" in
  `docs/contracts/gterm-protocols.md`.

### 2.3 Add the gobby-workspaces MCP registry [category: code] (depends: 2.2, 3.2)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/workspaces/__init__.py`
- `src/gobby/mcp_proxy/tools/workspaces/registry.py`
- `src/gobby/mcp_proxy/registries.py::*` — scope-reason: gobby-workspaces joins the internal registries and receives the WorkspaceManager
- `src/gobby/servers/http.py::*` — scope-reason: _init_mcp_subsystems passes the WorkspaceManager from the ServiceContainer to setup_internal_registries
- `src/gobby/servers/routes/mcp/endpoints/request_context.py::*` — scope-reason: _set_context_for_request seeds and resets the request-principal context var beside the session and project vars
- `docs/guides/mcp-tools.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/workspaces.md`
- `src/gobby/install/shared/skills/gobby/catalog.json::*` — scope-reason: the catalog lists the new workspaces reference
- `docs/reference-audit/sessions.json::*` — scope-reason: audit records for every gobby-workspaces tool (surface mcp) and the reference mapping
- `tests/mcp_proxy/test_workspaces_registry.py`

`create_workspaces_registry` on `InternalToolRegistry` registers sixteen
tools: `list_nodes`, `list_workspaces`, `get_workspace`,
`create_workspace`, `close_workspace`, `create_tab`, `close_tab`,
`split_pane`, `close_pane`, `move_pane`, `swap_panes`, `rename`,
`send_text`, `send_keys`, `read_pane`, and `wait_for_pane_output`, each
taking refs or ids and an optional `node`, all executing through
`src/gobby/terminals/workspace_ops.py`. The registry derives the actor
from two context variables the HTTP MCP endpoint seeds per request:
the existing `SessionContext` (set from the `X-Gobby-Session-Id` header
or the body `session_id`) gives `session:<id>`; without one, a new
request-principal context var, seeded in `_set_context_for_request` from
`AuthService.request_principal(request)` and reset with the session and
project vars, decides: `None` (the local CLI token, which is how 2.4's
commands arrive) gives `operator`, and `AgentApiTokenClaims` (a managed
run presenting its agent API token) is refused `forbidden` like an
autonomous session. An unseeded var (no request in scope) is refused the
same way, so nothing defaults to `operator`. The var is the only new
plumbing: `setup_internal_registries` keeps its signature. So the pane terminal tools, spawns, adopts, and closes carry the
`send_keys` policy (2.1) and events publish through 2.2's broadcaster. It
is wired in `setup_internal_registries` beside gobby-worktrees, receiving
the manager and the ops module that `HTTPServer._init_mcp_subsystems`
takes from the `ServiceContainer`. The tool table in `docs/guides/mcp-tools.md` gains the
server. The reference library closes over the new tools as
`docs/reference-audit/coverage-policy.md` requires: the operation reference
`references/sessions/workspaces.md` is listed in the skill's `catalog.json`
and every tool gets an audit record in `docs/reference-audit/sessions.json`
(the shape `gobby-worktrees` uses in `source-control.json`), so
`tests/skills/test_reference_library.py` stays green.

**Research context:**

- `InternalToolRegistry` (`src/gobby/mcp_proxy/tools/internal.py`) and the
  gobby-worktrees wiring in `setup_internal_registries` are the pattern;
  `send_keys` and `capture_output` under `src/gobby/mcp_proxy/tools/sessions/`
  show the session-keyed equivalents whose bodies 2.1 shares.
- `_set_context_for_request`
  (`src/gobby/servers/routes/mcp/endpoints/request_context.py`) derives a
  `SessionContext` only from the wrapper header or the body `session_id`;
  `AuthService.request_principal` returns `AgentApiTokenClaims` for a
  managed agent token, `None` for the operator's local CLI token, and
  `False` when rejected. `setup_internal_registries` receives the
  config resolver, services, and resolvers only, never the `HTTPServer`
  or the request, and today's context vars carry only the session,
  project, and agent-run id, so the principal needs its own var seeded
  where the request is held. `request_context.py` is also a 3.2 Target,
  which is why 2.3 depends on 3.2.
- The cataloging gate is `test_reference_contract_3_2_1` in
  `tests/skills/test_reference_library.py`; its inventory mocks every
  `setup_internal_registries` parameter, so the new registry's wiring
  keyword must be added there too.
- Rejected: extending gobby-sessions. Pane addressing is by ref, not by
  session, and the agent-facing surface should not require a session id.
- `docs/reference-audit/coverage-policy.md` maps every registered MCP tool
  and every visible Click leaf to an exact catalog reference plus an audit
  record; `gobby-worktrees` and `gobby worktrees` are covered by
  `references/source-control/worktrees.md` and `source-control.json`.
- Verification planned: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run
  pytest tests/mcp_proxy/test_workspaces_registry.py
  tests/skills/test_reference_library.py`.

**Acceptance:**

- 2.3.1 - The registry exposes the sixteen tools with schemas and executes
  them by ref through the shared ops module. symbol:
  `create_workspaces_registry`. file:
  `src/gobby/mcp_proxy/tools/workspaces/registry.py`.
- 2.3.2 - `read_pane` returns the pane's screen and `wait_for_pane_output`
  resolves on a matcher hit or times out typed. test:
  `tests/mcp_proxy/test_workspaces_registry.py::test_read_and_wait_address_panes_by_ref`.
- 2.3.5 - Through the HTTP MCP route, a wrapper call with a session id
  acts as `session:<id>`, a local-CLI-token call with no session acts as
  `operator`, an agent-token call with no session is refused
  `forbidden`, and a direct registry call with no request principal
  seeded is refused `forbidden`. test:
  `tests/mcp_proxy/test_workspaces_registry.py::test_actor_is_derived_from_session_context_and_principal`.
- 2.3.3 - The MCP tools guide lists the server. behavior:
  "gobby-workspaces" in `docs/guides/mcp-tools.md`.
- 2.3.4 - Every gobby-workspaces tool maps to the workspaces reference and
  an audit record. test:
  `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.

### 2.4 Add the workspaces, panes, and nodes CLI groups [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `src/gobby/cli/workspaces.py`
- `src/gobby/cli/__init__.py::*` — scope-reason: the three command groups register on the root cli
- `docs/guides/cli-commands.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/workspaces.md`
- `docs/reference-audit/sessions.json::*` — scope-reason: audit records for every visible workspaces, panes, and nodes leaf (surface cli)
- `tests/cli/test_workspaces.py`

`gobby workspaces list|show|create|delete` (backed by `list_workspaces`,
`get_workspace`, `create_workspace`, `close_workspace`), `gobby panes
split|close|move|swap|rename|send|read|wait <ref>` (`split` takes exactly
one of `--right`, `--left`, `--above`, `--below` and an optional
`--terminal <id>` to adopt; `move` takes `--tab <ref>` and the same
direction flags for `beside`), and `gobby nodes list` (backed by
`list_nodes`) copy the `_call_worktree_tool` shape of
`src/gobby/cli/worktrees.py` with its `--json` flag, calling the daemon's
internal registry with the local CLI token, which the MCP route classifies
as the `operator` actor (2.3); the ops module never sees the token. Every command takes `--node <ref|id|hostname|label>`
(default: the daemon's node) and a full ref such as `n2:w1:t1:p2`
overrides it. Groups are registered in `cli` beside `worktrees`. The
command table and a `### Workspaces` section after `### Worktrees` in
`docs/guides/cli-commands.md` document them, and each visible leaf gets
its audit record in `docs/reference-audit/sessions.json` against the same
reference 2.3 added, as the coverage policy requires for Click leaves.

**Research context:**

- `src/gobby/cli/worktrees.py` (`_call_worktree_tool`, the group and
  command functions) is the operator CLI pattern that calls the daemon's
  internal MCP registry over HTTP.
- Verification planned: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run
  pytest tests/cli/test_workspaces.py tests/skills/test_reference_library.py`
  against a fake daemon.

**Acceptance:**

- 2.4.1 - The three groups exist with the listed commands, `--node`, and
  `--json`; a full ref overrides `--node`; `panes split` requires exactly
  one direction flag and forwards `--terminal`. test:
  `tests/cli/test_workspaces.py::test_panes_split_resolves_ref_node_and_direction`.
- 2.4.2 - The CLI guide lists every command. behavior: "### Workspaces"
  in `docs/guides/cli-commands.md`.
- 2.4.3 - Every visible workspaces, panes, and nodes leaf maps to the
  workspaces reference and an audit record. test:
  `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.

## P3: Pane identity and session binding
`kind: framing`

**Goal:** every native terminal carries its identity in its environment,
nothing else inherits it, and a CLI session started in a pane binds to
that terminal row.

### 3.1 Export the terminal and pane identity into every native spawn [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/constants.py::*` — scope-reason: the identity variable names, IDENTITY_ENV_VARS, ALL_TERMINAL_ENV_VARS, and get_terminal_env_vars
- `src/gobby/terminals/native_runtime.py::*` — scope-reason: prepare_spawn sets GOBBY_TERMINAL_ID after the caller env
- `src/gobby/runner.py::*` — scope-reason: main pops the identity names from the daemon's own environment before any subprocess starts
- `tests/terminals/test_native_runtime.py::*` — scope-reason: FakeHostClient asserts the spawn env
- `tests/test_runner_env_scrub.py`

`GOBBY_TERMINAL_ID` is set for every native spawn in
`NativeTerminalRuntime.prepare_spawn` after the caller's env
(`env=dict(request.env or {})`) so nothing shadows it. The pane variables
`GOBBY_NODE_ID`, `GOBBY_NODE_REF`, `GOBBY_WORKSPACE_ID`, `GOBBY_TAB_ID`,
`GOBBY_PANE_ID`, `GOBBY_PANE_REF` (`n1:w1:t2:p3`) are named in
`src/gobby/agents/constants.py` beside `GOBBY_SESSION_ID`, grouped with
`GOBBY_TERMINAL_ID` as `IDENTITY_ENV_VARS`, listed in
`ALL_TERMINAL_ENV_VARS`, and set only by 2.1's pane paths; `GTERM_ENV=1`
stays the host marker. `GOBBY_PROJECT_ID` and `GOBBY_DAEMON_URL` stay
agent-only (`get_terminal_env_vars`) and never enter a pane shell, so
the scrub below has nothing to cover for them. Panes are native rows (Constraints), so the tmux
backend never carries them and `tmux_spawn_shell_and_env` is untouched.

Identity is scoped to the process it names. A daemon restarted from a pane
(`uv run gobby restart --wait` typed there, V1.7) inherits the pane's
identity, and every one of the daemon's `os.environ.copy()` sites (there
are 24: `make_spawn_env`, the tmux servers, the `gterm host` launch, the
maintenance children) would hand it to every later agent terminal, web
shell, and tmux server, and 4.3's nested check would refuse. Scrubbing one
env builder is not enough, so the fix is at the single root: `main` in
`src/gobby/runner.py` pops every `IDENTITY_ENV_VARS` name from
`os.environ` before the runner is constructed, so no child of the daemon,
including a `gterm host` it starts, can inherit a pane identity, and the
host needs no scrub of its own. The consumer side is guarded too: 3.2's
`bind_session` accepts only sessions whose `session_type` is `terminal`,
so a stray variable in a `web_chat` session's context binds nothing.

**Research context:**

- `NativeTerminalRuntime.prepare_spawn` (`src/gobby/terminals/native_runtime.py`)
  copies `request.env` and hands it to the host's spawn; the host
  (`crates/gterminal/src/host/spawn.rs`) applies the env over its own
  inherited environment, removes `CODEX_THREAD_ID` and `GTERM_TEST_HELPER`,
  and adds `GTERM_ENV=1`.
- `make_spawn_env` (`src/gobby/agents/spawners/base.py`) copies
  `os.environ` and scrubs `TMUX` and `TMUX_PANE`, and it is one of 24
  `os.environ.copy()` sites in `src/gobby/`; every spawner, the
  daemon-started tmux servers, and the host launch inherit from the
  daemon's environment, which `main` in `src/gobby/runner.py` owns.
- Rejected: scrubbing in `make_spawn_env` and in the host at start; the
  other 23 copy sites would still leak, and the host only inherits what
  the daemon carries.
- `get_terminal_env_vars` (`src/gobby/agents/constants.py`) already builds
  `GOBBY_DAEMON_URL` and `GOBBY_PROJECT_ID` for agents.
- `tests/terminals/test_native_runtime.py` has `FakeHostClient` recording
  every spawn request.
- Rejected: a `GOBBY_ENV=1` marker beside `GTERM_ENV=1`; the terminal id
  is the marker.
- Verification planned: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run
  pytest tests/terminals/test_native_runtime.py
  tests/test_runner_env_scrub.py`.

**Acceptance:**

- 3.1.1 - Every native spawn env carries `GOBBY_TERMINAL_ID` equal to the
  minted id, and a caller env cannot override it. test:
  `tests/terminals/test_native_runtime.py::test_spawn_env_carries_terminal_id`.
- 3.1.2 - The identity names are registered and listed in
  `IDENTITY_ENV_VARS` and `ALL_TERMINAL_ENV_VARS`. symbol:
  `get_terminal_env_vars`. file: `src/gobby/agents/constants.py`.
- 3.1.3 - After `main` starts, the daemon's own environment carries no
  identity name whatever its parent exported, so every spawn root
  (`make_spawn_env`, the host launch, the tmux servers) inherits a clean
  environment. test:
  `tests/test_runner_env_scrub.py::test_main_pops_inherited_identity_before_the_runner_starts`.

### 3.2 Bind CLI sessions to their native terminal at session start [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/ghook/src/terminal_context.rs::*` — scope-reason: build_context emits the terminal id and pane ref and its key test moves
- `src/gobby/mcp_proxy/terminal_context.py::*` — scope-reason: the allowlist and reader gain the two variables
- `src/gobby/servers/routes/mcp/endpoints/request_context.py::*` — scope-reason: the strict terminal-context key subset moves with the allowlist
- `src/gobby/storage/sessions/_discovery_helpers.py::*` — scope-reason: the context filter fields gain the terminal id
- `src/gobby/storage/terminals.py::*` — scope-reason: TerminalManager gains bind_session and release_session
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::*` — scope-reason: the native bind branch beside seed_external_terminal
- `src/gobby/hooks/event_handlers/_session_end.py::*` — scope-reason: session end releases a gobby-owned binding instead of marking it exited
- `src/gobby/sessions/liveness_monitor.py::*` — scope-reason: liveness expiry releases the binding the same way
- `src/gobby/servers/routes/attention.py::*` — scope-reason: the roster admits any session with a live terminals row
- `docs/guides/ghook-development-guide.md`
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: bind, rebind, agent-row refusal, and nested-skip cases
- `tests/mcp_proxy/test_mcp_proxy_terminal_context.py::*` — scope-reason: the allowlist test moves
- `tests/storage/test_terminal_bindings.py`
- `tests/servers/test_attention_native_roster.py`

ghook `build_context` emits `gobby_terminal_id` and `gobby_pane_ref` from
the environment and `capture_emits_expected_keys` moves; Python
`current_terminal_context` reads the same two variables, and the
allowlists in `terminal_context.py`, `request_context.py` (an unknown key
is HTTP 409 today), and `_discovery_helpers.py` move together; the docs
table in `docs/guides/ghook-development-guide.md` records the union, which
is already out of sync between ghook and Python.

`TerminalManager.bind_session(terminal_id, session_id, project_id)` is a
guarded `UPDATE` beside `get_live_for_session`: it refuses rows with
`agent_run_id` (agent terminals are bound at spawn), refuses a session
whose `session_type` is not `terminal` (3.1's consumer guard), refuses a
row whose `project_id` is not the session's project (the tmux seed raises
`ProjectOwnershipConflictError` for the same case; the native branch logs
and skips, so a `cd` into another project's checkout inside a pane starts
an unbound session rather than a mis-bound one), and rebinds only when the
previous session is no longer active or paused, or when the previous
session's stored terminal-context `parent_pid` is no longer alive on this
node (the check the liveness monitor makes for its own records), so a CLI
whose end hook never ran does not pin the pane. `activate_materialized_session`
calls it from a native branch next to the tmux-only `seed_external_terminal`
when the context carries `gobby_terminal_id` and no `tmux_pane`: tmux
identity wins, so a tmux the user starts by hand inside a pane (Decision 2)
seeds its tmux row and never binds the outer pane, and a session ending on
that socket never has an outer binding to release. The native nested guard
is the rebind rule itself: `session_start_is_nested_cli_child` inspects
tmux pane commands only, and a CLI child started while the pane's bound
session is still active is refused by "previous session still active or
paused", which is the same outcome. `_load_roster_entries` in
`attention.py` admits any session with a live terminals row, so a `claude`
started in a pane shows on the roster with backend `native` and accepts
`gobby-sessions:send_keys`. Session end and liveness expiry release the
binding on a gobby-owned row with no agent run instead of marking it
exited, so quitting `claude` keeps the pane and the next start rebinds.

**Research context:**

- Only agent spawns pre-bind a session today (`GOBBY_SESSION_ID` in
  `handle_session_start`, `src/gobby/hooks/event_handlers/_session_start/flow.py`).
- `seed_external_terminal` (`src/gobby/terminals/discovery.py`) is
  tmux-only and is called from `activate_materialized_session`.
- Roster filtering lives in `_load_roster_entries`
  (`src/gobby/servers/routes/attention.py`); memory 5c7244d8 records the
  earlier decision that a live terminals row is the admission criterion.
- Session end marks the terminal exited in `SessionEndMixin.handle_session_end`
  and in `SessionLivenessMonitor._expire_session`.
- `session_start_is_nested_cli_child`
  (`src/gobby/hooks/event_handlers/_session_start/terminal_runtime.py`)
  reads `_tmux_pane_current_command` and returns false without a tmux
  context, so it cannot guard native nesting.
- Rejected: seeding a terminals row from `TMUX_PANE` for native panes; the
  row exists at spawn and only the binding is missing.
- Rejected: binding the outer pane when a tmux pane is also present; the
  session lives in its innermost terminal and two live rows for one session
  would make `get_live_for_session` pick by recency.
- Verification planned: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run
  pytest tests/hooks/test_session_start_handlers.py
  tests/mcp_proxy/test_mcp_proxy_terminal_context.py
  tests/storage/test_terminal_bindings.py
  tests/servers/test_attention_native_roster.py`, `cargo nextest run -p
  gobby-hooks`.

**Granularity:** bind, release, the roster admission, and the context
keys change together because the session-start hook path is one flow:
a key the hook cannot read binds nothing, and a binding nothing releases
pins the pane.

**Acceptance:**

- 3.2.1 - ghook and Python emit and accept `gobby_terminal_id` and
  `gobby_pane_ref`, and the documented key table matches both. test:
  `crates/ghook/src/terminal_context.rs::capture_emits_expected_keys`.
- 3.2.2 - `bind_session` binds a gobby-owned row, refuses an agent row, a
  project mismatch, and a non-terminal session type, and rebinds after the
  previous session ends or when its stored parent pid is dead. test:
  `tests/storage/test_terminal_bindings.py::test_bind_session_guards`.
- 3.2.3 - A session starting with `GOBBY_TERMINAL_ID` binds at
  materialization; a nested CLI child started while the bound session is
  active does not; a context that also carries `tmux_pane` seeds the tmux
  row and leaves the native row unbound. test:
  `tests/hooks/test_session_start_handlers.py::test_native_terminal_id_binds_unless_tmux_or_nested`.
- 3.2.4 - The attention roster lists a bound native session with its
  terminal, and session end releases the binding without exiting the
  terminal. test:
  `tests/servers/test_attention_native_roster.py::test_bound_native_session_is_on_the_roster`.

## P4: gclient on daemon-owned workspaces
`kind: framing`

**Goal:** gclient attaches a workspace, renders the daemon's layout,
mutates only through the daemon, and several windows share one workspace
with their own focus. This phase is the coordinator's own UI leaf set.

### 4.1 Add the workspace messages to the client daemon layer [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/daemon/workspace.rs`
- `crates/gclient/src/daemon/live_workspace.rs`
- `crates/gclient/src/daemon/mod.rs::*` — scope-reason: DaemonEvent::Workspace, the two Daemon trait methods, and the ScriptedDaemon arms
- `crates/gclient/src/daemon/live.rs::*` — scope-reason: SUBSCRIBED_EVENTS, attach and snapshot requests on LiveDaemon
- `crates/gclient/src/daemon/live_reader.rs::*` — scope-reason: the typed workspace_event lands as DaemonEvent::Workspace
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: ReconnectDaemon implements the two new trait methods
- `crates/gclient/tests/teardown.rs::*` — scope-reason: TraceDaemon implements the two new trait methods
- `crates/gclient/tests/daemon_live.rs::*` — scope-reason: attach, op, and event round trips over the live daemon

`crates/gclient/src/daemon/workspace.rs` holds the row types
(`WorkspaceRow`, `TabRow`, `PaneRow`), its own `LayoutNode` for the daemon
layout tree (`persist.rs` keeps its file-shaped `LayoutNode` untouched until
4.3 deletes it), the `WorkspaceOp` and `WorkspaceEvent` enums, and their
serde shapes matching the golden fixtures. The `Daemon` trait gains
`attach_workspace(node, workspace) -> Snapshot` and `workspace_op(op) ->
Reply`; every implementor moves with it: `LiveDaemon` (`daemon/live.rs`),
`ScriptedDaemon` (`daemon/mod.rs`), `ReconnectDaemon`
(`tests/client_loop.rs`), and `TraceDaemon` (`tests/teardown.rs`). `DaemonEvent::Workspace(WorkspaceEvent)`
is the typed variant that `live_reader.rs::daemon_event` produces from
`workspace_event` (today it lands as `DaemonEvent::Message`).
`SUBSCRIBED_EVENTS` does not gain `workspace_event`: `_is_subscribed` in
`src/gobby/servers/websocket/broadcast.py` matches a bare type before the
parametric filter, so a bare entry would deliver every workspace's events
to every window. The parametric `workspace_event:workspace_id=<id>`
subscription that `workspace_attach` registers (2.2) is the only
subscription, re-registered by the reconnect re-attach (4.2.6), and
`live_reader.rs` drops a `workspace_event` whose `workspace_id` is not
the attached workspace. `ScriptedDaemon` gains a `set_workspace(...)`
seed. Split: `crates/gclient/src/daemon/mod.rs` (936
lines) moves the scripted workspace state and arms into
`crates/gclient/src/daemon/workspace.rs`, and `crates/gclient/src/daemon/live.rs`
(912 lines) moves the workspace requests into
`crates/gclient/src/daemon/live_workspace.rs`, keeping both under the
1000-line guard. Nothing in the app layer consumes the new types yet, so
the leaf builds on its own.

**Research context:**

- `route_key` (`crates/gclient/src/daemon/ws.rs`) correlates any
  `request_id`, so no routing change is needed; `daemon_event`
  (`live_reader.rs`) is where unknown event types become
  `DaemonEvent::Message`.
- `impl Daemon for` exists in `daemon/live.rs`, `daemon/mod.rs`,
  `tests/client_loop.rs` (`ReconnectDaemon`), and `tests/teardown.rs`
  (`TraceDaemon`); a new trait method without a default breaks the two
  test doubles.
- `GOLDEN_NAMES` and the corpus assert are already updated by 2.2.
- Verification planned: `cargo nextest run -p gobby-client --test
  daemon_live --test ws_golden --test client_loop --test teardown`, `cargo
  clippy -p gobby-client --all-targets -- -D warnings`, `cargo nextest run
  -p gobby-client --test source_size`.

**Acceptance:**

- 4.1.1 - The client decodes every workspace fixture into typed rows,
  ops, and events. symbol: `WorkspaceEvent`. file:
  `crates/gclient/src/daemon/workspace.rs`.
- 4.1.2 - `LiveDaemon` attaches a workspace, sends an op, and receives the
  typed event after reconnect, while a second workspace's event on the
  same daemon is neither delivered (no bare subscription) nor surfaced by
  the reader. test:
  `crates/gclient/tests/daemon_live.rs::workspace_attach_op_and_event_round_trip`.
- 4.1.3 - `crates/gclient/src/daemon/mod.rs` and `crates/gclient/src/daemon/live.rs` stay under the guard after
  the split. test: `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines`.

### 4.2 Render the daemon layout with viewer-local state and route every mutation through it [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gterminal/src/layout.rs::*` — scope-reason: TileLayout drops its focus field and the *_focused family takes the focus explicitly
- `crates/gterminal/src/layout/tests.rs::*` — scope-reason: the layout unit tests pass focus explicitly
- `crates/gterminal/src/pane/runtime.rs::*` — scope-reason: the runtime's focus reads take the explicit focus
- `crates/gclient/src/app/viewer_state.rs`
- `crates/gclient/src/app/workspace_ops.rs`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: the viewer_state and workspace_ops modules, PaneId interning, reconcile order, snapshot pinning, and the workspace event gate
- `crates/gclient/src/app/live_workspace.rs`
- `crates/gclient/src/app/live.rs::*` — scope-reason: reconcile_subscribe_first, apply_live_event, and the reconnect paths call into live_workspace.rs for attach, apply, and reattach
- `crates/gclient/src/app/live_attach.rs::*` — scope-reason: attach paths read focus from the viewer state
- `crates/gclient/src/app/attach.rs::*` — scope-reason: attach paths read focus from the viewer state
- `crates/gclient/src/app/pane.rs::*` — scope-reason: pane lookups key on daemon pane ids through the interning map
- `crates/gclient/src/app/project_tabs.rs::*` — scope-reason: TabSet drops active_tab, tabs are keyed by daemon tab id, and load_node builds slots from the daemon layout without allocating ids
- `crates/gclient/src/app/run_loop.rs::*` — scope-reason: the loop reads active tab and focus from the viewer state
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: the new live_loop submodules are declared and the loop's focus and active-tab reads move
- `crates/gclient/src/app/live_loop/workspace_actions.rs`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: split, close, swap, spawn placement, and sync_live_chrome send ops instead of mutating
- `crates/gclient/src/app/live_loop/control.rs::*` — scope-reason: control reads focus from the viewer state
- `crates/gclient/src/app/live_loop/menu_items.rs`
- `crates/gclient/src/app/live_loop/menu.rs::*` — scope-reason: swap_with_focused and the pane and tab menu items send ops
- `crates/gclient/src/app/live_loop/modal_input.rs::*` — scope-reason: tab and pane renames send ops
- `crates/gclient/src/app/live_loop/mouse/mod.rs::*` — scope-reason: hit routing reads focus and active tab from the viewer state
- `crates/gclient/src/app/live_loop/mouse/forward.rs::*` — scope-reason: forwarding resolves the focused pane through the viewer state
- `crates/gclient/src/app/live_loop/mouse/links.rs::*` — scope-reason: link hits resolve the pane through the interning map
- `crates/gclient/src/app/live_loop/mouse/pointer.rs::*` — scope-reason: move_tab sends an op, the ratio drag sends on drop, and tab clicks write the viewer state
- `crates/gclient/src/app/live_loop/mouse/select.rs::*` — scope-reason: selection resolves the pane through the interning map
- `crates/gclient/src/app/live_loop/mouse/wheel.rs::*` — scope-reason: tab switching writes the viewer state
- `crates/gclient/src/app/live_loop/projects.rs::*` — scope-reason: activate_live_tab and tab.worktree_id writes go through the viewer state and ops
- `crates/gclient/src/copy_mode.rs::*` — scope-reason: copy mode resolves the focused pane explicitly
- `crates/gclient/src/ui/chrome.rs::*` — scope-reason: Tab loses zoomed, tabs are keyed by daemon tab id, and focused-pane accessors take the viewer state
- `crates/gclient/src/ui/chrome_render.rs::*` — scope-reason: rendering reads zoom and focus from the viewer state
- `crates/gclient/src/ui/pane_layout.rs::*` — scope-reason: pane_geometry reads zoom from the viewer state
- `crates/gclient/src/ui/navigator.rs::*` — scope-reason: navigation reads focus and active tab from the viewer state
- `crates/gclient/src/ui/panes.rs::*` — scope-reason: pane chrome reads focus from the viewer state
- `crates/gclient/src/ui/tabs.rs::*` — scope-reason: the tab strip reads the active tab from the viewer state
- `crates/gclient/src/ui/status.rs::*` — scope-reason: the status line shows the pane ref
- `crates/gclient/src/ui/sidebar/sessions.rs::*` — scope-reason: session rows resolve their pane through the interning map
- `crates/gclient/src/ui/hit/tests.rs::*` — scope-reason: hit tests seed the viewer state instead of TabSet.active_tab
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: pin_tabs seeds the mock workspace row and every request count moves
- `crates/gclient/tests/workspace.rs::*` — scope-reason: two viewers on one workspace, stable pane ids, and op and event round trips through the loop
- `crates/gclient/tests/parity/tabs.rs::*` — scope-reason: compile-time consumer of TabSet.active_tab and focused_pane
- `crates/gclient/tests/parity/panes.rs::*` — scope-reason: compile-time consumer of split_focused and focused_pane
- `crates/gclient/tests/parity/chrome.rs::*` — scope-reason: compile-time consumer of Chrome::focused_pane and active_tab
- `crates/gclient/tests/parity/dialogs.rs::*` — scope-reason: compile-time consumer of the focused-pane accessors
- `crates/gclient/tests/parity/sidebar.rs::*` — scope-reason: compile-time consumer of TabSet.active_tab
- `crates/gclient/tests/attention_flow.rs::*` — scope-reason: compile-time consumer of focused_pane and open_tab
- `crates/gclient/tests/copy_paste.rs::*` — scope-reason: compile-time consumer of the focused-pane accessors
- `crates/gclient/tests/screens.rs::*` — scope-reason: compile-time consumer of split_focused and active_tab
- `crates/gclient/tests/terminal_links.rs::*` — scope-reason: compile-time consumer of the focused-pane accessors
- `crates/gclient/tests/ui_carve_guard.rs::*` — scope-reason: compile-time consumer of the chrome accessors it guards
- `crates/gclient/tests/persist.rs::*` — scope-reason: reads tabs.tabs[1].focused_pane() and must compile until 4.3 deletes it

One leaf, because the focus split and the mutation routing share the same
callers: the reviewer's consumer walk showed that `TileLayout.focus`,
`Tab.zoomed`, `TabSet.active_tab`, and `PaneId::alloc` are read or written
from the action, menu, modal, pointer, and wheel handlers that also own the
twelve mutation sites, so a viewer-state leaf could not build without
touching them, and a half-routed client would carry two layout
authorities. Every file above is a compile-time consumer of a changed
signature or deleted field, including the integration tests under
`crates/gclient/tests/` that read `active_tab`, `focused_pane()`, or
`split_focused(`; the Targets list is the closure, so the leaf builds and
its whole test suite compiles on its own.

`crates/gclient/src/app/viewer_state.rs` holds the per-window `ViewerState
{ focus: BTreeMap<TabId, PaneId>, zoomed: BTreeSet<TabId>, active_tab:
BTreeMap<ProjectId, TabId>, panes: PaneInterner }` keyed by daemon ids.
`PaneInterner` is a per-process map from daemon pane uuid to the app
`PaneId`, minted with `PaneId::alloc()` (the one `NEXT_PANE_ID` counter
in `layout.rs`, never `from_raw`, so an interned id can never collide
with one the layout allocated; the two windows are separate processes and
never need to agree on the u32).
`TileLayout` (`crates/gterminal/src/layout.rs`) loses its `focus` field:
`focused`, `split_focused`, `close_focused`, `resize_focused`, and the
`*_focused` family take the focus explicitly. `Tab` loses `zoomed`;
`Tab::focused_pane` and `Chrome::focused_pane`/`active_tab` read the viewer
state; `pane_geometry` takes zoom from it. `TabSet` loses `active_tab`, is
keyed by daemon tab id, and `load_node` builds slots from the daemon layout
through the interner. Parity and screen captures stay byte-identical.

The mutation surface is about twelve call sites: `Chrome::open_split`,
`open_tab`, `close_focused`; `swap_panes` (`actions.rs`, `menu.rs`);
`set_ratio_at` (`pointer.rs`, fires per mouse-move, applied locally and
sent on drop); `move_tab` (`pointer.rs`); the tab title (`modal_input.rs`);
`tab.worktree_id` (`projects.rs`); `close_slot`, `spawn_live_shell`
placement, and `open_agent_in_new_tab` (`tab.create` with `terminal_id`).
Each sends a `workspace_op` with explicit pane and tab ids through
`crates/gclient/src/app/workspace_ops.rs`, and the client applies the
resulting `workspace_event` (daemon-authoritative, no optimistic apply
except the ratio drag). `sync_live_chrome` stops deleting slots it cannot
resolve and renders them empty; `open_live_terminal` is called on demand
when a `workspace_event` names a terminal the roster has not delivered yet.
Reconcile order: `workspace_attach` inside `reconcile_subscribe_first`
right after `daemon.subscribe()`, the snapshot after `fetch_roster` (panes
resolve through `pane_for_terminal`), its `{epoch, seq}` pinned beside
`lifecycle`, `DaemonEvent::Workspace` gated in `apply_live_event`, and
re-attach plus re-fetch on reconnect and in `drain_receiver`'s relist
branch. Until 4.3, the attached workspace's layout replaces the snapshot
file's layout at reconcile and the file writers keep compiling as dead
paths. Focus changes send `workspace.set_focus_hints` so the row remembers
the last actor for the next window. The status line shows the pane ref.
Splits: `app/mod.rs` (970 lines) moves the workspace reconcile state into
`app/workspace_ops.rs`; `app/live.rs` (799) moves the attach, apply, and
reattach logic into `app/live_workspace.rs` and only wires it; `actions.rs`
(867) moves the op senders into `live_loop/workspace_actions.rs`; `menu.rs`
(903) moves the item builders into `live_loop/menu_items.rs`;
`live_loop.rs` declares the new modules.

**Research context:**

- `TileLayout.focus` is read by `Tab::focused_pane` and every `*_focused`
  method; `Tab.zoomed` is read by `pane_geometry`; `TabSet.active_tab` is
  written from `Chrome::open_split/open_tab/close_focused`, the pointer
  and wheel handlers, and `activate_live_tab`; `PaneId` is consumed across
  `app/attach.rs`, `live.rs`, `live_attach.rs`, `live_loop.rs`, the
  `live_loop/*` handlers, `pane.rs`, `project_tabs.rs`, `run_loop.rs`,
  `ui/chrome.rs`, and `ui/chrome_render.rs`; `active_tab` across
  `live_loop.rs`, `actions.rs`, `menu.rs`, `modal_input.rs`, the mouse
  handlers, `projects.rs`, `project_tabs.rs`, `run_loop.rs`, `persist.rs`,
  `ui/chrome.rs`, `ui/hit/tests.rs`, `ui/panes.rs`, and `ui/tabs.rs`.
- `layout::PaneId::alloc` is a process-global `AtomicU32` and `load_node`
  reallocates on every load.
- `reconcile_subscribe_first`, `drain_receiver`, and `apply_live_event`
  in `crates/gclient/src/app/live.rs` own the ordering; `lifecycle`
  pinning is `accept_lifecycle`/`advance_lifecycle`.
- `sync_live_chrome` (`actions.rs`) only reaps slots today and never opens
  panes (memory 7cb7290f); the `pin_tabs` helper in
  `crates/gclient/tests/client_loop.rs` pins a snapshot before the loop and
  becomes "seed the mock's workspace row"; `live_workspace_with_scripted_direct`
  request counts change for every caller.
- Rejected: keeping shared focus in `TileLayout` and overriding per
  window; every mutation path would have to reconcile two focus sources.
- Rejected: a u32 hash of the pane uuid for `PaneId`; it can collide and
  cross-process agreement is not needed.
- Rejected: optimistic local apply for splits and closes; a divergent
  viewer mid-attach would render a layout the row never had.
- Verification planned: `cargo nextest run -p gobby-terminal --features
  vt-engine --lib layout`, `cargo nextest run -p gobby-client` (full
  crate), `cargo clippy -p gobby-client --all-targets -- -D warnings`, the
  parity and screen captures unchanged.

**Acceptance:**

- 4.2.1 - `TileLayout` has no focus field and every focus-dependent method
  takes the focus explicitly. symbol: `TileLayout::split_focused`. file:
  `crates/gterminal/src/layout.rs`.
- 4.2.2 - Two viewers on one workspace keep separate focus, zoom, and
  active tab while sharing tabs and panes. test:
  `crates/gclient/tests/workspace.rs::two_viewers_share_layout_and_keep_their_own_focus`.
- 4.2.3 - Pane ids are interned per process from daemon pane ids, so a
  reloaded layout keeps every slot and two distinct daemon panes never
  share a `PaneId`. test:
  `crates/gclient/tests/workspace.rs::pane_ids_are_interned_without_collision`.
- 4.2.4 - Every layout mutation sends a `workspace_op` with explicit ids
  and the loop applies the daemon's `workspace_event`. test:
  `crates/gclient/tests/client_loop.rs::wired_actions_split_focus_swap_and_switch_tabs`.
- 4.2.5 - A `workspace_event` naming an undelivered terminal opens it on
  demand and an unresolved slot renders empty instead of being reaped.
  test: `crates/gclient/tests/client_loop.rs::workspace_events_open_terminals_on_demand`.
- 4.2.6 - Reconnect re-attaches the workspace and re-fetches the snapshot
  under the pinned watermark. test:
  `crates/gclient/tests/client_loop.rs::daemon_restart_keeps_panes_and_reattaches`.
- 4.2.7 - The ratio drag applies locally and sends one op on drop. test:
  `crates/gclient/tests/client_loop.rs::ratio_drag_sends_one_op_on_drop`.
- 4.2.8 - `app/mod.rs`, `app/live.rs`, `actions.rs`, `menu.rs`, and
  `live_loop.rs` stay under the guard after the splits. test:
  `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines`.

### 4.3 Attach by workspace at startup and retire the client snapshot files [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/persist.rs::*` — operation: delete — scope-reason: retire the client snapshot and session files
- `crates/gclient/tests/persist.rs::*` — operation: delete — scope-reason: the file contract tests go with the file
- `crates/gclient/src/lib.rs::*` — scope-reason: the persist module declaration goes
- `crates/gclient/src/app/mod.rs::*` — scope-reason: the snapshot pin and persist imports go
- `crates/gclient/src/app/project_tabs.rs::*` — scope-reason: load_node stops taking the file LayoutNode
- `crates/gclient/src/app/persistence.rs::*` — scope-reason: snapshot conversions become row conversions
- `crates/gclient/src/app/live_loop/projects.rs::*` — scope-reason: restore_focused seeds from the workspace row and the file writers go away
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: the persist_if_changed and save_client_session imports and calls go
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: the save_client_session calls go
- `crates/gclient/tests/reconciliation.rs::*` — scope-reason: persist_workspace goes with the file
- `crates/gclient/src/prefs.rs::*` — scope-reason: sidebar collapse, project order, project labels, and agent sort move into ClientPrefs
- `crates/gclient/src/startup.rs::*` — scope-reason: the --node and --workspace flags, the nested pane check, and initial_project take the attached workspace
- `crates/gclient/src/views/mod.rs::*` — scope-reason: run_ready attaches the workspace before the loop and stops reading the session file
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: the snapshot-file helpers go and the loop seeds rows
- `crates/gclient/tests/teardown.rs::*` — scope-reason: teardown stops asserting on snapshot files
- `crates/gclient/tests/parity/sidebar.rs::*` — scope-reason: the sidebar parity fixture seeds prefs and rows instead of a session file
- `crates/gclient/tests/startup.rs::*` — scope-reason: flag parsing, usage text, the initial project order, and the persist-free assertions move
- `crates/gclient/tests/workspace_rows.rs`
- `docs/reference-audit/admin.json::*` — scope-reason: the mirrored gclient usage line

`gclient [--node <ref|id>] [--workspace <ref|name>]` attaches the local
node's `default` when both are omitted; `--workspace n2:w1` carries the
node. The per-project snapshot files, the client session file,
`restore_project`'s file read, `persist_if_changed`, and
`save_client_session` go away (rule 10), and every importer of
`persist` (`lib.rs`, `app/mod.rs`, `app/persistence.rs`, `project_tabs.rs`,
`live_loop.rs`, `live_loop/actions.rs`, `live_loop/projects.rs`,
`startup.rs`, `views/mod.rs`, and the `client_loop`, `teardown`,
`parity/sidebar`, `reconciliation`, and `startup` tests) moves in the same
leaf so the crate builds without the module. A window seeds its
focus from the workspace's `focused_tab_id` and each tab's
`focused_pane_id` and updates those hints through
`workspace.set_focus_hints` as the last actor (already sent by 4.2).
Sidebar width and collapse, project order, project labels, and agent sort
move from the session file into `ClientPrefs` beside `sidebar_width`.
`initial_project` takes the attached workspace's `focused_project_id`
between `--project` and the personal fallback; `tests/startup.rs` asserts
the literal usage text and moves. A gclient started inside a pane reads
`GOBBY_PANE_ID`, shifts the prefix as the nested-tmux path does, and
refuses to open its own terminal. The usage line mirrored at
`docs/reference-audit/admin.json` moves.

**Research context:**

- `persist.rs` defines `WorkspaceSnapshot`, `ClientSession`,
  `save_snapshot`, `load_session`, and the quarantine path; `prefs.rs`
  holds `ClientPrefs`/`UiPrefs` with `sidebar_width` already there (memory
  468a4164 records the prefs contract; 7cb7290f the snapshot contract).
- `persist` is imported from `lib.rs`, `app/mod.rs`, `app/persistence.rs`,
  `app/project_tabs.rs`, `app/live_loop.rs` (`persist_if_changed`,
  `save_client_session`), `app/live_loop/actions.rs`
  (`save_client_session`), `app/live_loop/projects.rs`, `startup.rs`,
  `views/mod.rs`, `tests/client_loop.rs`, `tests/teardown.rs`,
  `tests/parity/sidebar.rs`, `tests/reconciliation.rs`
  (`persist_workspace`), `tests/startup.rs`, and `tests/persist.rs`.
- `startup::initial_project` picks resolved project, then the session
  file's `focused_project`, then the personal project (memory 430a050f).
- `restore_focused` and `persist_if_changed` in
  `crates/gclient/src/app/live_loop/projects.rs` are the readers and
  writers; `run_ready` (`views/mod.rs`) calls `initial_project`.
- Rejected: keeping `session.json` for prefs-like fields; `prefs.toml`
  already owns user preferences and the file would have no other content.
- Verification planned: `cargo nextest run -p gobby-client --test startup
  --test workspace_rows --test client_loop --test teardown --test parity`,
  `cargo clippy -p gobby-client --all-targets -- -D warnings`.

**Acceptance:**

- 4.3.1 - `--node` and `--workspace` parse, default to the local `default`
  workspace, and a full ref overrides `--node`. test:
  `crates/gclient/tests/startup.rs::workspace_flags_default_to_the_local_default`.
- 4.3.2 - No client code writes or reads the snapshot or session files
  under the client directory; sidebar and project preferences round-trip through
  `prefs.toml`. test:
  `crates/gclient/tests/startup.rs::prefs_carry_sidebar_and_project_preferences`.
- 4.3.3 - A window seeds focus from the row hints and writes hints back
  when focus changes. test:
  `crates/gclient/tests/workspace_rows.rs::focus_hints_seed_and_follow_the_last_actor`.
- 4.3.4 - A gclient started inside a pane shifts its prefix and opens no
  terminal of its own. test:
  `crates/gclient/tests/startup.rs::nested_pane_launch_shifts_prefix_and_opens_nothing`.

**Granularity:** P4 carries three deliverables: the daemon seam (4.1)
compiles with no app consumer; the viewer-local state and the mutation
routing (4.2) are one leaf because they share the same handler files and a
partially routed client would carry two layout authorities; startup and
file retirement (4.3) come last because the layout must already load from
the daemon before the files can go.

## P5: Ghostty, docs, and acceptance
`kind: framing`

**Goal:** the documentation describes the daemon-owned model, the Ghostty
config returns to plain zsh, and the whole path is proven live. Task
#22202 is re-parented under this phase as deferred section D1 and owns
only the Ghostty config edit and its acceptance (V1 step 8); the
orphaned-terminals doc note belongs to 5.1.

### 5.1 Document workspaces across the client, protocol, CLI, MCP, and ghook guides [category: docs] (depends: 2.4, 3.2, 4.3)
`kind: deliverable`

Targets:
- `docs/guides/gclient-user-guide.md`
- `docs/contracts/gterm-protocols.md`
- `docs/guides/cli-commands.md`
- `docs/guides/mcp-tools.md`
- `docs/guides/ghook-development-guide.md`
- `crates/gclient/src/app/live_loop/orphans.rs::*` — scope-reason: the module doc drops the Ghostty destroy-unattached note

The gclient user guide gains the usage line and flag table for `--node`
and `--workspace`, a rewritten Layout section, a "Workspaces" section
(named workspaces, refs, several windows, focus hints), a rewritten
"Workspace persistence" section describing daemon rows instead of files,
a workspace re-attach step under "Daemon restarts and reconnects", a
"Bringing a bare terminal in" section (tmux by hand, or resume the
provider session in a pane), and pane labels described as row state
("renames apply locally" goes). The Orphaned terminals section drops the
Ghostty bullet and the `destroy-unattached off` advice, as does the
`orphans.rs` module doc. The protocol contract, CLI, MCP, and ghook guides
receive their final cross-links to the sections 2.1, 2.4, 2.3, and 3.2
added.

**Research context:**

- Guide sections today: Launching, Layout, Tabs and panes, Mouse,
  Workspace persistence (the file table), Daemon restarts and reconnects,
  Orphaned terminals. Memory 392cc53f maps guide sections to their source
  files; this deliverable updates that map's persistence row.
- Verification planned: `uv run gobby docs audit` if present, else a
  markdown link check over the touched guides, plus a read-through against
  the shipped binaries.

**Acceptance:**

- 5.1.1 - The gclient guide documents workspaces, refs, multi-window
  behavior, daemon-owned persistence, and bringing a bare terminal in.
  behavior: "## Workspaces" in `docs/guides/gclient-user-guide.md`.
- 5.1.2 - No shipped doc or module comment still advises Ghostty tabs to
  run tmux or to set `destroy-unattached`. behavior: "Orphaned terminals"
  in `docs/guides/gclient-user-guide.md`.

### D1 Retire the tmux wrapper from the Ghostty config (depends: 5.1)
`kind: deferred`

Task #22202 predates this plan and asked for the Ghostty tmux wrapper to
go once native tabs were daemon-visible. Its original criteria assume a
single-pane attach client that Decision 1 dropped, so the work left is
the config edit itself plus V1 step 8: remove the tmux wrapper line from
`~/Library/Application Support/com.mitchellh.ghostty/config`
(`~/.tmux.conf` untouched; Ghostty picks it up on reload or next launch),
then confirm a fresh Ghostty tab is plain zsh with no terminals row. The
docs side of that promise is 5.1.2, which is why D1 waits for 5.1. At
expansion the task is re-parented under this epic, labelled
`deferred-from:gclient-workspaces:D1`, blocked by 5.1, and its criteria
are rewritten to V1 step 8.

```yaml
deferral:
  task_ref: "#22202"
  reason: "Its criteria assume a native single-pane attach client that Decision 1 dropped; the remaining work is the Ghostty config edit plus V1 step 8, and it must wait for the docs leaf so the guides stop advising the wrapper first."
  owner: "coordinator gobby#12967"
  original_acceptance_items:
    - 5.1.2
```

## V1: Live acceptance
`kind: verification`

Run on this machine after each phase's cutover, in order, before P5
closes and before #22202 edits the Ghostty config:

1. `gclient` from `$HOME` opens `default` (`n1:w1`); `gobby workspaces
   list` shows it with the same ref.
2. A second `gclient` attaches the same workspace and shows the same tabs;
   focus in one window does not move focus in the other.
3. `gobby panes split n1:w1:t1:p1 --right` appears in both windows; `echo
   $GOBBY_PANE_REF` in the new pane prints `n1:w1:t1:p2`.
4. `claude` started in that pane shows on the attention roster with
   backend `native` and accepts `gobby-sessions:send_keys`; quit `claude`,
   the pane stays live, start it again and the new session rebinds.
5. `gobby-workspaces:read_pane` from an interactive session in the same
   project returns the pane's screen; `wait_for_pane_output` resolves on a
   typed prompt; the same call from a spawned agent session is refused.
6. Close the Ghostty window, relaunch `gclient`, the workspace is back on
   the live terminals with their markers.
7. `uv run gobby restart --wait` typed inside a pane (announced with a
   global send, quiet window) keeps every pane; a pane whose terminal was
   killed during the restart is gone from the relaunched window, and an
   agent spawned after the restart carries no pane identity.
8. Remove the tmux wrapper line from the Ghostty config (#22202, D1); a fresh
   Ghostty tab is plain zsh with no terminals row, and the docs no longer
   claim otherwise.

Per phase: `uv run ruff format src/ tests/`, `uv run ruff check src/
tests/`, `uv run mypy src/`, the targeted pytest runs listed in each
section under `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`,
`uv run gobby test-types audit tests/ --baseline
.gobby/test-types-baseline.json --fail-on-new`, `cargo fmt --check`,
`cargo clippy --all-targets -- -D warnings`, and `cargo nextest run` for
gobby-core, gobby-daemon, gobby-hooks, gobby-terminal (`--features
vt-engine`), and gobby-client, the WS golden corpus on both sides, and the
1000-line guards.

Round 3 (adversary run 69da1908, Claude Opus xhigh): needs_review with five blocking findings and two nits. The coordinator (gobby#12967) verified each against source under unattended authority and accepted all seven. F1 accepted: the in-flight spawn guard moves onto the single WorkspaceManager (mark_spawn_in_flight / clear_spawn_in_flight, registered before the insert transaction, read by sweep_dead_panes under the parent-row lock) so every surface shares it and 1.2.4 tests it directly; HTTPServer is built at servers.py:285 before WebSocketServer at :345. F2 accepted: setup_internal_registries takes config and resolvers only, so 2.3 seeds a request-principal context var in _set_context_for_request, targets request_context.py, and depends on 3.2. F3 accepted: pane spawns resolve the native runtime explicitly; WorkspaceOps takes the runtime registry and resolves adopted rows by backend. F4 accepted: GOBBY_PROJECT_ID and GOBBY_DAEMON_URL leave the pane env (stdio_proxy.read_project_id reads the env before cwd). F5 accepted: workspace_event stays out of SUBSCRIBED_EVENTS because _is_subscribed matches a bare type before the parametric filter; the client ignores other workspaces' events. F6 accepted: tab.close and workspace.close raise busy while a child pane is in flight. F7 accepted: multi-parent ops lock parent rows in ascending id order. Repairs are prose edits applied by the coordinator after finalization; this was the last round under the cap of 3.

```json plan-review-round
{"evidence_id":"90796f96-9d90-4889-8c33-f39891dee797","plan_hash":"7fd819e218934954b88f7b75fe42c909078b26821404303b26e4067c340beb95","round_number":3,"round_result":{"coordinator_votes":{"F1-in-flight-set-owner":"accept","F2-principal-carrier":"accept","F3-native-pane-runtime":"accept","F4-pane-project-env":"accept","F5-bare-workspace-subscription":"accept","F6-close-in-flight-contradiction":"accept","F7-move-lock-order":"accept"},"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"6acc6f3213298d514c25761466fd41e8bcfa6a0211c409a4f792e5e0f6d01df1","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":7,"total":14},"evidence_id":"90796f96-9d90-4889-8c33-f39891dee797","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":12,"manifest_digest":"04586bf20ad524f4b8edf3a4d37fb159f0f61f8b68d09403af35953c4e512729","status":"valid"},"source_digest":"022c357f2500740652705c837bb062542162a1efeacd1b0fde0738c5f83fc986","version":1},"dismissed_candidates":{"RB-2":"verify_ws_token accepts only the local operator bearer, so operator actor on the socket is correct","RB-3":"machines.owner_user_id is NOT NULL, so the (owner_user_id, ref) unique index enforces","RB-4":"all Daemon implementors, persist importers, and TileLayout focus consumers are targeted","RB-5":"gclient orphan rule keys only on orphaned native and detached external tmux","RI-6":"orphaned transitions only to exited; host death reaps processes","RI-7":"ghook parent_pid is getppid of the hook, the CLI process, as liveness already relies on","RT-2":"reference-library inventory mocks setup_internal_registries parameters by signature; no test edit needed"},"evidence_id":"90796f96-9d90-4889-8c33-f39891dee797","findings":[{"category":"unhandled-edge","check_key":"runtime_invariants:in-flight-spawn-guard-ownership","description":"The guard that stops the sweep pruning a mid-spawn NULL pane is a process-local in-flight set that 2.1 keeps, but no deliverable constructs one shared WorkspaceOps. In src/gobby/runner_init/servers.py::init_servers, HTTPServer (and setup_internal_registries through _init_mcp_subsystems) is built at :285, before WebSocketServer at :345, which owns broadcast_workspace_event. Nothing targets servers.py/app_context.py in 2.1-2.3 to share one instance. If WS (2.2) and MCP (2.3) each build their own ops, a CLI or MCP op on the same workspace sweeps a gclient split's NULL pane, set_pane_terminal finds the row gone, and the fresh terminal is killed with not_found. The storage-level sweep_dead_panes(workspace_id) has no in-flight parameter, so 1.2.4 ('keeps a NULL pane whose split is in flight') cannot be tested in 1.2. Ordering race: the id joins the set after the insert commits, but DB calls run off-loop, so a sweep in another thread can see the committed NULL row before registration.","finding_id":"F1-in-flight-set-owner","fix":"Put the in-flight set on the single WorkspaceManager that 1.2.5 already shares (mark_spawn_in_flight / clear_spawn_in_flight). The split mints the pane uuid and registers it before the insert transaction; sweep_dead_panes reads the set after taking the parent-row FOR UPDATE lock. 1.2.4 then tests it directly. Alternatively state one WorkspaceOps built in the composition root with a late-bound publish resolving services.websocket_server, handed to configure_terminals and ServiceContainer, with servers.py/app_context.py targeted in 2.1 and a composition-root assertion.","location":"1.2 sweep_dead_panes(workspace_id) and acceptance 1.2.4; 2.1 pane.split order and the 'WorkspaceOps is constructed with' paragraph; 2.3 'the ops module that HTTPServer._init_mcp_subsystems takes from the ServiceContainer'","prevention":"When a correctness guard lives in process memory, name its single owner and every surface that must share it, and check composition-root construction order.","root_cause":"The in-flight guard was specified inside the ops module without tracing that two surfaces are constructed at different times in init_servers.","section_id":"1.2","severity":"blocking"},{"category":"missing-requirement","check_key":"requirements_traceability:actor-principal-carrier","description":"The research claim is false. setup_internal_registries is called from HTTPServer._init_mcp_subsystems with services and resolvers only, no HTTPServer and no request. AuthService.request_principal(request) needs the HTTPConnection, and no context variable carries the principal: _set_context_for_request and _bind_agent_run_context seed only the session, project, and agent-run-id context vars. A registry tool therefore cannot tell 'local CLI token, no session' (operator) from 'agent token, no session' (refuse). daemon_auth_headers sends the managed agent token whenever GOBBY_AGENT_API_TOKEN is set, so the unguarded default admits an agent-token caller as operator, with kill/spawn/adopt anywhere. The mechanism needs a file 2.3 does not target.","finding_id":"F2-principal-carrier","fix":"In 2.3, add src/gobby/servers/routes/mcp/endpoints/request_context.py::* (and the session_context utility if the new var lives there) to Targets. Seed a request-principal context var (operator vs agent claims) in _set_context_for_request, which already holds the request, and reset it with the other seeded tokens. The registry derives the actor from it. request_context.py is already a 3.2 Target, so add a dependency edge between 2.3 and 3.2 for shared-target ordering. Correct the Research context sentence.","location":"2.3 actor derivation paragraph, Research context 'Registries receive the HTTPServer, so the request is in reach without a new parameter', acceptance 2.3.5","prevention":"Before claiming a value is 'in reach' of a tool body, trace the actual call signature and context vars from the HTTP route into the registry.","root_cause":"Assumed registries receive the HTTPServer or request without checking setup_internal_registries' parameters.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"repository_blast_radius:pane-spawn-backend-selection","description":"Constraints say pane terminals are always native and tmux never carries pane identity, but 2.1 (the leaf's standalone spec) never says it. spawn_web_terminal runs on whatever runtime it is handed. The existing WS path (_handle_terminal_create) resolves terminal_config.default_backend, which TerminalConfig allows to be 'tmux', and TmuxTerminalRuntime.prepare_spawn forwards request.env. On a tmux default, pane shells would be tmux rows carrying the pane env but not GOBBY_TERMINAL_ID (3.1 sets it only in NativeTerminalRuntime.prepare_spawn), so 3.2 never binds and the constraint is broken. Adopt (tab.create/pane.split with terminal_id, used by open_agent_in_new_tab) can name tmux-backed agent terminals, yet the ops hold 'the terminal runtime' rather than the registry needed to read, write, or kill by row.backend.","finding_id":"F3-native-pane-runtime","fix":"In 2.1, state that pane spawns resolve the native runtime explicitly (registry.resolve('native')), independent of default_backend, and raise terminal_failed when native is unavailable. WorkspaceOps takes the runtime registry and resolves an adopted terminal's runtime by row.backend. State the adopt policy for non-native rows (allow and document, or refuse invalid_op) and align the Constraint bullet. Extend 2.1.1 to assert a native spawn under default_backend=tmux and 2.1.2 to cover a non-native adopt.","location":"2.1 'WorkspaceOps is constructed with ... the terminal runtime', pane.split and tab.create spawn and adopt paths, acceptance 2.1.1/2.1.2","prevention":"Copy framing constraints that change a leaf's implementation choice into that leaf's body and acceptance.","root_cause":"The native-only constraint lives in framing, and the reused spawn helper is backend-agnostic.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime_invariants:pane-env-project-routing","description":"2.1 exports GOBBY_PROJECT_ID and GOBBY_DAEMON_URL into user pane shells, but 3.1's IDENTITY_ENV_VARS scrub omits both. src/gobby/mcp_proxy/stdio_proxy.py::read_project_id returns GOBBY_PROJECT_ID before walking cwd for .gobby/project.json, and the proxy sends it as X-Gobby-Caller-Project-Id and X-Gobby-Project-Id. _set_context_for_request resolves the wrapper's terminal-context session inside that caller project. A user who cds into another project's checkout in a pane and starts claude sends the pane's original project, so session resolution looks in the wrong project (409 SESSION_REQUIRED or misrouted calls), while 3.2 deliberately leaves that session unbound. A daemon restarted from a pane (V1.7) keeps GOBBY_PROJECT_ID. get_project_context's env fallback then returns the pane's project for daemon-context lookups, and every os.environ.copy() child inherits it, which is the leak class 3.1 set out to close. No pane consumer of either variable is named.","finding_id":"F4-pane-project-env","fix":"Remove GOBBY_PROJECT_ID and GOBBY_DAEMON_URL from the pane identity env in 2.1; the pane's project is on its terminals and tab rows. If a consumer needs them, add both to the runner.main scrub in 3.1 and add an acceptance proving a CLI started after cd routes MCP calls to its cwd project.","location":"2.1 pane.split identity env list (GOBBY_DAEMON_URL, GOBBY_PROJECT_ID); 3.1 IDENTITY_ENV_VARS scrub","prevention":"For every environment variable exported into a user shell, check who reads it with precedence over local discovery and whether the root scrub covers it.","root_cause":"The agent env builder's variables were reused for interactive shells, where cwd, not spawn-time project, is authoritative.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime_invariants:parametric-subscription-gate","description":"gclient sends SUBSCRIBED_EVENTS as a bare subscribe on connect (crates/gclient/src/daemon/live.rs). _handle_subscribe adds to websocket.subscriptions, and BroadcastMixin._is_subscribed returns true for a bare type before any parametric check. Adding bare workspace_event makes every gclient receive every workspace's events, contradicting the Constraint that workspace_event is gated by workspace_event:workspace_id=<id> from day one and the intent of 2.2.3. 4.2 never says the client drops events for other workspaces, so a window on w1 would apply w2's pane and tab events.","finding_id":"F5-bare-workspace-subscription","fix":"In 4.1, do not add workspace_event to SUBSCRIBED_EVENTS. Rely on the parametric subscription registered by workspace_attach and re-registered on reconnect (4.2.6). DaemonEvent::Workspace ignores events whose workspace_id is not the attached workspace. Extend 4.1.2 or add a test that a second workspace's event is neither delivered nor applied.","location":"4.1 'SUBSCRIBED_EVENTS gains workspace_event'; Constraints parametric gating; 2.2.3","prevention":"When adding a gated event, check the client's connect-time bare subscription list against the server's subscription matching order.","root_cause":"Followed the existing pattern of listing every consumed event in SUBSCRIBED_EVENTS without noticing that bare entries override parametric gating.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime_invariants:close-vs-in-flight-busy","description":"pane.close raises busy on an in-flight pane, and tab.close and workspace.close close each pane the same way, so none of the three can remove a row mid-spawn; only a cascade (project delete) can. The executor cannot tell whether tab.close/workspace.close should fail busy or delete the in-flight pane and rely on the not_found kill path.","finding_id":"F6-close-in-flight-contradiction","fix":"Pick one: tab.close and workspace.close raise busy while any child pane is in flight, or they delete the in-flight pane and the split's set_pane_terminal kill path handles it. Reword the split paragraph to match.","location":"2.1 pane.split ('closed while the spawn ran (pane.close, tab.close, workspace.close, or a cascade)') vs pane.close ('raises busy') and tab.close ('closes each pane as pane.close does')","prevention":"Cross-check every named interleaving against each op's stated guard.","principle":"One op's guard must not make another op's recovery path unreachable without saying so.","section_id":"2.1","severity":"nit"},{"category":"unhandled-edge","check_key":"runtime_invariants:two-parent-lock-order","description":"pane.move changes the layouts of two tabs and allocates a ref under the destination tab's row lock, so it locks two parent rows. Opposite concurrent moves (t1 to t2 and t2 to t1, e.g. two windows) can deadlock. PostgreSQL aborts one with DeadlockDetected, which is not one of the six typed codes.","finding_id":"F7-move-lock-order","fix":"State that multi-parent ops lock their parent rows in ascending id order in one transaction, or map a serialization failure to busy.","location":"2.1 pane.move {pane, tab, beside?, axis?}; 1.2 parent-row FOR UPDATE allocation","prevention":"Any op that takes more than one row lock names its lock order.","principle":"Row-lock serialization needs a global lock order once an operation spans two parents.","section_id":"2.1","severity":"nit"}],"lanes_execution":"No subagent facility in this runtime; all three lanes ran sequentially in the parent. repository_blast_radius was spot-checked against exact source symbols and consumers. No plan reviewer-miss lessons exist (class recall and list_check_keys both empty).","plan_hash":"7fd819e218934954b88f7b75fe42c909078b26821404303b26e4067c340beb95","plan_id":"gclient-workspaces","protocol_note":"derive_plan_review_manifest's MCP wrapper adds ok:true, but validate_plan_review_coverage compares against the service-level derivation, which has no ok key (src/gobby/plans/review_evidence.py validate_plan_review_coverage vs src/gobby/mcp_proxy/tools/plans/review_evidence.py). shadow_manifest_status was therefore passed as the derivation's status/routing_decisions/manifest_entries/manifest_digest/entry_count without the wrapper's ok key; every other byte came from the tool result. This is the likely cause of the round 1-2 shadow_manifest_mismatch. Validation passed.","round":3,"routing_decisions_used_for_shadow":{"1.1":{"category":"code","implementation_domain":"backend"},"1.2":{"category":"code","implementation_domain":"backend"},"2.1":{"category":"code","implementation_domain":"backend"},"2.2":{"category":"code","implementation_domain":"backend"},"2.3":{"category":"code","implementation_domain":"backend"},"2.4":{"category":"code","implementation_domain":"backend"},"3.1":{"category":"code","implementation_domain":"backend"},"3.2":{"category":"code","implementation_domain":"backend"},"4.1":{"category":"code","implementation_domain":"frontend"},"4.2":{"category":"code","implementation_domain":"frontend"},"4.3":{"category":"code","implementation_domain":"frontend"},"5.1":{"category":"docs"}},"verdict":"needs_review"},"session_id":"47981968-3c34-4e7b-aaee-8c5fb187c963"}
```

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add the workspaces schema and node refs
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Migration 440 creates `workspaces`, `workspace_tabs`,
    `workspace_panes`, adds `machines.ref`, and grants the runtime role. file: `crates/gcore/assets/schema/migrations/440_add_workspaces.sql`.

    1.1.2: The embedded migration table, catalog manifest, grant identity pins, contract
    tests, and expected identity file agree on the new schema identity. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.

    1.1.3: The signed golden grant vectors verify against the new identity. test:
    `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.'
  labels:
  - covers:gclient-workspaces:1.1:1.1.1
  - covers:gclient-workspaces:1.1:1.1.2
  - covers:gclient-workspaces:1.1:1.1.3
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Add WorkspaceManager with ref allocation and the dead-pane sweep
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `WorkspaceManager` creates, resolves (uuid, name, every
    ref form), mutates, and closes workspaces, tabs, and panes with reusable lowest-free
    refs inside single transactions. symbol: `WorkspaceManager`. file: `src/gobby/storage/workspaces.py`.

    1.2.2: A closed pane''s ref is reused by the next split and a closed workspace''s
    `w#` by the next create. test: `tests/storage/test_workspaces.py::test_refs_are_lowest_free_and_reused`.

    1.2.3: Node resolution accepts `n#`, uuid, hostname, and label, and `None` resolves
    to the daemon''s own machine id; `machines.ref` is allocated on `upsert_seen`.
    test: `tests/storage/test_machines.py::test_upsert_seen_allocates_lowest_free_ref`.

    1.2.4: The sweep drops panes whose terminal was transitioned by any writer (`mark_exited`,
    `mark_orphaned`, `fail_pending_attempt`, a deleted row), keeps a `NULL` pane while
    `mark_spawn_in_flight` holds its id and prunes it once `clear_spawn_in_flight`
    releases it, removes empty tabs, and keeps empty workspaces. test: `tests/storage/test_workspaces.py::test_sweep_dead_panes_prunes_layouts`.

    1.2.5: The daemon composition root constructs one `WorkspaceManager` and hands
    it to both the WebSocket server and the `ServiceContainer`. test: `tests/terminals/test_composition_roots.py::test_wiring_hands_one_workspace_manager_to_both_servers`.'
  labels:
  - covers:gclient-workspaces:1.2:1.2.1
  - covers:gclient-workspaces:1.2:1.2.2
  - covers:gclient-workspaces:1.2:1.2.3
  - covers:gclient-workspaces:1.2:1.2.4
  - covers:gclient-workspaces:1.2:1.2.5
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Execute workspace ops through a shared module with actor scope
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '3.1'
  validation_criteria: '2.1.1: `pane.split` inserts the pane before spawning, spawns
    on the native runtime even when `default_backend` is `tmux`, with the identity
    env (and without `GOBBY_PROJECT_ID` or `GOBBY_DAEMON_URL`) and the tab''s checkout
    as cwd, records the terminal id, and on spawn failure or an unavailable native
    runtime removes the row, restores the layout, and raises `terminal_failed`; a
    sweep during the spawn leaves the pane alone; a pane whose row a cascade removed
    mid-spawn has its minted terminal killed and the op raises `not_found`; `pane.close`,
    `tab.close`, and `workspace.close` on an in-flight pane raise `busy`. test: `tests/terminals/test_workspace_ops.py::test_split_spawns_with_pane_identity_env_and_rolls_back`.

    2.1.2: `tab.create` and `pane.split` adopt a named live terminal of either backend
    without spawning and raise `busy` with the holder''s ref when it is already paned;
    `pane.read` and `pane.send_keys` on an adopted tmux terminal go through the tmux
    runtime resolved from the row''s `backend`; `pane.close` kills only an owned live
    terminal, releases an adopted one, and removes the row of an exited one immediately;
    `pane.move` across tabs collapses the source split and allocates a destination
    ref. test: `tests/terminals/test_workspace_ops.py::test_adopt_close_and_move_semantics`.

    2.1.3: A session actor outside the target''s project and agent tree, or an autonomous
    agent-run session, gets `forbidden` from the pane terminal ops, from a spawn or
    adopt, and from `pane.close`, `tab.close`, and `workspace.close` over an owned
    live terminal, while the operator actor and row-only ops are admitted; `send_keys`
    keeps its behavior through the shared policy. test: `tests/terminals/test_workspace_ops.py::test_actor_scope_guards_kill_spawn_and_adopt`.

    2.1.4: Every mutation publishes its `workspace_event` through the injected callable
    and every failure is a typed exception with one of the six codes. test: `tests/terminals/test_workspace_ops.py::test_ops_publish_events_and_raise_typed_errors`.'
  labels:
  - covers:gclient-workspaces:2.1:2.1.1
  - covers:gclient-workspaces:2.1:2.1.2
  - covers:gclient-workspaces:2.1:2.1.3
  - covers:gclient-workspaces:2.1:2.1.4
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Serve workspace messages and events over the terminal WebSocket
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: `workspace_attach` without a node or workspace attaches
    the daemon node''s `default`, creating it on first use, and its reply carries
    the resolved `n#` and a lifecycle watermark. test: `tests/servers/test_workspace_ws.py::test_attach_creates_default_on_the_local_node`.

    2.2.2: Every `workspace_op` round-trips with its `request_id`, and each typed
    ops exception yields a `workspace_error` with its code. test: `tests/servers/test_workspace_ws.py::test_ops_round_trip_and_errors_are_typed`.

    2.2.3: A `workspace_event` and the terminal event for the same pane share the
    lifecycle order, and only connections subscribed to that workspace receive it.
    test: `tests/servers/test_workspace_ws.py::test_workspace_events_share_lifecycle_order_and_filter`.

    2.2.4: The golden corpus gains the five workspace fixtures and both emitters match
    them. test: `tests/servers/test_terminal_ws_golden.py::test_python_matches_terminal_ws_golden_corpus`.

    2.2.5: The Rust replay of the corpus accepts the grown manifest. test: `crates/gclient/tests/ws_golden.rs::corpus_replays_from_canonical_manifest`.

    2.2.6: The web replay of the corpus accepts the grown manifest. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::replays
    every canonical terminal WS fixture from the manifest`.

    2.2.7: The protocol contract documents the four messages and their error codes.
    behavior: "workspace messages" in `docs/contracts/gterm-protocols.md`.'
  labels:
  - covers:gclient-workspaces:2.2:2.2.1
  - covers:gclient-workspaces:2.2:2.2.2
  - covers:gclient-workspaces:2.2:2.2.3
  - covers:gclient-workspaces:2.2:2.2.4
  - covers:gclient-workspaces:2.2:2.2.5
  - covers:gclient-workspaces:2.2:2.2.6
  - covers:gclient-workspaces:2.2:2.2.7
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Add the gobby-workspaces MCP registry
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '3.2'
  validation_criteria: '2.3.1: The registry exposes the sixteen tools with schemas
    and executes them by ref through the shared ops module. symbol: `create_workspaces_registry`.
    file: `src/gobby/mcp_proxy/tools/workspaces/registry.py`.

    2.3.2: `read_pane` returns the pane''s screen and `wait_for_pane_output` resolves
    on a matcher hit or times out typed. test: `tests/mcp_proxy/test_workspaces_registry.py::test_read_and_wait_address_panes_by_ref`.

    2.3.5: Through the HTTP MCP route, a wrapper call with a session id acts as `session:<id>`,
    a local-CLI-token call with no session acts as `operator`, an agent-token call
    with no session is refused `forbidden`, and a direct registry call with no request
    principal seeded is refused `forbidden`. test: `tests/mcp_proxy/test_workspaces_registry.py::test_actor_is_derived_from_session_context_and_principal`.

    2.3.3: The MCP tools guide lists the server. behavior: "gobby-workspaces" in `docs/guides/mcp-tools.md`.

    2.3.4: Every gobby-workspaces tool maps to the workspaces reference and an audit
    record. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.'
  labels:
  - covers:gclient-workspaces:2.3:2.3.1
  - covers:gclient-workspaces:2.3:2.3.2
  - covers:gclient-workspaces:2.3:2.3.5
  - covers:gclient-workspaces:2.3:2.3.3
  - covers:gclient-workspaces:2.3:2.3.4
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Add the workspaces, panes, and nodes CLI groups
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: '2.4.1: The three groups exist with the listed commands, `--node`,
    and `--json`; a full ref overrides `--node`; `panes split` requires exactly one
    direction flag and forwards `--terminal`. test: `tests/cli/test_workspaces.py::test_panes_split_resolves_ref_node_and_direction`.

    2.4.2: The CLI guide lists every command. behavior: "### Workspaces" in `docs/guides/cli-commands.md`.

    2.4.3: Every visible workspaces, panes, and nodes leaf maps to the workspaces
    reference and an audit record. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.'
  labels:
  - covers:gclient-workspaces:2.4:2.4.1
  - covers:gclient-workspaces:2.4:2.4.2
  - covers:gclient-workspaces:2.4:2.4.3
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Export the terminal and pane identity into every native spawn
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '3.1.1: Every native spawn env carries `GOBBY_TERMINAL_ID`
    equal to the minted id, and a caller env cannot override it. test: `tests/terminals/test_native_runtime.py::test_spawn_env_carries_terminal_id`.

    3.1.2: The identity names are registered and listed in `IDENTITY_ENV_VARS` and
    `ALL_TERMINAL_ENV_VARS`. symbol: `get_terminal_env_vars`. file: `src/gobby/agents/constants.py`.

    3.1.3: After `main` starts, the daemon''s own environment carries no identity
    name whatever its parent exported, so every spawn root (`make_spawn_env`, the
    host launch, the tmux servers) inherits a clean environment. test: `tests/test_runner_env_scrub.py::test_main_pops_inherited_identity_before_the_runner_starts`.'
  labels:
  - covers:gclient-workspaces:3.1:3.1.1
  - covers:gclient-workspaces:3.1:3.1.2
  - covers:gclient-workspaces:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Bind CLI sessions to their native terminal at session start
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: ghook and Python emit and accept `gobby_terminal_id`
    and `gobby_pane_ref`, and the documented key table matches both. test: `crates/ghook/src/terminal_context.rs::capture_emits_expected_keys`.

    3.2.2: `bind_session` binds a gobby-owned row, refuses an agent row, a project
    mismatch, and a non-terminal session type, and rebinds after the previous session
    ends or when its stored parent pid is dead. test: `tests/storage/test_terminal_bindings.py::test_bind_session_guards`.

    3.2.3: A session starting with `GOBBY_TERMINAL_ID` binds at materialization; a
    nested CLI child started while the bound session is active does not; a context
    that also carries `tmux_pane` seeds the tmux row and leaves the native row unbound.
    test: `tests/hooks/test_session_start_handlers.py::test_native_terminal_id_binds_unless_tmux_or_nested`.

    3.2.4: The attention roster lists a bound native session with its terminal, and
    session end releases the binding without exiting the terminal. test: `tests/servers/test_attention_native_roster.py::test_bound_native_session_is_on_the_roster`.'
  labels:
  - covers:gclient-workspaces:3.2:3.2.1
  - covers:gclient-workspaces:3.2:3.2.2
  - covers:gclient-workspaces:3.2:3.2.3
  - covers:gclient-workspaces:3.2:3.2.4
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Add the workspace messages to the client daemon layer
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '4.1.1: The client decodes every workspace fixture into typed
    rows, ops, and events. symbol: `WorkspaceEvent`. file: `crates/gclient/src/daemon/workspace.rs`.

    4.1.2: `LiveDaemon` attaches a workspace, sends an op, and receives the typed
    event after reconnect, while a second workspace''s event on the same daemon is
    neither delivered (no bare subscription) nor surfaced by the reader. test: `crates/gclient/tests/daemon_live.rs::workspace_attach_op_and_event_round_trip`.

    4.1.3: `crates/gclient/src/daemon/mod.rs` and `crates/gclient/src/daemon/live.rs`
    stay under the guard after the split. test: `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines`.'
  labels:
  - covers:gclient-workspaces:4.1:4.1.1
  - covers:gclient-workspaces:4.1:4.1.2
  - covers:gclient-workspaces:4.1:4.1.3
  tdd: true
  source_section: '4.1'
  implementation_domain: frontend
- title: Render the daemon layout with viewer-local state and route every mutation
    through it
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: `TileLayout` has no focus field and every focus-dependent
    method takes the focus explicitly. symbol: `TileLayout::split_focused`. file:
    `crates/gterminal/src/layout.rs`.

    4.2.2: Two viewers on one workspace keep separate focus, zoom, and active tab
    while sharing tabs and panes. test: `crates/gclient/tests/workspace.rs::two_viewers_share_layout_and_keep_their_own_focus`.

    4.2.3: Pane ids are interned per process from daemon pane ids, so a reloaded layout
    keeps every slot and two distinct daemon panes never share a `PaneId`. test: `crates/gclient/tests/workspace.rs::pane_ids_are_interned_without_collision`.

    4.2.4: Every layout mutation sends a `workspace_op` with explicit ids and the
    loop applies the daemon''s `workspace_event`. test: `crates/gclient/tests/client_loop.rs::wired_actions_split_focus_swap_and_switch_tabs`.

    4.2.5: A `workspace_event` naming an undelivered terminal opens it on demand and
    an unresolved slot renders empty instead of being reaped. test: `crates/gclient/tests/client_loop.rs::workspace_events_open_terminals_on_demand`.

    4.2.6: Reconnect re-attaches the workspace and re-fetches the snapshot under the
    pinned watermark. test: `crates/gclient/tests/client_loop.rs::daemon_restart_keeps_panes_and_reattaches`.

    4.2.7: The ratio drag applies locally and sends one op on drop. test: `crates/gclient/tests/client_loop.rs::ratio_drag_sends_one_op_on_drop`.

    4.2.8: `app/mod.rs`, `app/live.rs`, `actions.rs`, `menu.rs`, and `live_loop.rs`
    stay under the guard after the splits. test: `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines`.'
  labels:
  - covers:gclient-workspaces:4.2:4.2.1
  - covers:gclient-workspaces:4.2:4.2.2
  - covers:gclient-workspaces:4.2:4.2.3
  - covers:gclient-workspaces:4.2:4.2.4
  - covers:gclient-workspaces:4.2:4.2.5
  - covers:gclient-workspaces:4.2:4.2.6
  - covers:gclient-workspaces:4.2:4.2.7
  - covers:gclient-workspaces:4.2:4.2.8
  tdd: true
  source_section: '4.2'
  implementation_domain: frontend
- title: Attach by workspace at startup and retire the client snapshot files
  category: code
  task_type: feature
  depends_on:
  - '4.2'
  validation_criteria: '4.3.1: `--node` and `--workspace` parse, default to the local
    `default` workspace, and a full ref overrides `--node`. test: `crates/gclient/tests/startup.rs::workspace_flags_default_to_the_local_default`.

    4.3.2: No client code writes or reads the snapshot or session files under the
    client directory; sidebar and project preferences round-trip through `prefs.toml`.
    test: `crates/gclient/tests/startup.rs::prefs_carry_sidebar_and_project_preferences`.

    4.3.3: A window seeds focus from the row hints and writes hints back when focus
    changes. test: `crates/gclient/tests/workspace_rows.rs::focus_hints_seed_and_follow_the_last_actor`.

    4.3.4: A gclient started inside a pane shifts its prefix and opens no terminal
    of its own. test: `crates/gclient/tests/startup.rs::nested_pane_launch_shifts_prefix_and_opens_nothing`.'
  labels:
  - covers:gclient-workspaces:4.3:4.3.1
  - covers:gclient-workspaces:4.3:4.3.2
  - covers:gclient-workspaces:4.3:4.3.3
  - covers:gclient-workspaces:4.3:4.3.4
  tdd: true
  source_section: '4.3'
  implementation_domain: frontend
- title: Document workspaces across the client, protocol, CLI, MCP, and ghook guides
  category: docs
  task_type: feature
  depends_on:
  - '2.4'
  - '3.2'
  - '4.3'
  validation_criteria: '5.1.1: The gclient guide documents workspaces, refs, multi-window
    behavior, daemon-owned persistence, and bringing a bare terminal in. behavior:
    "## Workspaces" in `docs/guides/gclient-user-guide.md`.

    5.1.2: No shipped doc or module comment still advises Ghostty tabs to run tmux
    or to set `destroy-unattached`. behavior: "Orphaned terminals" in `docs/guides/gclient-user-guide.md`.'
  labels:
  - covers:gclient-workspaces:5.1:5.1.1
  - covers:gclient-workspaces:5.1:5.1.2
  tdd: false
  source_section: '5.1'
  assigned_agent: tech-writer
```

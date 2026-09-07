Plan artifact: `.gobby/plans/gclient-workspace-sidebar.md`

# gclient workspace: projects, agents, menus, and viewer sizing

**Plan ID:** gclient-workspace-sidebar

## Overview
`kind: framing`

The live gclient (0.5.0 @ ba07053) was compared screen by screen against herdr 0.9.0
(evidence: `docs/evidence/gclient-herdr-parity-2026-09-07.md`, #21971). The user rejected the
sidebar model: gclient lists daemon terminals and attention entries, herdr lists workspaces
and agents. This plan reworks the left sidebar into **projects** and **agents**, makes tabs
belong to a project, restores a snapshot instead of auto-opening every terminal, adds the
right-click context menus (taking over sections 5.1 and 5.2 of
`.gobby/plans/gclient-mouse-parity.md`, and carrying its paused 4.2, 4.3 and 4.4), makes every
shown pane size its terminal with a daemon-arbitrated precedence (web UI, then gclient, then
plain tmux), and seeds the fleet model (machine id on every row, machine filter) that #20202's
hub roster will feed later. It also satisfies planning task #21908 (D4: worktree groups and
workspace lifecycle in the client sidebar), which closes on this artifact.

## Constraints
`kind: framing`

Decision Record (user-confirmed 2026-09-07):

1. Sidebar sections are `projects` and `agents`; `terminals` and `attention` go away.
2. Tabs belong to a project. Focusing a project row swaps the tab bar; the agents section
   shows only the focused project's agents; snapshots are per project.
3. An agent row is a roster entry (`/api/attention/roster`) that has a terminal (tmux or
   gterm native) and belongs to the focused project, joined to `/api/sessions` and
   `/api/agents/runs` for `project_id`, `ref`, `title`, `git_branch`, `machine_id`. Sessions
   without a terminal are not listed.
4. Fleet: the same agents section with a machine filter in its header, default = the machine
   gclient was launched on (`gobby_core::machine::read_local_machine_id`). Every row carries
   `machine_id` from day one; the hub roster (#20202) feeds the same rows later (deferred D1).
5. Project rows show name plus a branch and `↑ahead ↓behind` line; worktree child rows come
   from the daemon registry (`/api/source-control/worktrees`). Worktree create, open and
   delete run from the client through the daemon (parity plus).
6. The projects footer `new` opens a path dialog; a daemon route inits and registers the
   project on this machine; the project opens with one shell tab.
7. First run without a snapshot opens one shell tab in the focused project. Existing
   terminals are never auto-opened; a snapshot restores tabs and panes.
8. Right-click menus exist for pane, tab, project row, worktree row, agent row and empty
   chrome; `close pane` and `close tab` land first (5.1 before the sidebar rework).
9. States: `blocked` (attention pending), `working`, `done` (idle and unseen), `idle`; icon
   plus text, never hue alone (`.impeccable.md` lines 177-243; deutan-safe palette).
10. Closing a tab or a project terminates its gobby-owned terminals through the daemon; a
    refused kill (external ownership) keeps the pane; confirm-close text names the scope.
11. Branch and ahead/behind come from the daemon; the client never shells out to git.
12. Data flow stays REST snapshot at reconcile plus WebSocket deltas; the client subscribes to
    `project_event`, `worktree_event`, `session_event`, `terminal_event`, `agent_event`; only
    git status refreshes on a slow timer (10 s while the sidebar is expanded).
13. This plan takes over mouse-parity 4.2, 4.3, 4.4, 5.1 and 5.2 (their text is carried into
    4.1, 4.2, 2.3, 5.1 and 5.2 here; the mouse plan's V1 changelog records the move). The
    stray-glyph renderer defect joins it (6.1).
14. Sizing: every pane gclient shows resizes its terminal to the pane, tmux and native,
    external included, held or not. Precedence when several viewers show one terminal:
    web UI > gclient > plain tmux client; among equals the last interacting viewer wins
    (herdr 0.9.0 rule). Closing the pane or exiting gclient restores automatic tmux sizing.
    The tmux half is transitional until the native default flip (#21357).
15. Nested prefix (#21923), temporary: when `tmux_identity::current()` resolves an outer
    tmux client at startup, the effective prefix becomes the obscure `ctrl+]` (a prefs
    `[keymap] prefix` override still wins); `prefix prefix` sends the literal chord to the
    pane (herdr parity); `ctrl+\` is a non-prefix escape that releases control of a held
    pane; the help dialog and status line show the live prefix. The nested special case is
    removed and herdr's `ctrl+b` default returns once the native flip (#21357) retires tmux;
    the code carries a `TODO(#21357)` at the nested branch.
16. tmux spawn geometry (#21914): a spawn honours the rows/cols it was asked for
    (`terminal_create` threads them into `new-session -x/-y`), agent spawns keep their
    200x50 default, and the terminals row always reports the geometry tmux actually created;
    the viewer sizing of decision 14 fits the pane afterwards.

Absorbed open tasks: #21914 → 1.5, #21932 → 1.6, #21923 → 4.3, #21936 → fixed standalone
before expansion (red test, XS), #21908 → closed by this artifact. Epic siblings referenced,
not absorbed: #21357 (native default flip; decision 14's tmux path is transitional until it
lands), #20201 (plugin menu; `custom_command` stays reserved in the global menu), #20202
(D1 below).

Ordering: the daemon leaves (P1) are parallel codex worktree tasks, ordered only where they
share a file (1.2 after 1.1, 1.4 after 1.5). The client leaves are one implementer's work
and share `chrome.rs`, `actions.rs`, `live_loop.rs` and the `client_loop.rs` suite, so they
form one chain whose `depends` entries are that order plus the real daemon prerequisite:
4.1 → 5.1 → 5.2 → 2.1 → 2.2 → 2.3 → 6.1 → 3.1 → 3.2 → 3.3 → 5.3 → 4.2 → 4.3. Close pane and
close tab (5.1, 5.2) come first because the user asked for them first; the sidebar rework
follows the data model; the keymap and prefix leaves close the chain.

Engineering constraints:

- Monolith ceiling (1,000 lines, `crates/gclient/tests/source_size.rs`). Current: `ui/keymap.rs`
  947, `app/mod.rs` 942, `source_control.py` 938, `terminal_ws.py` 895, `useTmuxSessions.ts`
  884; tests exempt. Every section touching one of those names a new same-extension file
  and states the move. New modules:
  `crates/gclient/src/app/sidebar_model.rs`, `crates/gclient/src/app/project_tabs.rs`,
  `crates/gclient/src/app/live_loop/projects.rs`, `crates/gclient/src/app/live_loop/menu.rs`,
  `crates/gclient/src/app/live_loop/modal_input.rs`, `crates/gclient/src/ui/sidebar/projects.rs`,
  `crates/gclient/src/ui/sidebar/agents.rs`, `crates/gclient/src/ui/context_menu.rs`,
  `crates/gclient/src/ui/dialogs/project.rs`, `crates/gclient/src/ui/keymap/names.rs`,
  `crates/gclient/src/ui/keymap/prefix.rs`, `crates/gclient/src/daemon/projects.rs`,
  `src/gobby/servers/routes/source_control_git.py`,
  `src/gobby/servers/routes/source_control_worktrees.py`, `src/gobby/worktrees/creation.py`,
  `src/gobby/servers/websocket/terminal_sizing.py`, `web/src/hooks/tmuxSessionMessages.ts`,
  `src/gobby/agents/sandbox_domains.py`.
  Module placement follows memory d71c4299: app-level modules under `app/live_loop/`
  declared from `live_loop.rs`.
- herdr oracle is `~/.gobby/clones/herdr` at v0.9.0 (read-only). Ported surfaces mirror its
  client shell modules `sidebar.rs` (workspace rows), `agent_sidebar.rs`, `context_menu.rs`
  (`items()`), `overlays.rs` (menu popup), `tabs.rs`, and `src/ui.rs` (workspace creation
  dialog); labels stay lowercase verbs per the mouse plan's contract.
- Design contract: `.impeccable.md` + `impeccable` skill loaded before any chrome edit.
  State palette by lightness (info 250, warning 75, destructive 350, success 125), no
  red/green, no pure black/white, icon or position cue on every state.
- No backward compatibility (0.5.0 unshipped): the `terminals`/`attention` sections, their
  golden fixture, `Hit::Roster`/`Hit::Attention`, `SidebarSection::{Roster, Attention}` and the
  deferred parity markers are replaced, not kept.
- Validation per leaf: `cargo fmt -p gobby-client -- --check`,
  `cargo clippy -p gobby-client --all-targets -- -D warnings`,
  `cargo nextest run -p gobby-client <filter>` (bare, foreground); Python leaves:
  `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest <file>`, `uv run ruff check src/`,
  `uv run mypy src/`; the web leaf: `npm --prefix web test -- useTmuxSessions`. Never the
  full pytest suite.
- UI leaves are implemented by the coordinator; daemon leaves (P1) go to codex agents in
  worktrees off `0.5.0` and land via `merge_worktree` then `delete_worktree`; 1.6 is
  dispatched first because the other worktree builds need its network grant.
- Data facts the design rests on: `/api/projects` returns id, name, display_name,
  `checkout {machine_id, root_path}`, stats; `/api/source-control/status?project_id=` returns
  `current_branch`, `repo_path`, `worktree_count`; `/api/source-control/worktrees?project_id=`
  returns `id, project_id, machine_id, task_id, branch_name, worktree_path, base_branch,
  agent_session_id, status, workspace_role`; the roster entry carries `entry_id`, `run_id`,
  `session_id`, `lifecycle_status`, `attention`, `task {id, ref, stage}`, `provider`, `model`,
  `terminal {terminal_id, backend, attach}`, `tmux {session_name, ...}`, `last_activity_at`;
  `/api/sessions?project_id=&status_in=` returns `id, ref, title, source, status,
  git_branch, machine_id, agent_run_id, terminal_context`; `/api/agents/runs?project_id=`
  returns `run_id, agent_name, provider, model, status, task_id, terminal_id, worktree_id,
  machine_id`. `/api/terminals` requires `project_id`. gclient's `RosterEntry` and
  `TerminalRow` already keep every JSON field in `fields` (only the projection drops them).
  Worktree records already carry `workspace_role` (default `task`); the client value is
  `client`.

## P1: Daemon surface for the sidebar
`kind: framing`

**Goal**: The daemon exposes what the projects and agents sections and the sizing rule need;
each leaf is a codex worktree task.

### 1.1 Add ahead and behind counts to source-control status [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/source_control.py::*` — scope-reason: `get_status` and `list_branches` share the new tracking parse; the git helpers leave the router module for the new split file
- `src/gobby/servers/routes/source_control_git.py`
- `tests/servers/routes/test_source_control_routes.py::*` — scope-reason: new test functions appended

`GET /api/source-control/status?project_id=` gains `ahead: int | null` and
`behind: int | null` for `current_branch`, computed from the same
`git for-each-ref %(upstream:track)` parse `list_branches` uses (`null` when the branch has
no upstream). The response keeps every existing field. Cached under the existing
`_GIT_TTL`. `worktree_event` and `project_event` remain the change signals; no new event.

`source_control.py` is at 938 lines: the git helpers (`_run_git`, the cache trio,
`_resolve_project`, and the new `parse_upstream_track` shared by `get_status` and
`list_branches`) move into `source_control_git.py`, and the router module imports them.

**Acceptance:**

- 1.1.1 - The status payload carries `ahead` and `behind` for a branch with an upstream and `null` for one without, with the existing fields unchanged. symbol: `src/gobby/servers/routes/source_control.py::get_status`. test: `tests/servers/routes/test_source_control_routes.py::test_status_reports_ahead_behind`.

### 1.2 Add the client worktree create route [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/source_control.py::*` — scope-reason: the worktree and clone handlers leave the router module for the new split file
- `src/gobby/servers/routes/source_control_worktrees.py`
- `src/gobby/worktrees/creation.py`
- `src/gobby/mcp_proxy/tools/worktrees/_create.py::*` — scope-reason: the create body becomes a call into the shared creation service
- `tests/servers/routes/test_source_control_routes.py::*` — scope-reason: new test functions appended

`POST /api/source-control/worktrees` body `{project_id, branch_name, base_branch?,
workspace_role: "client"}` creates a worktree through the same steps the
`gobby-worktrees:create_worktree` MCP tool performs (branch conflict check, default path,
git worktree add, storage record with `workspace_role="client"`, no task binding) and
returns `Worktree.to_dict()`. Those steps are extracted from the MCP tool body into
`creation.py` so the route and the tool share one implementation. Errors: 404 unknown
project, 409 branch already checked out, 400 invalid branch. The existing
`DELETE /api/source-control/worktrees/{id}` is the delete path; it already refuses
task-owned worktrees with active runs, and a `client` worktree with a live gobby-owned
terminal inside is refused with 409 `terminals_live` so the client kills them first
(decision 10). `worktree_event` fires `created`/`deleted` as today.

`source_control.py` stays above 850 lines after 1.1: the worktree and clone handlers
(`list_worktrees`, `get_worktree_stats`, `delete_worktree`, `cleanup_worktrees`,
`sync_worktree`, `list_clones`, `delete_clone`, `sync_clone`) move into
`source_control_worktrees.py` as a sub-router the main router includes, and the new create
handler lives there.

**Acceptance:**

- 1.2.1 - Posting a valid body creates a `client` worktree, returns its row and publishes `worktree_event created`; the conflict, missing-project and invalid-branch cases return 409, 404 and 400. file: `src/gobby/servers/routes/source_control_worktrees.py`. test: `tests/servers/routes/test_source_control_routes.py::test_create_client_worktree`.
- 1.2.2 - Deleting a `client` worktree that still hosts a live gobby-owned terminal returns 409 `terminals_live`; after the terminal exits the delete succeeds. file: `src/gobby/servers/routes/source_control_worktrees.py`. test: `tests/servers/routes/test_source_control_routes.py::test_delete_client_worktree_refuses_live_terminals`.

### 1.3 Add the project init route [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/projects.py::*` — scope-reason: new `POST /init` handler beside `register_checkout`
- `tests/servers/routes/test_projects_routes.py::*` — scope-reason: new test functions appended

`POST /api/projects/init` body `{path}` runs `initialize_project` (the function behind
`gobby init`) for an absolute directory on this machine, which creates or adopts
`.gobby/project.json` and binds the checkout for `require_machine_id()`, then returns the
`/api/projects/{id}` payload. 400 when the path is not an absolute existing directory, 409
when the directory belongs to another project's checkout. `project_event` fires
`checkout_registered` as today.

**Acceptance:**

- 1.3.1 - Initializing a fresh directory returns the project payload with this machine's checkout and publishes `project_event checkout_registered`; a relative or missing path is 400 and a directory already bound to another project is 409. file: `src/gobby/servers/routes/projects.py`. test: `tests/servers/routes/test_projects_routes.py::test_init_project_route`.

### 1.4 Arbitrate terminal size by viewer precedence [category: code] (depends: 1.5)
`kind: deliverable`

Targets:
- `src/gobby/terminals/leases.py::*` — scope-reason: `TerminalLeaseRegistry.attach`, `resize_pty` and `finalize` carry the owner election; `_Attachment` gains `viewer` and last geometry; `_Lease` gains `sizing_owner`
- `src/gobby/servers/websocket/terminal_ws.py::*` — scope-reason: `TerminalWsMixin._handle_terminal_attach` reads `viewer`; the resize and viewport handlers leave the mixin for the new split file
- `src/gobby/servers/websocket/terminal_sizing.py`
- `src/gobby/servers/websocket/tmux.py::*` — scope-reason: the tmux bridge resize path follows the same owner rule
- `src/gobby/terminals/tmux_runtime.py::*` — scope-reason: `TmuxTerminalRuntime.resize` pins `window-size manual`; new `release_size` beside it
- `web/src/hooks/useTmuxSessions.ts::*` — scope-reason: the attach and resize message builders leave the hook for the new split file
- `web/src/hooks/tmuxSessionMessages.ts`
- `tests/terminals/test_lease_authority.py::*` — scope-reason: new test functions appended
- `tests/servers/test_terminal_ws_resize.py`
- `web/src/hooks/__tests__/useTmuxSessions.test.ts::*` — scope-reason: new test case appended

Today `resize_pty` admits only the lease holder and the native handler refuses `external`
rows, so a viewer that only observes can never fit a terminal to its pane. Decision 14 makes
size a viewer property:

- `terminal_attach` gains `viewer: "web" | "gclient"` (default `gclient` when absent; the web
  hook sends `web`). Precedence ranks `web` = 2, `gclient` = 1; a plain tmux client is rank 0
  and never sends geometry.
- The lease keeps `sizing_owner: attachment_id | None` plus each attachment's last
  `(rows, cols)`. `resize_pty` admits any live attachment, records its geometry, and elects
  the owner: highest rank, ties broken by most recent resize. Only the owner's geometry is
  applied to the runtime; a lower-rank resize is recorded and answered with
  `terminal_resize_result {applied: false, owner_viewer}` so gclient can show the pane cropped
  (viewport) rather than resized.
- `runtime.resize` is applied for `external` tmux rows too (the refusal goes away);
  `TmuxTerminalRuntime.resize` sets `window-size manual` on the window before
  `resize-window`; new `TmuxTerminalRuntime.release_size` runs `set-option -wu window-size`
  so tmux returns to automatic sizing.
- `finalize` (detach, socket close, daemon shutdown) re-elects the owner from the remaining
  attachments and applies its geometry; when no attachment with rank ≥ 1 remains, the
  runtime's `release_size` runs for tmux rows. Native rows keep the last applied size.
- The equal-size short-circuit (#20805) stays: applying the owner's geometry when the row
  already has it is a no-op.

`terminal_ws.py` is at 895 lines: `_handle_terminal_resize` and
`_handle_terminal_set_viewport` move into `terminal_sizing.py` as a second mixin the
websocket server composes, and the owner election lives beside them. `useTmuxSessions.ts`
is at 884 lines: the `terminal_attach` and `terminal_resize` message builders move into
`tmuxSessionMessages.ts`, and the attach builder sets `viewer: "web"`.

**Acceptance:**

- 1.4.1 - With a web and a gclient attachment on one terminal, a gclient resize is recorded but not applied while the web attachment holds the owner slot; the web resize is applied; after the web attachment finalizes the gclient geometry is applied. symbol: `src/gobby/terminals/leases.py::TerminalLeaseRegistry.resize_pty`. test: `tests/terminals/test_lease_authority.py::test_sizing_owner_follows_viewer_precedence`.
- 1.4.2 - A gclient resize of an `external` tmux row pins the window to manual size and resizes it; finalizing the last gclient attachment unsets `window-size`; native rows are resized through the PTY without the pin. file: `src/gobby/servers/websocket/terminal_sizing.py`. test: `tests/servers/test_terminal_ws_resize.py::test_external_tmux_row_resizes_and_releases`.
- 1.4.3 - The web hook sends `viewer: "web"` on attach and keeps sending `terminal_resize` on fit. file: `web/src/hooks/tmuxSessionMessages.ts`. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::attach declares the web viewer`.

### 1.5 Make tmux spawns honour their request and report their real geometry [category: code]
`kind: deliverable`

Targets:
- `src/gobby/terminals/tmux_runtime.py::*` — scope-reason: `TmuxTerminalRuntime.prepare_spawn` threads the validated rows/cols through and reads back the created pane size
- `src/gobby/agents/tmux/session_manager.py::TmuxSessionManager.create_session`
- `src/gobby/agents/tmux/session_activation.py::activate_session`
- `tests/terminals/test_tmux_runtime.py::*` — scope-reason: new test functions appended

Absorbs #21914. `prepare_spawn` validates the requested rows/cols and then drops them while
`activate_session` hardcodes `-x 200 -y 50`, so a `terminal_create` asking for 24x80 gets a
200x50 pane and the row claims 80x24. Decision 16: `activate_session` and `create_session`
gain `rows: int | None, cols: int | None` (defaults 50 and 200, the agent-spawn width kept
on purpose); `prepare_spawn` threads the validated request through; after creation the
runtime reads `display-message -p '#{pane_height} #{pane_width}'` and `manager.set_dims`
records that, never the request. Direction (1) and (2) of the task both hold: the request
is honoured where one exists and the row never lies.

**Acceptance:**

- 1.5.1 - A tmux spawn requesting rows 24 cols 80 creates a pane tmux reports as 80x24 and the terminals row carries 80x24; an agent spawn without a request creates 200x50 and the row carries 200x50; the request-to-row agreement test fails if they diverge. symbol: `src/gobby/terminals/tmux_runtime.py::TmuxTerminalRuntime.prepare_spawn`. test: `tests/terminals/test_tmux_runtime.py::test_spawn_geometry_matches_request_and_row`.

### 1.6 Allow the Ghostty Zig dependency host in managed sandbox builds [category: config]
`kind: deliverable`

Targets:
- `src/gobby/agents/sandbox_policy.py::*` — scope-reason: `allowed_domains` reads the table from the new module; the package-registry domain table gains the narrowly scoped `deps.files.ghostty.org` entry next to the existing registry hosts
- `src/gobby/agents/sandbox_domains.py`
- `tests/agents/test_sandbox_policy.py::*` — scope-reason: new test functions appended

Absorbs #21932. The vendored Ghostty Zig build fetches from `deps.files.ghostty.org`; managed
sandbox builds of `gobby-client`/`vt-engine` fail `blocked-by-allowlist` with a clean Zig
cache. Add that single host to the package-registry domain set that `allow_package_registries`
enables (project or build scope only), and nothing else; the config model's field
description already covers registries and needs no change. Codex worktree agents building
the Rust workspace in this plan depend on it, which is why it is first in P1's dispatch order.

`sandbox_policy.py` is at 885 lines: the outbound domain tables (git forges, package
registries and the new Ghostty host) move into `sandbox_domains.py`, and `allowed_domains`
reads them from there.

**Acceptance:**

- 1.6.1 - With `allow_package_registries` on, the SRT policy allows `deps.files.ghostty.org` and still denies an unrelated control host; with it off both are denied. symbol: `src/gobby/agents/sandbox_policy.py::allowed_domains`. test: `tests/agents/test_sandbox_policy.py::test_ghostty_dependency_host_grant`.

## P2: Client data model, tab sets and sizing
`kind: framing`

**Goal**: gclient holds a typed sidebar model, per-project tab sets restored from snapshots,
and sizes every shown pane.

### 2.1 Build the typed roster and the sidebar model [category: code] (depends: 1.1, 5.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/daemon/mod.rs::*` — scope-reason: `RosterEntry` becomes typed; the `Daemon` trait gains `projects`, `source_status`, `worktrees`, `sessions`, `agent_runs`
- `crates/gclient/src/daemon/rest.rs::RestClient`
- `crates/gclient/src/daemon/projects.rs`
- `crates/gclient/src/app/sidebar_model.rs`
- `crates/gclient/src/app/attention.rs::*` — scope-reason: `parse_prompt` reads the typed `attention` field
- `crates/gclient/src/app/live.rs::fetch_attention`
- `crates/gclient/src/app/live.rs::install_live_rows`
- `crates/gclient/src/app/live.rs::apply_live_event`
- `crates/gclient/src/app/live.rs::reconcile_subscribe_first`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: `AttentionState.entries` becomes `Vec<RosterEntry>` and `Workspace` gains `sidebar: SidebarModel`; the sidebar accessors leave for the new module
- `crates/gclient/src/ui/chrome.rs::WorkspaceView`
- `crates/gclient/src/ui/chrome.rs::row_state`
- `crates/gclient/tests/mock_daemon/mod.rs::*` — scope-reason: default responses for the five new routes
- `crates/gclient/tests/sidebar_model.rs`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended

`daemon/projects.rs` adds typed rows parsed with serde (unknown fields ignored):
`ProjectRow {id, name, display_name, checkout: Option<Checkout {machine_id, root_path}>,
session_count, last_activity_at}`, `SourceStatus {current_branch, ahead, behind, repo_path,
worktree_count}`, `WorktreeRow {id, project_id, machine_id, task_id, branch_name,
worktree_path, base_branch, agent_session_id, status, workspace_role}`, `SessionRow {id, ref,
title, source, status, git_branch, machine_id, agent_run_id}`, `RunRow {run_id, agent_name,
provider, model, status, task_id, terminal_id, worktree_id, machine_id}`. `RosterEntry`
becomes typed: `entry_id, run_id, session_id, lifecycle_status, attention: Option<Attention
{attention_id, kind, reason, fingerprint, seen_at}>, task: Option<TaskRef {id, ref}>,
provider, model, terminal: Option<TerminalRef {terminal_id, backend}>, tmux_session_name,
last_activity_at` (`parse_prompt` reads the typed `attention`).

`app/sidebar_model.rs`:

```rust
pub struct SidebarModel { pub local_machine: String, pub machines: Vec<String>,
    pub projects: Vec<ProjectEntry>, pub agents: Vec<AgentEntry>, pub git_refreshed_at: Instant }
pub struct ProjectEntry { pub project_id: String, pub name: String, pub root_path: Option<PathBuf>,
    pub branch: Option<String>, pub ahead: Option<u32>, pub behind: Option<u32>,
    pub worktrees: Vec<WorktreeEntry>, pub collapsed: bool, pub state: RowState }
pub struct WorktreeEntry { pub worktree_id: String, pub branch: String, pub path: PathBuf,
    pub task_ref: Option<String>, pub role: String, pub state: RowState }
pub struct AgentEntry { pub entry_id: String, pub project_id: String, pub machine_id: String,
    pub terminal_id: String, pub backend: String, pub name: String, pub provider: String,
    pub model: Option<String>, pub task_ref: Option<String>, pub worktree_id: Option<String>,
    pub state: RowState, pub attention: Option<Attention>, pub last_activity_at: Option<String> }
pub fn build(inputs: &SidebarInputs) -> SidebarModel;
pub fn agent_state(entry: &RosterEntry, pane: Option<&Pane>) -> RowState;
```

`build` joins: projects from `/api/projects` (hidden names excluded, `Personal` last),
`SourceStatus` per project (refreshed every 10 s while the sidebar is expanded and on every
`worktree_event`/`project_event`), worktrees per project, agents = roster entries whose
`terminal` is set, joined to sessions (by `session_id`) or runs (by `run_id`) for
`project_id`, `ref`, `title`, `machine_id`, `worktree_id`. `agent_state`: `attention` set →
`Attention`; `lifecycle_status` in `running|active|awaiting_*` with `pane.new_output && live`
→ `Working`; `new_output` → `Unseen`; else `Idle`. A project's state is the max-priority
state of its agents (blocked > done > working > idle), like herdr's collapsed parent. Terminal
rows for the focused project still come from `/api/terminals?project_id=`; `fetch_roster`
runs for the focused project only. WebSocket subscriptions gain `project_event`,
`worktree_event`, `session_event`; each triggers the corresponding refetch (coalesced to one
per render tick), and every attention refetch drops roster entries the daemon no longer
returns. `WorkspaceView` gains `sidebar(&self) -> &SidebarModel` and
`focused_project(&self) -> Option<&str>`; `roster_terminal_ids`/`attention_entry_ids` remain
for the tab and pane paths. `row_state` delegates to `agent_state`.

`app/mod.rs` is at 942 lines: the `sidebar` accessors and the attention projection move
into `sidebar_model.rs`, and `mod.rs` keeps only the field and the delegating methods.

**Acceptance:**

- 2.1.1 - A roster entry with `attention == null` renders `idle`/`working`, only an entry with `attention` set renders `blocked`, and an entry without a `terminal` never becomes an agent row. symbol: `crates/gclient/src/app/sidebar_model.rs::agent_state`. test: `crates/gclient/tests/sidebar_model.rs::agent_state_follows_attention_and_terminal`.
- 2.1.2 - From mock responses for projects, status, worktrees, sessions, runs and the roster, `build` yields projects with branch and ahead/behind, worktree children with task refs, and agents grouped to the right project with the joined ref, title and machine id. symbol: `crates/gclient/src/app/sidebar_model.rs::build`. test: `crates/gclient/tests/sidebar_model.rs::build_joins_projects_worktrees_and_agents`.
- 2.1.3 - A `worktree_event` or `project_event` on the live socket refetches the affected project's status and worktrees once per tick; stale roster entries the daemon no longer returns are dropped on every attention refetch. symbol: `crates/gclient/src/app/live.rs::apply_live_event`. test: `crates/gclient/tests/client_loop.rs::sidebar_model_follows_daemon_events`.

### 2.2 Give each project its tab set, restore the snapshot, open one shell on first run [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/project_tabs.rs`
- `crates/gclient/src/ui/chrome.rs::Chrome`
- `crates/gclient/src/ui/chrome.rs::Tab`
- `crates/gclient/src/ui/chrome.rs::Chrome::open_split`
- `crates/gclient/src/ui/chrome.rs::Chrome::compute_view`
- `crates/gclient/src/persist.rs::*` — scope-reason: `WorkspaceSnapshot` gains tabs with layouts and the client-wide `session.json` joins it
- `crates/gclient/src/app/persistence.rs::*` — scope-reason: `persist_workspace` writes the per-project snapshot from the tab set; `restore_project` rebuilds it
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: `sync_live_chrome` only reaps vacated slots and never opens panes
- `crates/gclient/src/app/live_loop/projects.rs`
- `crates/gclient/src/app/live_loop.rs::run_live_loop`
- `crates/gclient/src/views/mod.rs::run_ready`
- `crates/gclient/tests/persist.rs::*` — scope-reason: new round-trip cases
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended

`Chrome.tabs`/`active_tab` become a `TabSet {tabs: Vec<Tab>, active_tab: usize}` per project in
`project_tabs.rs`: `ProjectTabs {sets: BTreeMap<String, TabSet>, focused: Option<String>}`
with `focus(project_id)`, `set()`/`set_mut()`, and `Chrome::tabs()` returning the focused
set so the tab bar, `open_split`, `close_focused`, `focus_pane`, `reveal_pane`,
`compute_view` and the mouse hit map keep their shape. A tab opened from an agent row of
project P joins P's set; `Placement::Tab` spawns into the focused project's checkout
(`cwd = root_path`, or the worktree path when a worktree row is focused).

Snapshot (`~/.gobby/client/<project_id>/workspace.json`) becomes `WorkspaceSnapshot {project_id,
tabs: Vec<TabSnapshot {title, layout: LayoutNode, focused: Option<String>}>, active_tab,
focused_terminal_id}`; `~/.gobby/client/session.json` stores `{focused_project, sidebar:
{collapsed, width, section_split, machine_filter}}`. Restore rebuilds each tab's layout from
`LayoutNode`, skipping terminals the roster no longer has (an empty tab is dropped); a project
without a snapshot spawns one shell tab (decision 7). `sync_live_chrome` only reaps slots whose
pane left the workspace; it never opens panes. Focusing a project row saves the outgoing set's
snapshot and restores or seeds the incoming one (`focus_project` in the new
`live_loop/projects.rs`).

**Acceptance:**

- 2.2.1 - Starting against a daemon with eight roster terminals and no snapshot opens exactly one tab holding one freshly spawned shell in the focused project; the eight terminals appear only as agent rows. symbol: `crates/gclient/src/app/live_loop/actions.rs::sync_live_chrome`. test: `crates/gclient/tests/client_loop.rs::first_run_opens_one_shell_and_never_auto_opens`.
- 2.2.2 - Two projects keep separate tab sets: focusing the second project swaps the tab bar, a tab opened from its agent row lands in its set, and refocusing the first restores its tabs and active tab. symbol: `crates/gclient/src/app/project_tabs.rs::ProjectTabs`. test: `crates/gclient/tests/client_loop.rs::tab_sets_follow_the_focused_project`.
- 2.2.3 - A snapshot with two tabs (one split) round-trips through `save_snapshot`/`load_snapshot` and is rebuilt on startup with the split intact and a vanished terminal dropped. symbol: `crates/gclient/src/persist.rs::WorkspaceSnapshot`. test: `crates/gclient/tests/persist.rs::snapshot_restores_tab_layouts`.

### 2.3 Every shown pane renders its frame and sizes its terminal [category: code] (depends: 1.4, 2.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop.rs::run_live_loop`
- `crates/gclient/src/app/live_loop.rs::resize_live_workspace`
- `crates/gclient/src/app/run_loop.rs::propagate_geometry`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: the slot-change hook calls resize after every open, close, split and project focus
- `crates/gclient/src/app/attach.rs::*` — scope-reason: the attach message declares `viewer: "gclient"`
- `crates/gclient/src/app/live_attach.rs::*` — scope-reason: same declaration on the live attach path
- `crates/gclient/src/app/pane.rs::*` — scope-reason: the pane records `sized_by`
- `crates/gclient/src/views/grid.rs::render`
- `crates/gclient/src/ui/panes.rs::render_panes`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended

Carries mouse-parity 4.4. Every live capture in the parity review shows only the focused
pane painted while the other attached panes stay blank; herdr paints all panes. Static
tracing rules out the frame pump: both frame-source `recv` paths are cancellation-safe
(mpsc and broadcast), `recv_workspace_frame` records any pane's frame, and the host's
`publish_frame` sends to every attachment. It confirms one defect and two silent failure
modes, all fixed here:

- Initial geometry is never propagated: `resize_live_workspace` runs only from the SIGWINCH
  branch of `run_live_loop`, so a pane keeps the viewport the attach reply chose (24x80
  defaults on the proxy path) until the user resizes the window. `run_live_loop` calls
  `resize_live_workspace` after the initial `sync_live_chrome` and again whenever a slot
  opened, closed, split or changed project, so `SetViewport` always carries the pane's
  `inner_rect`.
- `grid::render` returns silently when `cells.len() != width * height` or when no frame has
  arrived. A size mismatch is a protocol error: the pane body shows
  `frame_size_mismatch WxH/N` (through `render_panes`, the same muted text style as
  `render_empty`) and `render_panes` logs the pane id, dimensions and cell count at debug
  level when it detects one. A pane with a live source and no frame yet shows
  `waiting for frames` in its body instead of nothing.
- The invariant gets a pin: two scripted sources feeding two panes in one tab through the
  shared render path, asserting both bodies carry their frame text after each pump.

Decision 14 extends `propagate_geometry`: every live pane sends `SetViewport` and,
regardless of backend or hold state, a `terminal_resize` carrying `viewer: "gclient"`;
`terminal_resize_result {applied: false, owner_viewer}` marks the pane
`sized_by: Some(viewer)` and the pane body keeps the viewport crop with a one-line
`sized by web` note in the muted style. Closing a pane or detaching sends nothing extra;
the daemon's finalize path (1.4) restores sizing. On exit the live loop detaches every pane
before tearing down the terminal so tmux windows return to automatic size.

If the live rows still show a blank pane after this lands, the body text and the debug log
name the failing side, and that fix is found work inside this task (rung 1), not a new task.

**Acceptance:**

- 2.3.1 - After startup and after every slot change each live pane has received `SetViewport` with its inner rect and a `terminal_resize` of the same size with `viewer: "gclient"`, for tmux and native panes alike, held or observing (the scripted daemon records the messages). symbol: `crates/gclient/src/app/run_loop.rs::propagate_geometry`. test: `crates/gclient/tests/client_loop.rs::every_shown_pane_sizes_its_terminal`.
- 2.3.2 - With two panes fed by two frame sources both bodies render their frame text; a mismatched frame renders `frame_size_mismatch`; a frameless live pane renders `waiting for frames`; a `terminal_resize_result` with `applied: false` renders the `sized by web` note and keeps the crop. symbol: `crates/gclient/src/views/grid.rs::render`. test: `crates/gclient/tests/client_loop.rs::both_panes_render_and_report_size_owner`.

## P3: Sidebar rendering and interaction
`kind: framing`

**Goal**: The sidebar shows projects and agents the way herdr 0.9.0 shows spaces and agents,
restyled to the Gobby contract, with mouse and keyboard parity.

### 3.1 Render the projects section [category: code] (depends: 6.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/sidebar/projects.rs`
- `crates/gclient/src/ui/sidebar.rs::*` — scope-reason: `render_roster`/`render_attention` are replaced by the two section renderers; geometry helpers keep their names
- `crates/gclient/src/ui/sidebar_rows.rs::*` — scope-reason: `RowKind`, `SidebarRow` and `row_line` gain depth, second line and group toggle
- `crates/gclient/src/ui/hit.rs::*` — scope-reason: `Hit::Roster`/`Hit::Attention` become `Hit::Project`, `Hit::Worktree`, `Hit::Agent`, `Hit::GroupToggle`, `Hit::ProjectsNew`, `Hit::ProjectsMenu`, `Hit::MachineFilter`; `SidebarSection::{Projects, Agents}`
- `crates/gclient/src/ui/hit/tests.rs::*` — scope-reason: hit cases renamed and extended
- `crates/gclient/src/ui/chrome.rs::SidebarState`
- `crates/gclient/src/ui/status.rs::state_label`
- `crates/gclient/src/app/live_loop/mouse/pointer.rs::*` — scope-reason: `down` routes the new project and worktree hits; roster drag becomes project drag; worktree rows are not draggable
- `crates/gclient/src/app/live_loop/mouse/wheel.rs::*` — scope-reason: section names
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: `handle_live_action` dispatches the project navigation actions
- `crates/gclient/src/ui/keymap.rs::*` — scope-reason: `NavigateUp/Down` walk project rows, `SwitchTerminal(n)` becomes `SwitchProject(n)`, new `NewProject`, `PreviousProject`, `NextProject`, `ToggleGroup`; the name and action tables leave for the new module
- `crates/gclient/src/ui/keymap/names.rs`
- `crates/gclient/tests/parity/sidebar.rs::*` — scope-reason: the roster cases become project cases; the module doc drops "worktree grouping" from the dropped list
- `crates/gclient/tests/parity/chrome.rs::*` — scope-reason: `expanded_sidebar_workspace_rows_show_state_before_name_without_numbers` loses its `#[deferred]` marker
- `crates/gclient/tests/fixtures/screens/roster_attention.txt`
- `crates/gclient/tests/fixtures/screens/projects_agents.txt`
- `crates/gclient/tests/screens.rs::*` — scope-reason: the `roster_attention` state becomes `projects_agents`

Port of herdr's workspace sidebar rows onto `SidebarModel.projects`: header ` projects`
(overlay0, bold, `WORKSPACE_HEADER_ROWS = 2`), footer ` new` left and `menu` right (footer
only with mouse capture; `menu` opens the global menu of 5.1), row 1 = state icon + name
(bold `text` when focused, else `subtext0`), row 2 = branch + `↑n ↓m` (`mauve` when focused
else `overlay0`; ahead in the success role, behind in the warning role, never hue-only: the
arrows are the cue), worktree children indented with `   ├─ `/`   └─ ` in `overlay0`
showing the branch (`worktree/` prefix stripped) and the task ref, a `▸`/`▾` accent toggle
at the row's right edge when a project has worktrees, collapsed parents showing the group's
max-priority state, the `~` row for the Personal project. Focused project row uses
`active_row_bg`; Navigate cursor uses `selection_bg`. Click focuses the project
(`focus_project`), click on a worktree row focuses its parent and opens (or reveals) a shell
tab in that checkout, drag reorders projects and persists the order in `session.json`, wheel
scrolls, the divider and section drags keep their gestures. Collapsed rail shows `{n:<2}{dot}`
per project above the divider. Keyboard: `navigate_up/down` (Navigate mode) walk project and
worktree rows, `1-9` focus the nth project, `enter` focuses, `prefix+shift+n` = `new_project`.
The old golden fixture is deleted and the `projects_agents` golden is recorded in its place.

`keymap.rs` is at 947 lines: the action name table and the default binding table move into
`crates/gclient/src/ui/keymap/names.rs`, and `keymap.rs` keeps the `Keymap` type, `build`
and the override loader.

**Acceptance:**

- 3.1.1 - A model with two projects, one carrying a worktree child and ahead/behind counts, renders the two-line rows, the indented child with its task ref, the group toggle and the focused-row background exactly as the `projects_agents` golden pins; the collapsed rail lists the projects. symbol: `crates/gclient/src/ui/sidebar/projects.rs`. test: `crates/gclient/tests/screens.rs::projects_agents_golden`.
- 3.1.2 - Clicking a project row focuses it and swaps the tab set, clicking its worktree child opens a shell tab in the worktree path, the group toggle collapses and expands, drag reorder persists, and `navigate_down` plus `enter` focus the next project. symbol: `crates/gclient/src/app/live_loop/mouse/pointer.rs::down`. test: `crates/gclient/tests/parity/sidebar.rs::project_rows_focus_toggle_and_reorder`.
- 3.1.3 - `expanded_sidebar_workspace_rows_show_state_before_name_without_numbers` passes with its `#[deferred]` marker removed and `parity::deferred_cases_are_still_red` stays green. file: `crates/gclient/tests/parity/chrome.rs`. test: `crates/gclient/tests/parity/chrome.rs::expanded_sidebar_workspace_rows_show_state_before_name_without_numbers`.

### 3.2 Render the agents section with the machine filter [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/sidebar/agents.rs`
- `crates/gclient/src/ui/sidebar_rows.rs::*` — scope-reason: agent rows carry the two-line layout and machine token
- `crates/gclient/src/ui/chrome.rs::attention_pane`
- `crates/gclient/src/ui/chrome.rs::attention_label`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: `PreviousAttention/NextAttention/FocusAttention` walk agent rows; new `CycleMachineFilter`, `ToggleAgentSort`
- `crates/gclient/src/app/live_loop/mouse/pointer.rs::*` — scope-reason: `Hit::Agent` reveals the pane and opens the respond dialog for a blocked row; `Hit::MachineFilter` cycles
- `crates/gclient/src/prefs.rs::*` — scope-reason: `[ui] agent_sort = "grouped" | "priority"`
- `crates/gclient/src/ui/settings.rs::*` — scope-reason: `ClientPrefs.agent_sort` and its settings row
- `crates/gclient/tests/parity/sidebar.rs::*` — scope-reason: attention cases become agent cases
- `crates/gclient/tests/attention_flow.rs::*` — scope-reason: the jump test targets an agent row
- `crates/gclient/tests/startup.rs::*` — scope-reason: the prefs round-trip case for `agent_sort` joins the existing prefs cases
- `crates/gclient/tests/terminal_naming.rs::*` — scope-reason: labels come from session ref and title

Port of herdr's agent sidebar onto `SidebarModel.agents` filtered to the focused project and
the machine filter: rule row, header ` agents` with the right-aligned sort label (`grouped`
= roster order by tab, `priority` = blocked > done > working > idle then last activity) and,
left of it, the machine filter label (`local` default, `all`, or a machine name; hidden when
the model knows one machine). Row 1 = state icon + name (`title` or `provider #ref`, bold),
row 2 = `provider · model · task ref` with the tab token when the project has more than one
tab and the machine token when the filter is `all`. State word per `state_label`. Click
reveals the pane (opening a tab in the project's set when it is not shown) and, for a
`blocked` row, opens the respond dialog; `previous_attention`/`next_attention`/
`focus_attention` walk blocked rows first, then all agent rows. The collapsed rail lists
agents below the divider. The attention roster join uses `attention_pane` (session-keyed,
memory 2c8899dc). The `all` view is fed by the local daemon only until the hub roster of
D1 arrives; rows from other machines are joined and rendered the same way once they exist.

**Acceptance:**

- 3.2.1 - Agents of another project or another machine are hidden under the default filter, appear under `all` with the machine token, and the sort toggle reorders blocked rows first. symbol: `crates/gclient/src/ui/sidebar/agents.rs`. test: `crates/gclient/tests/parity/sidebar.rs::agent_rows_follow_project_and_machine_filter`.
- 3.2.2 - Clicking a blocked agent row focuses its pane and opens the respond dialog; clicking an idle row only focuses; the label shows the session ref and title and never a raw UUID. symbol: `crates/gclient/src/ui/chrome.rs::attention_pane`. test: `crates/gclient/tests/attention_flow.rs::agent_row_click_jumps_and_labels_the_session`.
- 3.2.3 - `agent_sort` round-trips through prefs.toml and the settings row, and an unknown value is rejected with the line-numbered prefs error. file: `crates/gclient/src/prefs.rs`. test: `crates/gclient/tests/startup.rs::agent_sort_pref_round_trips`.

### 3.3 Add project and worktree actions and dialogs [category: code] (depends: 1.2, 1.3, 3.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/dialogs/project.rs`
- `crates/gclient/src/ui/dialogs.rs::*` — scope-reason: `Dialog::{NewProject, NewWorktree, OpenWorktree, RemoveWorktree}` and `CloseTarget::{Project, WorktreeGroup}` join `ConfirmClose`
- `crates/gclient/src/app/live_loop/projects.rs`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: `handle_live_action` dispatches the project and worktree dialog actions
- `crates/gclient/src/daemon/projects.rs`
- `crates/gclient/src/daemon/mod.rs::*` — scope-reason: `Daemon` gains `init_project`, `create_worktree`, `delete_worktree`
- `crates/gclient/tests/parity/chrome.rs::*` — scope-reason: `workspace_creation_dialog_renders_new_workspace_title` loses its marker
- `crates/gclient/tests/parity/dialogs.rs::*` — scope-reason: `confirm_close_text_reports_parent_group_scope` loses its marker
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended

Port of herdr's workspace creation dialog (`new workspace` title, path input with `~`
expansion and tab completion of directories) as `Dialog::NewProject`: enter posts
`/api/projects/init`, the returned project is focused with one shell tab; errors show the
daemon reason inline. `Dialog::NewWorktree` (branch name, base branch defaulting to the
current branch) posts 1.2's route and focuses the new child row with a shell tab in it.
`Dialog::OpenWorktree` lists the project's registry worktrees not yet shown and opens a shell
tab in the chosen path. `Dialog::RemoveWorktree` confirms (`delete worktree checkout? <branch>
— 1 tab, 2 panes`), kills the gobby-owned terminals in it, then deletes through the daemon.
`close project` uses `ConfirmClose {target: Project}` with herdr's group text (`close worktree
group? gobby — 2 workspaces, 3 panes` when children exist, else `close project? gobby — 2
tabs, 3 panes`) and terminates every tab's gobby-owned terminals (decision 10). `rename
project` sets a client-side label stored in `session.json`.

**Acceptance:**

- 3.3.1 - The new-project dialog renders herdr's title and, on enter, calls the init route and focuses the created project with one shell tab; `workspace_creation_dialog_renders_new_workspace_title` passes unmarked. symbol: `crates/gclient/src/ui/dialogs/project.rs`. test: `crates/gclient/tests/client_loop.rs::new_project_dialog_inits_and_focuses`.
- 3.3.2 - New, open and remove worktree flows call the daemon routes and update the child rows; remove refuses to proceed until the terminals inside are gone. symbol: `crates/gclient/src/app/live_loop/projects.rs`. test: `crates/gclient/tests/client_loop.rs::worktree_flows_round_trip_the_daemon`.
- 3.3.3 - Closing a project with worktree children shows the group-scoped confirm text and terminates only gobby-owned terminals; `confirm_close_text_reports_parent_group_scope` passes unmarked. file: `crates/gclient/tests/parity/dialogs.rs`. test: `crates/gclient/tests/parity/dialogs.rs::confirm_close_text_reports_parent_group_scope`.

## P4: Modal routers and keymap overrides
`kind: framing`

**Goal**: The modal input routers and keymap loading that the menus and dialogs depend on
(carried from `.gobby/plans/gclient-mouse-parity.md` 4.2 and 4.3 with dependencies rebased on
this plan), plus the nested-tmux prefix stopgap.

### 4.1 Modal input routers and the live settings toggle [category: code]
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/modal_input.rs`
- `crates/gclient/src/app/live_loop/mouse/mod.rs::*` — scope-reason: the modal branch of `route_mouse` maps settings-row clicks and wheel
- `crates/gclient/src/app/live_loop.rs::route_live_input`
- `crates/gclient/src/app/live_loop.rs::run_live_loop`
- `crates/gclient/src/app/run_loop.rs::route_scripted_input`
- `crates/gclient/src/ui/chrome.rs::Chrome`
- `crates/gclient/src/ui/settings.rs::*` — scope-reason: settings rows gain click activation and the live mouse-capture toggle
- `crates/gclient/src/teardown.rs::*` — scope-reason: `TerminalGuard` implements `MouseCaptureSwitch`
- `crates/gclient/src/views/mod.rs::run_ready`
- `crates/gclient/src/startup.rs::start_session`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended
- `crates/gclient/tests/parity/dialogs.rs::*` — scope-reason: new test functions appended

`route_live_input` today special-cases `Mode::Respond` and sends every other mode to
`resolve_chord`, so the settings dialog, keybind help, navigator, confirm-close, rename and
resize modes render but take no keys. `modal_input.rs` adds
`route_modal_key<W: WorkspaceView>(ws: &W, chrome: &mut Chrome, key: &KeyInput) -> ModalOutcome`
with `ModalOutcome { Consumed, Close, Focus(PaneId), Action(Action), Confirm(CloseTarget), Commit(RenameKind, String), Passthrough }`,
called from `route_live_input` and `route_scripted_input` before `resolve_chord` for every
mode except `Terminal`, `Prefix`, `Copy` and `Respond` (which keeps `route_response_input`):

- `KeybindHelp`: printable keys edit `keybind_help.query`, up/down/page keys scroll, esc
  closes.
- `Navigator`: printable keys edit `navigator.query`, up/down move `selected`, tab cycles
  `filter`, enter yields `Focus` for a terminal row or the attention jump for an attention
  row, esc closes.
- `Settings`: up/down move `selected`; enter or space toggles a boolean row or cycles
  `theme` and `right_click_passthrough_modifier`; left/right step `sidebar_width` within the
  sidebar bounds; every change is applied to `chrome.prefs` at once and written with the
  prefs writer, a write error becoming `status_message`; esc closes. Toggling `mouse capture`
  sets `chrome.pending_mouse_capture: Option<bool>`; `run_live_loop` takes a
  `&mut dyn MouseCaptureSwitch` (a one-method trait implemented by `TerminalGuard` over
  `set_mouse_capture`, and by a recording stub in tests) and applies the pending value after
  each input event, so `run_ready` passes the guard it already owns through `start_session`.
  Mouse in `Mode::Settings` (herdr `handle_settings_mouse`): the modal branch of
  `route_mouse` maps left Down on `Hit::SettingsRow(i)` to `settings.selected = i` followed by
  the activation enter performs (`activate_settings_row` in `modal_input.rs`, shared by the
  key and mouse paths), wheel over `Hit::SettingsDialog` moves `selected` by one, and a Down
  outside the dialog closes it like esc.
- `ConfirmClose`: `y`/enter yields `Confirm(target)` (terminal, pane or every pane of the
  tab, each through `terminate_live_terminal`), `n`/esc cancels.
- `Rename`: printable keys insert at `cursor`, backspace/delete/left/right/home/end edit,
  enter yields `Commit(kind, value)` (applied by the existing rename arms), esc cancels.
- `Resize`: `h`/`j`/`k`/`l` and the arrows call `tab.layout.resize_focused` with a 0.05 step
  in that direction, enter/esc return to `Terminal`.
- `Navigate`: up/down move `sidebar.selected`, enter yields `Focus` for the selected sidebar
  row, esc returns to `Terminal`.

Closing a modal always restores `Mode::Terminal` and clears `chrome.dialog`. `live_loop.rs`
gains the one dispatch call and the pending-capture check.

**Acceptance:**

- 4.1.1 - Each modal mode consumes its keys as listed: help and navigator filter on typing, the navigator's enter focuses the row, confirm-close accepts and cancels, rename commits the edited text, resize steps the ratio, navigate moves the sidebar selection. symbol: `crates/gclient/src/app/live_loop/modal_input.rs`. test: `crates/gclient/tests/parity/dialogs.rs::modal_keys_drive_every_mode`.
- 4.1.2 - Toggling `mouse capture` in settings flips capture on the guard immediately and persists `mouse_capture` in the prefs file; toggling it back re-enables capture. symbol: `crates/gclient/src/app/live_loop.rs::run_live_loop`. test: `crates/gclient/tests/client_loop.rs::settings_toggle_switches_mouse_capture_and_saves_prefs`.
- 4.1.3 - Clicking a settings row selects and activates it with the same effect as enter (a boolean flips, `theme` cycles) and a click outside the dialog closes it. symbol: `crates/gclient/src/app/live_loop/modal_input.rs`. test: `crates/gclient/tests/parity/dialogs.rs::settings_rows_respond_to_clicks`.

### 4.2 Load keymap overrides at startup and on reload [category: code] (depends: 5.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/startup.rs::Ready`
- `crates/gclient/src/startup.rs::prepare_at`
- `crates/gclient/src/views/mod.rs::run_ready`
- `crates/gclient/src/ui/keymap/names.rs`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: `handle_live_action` reloads the keymap on `reload_config`
- `crates/gclient/tests/startup.rs::*` — scope-reason: new test functions appended
- `crates/gclient/tests/keymap.rs::*` — scope-reason: new test functions appended

`Keymap::load_overrides` and `default_override_path` exist and are covered by tests, but
`Chrome::new` always installs `Keymap::defaults()` and nothing calls the loader, so
`~/.gobby/client/keymap.toml` is dead. `prepare_at` resolves the override file
(`prefs.keybinds` when non-empty, relative paths under the gobby home, else
`default_override_path()`), loads it with `Keymap::load_overrides` into `Ready::keymap`, and
turns a `KeymapError` into `StartupError::Keymap(String)` naming the path and the parse
error, failing before the alternate screen like a bad prefs file; a missing file is the
default keymap. `run_ready` assigns `chrome.keymap = ready.keymap`. The `ReloadConfig` arm
reuses the same resolution and keeps the current keymap on error. `switch_terminal` stays
unbound by default; an override binds it. The name table in `keymap/names.rs` carries this
plan's actions (`new_project`, `previous_project`, `next_project`, `switch_project`,
`toggle_group`, `cycle_machine_filter`, `toggle_agent_sort`) so an override can bind them.

**Acceptance:**

- 4.2.1 - With an override file that rebinds `help` and `new_project`, `Ready::keymap` carries the new chords and the live chrome resolves them after startup and after `reload_config`; a malformed file is a `StartupError::Keymap` naming the path; a missing file yields the defaults. symbol: `crates/gclient/src/startup.rs::prepare_at`. test: `crates/gclient/tests/startup.rs::keymap_overrides_load_or_fail_loud`.
- 4.2.2 - `Keymap::load_overrides` on the resolved path is the only keymap source at startup: the override chord wins over the default chord for the same action. file: `crates/gclient/src/views/mod.rs`. test: `crates/gclient/tests/keymap.rs::override_chord_replaces_default_chord`.

### 4.3 Break the nested-tmux prefix lockout [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/keymap.rs::*` — scope-reason: `Keymap::defaults` takes the effective prefix; `DEFAULT_PREFIX` is replaced by the prefix module's function; `build` accepts the effective prefix
- `crates/gclient/src/ui/keymap/prefix.rs`
- `crates/gclient/src/tmux_identity.rs::current`
- `crates/gclient/src/startup.rs::prepare_at`
- `crates/gclient/src/key_input.rs::*` — scope-reason: literal-prefix passthrough and the `ctrl+\` escape join terminal mode
- `crates/gclient/src/ui/keybind_help.rs::*` — scope-reason: entries render the live prefix
- `crates/gclient/src/ui/status.rs::render_status_line`
- `crates/gclient/tests/keymap.rs::*` — scope-reason: new test functions appended
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended

Absorbs #21923 (decision 15, a stopgap until #21357 retires tmux). At startup
`tmux_identity::current()` decides `nested`; the effective prefix is the prefs `[keymap]
prefix` when set, else `ctrl+]` when nested, else herdr's `ctrl+b`. The nested branch is one
function, `default_prefix(nested)`, marked `TODO(#21357)` so the native flip deletes it and
the default returns to `ctrl+b` everywhere. In terminal mode `prefix prefix` writes the
prefix chord's bytes to the held pane (herdr's `ctrl+b ctrl+b`), and `ctrl+\` dispatches
`Action::ReleaseControl` before any forwarding, so a held pane always has a keyboard exit.
The keybind help renders labels from the live prefix and the status line shows
`prefix ctrl+]` while nested.

`keymap.rs` is still near the ceiling after 3.1's table move: the prefix selection
(`default_prefix`, the nested detection glue and its `TODO(#21357)`) moves into
`crates/gclient/src/ui/keymap/prefix.rs`, and `keymap.rs` only calls it.

**Acceptance:**

- 4.3.1 - With an outer tmux identity present and no override, the effective prefix is `ctrl+]` and help, settings, tab switching and release-control are reachable; with an override the override wins; without nesting it stays `ctrl+b`. symbol: `crates/gclient/src/ui/keymap/prefix.rs`. test: `crates/gclient/tests/keymap.rs::nested_tmux_shifts_the_prefix_unless_overridden`.
- 4.3.2 - In a held pane `prefix prefix` reaches the pane as the literal chord and `ctrl+\` releases control without the prefix. file: `crates/gclient/src/key_input.rs`. test: `crates/gclient/tests/client_loop.rs::held_pane_has_literal_prefix_and_keyboard_escape`.

## P5: Context menus
`kind: framing`

**Goal**: Right-click opens the right menu everywhere; closing panes and tabs works from the
mouse (takes over mouse-parity 5.1 and 5.2; the sidebar row menus follow in 5.3 once the
rows exist).

### 5.1 Context menu state, items and dispatch for panes, tabs and empty chrome [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/menu.rs`
- `crates/gclient/src/app/live_loop/mouse/mod.rs::*` — scope-reason: `route_mouse` consults the open menu first
- `crates/gclient/src/app/live_loop/mouse/pointer.rs::*` — scope-reason: `right_down` opens the menu on the matching hit and `down` activates or dismisses it
- `crates/gclient/src/app/live_loop/modal_input.rs`
- `crates/gclient/src/ui/chrome.rs::Mode`
- `crates/gclient/src/ui/chrome.rs::Chrome`
- `crates/gclient/src/ui/status.rs::mode_name`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended

`menu.rs`:

```rust
pub enum ContextMenuKind { Pane(PaneId), Tab(usize), Project(String), Worktree(String), Agent(String), Global }
pub enum MenuAction { Act(Action), TogglePassthrough(PaneId), FocusProject(String), OpenWorktreeTab(String),
    NewWorktree(String), OpenWorktree(String), RemoveWorktree(String), ToggleGroup(String), CloseProject(String),
    RenameProject(String), FocusAgent(String), OpenAgentInNewTab(String), Respond(String), MarkSeen(String) }
pub struct MenuItem { pub label: &'static str, pub action: MenuAction, pub enabled: bool }
pub struct ContextMenuState { pub kind: ContextMenuKind, pub anchor: (u16, u16), pub items: Vec<MenuItem>, pub selected: usize, pub item_rects: Vec<Rect> }
pub fn build_menu<W: WorkspaceView>(ws: &W, chrome: &Chrome, kind: ContextMenuKind, anchor: (u16, u16)) -> ContextMenuState;
pub fn menu_hit(state: &ContextMenuState, column: u16, row: u16) -> Option<usize>;
```

The enum carries every kind from day one; this leaf builds the `Pane`, `Tab` and `Global`
item lists and 5.3 fills in the row kinds. Items (lowercase verbs, no punctuation, per the
contract), mirroring herdr's `items()`:

- Pane: `rename pane`, `clear pane name` (when labelled), `swap with focused pane` (when
  another pane is focused), `split right`, `split down`, `zoom`/`unzoom`, `take control`/
  `release control`, `respond` (blocked agent only), `copy mode`, `send right-clicks to pane`/
  `use gclient menu` (`TogglePassthrough`), `close pane`.
- Tab: `new tab`, `rename tab`, `close tab`.
- Global (empty tab-bar space, empty pane area, empty sidebar): `new terminal`, `new tab`,
  `settings`, `keybinding help`, `reload config`, `toggle sidebar`, `detach`
  (`custom_command` stays reserved for #20201; `new project` joins in 5.3).

Right Down on the matching `Hit` (unless passthrough forwards it) opens the menu:
`chrome.menu = Some(build_menu(..))`, `chrome.mode = Mode::ContextMenu` (new variant;
`mode_name` shows `menu`). While open, `route_mouse` consults `menu_hit` first: Moved
highlights the item under the pointer, left Down on an item activates it, any Down outside
closes. `modal_input.rs` handles up/down (clamped, no wrap), enter (activate) and esc
(close). Activation closes the menu and dispatches: `Act(action)` through
`handle_live_action` (`close pane` is the existing `ClosePane` arm, which terminates the
pane's terminal through the daemon and reaps the slot; `close tab` is the `CloseTab` arm
and its confirm-close path), `TogglePassthrough` flips `Pane::right_click_passthrough`, and
`Respond` opens the respond dialog for the pane's attention entry.

**Acceptance:**

- 5.1.1 - Right-clicking a pane, a tab and empty chrome opens the menu with exactly the listed items for that target and state (held vs observe, blocked vs plain, zoomed vs not, labelled vs not). symbol: `crates/gclient/src/app/live_loop/menu.rs::build_menu`. test: `crates/gclient/src/app/live_loop/menu.rs::menus_list_items_per_target_and_state`.
- 5.1.2 - Hover follows the pointer, clicking `close pane` terminates the pane's terminal and reaps the slot, `close tab` runs the confirm-close path, `split right` spawns into a split, keys navigate and activate, and a click outside closes the menu. symbol: `crates/gclient/src/app/live_loop/mouse/pointer.rs::right_down`. test: `crates/gclient/tests/client_loop.rs::context_menu_dispatches_items_and_closes_outside`.

### 5.2 Context menu rendering [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/context_menu.rs`
- `crates/gclient/src/ui/mod.rs::*` — scope-reason: module declaration
- `crates/gclient/src/ui/chrome_render.rs::render_workspace_with`
- `crates/gclient/tests/parity/dialogs.rs::*` — scope-reason: new test functions appended

`context_menu.rs` draws `chrome.menu` in the `Mode::ContextMenu` arm of
`render_workspace_with` (no background dim: the menu is contextual, the workspace stays
readable), matched to herdr's overlay geometry. The popup is anchored at the click cell,
`longest label + 4` wide (floor 14) and `items + 2` tall, flipped left or up when it would
overflow the frame; `render_panel_shell` provides the surface (`panel_bg` fill, `surface0`
border), rows use `text` on the surface, the selected row `accent` foreground with
`REVERSED` and bold, disabled rows `overlay0` with `DIM`, and the row rects are written back
into `ContextMenuState::item_rects` for `menu_hit`. State is carried by text, weight and
reversal, never hue alone; no glyphs, emoji or exclamation marks; the same rules as
`.impeccable.md` lines 177-243 and the existing dialogs. The menu is composited last.

**Acceptance:**

- 5.2.1 - The rendered menu sits at the anchor, flips to stay inside the frame at the right and bottom edges, marks the selected row with accent plus reversal and disabled rows with dim, and its item rects match the drawn rows. symbol: `crates/gclient/src/ui/context_menu.rs`. test: `crates/gclient/tests/parity/dialogs.rs::context_menu_renders_anchored_and_clamped`.

### 5.3 Row context menus and the projects footer menu [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/menu.rs`
- `crates/gclient/src/app/live_loop/mouse/pointer.rs::*` — scope-reason: `right_down` adds the project, worktree, agent and footer menu kinds
- `crates/gclient/src/app/live_loop/projects.rs`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended

`build_menu` gains the row kinds, with the same lowercase-verb labels:

- Project row: `rename`, `close`, `new worktree`, `open worktree…` (git projects),
  `expand`/`collapse` (with children); Worktree row: `rename`, `close`, `delete worktree
  checkout…`.
- Agent row: `focus`, `open in new tab`, `respond` (blocked), `mark seen`, `take control`/
  `release control`, `close terminal`.
- Global gains `new project`; the projects footer `menu` and `Hit::ProjectsMenu` open the
  global menu; right Down on `Hit::Project`, `Hit::Worktree` and `Hit::Agent` opens the
  matching kind.

Activation dispatches through the project helpers of 3.3 (`FocusProject`,
`OpenWorktreeTab`, `NewWorktree`, `OpenWorktree`, `RemoveWorktree`, `ToggleGroup`,
`CloseProject`, `RenameProject`) and the agent helpers of 3.2 (`FocusAgent`,
`OpenAgentInNewTab` opens a new tab in the project's set holding that pane, `Respond`,
`MarkSeen` calls `Daemon::mark_seen` with the entry and attention ids the roster carries).

**Acceptance:**

- 5.3.1 - Right-clicking a project row, a worktree row, an agent row and the projects footer opens the menu with exactly the listed items for that target and state (children vs none, blocked vs plain, held vs observe). symbol: `crates/gclient/src/app/live_loop/menu.rs::build_menu`. test: `crates/gclient/src/app/live_loop/menu.rs::row_menus_list_items_per_target_and_state`.
- 5.3.2 - `new worktree`, `delete worktree checkout…`, `close` on a project, `open in new tab` and `mark seen` dispatch to the project and agent helpers and reach the daemon. symbol: `crates/gclient/src/app/live_loop/projects.rs`. test: `crates/gclient/tests/client_loop.rs::row_menus_dispatch_project_and_agent_actions`.

## P6: Renderer defects
`kind: framing`

**Goal**: No stale cells leak between frames.

### 6.1 Clear stale cells when a pane's frame shrinks or moves [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/views/grid.rs::render`
- `crates/gclient/src/ui/panes.rs::render_panes`
- `crates/gclient/src/app/pane.rs::*` — scope-reason: the pane records the last painted rect
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: new test functions appended

Captures 01 and 02 of the parity review show single glyphs from an earlier frame on
otherwise blank rows of a pane. `render` paints only the cells the frame carries; cells the
previous frame covered but the new one does not (a shrunk viewport, a moved split, a
narrower frame after `SetViewport`) are never cleared. `render_panes` clears the pane's inner
rect with the pane background before `grid::render` paints, and the pane remembers its last
painted rect so a move or shrink clears the vacated area.

**Acceptance:**

- 6.1.1 - After a pane receives a 40x10 frame and then a 20x5 frame, every cell outside the new frame inside the pane rect is blank, and after a split moves the pane the vacated cells are blank. symbol: `crates/gclient/src/ui/panes.rs::render_panes`. test: `crates/gclient/tests/client_loop.rs::stale_cells_are_cleared_on_shrink_and_move`.

## D1 Hub-wide agents across machines
`kind: deferred`

The machine filter's `all` and per-machine views are fed by the local daemon only (3.2);
showing agents that run on other machines needs the hub-wide roster,
`terminal.machine_id` endpoint resolution and attach-capability tokens that #20202 plans.
The acceptance item that would have pinned it here is recorded as 3.2.4 (an agent row of
another machine appears under `all`, attaches through the hub, and shows the machine token).

```yaml
deferral:
  task_ref: "#20202"
  reason: "Agents on other machines need the hub-wide roster, terminal.machine_id endpoint resolution and attach-capability tokens that #20202 plans; this plan ships the model, the filter and the local source only."
  owner: "coordinator"
  original_acceptance_items:
    - 3.2.4
```

## V2 End-to-End Verification
`kind: verification`

End to end against the live daemon: launch `~/.gobby/bin/gclient` (rebuilt and installed via
new inode) in a 160x48 Ghostty window beside herdr 0.9.0 (`/private/tmp/herdr-target/release/herdr`)
and capture pairs with peekaboo for: startup (one shell tab, projects with branch and ahead,
agents of the focused project), a second project focus (tab set swap), agent row click and
respond, right-click on pane/tab/project/agent/empty chrome, close pane and close tab from the
menu, new project dialog, worktree new/open/delete, a split whose terminal fits the pane (tmux
and native), a web UI attach that takes the size, and the collapsed rail. Append the pairs to
`docs/evidence/gclient-herdr-parity-2026-09-07.md`. Automated: `cargo nextest run -p gobby-client`
filters per leaf, `cargo clippy -p gobby-client --all-targets -- -D warnings`,
`cargo fmt -p gobby-client -- --check`, `crates/gclient/tests/parity` (deferred markers gone,
inventory hash updated), Python route tests per P1 leaf, `tests/e2e/test_terminal_client_stack.py`.

## V1 Plan Changelog
`kind: framing`

- 2026-09-07: drafted from the gclient vs herdr 0.9.0 parity review (#21971) and the
  confirmed sixteen-decision record. Takes over mouse-parity 4.2, 4.3, 4.4, 5.1, 5.2 (the
  mouse plan's changelog records the move); satisfies #21908 (D4); defers hub-wide agents
  to #20202; adds viewer sizing precedence (web > gclient > tmux).
- 2026-09-07 (materialization): the client leaves form one ordered chain (Constraints,
  Ordering) so every shared file has a dependency path; the row context menus split into
  5.3 so close pane and close tab (5.1, 5.2) land before the sidebar rework; every target at
  or above 850 lines names its split file; the nested prefix (#21923) is absorbed as 4.3.

## Task Mapping
`kind: framing`

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| 1.5 | #21914 (re-parented at expansion) | open |
| 1.6 | #21932 (re-parented at expansion) | open |
| 4.3 | #21923 (re-parented at expansion) | open |
| plan artifact | #21908 (closes on materialization + validate) | open |
| D1 | #20202 | open, blocked |

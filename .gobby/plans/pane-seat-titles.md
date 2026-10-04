# Pane Seat Titles

Plan artifact: `.gobby/plans/pane-seat-titles.md`

**Plan ID:** pane-seat-titles

## Overview
`kind: framing`

Task #23337 (Plan pane labels as the only seat name and remove automatic
session titles). Josh approved the design in Telegram decision 67d0b8de
(relayed 14:16 CDT 2026-10-02) and set the render format in decision cbf258a3
(relayed by the Program Director at 15:28 CDT): "Agents should display
project#session_ref: pane_title".

Principle: the pane is the seat. A pane outlives the sessions in it (rotation,
`/clear`, resume), so the seat name lives on the pane. Automatic session titles
existed to name tmux windows, and tmux window naming goes with them.

Outcomes:

- gclient names every agent seat `<project>#<seq>: <pane label>` in the pane
  header and on line 1 of its Agents sidebar row (1.1, first).
- Spawn placement labels the pane it creates (2.1).
- Sessions carry no automatic title, no title source, no precedence and no
  `set_title` tool. `sessions.title` stays as an optional name (3.4).
- One read-time `display_name` serves the web session list, Telegram agent
  buttons and `gobby sessions list` (4.1, 4.2).
- Guides, skills and the shared role file describe the new model (5.1).

## Decision Record
`kind: framing`

1. **The pane label is the only seat name.** Its writers are `gclient title`,
   the MCP `rename_workspace_item` tool, and spawn placement (2.1). Josh's text
   names the web terminal UI as a writer. No web control exists, and the
   Program Director ruled at 15:04 that none is added (Q2).
2. **Render format (cbf258a3).** The pane header and Agents line 1 render
   `<project>#<seq>: <pane label>`, for example `gobby#15011: L3 hooks`. An
   unlabelled pane renders `<project>#<seq>: <Provider>`, for example
   `gobby#15011: Claude` (the Assistant's fallback, adopted by the PD at 15:28).
   This supersedes the 14:16 `<pane label> · <ref>` shape and replaces
   `AgentEntry::definition_label`'s ladder (manual title, then definition name,
   then provider). Line 2 keeps the task ticker. The pane label also leads
   `AgentEntry.name`, so attention chrome and the row label name a labelled
   seat by it.
3. **Spawn writes the placement title as the pane label for both placement
   kinds.** `title` becomes optional and defaults to the agent definition name
   (Q1). Labels name the role ("L3 hooks"), never the session number.
4. **Sessions lose automatic titles.** Removed: `title_source`; the task and
   provisional title writes on claim, close, spawn, session start, web chat
   creation and registration; the startup normalization sweep; the
   continue-in-chat title copy; the order of precedence; clear-successor title
   computation; the `set_title` MCP tool; gclient `SessionRow::manual_title`.
   No backward compatibility (AGENTS.md rule 10).
5. **`sessions.title` stays as an optional name** (Q4). It is written by
   `POST /api/sessions/{id}/rename` (kept, with no new UI, Q3) and by register
   callers that pass an explicit title. The communications identities path
   passes "Comms: <user>" names, and those stay as names (PD 15:04).
6. **A web-chat `/clear` successor copies its predecessor's `title` column
   verbatim** (PD ruling 15:47). `clear_successor_title` and its recompute go.
   This keeps chat names (Josh's item 4). Terminal seats need nothing because
   the pane carries the name.
7. **`display_name` is computed at read time** (Q5, cbf258a3). It is the name
   part: the bound pane's label, else the session title, else
   `<Provider> · #<newest claimed task seq>`, else `<Provider>`. A surface that
   shows a ref renders `<ref>: <display_name>`. One Python helper serves the
   `/api/sessions` list, Telegram agent buttons, and `gobby sessions list` and
   `show`. Nothing new is stored. The pane is the newest pending or live
   terminal bound to the session through `terminals.session_id` (memory
   a2182bf1).
8. **A pane rename reaches the web list at its next session refetch.** Pane
   renames emit `pane.renamed` workspace events. The web list refetches
   `/api/sessions` on session events. Rejected: a session event per pane rename,
   which adds a second event for one change on a file already at 858 lines
   (`terminals/workspace_ops.py`).
9. **Migration 459 ships inside 3.4 with the code it guards** (Q4; PD
   ruling 22:51, Decision 13). It nulls every title whose
   `title_source` is not `manual`, keeps communications titles, and drops the
   column. It is irreversible on the live hub and ships in a package whose
   cutover Josh approves. 4.1 and 4.2 depend on it (PD ruling 16:06): until
   it runs, legacy automatic titles carry `<project>#<seq>:` prefixes that
   `display_name` would show and that 4.2 no longer strips.
10. **tmux window naming is deleted** (Q6). #21565's tmux fallback contract does
    not use it. The repair loop's tmux liveness expiry stays: it expires
    sessions whose tmux server or pane is gone, and the tmux fallback still
    needs that.
11. **Memories 0f664595 (gclient title ladder) and a3e246d0 (title
    precedence)** are superseded by the PD after the last leaf lands (5.1).
12. **The project prefix is unconditional** (cbf258a3 read literally, as L1
    gobby#14909 recommends; a Writer decision for the PD's review). The
    header and Agents line 1 lead with the project in every sort and
    grouping, so `agent_reference` drops its all-sessions and priority-sort
    condition. Grouped lists repeat the project their group row names. The
    project name is `project_label`: the user's card label, else the
    daemon's name. A project the sidebar does not list yields the bare
    `#<seq>`.
13. **The title removals and migration 459 are one deliverable, 3.4** (PD
    rulings 19:48 and 22:51; close reviews `cfd753e3` and `173e8399`).
    Migration 459 keeps only titles stamped `manual`. The clear-successor
    copy and the title writes after the title-source removal set no
    `title_source`, so a name a person set after either went live and
    before the cutover would be nulled. One deliverable lands in one merge,
    so no part of it can go live ahead of 459, and the hold needs no
    mechanism. The old code stamps `manual` on every person-set title write
    up to the stop, and 459 runs before the new code starts, so no title
    writer has a window. 3.4.4 proves the migration side in isolation: a
    `manual` title written before the stop survives, automatic titles are
    nulled, and only 459 drops the column. Rejected: four leaves held by the
    MM and landed together, whose only acceptance was a comment phrase
    (`173e8399`); new hold code in the daemon wind-down (option B); and
    stamping `manual` on each writer and removing the stamps later (option
    C), which is churn on seven write paths where one missed writer silently
    loses a name.

## As-Is Facts
`kind: framing`

- gclient: `AgentEntry::definition_label` (`crates/gclient/src/app/sidebar_model.rs`,
  ~155-162) ladders `manual_title`, then `agent_definition_name`, then the
  provider label. `build_agents` (~261-382) matches the workspace pane by
  `terminal_id` (~280-284). `PaneRow.label` (`src/daemon/workspace.rs` ~78)
  projects to `Pane.label` (`src/app/pane.rs` ~132) through
  `src/app/live_loop/projection.rs` (~103-116). `WorkspaceModel::apply`
  (`src/app/workspace_ops.rs` ~47-83) bumps the generation on `pane.renamed`,
  and the sidebar rebuilds every frame (`render.rs` ~20). L1 confirmed that the
  header and line 1 move together.
- `SessionRow` (`crates/gclient/src/daemon/projects.rs` ~58-78) carries `title`
  and `title_source`. `SessionRow::manual_title` (~90-112) strips a ref prefix
  from a `manual` title.
- `AgentPlacement.parse` (`src/gobby/terminals/workspace_agent_panes.py`
  ~103-124) requires exactly `{"tab": {workspace, title}}` or
  `{"split": {pane, axis, title}}` with a non-blank title.
  `AgentPaneReserver._insert` (~275-298) creates a tab with `create_tab(title=...)`,
  which leaves the pane label NULL. A split names its pane with `rename_pane`.
  `AgentPaneReserver._seat_held` (~219-233) keys split seats on the label.
- Automatic title writers: `src/gobby/sessions/title_lifecycle.py` (170 lines)
  writes task and provisional titles. It is called from task create, claim and
  close, spawn execution, session-start materialization, web chat creation and
  the web-chat clear successor. Registration writes and backfills a provisional
  title (`storage/sessions/_crud.py`). The startup sweep
  `_TitleFieldMixin.normalize_automatic_title_refs` runs from
  `runner_init/storage.py` (~264).
- `storage/sessions/_title_update.py` holds the precedence SQL (manual over open
  claimed task over provisional). `storage/sessions/_title_defaults.py` holds
  the source constants, formatters and `provider_title_label`.
- `set_title` is registered in `mcp_proxy/tools/sessions/_handoff.py`
  (~389-408, registration ~448-459).
- `title_source` literal sweep (`gcode grep -l -w title_source`, run on this
  branch 2026-10-02) outside plans and evidence dumps:
  - `.gobby/roles/_common.md`
  - crates: `gclient/src/daemon/projects.rs`, `gclient/src/ui/pane_chrome/tests.rs`,
    `gclient/src/ui/sidebar_rows/tests.rs`, `gcore/assets/schema/baseline.sql`,
    `gcore/assets/schema/catalog.manifest.json`, `gcore/src/schema/verify.rs`
  - docs: `docs/guides/sessions.md`, `docs/reference-audit/sessions.json`
  - src: `hooks/session_types.py`, `mcp_proxy/tools/sessions/_handoff.py`,
    `mcp_proxy/tools/sessions/_registration.py`, `servers/routes/sessions/core.py`,
    `servers/routes/sessions/lifecycle.py`,
    `servers/websocket/handlers/session_observe_continue.py`,
    `sessions/clear_continuation.py`, `sessions/title_lifecycle.py`,
    `storage/session_models.py`, and `storage/sessions/` `_bulk_update.py`,
    `_crud.py`, `_registration.py`, `_title_fields.py`, `_title_update.py`,
    `_upsert.py`, `_web_chat_crud.py`
  - web: `lib/sessionTitle.ts`, `types/sessions.ts`, and three web tests
  - Python tests: listed in each owning leaf
- `set_title` literal sweep: `.gobby/roles/_common.md`,
  `docs/contracts/session-boundary.md`, `docs/guides/sessions.md`,
  `docs/reference-audit/sessions.json`,
  `src/gobby/install/shared/skills/gobby/references/sessions/discovery.md`,
  `mcp_proxy/tools/sessions/_handoff.py`, `tests/sessions/test_handoff.py`.
  No rule or tool catalog names it.
- tmux window naming lives in `src/gobby/sessions/tmux_window_naming.py`
  (373 lines). Its consumers are `hooks/event_handlers/_session_start/__init__.py`,
  `flow.py`, `materialize.py`, `hooks/session_lookup.py`,
  `runner_maintenance/isolation.py` and `storage/sessions/_title_fields.py`.
  `tmux_window_name_repair_loop` (`runner_maintenance/isolation.py` ~53-172)
  probes each tmux-backed session with `probe_tmux_pane`. It expires sessions on
  a missing server or pane, and only then renames windows through
  `resolve_tmux_repair_owner`, `enforce_window_name_if_unmanaged` and
  `release_window_name_if_unowned`.
- No production code registers a title listener. Only tests call
  `register_title_listener` (`storage/sessions/_bootstrap.py` ~25-36).
- Session change broadcasts carry the event, session id and project id
  (`servers/websocket/broadcast.py::broadcast_session_event`), and web listeners
  refetch `/api/sessions`.
- Communications sessions register with `title="Comms: <user>"`
  (`communications/identities.py` ~118-140). `manual_registration_title`
  stamps an explicit registration title `manual`.

## Constraints
`kind: framing`

- Rollout: Python leaves go live after a PD-owned daemon restart from the main
  checkout, announced with a `global` message before and after, outside quiet
  hours 04:45-06:45 CT. 3.4 ships in a package whose cutover Josh approves,
  and none of its code goes live before that cutover (Decision 13).
- Packaging: 3.4 lands on 0.5.0 right before its cutover, and the MM holds it
  until then. Once it lands, `gobby restart` refuses, because the installed
  gdaemon's schema identity no longer matches the checkout pin 3.4 refreshes
  (`storage/schema_divergence.py::schema_apply_refusal`). A plain
  `gobby start` checks only binary-set coherence (`cli/daemon_start.py`
  ~306-313), so the hold is still needed.
  1.1 ships through a gclient version bump and a Release Manager install:
  gclient is promoted separately from the coherent `gcode`/`gdaemon`/`ghook` set.
- Routing: 1.1 goes to the gclient lane L1 (gobby#14909). 2.1 through 5.1 go to
  L5 (gobby#14768), per the PD at 15:04. The PD may re-route.
- Josh directive: no logging or diagnostic emission on daemon event-loop paths.
  The display path adds none.
- Production `.py/.ts/.tsx/.rs` files stay under 1,000 lines. 3.4 and 4.1 name
  their splits.
- Cross-plan: the #22909 plan (`.gobby/plans/mcp-brief-response-contract.md`,
  1.5) moves `build_task_tree` from `mcp_proxy/tools/tasks/_crud.py` into
  `mcp_proxy/tools/tasks/_crud_tree.py`. 3.4 names the same move to the same
  file, and whichever leaf lands first performs it. If #22909's leaf lands first,
  `_crud.py` stays at or above 850 lines, and the Writer re-points 3.4's split at
  the completion pass.
- Cross-plan: `.gobby/roles/_common.md` is also edited by other plans. 5.1
  changes one line and rebases onto whatever lands first.
- Cross-plan: #23280 (L1, in progress) restyles Agents line 1 in
  `ui/sidebar_rows.rs` and changes the `agent_rows`, `projects_agents`,
  `monochrome` and `label_ladder` screen fixtures. 1.1 lands after it
  (package 10) and rebases. 1.1 keeps `SidebarRow.reference` and
  `SidebarRow.definition`, so the styling carries over.
- Migration 459 is the next free number at drafting. The latest on 0.5.0 is
  `457_workspace_pane_role.sql`, and #23271 '`api_keys`, key format,
  issuance routes, and local-key adoption' owns `458_add_api_keys.sql` (PD
  ruling 2026-10-03 00:26). If another migration lands first, the 3.4
  executor takes the next free number and renames the file, its `assets.rs`
  entry and its tests to match.
- The executor of a leaf that touches `src/gobby/install/shared/` reads
  `src/gobby/install/shared/AGENTS.md` first. The 1.1 executor loads the `rust`
  skill, and the 4.2 executor loads `impeccable`.

## P1: gclient seat render
`kind: framing`

**Goal:** every pane header and Agents row names its seat by the pane label.

### 1.1 gclient renders the project ref and pane label [category: code]
`kind: deliverable`

Targets:
- `crates/gclient/src/app/sidebar_model.rs::*` — scope-reason: replace the definition_label ladder with the matched pane's label, lead the name ladder with it, and drop agent_definition_name and manual_title
- `crates/gclient/src/daemon/projects.rs::*` — scope-reason: drop SessionRow::manual_title and the title_source field
- `crates/gclient/src/ui/pane_chrome.rs::*` — scope-reason: render the header as the project ref then the seat label
- `crates/gclient/src/ui/sidebar/agents.rs::*` — scope-reason: feed the seat label to Agents line 1 and always prefix the project
- `crates/gclient/src/ui/sidebar_rows.rs::*` — scope-reason: format Agents line 1 as the project ref then the seat label
- `crates/gclient/src/app/pane.rs::*` — scope-reason: correct the display_name doc comment that names the session title
- `crates/gclient/src/ui/pane_chrome/tests.rs::*` — scope-reason: labelled and unlabelled header cases
- `crates/gclient/src/ui/sidebar_rows/tests.rs::*` — scope-reason: Agents line 1 cases
- `crates/gclient/tests/sidebar_model.rs::*` — scope-reason: seat label from the matched pane and a pane rename
- `crates/gclient/tests/attention_flow.rs::*` — scope-reason: expected row text and attention names from pane labels
- `crates/gclient/tests/parity/sidebar.rs::*` — scope-reason: drop agent_definition_name
- `crates/gclient/tests/parity/chrome.rs::*` — scope-reason: header digest
- `crates/gclient/tests/ui_carve_guard.rs::*` — scope-reason: expected row text
- `crates/gclient/tests/screens.rs::*` — scope-reason: screen cases for labelled and unlabelled panes
- `crates/gclient/tests/fixtures/screens/agent_rows.txt`
- `crates/gclient/tests/fixtures/screens/projects_agents.txt`
- `crates/gclient/tests/fixtures/screens/monochrome.txt`
- `crates/gclient/tests/fixtures/screens/pane_edges.txt`
- `crates/gclient/tests/fixtures/screens/status_segments.txt`
- `crates/gclient/tests/fixtures/screens/label_ladder.txt`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: pin the bumped gclient version

**Granularity:** seven production files (six gclient `.rs` files and
`version_pins.py`), one behavior: the pane label names the seat on every
gclient surface. The model, header and row edits share `AgentEntry` and the
same screen fixtures, and the version bump ships them, so they land in one
commit with one test run.

**Research context:** The ladder lives in `AgentEntry::definition_label`
(`sidebar_model.rs` ~155-162) over fields `agent_definition_name` (~86) and
`manual_title` (~89). `build_agents` (~261-382) already matches the workspace
pane by `terminal_id` (~280-284) and reads `SessionRow::manual_title` at
~318-327. Readers of `agent_definition_name` are `sidebar_model.rs` (~160),
`ui/pane_chrome.rs` (~112), `tests/parity/sidebar.rs` (~454) and
`tests/sidebar_model.rs` (~669, ~678). The run's `agent_name` still feeds
`AgentEntry.name` through a local at ~305-309 and stays.

Change: give `AgentEntry` a new `pane_label: Option<String>`, taken from the
matched pane's `Pane.label`, and a new `AgentEntry::seat_label()` that returns
the non-blank pane label, else `provider_label` (`PROVIDER_LABELS`, ~195-219).
Delete `definition_label`, `agent_definition_name` and `manual_title`. Delete
`SessionRow::manual_title` and the `title_source` field from `SessionRow`.
`SessionRow` has no `deny_unknown_fields`, so the `title_source` the daemon
sends until 3.4 lands is ignored. `SessionRow.title` stays because
`app/live_loop/orphans.rs` (~65-80) still reads it as a fallback after the
ref. A field the daemon stops sending deserializes as `None`. The
`rows.projects` lookup in `build_agents` (~318-327) serves only
`manual_title` and goes with it. Rewrite the doc comments on
`PROVIDER_LABELS` (~194) and `provider_label` (~208) to name the daemon's
provider label table without a module path, because 4.1 moves that table
after 1.1 lands.

Project ref (Decision 12): `agent_reference` (`ui/sidebar/agents.rs`
~300-316) already prefixes the project through `project_label`
(`ui/sidebar_rows.rs` ~97-111), which honours `chrome.sidebar.project_labels`,
but only in the all-sessions list sorted by priority. Drop that condition so
the header and line 1 always read `<project>#<seq>`.

Header: `pane_corners` (`ui/pane_chrome.rs` ~86-168, gate ~108-114, format
~123) renders `<project>#<seq>: <seat_label>`. Its gate becomes: the agent's
provider is non-blank or its pane label is present. A pane with no agent
entry (a shell pane) keeps `pane.display_name()`. Line 1: `agent_candidate`
(`ui/sidebar/agents.rs` ~320-341, `SidebarRow.definition`) carries the seat
label, and `ui/sidebar_rows.rs` (~401-405) formats line 1 as
`<project>#<seq>: <seat_label>`. Line 2 (the task ticker) is unchanged. An
entry with no session ref yet renders the seat label alone. Correct the
`Pane::display_name` doc comment (`app/pane.rs` ~320-327), which says a
session's title names the terminal on the sidebar.

Name ladder (Decision 2): `AgentEntry.name` (`sidebar_model.rs` ~302-317)
ladders the session title, the run's agent name, the tmux name, then
`Pane::display_name`. It feeds `agent_label`, which names attention chrome
(`attention_label`, `ui/chrome/labels.rs` ~97-100), and `agent_title`, which
fills `SidebarRow.label` (`agents.rs` ~331). Put the matched pane's non-blank
label first so those surfaces name a labelled seat by it. The rest of the
ladder is unchanged. Update the `agent_label` doc comment (`agents.rs`
~33-36), which describes `#ref: title`.

Freshness: `WorkspaceModel::apply` bumps the generation on `pane.renamed`, and
the sidebar rebuilds from live panes every frame, so a rename moves the header
and line 1 together without new wiring. Scripted panes in tests get
`label = terminal_id` (`app/mod.rs` ~416-430, memory f5164d15), so the screen
fixtures, `attention_flow.rs` and `ui_carve_guard.rs` expect terminal ids as
seat labels, and the 1.1.2 fallback test sets its pane's label to `None`.
Regenerate the screen fixtures with `GOBBY_UPDATE_SCREENS=1`, review each
diff, then rerun without the variable.

Version bump: `crates/gclient/Cargo.toml`, the gclient entry in `Cargo.lock`
and `src/gobby/install/version_pins.py` move together, +0.0.1 over the
predecessor at the final rebase (0.1.18 shipped in package 9, and #23280
bumps it too), as the release guide prescribes (~19-31). The Release Manager
installs gclient after the land.

Verification planned: `cargo nextest run -p gobby-client`,
`cargo clippy -p gobby-client --all-targets -- -D warnings`, then
`gcode grep -w -E 'manual_title|title_source|definition_label' crates/gclient`,
which must return nothing, then a live check: a labelled pane shows
`gobby#<seq>: <label>` in its header and Agents row.

**Acceptance:**

- 1.1.1 - A pane labelled "L3 hooks" running session `#15011` in project
  `gobby` renders `gobby#15011: L3 hooks` in its header. test:
  `crates/gclient/src/ui/pane_chrome/tests.rs::header_names_project_ref_and_pane_label`.
- 1.1.2 - An unlabelled pane renders `gobby#15011: Claude` for a Claude session.
  test: `crates/gclient/src/ui/pane_chrome/tests.rs::header_falls_back_to_provider`.
- 1.1.3 - Agents line 1 renders the same `<project>#<seq>: <seat label>` text as
  the header in every sort and grouping (Decision 12), and line 2 keeps the
  task ticker. test:
  `crates/gclient/src/ui/sidebar_rows/tests.rs::agent_line_one_names_seat`.
- 1.1.4 - After a `pane.renamed` event, the next frame's header and Agents line
  1 carry the new label. test:
  `crates/gclient/tests/sidebar_model.rs::pane_rename_moves_seat_label`.
- 1.1.5 - A session's title and its run's agent name no longer name the seat:
  an unlabelled pane running a titled session from a named definition gets the
  seat label `Claude`, and the 1.1 grep for `manual_title`, `title_source` and
  `definition_label` returns nothing. test:
  `crates/gclient/tests/sidebar_model.rs::session_title_does_not_name_seat`.
- 1.1.6 - The gclient version is bumped in its manifest, the lockfile and the
  install pin. file: `src/gobby/install/version_pins.py`.
- 1.1.7 - Attention chrome names a labelled seat `#<seq>: <pane label>`. test:
  `crates/gclient/tests/attention_flow.rs::attention_names_seat_by_pane_label`.

## P2: Spawn labels its pane
`kind: framing`

**Goal:** a placed spawn names its seat without a later rename.

### 2.1 Spawn writes the placement title as the pane label [category: code]
`kind: deliverable`

Targets:
- `src/gobby/terminals/workspace_agent_panes.py::*` — scope-reason: label a tab placement's pane with the placement title
- `src/gobby/mcp_proxy/tools/spawn_agent/_placement.py::*` — scope-reason: add the default-title helper
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: fill a missing placement title from the agent name and correct the placement docstring
- `tests/terminals/test_workspace_agent_panes.py::*` — scope-reason: tab label and seat collision cases
- `tests/mcp_proxy/tools/spawn_agent/test_placement.py::*` — scope-reason: default-title cases

**Research context:** `AgentPlacement.parse` (`workspace_agent_panes.py`
~103-124) accepts exactly `{"tab": {workspace, title}}` or
`{"split": {pane, axis, title}}` and refuses a blank title.
`AgentPaneReserver._insert` (~275-298) calls `create_tab(..., title=...)` for a
tab, which titles the tab and leaves the pane label NULL. For a split it calls
`rename_pane(pane_id, placement.title)` and returns that pane. Change the tab
path to name the new pane the same way: after `create_tab`, return
`change.tabs[0]` with `self._workspaces.rename_pane(pane_id, placement.title)`.
`WorkspaceManager.rename_pane` (read-only dependency in
`src/gobby/storage/workspaces.py` ~753-758) writes the label.

Default title: add a new `with_default_title(placement, agent_name)` to
`spawn_agent/_placement.py`. When `placement` is a dict with exactly one kind
key whose body is a dict, and that body has no `title` or a `None` title, it
returns a copy whose body carries `title=agent_name`. Every other shape passes
through unchanged, so `AgentPlacement.parse` stays strict and still refuses a
blank title or a malformed placement. The spawn tool function in
`_factory.py` (821 lines) applies it to `placement` before passing
`placement=` to the implementation (~643). Its `agent` parameter is the
definition name and defaults to `"default"` (~340). The factory is the only
caller that passes a placement: the spawn callers in `dispatch/spawn.py`,
`feedback/agent.py`, `scheduler/executor.py` and `servers/routes/agent_spawn.py`
pass none. `agents/resume_placement.py` builds `AgentPlacement` from the
recorded title (read-only). This keeps `spawn_agent/_implementation.py`
(909 lines, `preflight_placement` call ~382) untouched. Correct the factory
docstring's placement description (~400-401) to the two accepted shapes with
`title` optional.

Seat collision: `_seat_held` keys a seat on its label, so two untitled
placements of the same definition in one workspace name the same seat. The
second is refused by the existing seat-held refusal, and the caller passes a
distinct `title`. 2.1.3 pins that.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_workspace_agent_panes.py tests/mcp_proxy/tools/spawn_agent/test_placement.py -q`.

**Acceptance:**

- 2.1.1 - A tab placement's new pane carries the placement title as its label,
  as a split placement's pane already does. test:
  `tests/terminals/test_workspace_agent_panes.py::test_tab_placement_labels_pane`.
- 2.1.2 - A placement without `title` is filled with the agent definition name,
  and a blank `title` is still refused. test:
  `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_missing_title_defaults_to_agent_name`.
- 2.1.3 - A second placement with the same title into one workspace is refused
  as a held seat. test:
  `tests/terminals/test_workspace_agent_panes.py::test_same_title_seat_is_refused`.

## P3: Sessions lose automatic titles
`kind: framing`

**Goal:** nothing writes a session title except a person or an explicit
register caller, and the title source column is gone.

3.1, 3.2 and 3.3 were folded into 3.4 (PD ruling 22:51, Decision 13). Their
ids are retired.

### 3.4 Automatic titles, the title source and tmux naming retire with migration 459 [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/title_lifecycle.py::*` — operation: delete — scope-reason: the claim, close, spawn, session-start, web-chat and clear-successor title writers retire
- `src/gobby/mcp_proxy/tools/tasks/_crud.py::*` — scope-reason: create_task stops titling the claiming session; build_task_tree moves out
- `src/gobby/mcp_proxy/tools/tasks/_crud_tree.py`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py::*` — scope-reason: claim stops titling the session
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::*` — scope-reason: close stops recomputing the title
- `src/gobby/mcp_proxy/tools/spawn_agent/_execution.py::*` — scope-reason: spawn stops titling the child session
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::*` — scope-reason: session start stops titling the session and scheduling window renames
- `src/gobby/servers/websocket/chat/_session.py::*` — scope-reason: web chat creation stops titling the session; import the moved clear successor commit
- `src/gobby/sessions/clear_continuation.py::*` — scope-reason: the web-chat clear successor commit moves out
- `src/gobby/sessions/clear_web_chat_successor.py`
- `src/gobby/storage/sessions/_crud.py::*` — scope-reason: register stops writing and backfilling the provisional title and drops the title_source parameter
- `src/gobby/storage/sessions/_title_defaults.py::*` — scope-reason: remove the provisional and task title formatters, the title source constants and manual_title_source
- `src/gobby/storage/sessions/_title_fields.py::*` — scope-reason: remove the startup normalization sweep and the title-change side effects; update_title writes the title alone
- `src/gobby/runner_init/storage.py::*` — scope-reason: stop calling the normalization sweep
- `src/gobby/servers/websocket/handlers/session_observe_continue.py::*` — scope-reason: continue-in-chat stops copying the source title
- `src/gobby/storage/sessions/_registration_recovery.py::*` — scope-reason: drop the empty-title term from the recovery score
- `src/gobby/storage/sessions/_title_update.py::*` — operation: delete — scope-reason: the precedence SQL retires
- `src/gobby/storage/sessions/_bulk_update.py::*` — scope-reason: drop title_source from bulk updates
- `src/gobby/storage/sessions/_manager.py::*` — scope-reason: drop the valid title sources
- `src/gobby/storage/sessions/_registration.py::*` — scope-reason: drop manual_registration_title's source and require_valid_title_source
- `src/gobby/storage/sessions/_summary_protocols.py::*` — scope-reason: drop title_source from the protocol
- `src/gobby/storage/sessions/_upsert.py::*` — scope-reason: drop title_source from upserts
- `src/gobby/storage/sessions/_web_chat_crud.py::*` — scope-reason: web chat writes the title alone
- `src/gobby/storage/session_models.py::*` — scope-reason: drop the title_source field
- `src/gobby/hooks/session_types.py::*` — scope-reason: drop title_source
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py::*` — scope-reason: remove the set_title tool
- `src/gobby/mcp_proxy/tools/sessions/_registration.py::*` — scope-reason: register_session drops title_source
- `src/gobby/servers/routes/sessions/core.py::*` — scope-reason: drop title_source from responses
- `src/gobby/servers/routes/sessions/lifecycle.py::*` — scope-reason: rename writes the title alone
- `src/gobby/sessions/tmux_window_naming.py::*` — operation: delete — scope-reason: tmux window naming retires
- `src/gobby/runner_tmux_repair.py::*` — scope-reason: receive probe_tmux_pane and _tmux_manager_for_session
- `src/gobby/runner_maintenance/isolation.py::*` — scope-reason: the repair loop keeps tmux liveness expiry and drops the window rename branch
- `src/gobby/hooks/event_handlers/_session_start/__init__.py::*` — scope-reason: stop scheduling window renames
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: stop scheduling window renames
- `src/gobby/hooks/session_lookup.py::*` — scope-reason: stop calling window naming
- `src/gobby/storage/sessions/_bootstrap.py::*` — scope-reason: remove the title listener registry
- `crates/gcore/assets/schema/migrations/459_drop_session_title_source.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: embed migration 459
- `crates/gcore/src/schema/verify.rs::*` — scope-reason: drop title_source from the live mutable seed field list
- `crates/gcore/src/schema/verify_tests.rs::*` — scope-reason: migration 459 cases
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: drop the sessions.title_source column entry
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: regenerated grant bundle expectations for the new schema identity
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: migration count and schema identity
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: schema identity
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: refresh the schema identity for migration 459
- `tests/storage/test_session_title_source_migration.py`
- `tests/sessions/test_title_lifecycle.py::*` — operation: delete — scope-reason: covers the retired module
- `tests/mcp_proxy/tools/tasks/test_create_task.py::*` — scope-reason: import build_task_tree from its new module; create with claim leaves the title alone
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::*` — scope-reason: claim and close leave the title alone
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py::*` — scope-reason: spawn leaves the child title alone
- `tests/sessions/test_handoff.py::*` — scope-reason: drop the title lifecycle case, its provider_title_label import and the set_title cases
- `tests/sessions/test_clear_continuation.py::*` — scope-reason: the successor copies the predecessor title verbatim; import from the new module
- `tests/storage/sessions/test_storage_sessions_registration.py::*` — scope-reason: register leaves an untitled session untitled and takes no title_source; the provider_title_label import goes
- `tests/storage/sessions/test_register_fallback.py::*` — scope-reason: drop provisional title and title_source expectations
- `tests/storage/sessions/test_title_fields.py::*` — scope-reason: drop the sweep cases; update_title writes the title alone with no side effects
- `tests/storage/sessions/test_reference_resolution.py::*` — scope-reason: drop the sweep cases, title_source and the title listener
- `tests/storage/sessions/test_storage_sessions_lifecycle.py::*` — scope-reason: no title_source
- `tests/storage/sessions/test_storage_sessions_models.py::*` — scope-reason: no title_source field
- `tests/storage/test_sessions_import.py::*` — scope-reason: the session manager surface loses the sweep, the title sources and the title listeners
- `tests/storage/test_local_model_flags.py::*` — scope-reason: no title_source
- `tests/hooks/test_session_materialize.py::*` — scope-reason: session start leaves the title alone and renames no window
- `tests/hooks/test_hooks_manager.py::*` — scope-reason: no title_source and no window rename
- `tests/hooks/test_session_lookup_metadata.py::*` — scope-reason: no window rename
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: no window rename
- `tests/hooks/event_handlers/test_session_variable_preservation.py::*` — scope-reason: no window rename
- `tests/servers/test_session_control.py::*` — scope-reason: continue-in-chat leaves the target title alone; no title_source
- `tests/servers/test_http_models.py::*` — scope-reason: no title_source
- `tests/servers/routes/test_agent_spawn_routes.py::*` — scope-reason: a spawned conversation starts untitled; the provisional title and source expectation and its import go
- `tests/servers/routes/test_servers_routes_sessions_routes.py::*` — scope-reason: rename and list responses without title_source
- `tests/servers/routes/test_sessions_acp_routes.py::*` — scope-reason: no title_source
- `tests/servers/websocket/chat/test_stream_persistence.py::*` — scope-reason: no title_source
- `tests/sessions/test_acp_lifecycle_service.py::*` — scope-reason: no title_source
- `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py::*` — scope-reason: register_session without title_source
- `tests/sessions/test_tmux_window_naming.py::*` — operation: delete — scope-reason: covers the retired module; probe cases move to the repair loop tests
- `tests/test_runner_maintenance_tmux_repair.py::*` — scope-reason: the probe patch target moves; rename cases go; expiry cases stay

**Granularity:** one behavior across every file above: a session title is an
optional name a person sets, and nothing records where it came from. The
title-write removals, the title-source removal, tmux window naming and
migration 459 are safe only as one activation. The clear-successor copy and
the title-source removal write titles without a `manual` stamp, so either one
live before 459 lets 459 null a name a person set (close reviews `cfd753e3`
and `173e8399`). One deliverable lands in one merge, so a partial landing
cannot happen (Decision 13). tmux naming joins because it shares
`_title_fields.py` and `materialize.py` with the title removals.

**Research context:** four parts, each carried from the leaf it replaced.

**Automatic title writes (formerly 3.1).** Writers and the edit at each:

- `sessions/title_lifecycle.py`: delete the file. Its functions are
  `update_title_for_claim`, `recompute_automatic_title`, `latest_open_claimed_task`,
  `clear_successor_title` and `apply_clear_successor_title`.
- `mcp_proxy/tools/tasks/_crud.py` `create_task` (~319-321), `_lifecycle_claim.py`
  (~388-390), `_lifecycle_close_finalization.py` (~532-534),
  `spawn_agent/_execution.py` (~25, ~46-50, ~275-280),
  `hooks/event_handlers/_session_start/materialize.py` (~240-249) and
  `servers/websocket/chat/_session.py` (~298-302): remove the call and its import.
- `storage/sessions/_crud.py` register: the provisional title and its source
  (~186-195), the insert default (~255-265) and the conflict backfill
  (~337-346) go. An explicit `title` argument is still stored, so communications
  names and manual registrations keep theirs.
- `storage/sessions/_title_defaults.py`: remove `format_provisional_session_title`,
  `format_task_session_title` and `project_name_for_session_title`. Their only
  callers are `title_lifecycle.py`, `_crud.py` and `_title_fields.py`. The source
  constants go with the title source below.
- `_TitleFieldMixin.normalize_automatic_title_refs` (`_title_fields.py` ~37-91)
  and its call (`runner_init/storage.py` ~264): delete both.
- `servers/websocket/handlers/session_observe_continue.py`: remove the
  continue-in-chat title copy (~214-218, ~325-340, ~370-375, ~426-445) and its
  `title_source` reads. The continued chat starts untitled and shows its
  `display_name`.
- `_registration_recovery._recovery_score` (~17): drop the `not bool(session.title)`
  term. Untitled is now the default, so the term no longer separates candidates.
- Clear successor (PD ruling 15:47): `_commit_web_chat_clear_successor_rows`
  inserts the successor with `title` copied from the predecessor row and
  writes no `title_source`. `clear_successor_title` goes with
  `title_lifecycle.py`. Migration 459 would null a copy written before it
  ran, and this code never runs before 459 (Decision 13).
- Tests that import the retired helpers: `tests/sessions/test_handoff.py`
  drops `test_title_lifecycle_is_provisional_task_manual_and_clear_sticky`
  (~1888) and its `provider_title_label` import (~60).
  `tests/storage/sessions/test_storage_sessions_registration.py` asserts an
  untitled session where it asserted the provisional title (~592) and drops
  its `provider_title_label` import (~26).
  `tests/servers/routes/test_agent_spawn_routes.py` asserts an untitled
  conversation where it asserted the provisional title and source (~324-328)
  and drops its `format_provisional_session_title` import (~25). After this leaf
  no test imports `provider_title_label`, so 4.1 deletes `_title_defaults.py`
  without test Targets.

Split: `src/gobby/mcp_proxy/tools/tasks/_crud.py` is 996 lines. Move the
module-level `build_task_tree` (~917-996) into the new
`src/gobby/mcp_proxy/tools/tasks/_crud_tree.py`. Its only importer is
`tests/mcp_proxy/tools/tasks/test_create_task.py`, which imports it from the
new module. The #22909 plan names the same move (Constraints).

Split: `src/gobby/sessions/clear_continuation.py` is 860 lines. Move
`_ClearCommitAborted`, `commit_web_chat_clear_successor` and
`_commit_web_chat_clear_successor_rows` (~585-788) into the new
`src/gobby/sessions/clear_web_chat_successor.py`. Drop the name from
`clear_continuation.py`'s `__all__` (~58). Point
`servers/websocket/chat/_session.py` (~26, ~254) and the tests at the new
module. The moved code imports the helpers it still needs from
`clear_continuation.py`.

**Title source, precedence and set_title (formerly 3.2).** With the automatic
writes gone, only manual writes remain, so the source and the precedence
have nothing left to rank. Edits:

- Delete `src/gobby/storage/sessions/_title_update.py` (`TITLE_UPDATE_ALLOWED_SQL`,
  `apply_title_mutation`, `TitleMutationResult`). Its callers `_bulk_update.py`,
  `_title_fields.py` and `_upsert.py` write `title` directly.
- `_title_defaults.py`: remove `PROVISIONAL_TITLE_SOURCE`, `TASK_TITLE_SOURCE`,
  `MANUAL_TITLE_SOURCE` and `manual_title_source`. `provider_title_label` and
  `_PROVIDER_TITLE_LABELS` stay for 4.1.
- `_TitleFieldMixin.update_title`: write the stripped title, or NULL for a blank
  one, and notify the session change. The tmux and listener side effects go with
  tmux naming below.
- `_manager.py` (~78-82) valid sources, `_registration.py`
  (`manual_registration_title`, `require_valid_title_source`),
  `_summary_protocols.py` (~17), `_web_chat_crud.py`, `session_models.py`
  (~63, ~142, ~306, ~376) and `hooks/session_types.py` (~78): drop every
  `title_source` field, parameter and column reference.
- `mcp_proxy/tools/sessions/_handoff.py`: delete `set_title` (~389-408) and its
  registration (~448-459). `mcp_proxy/tools/sessions/_registration.py`
  (~12, ~130): `register_session` drops `title_source`.
- `servers/routes/sessions/core.py` (~317) stops serving `title_source`.
  `servers/routes/sessions/lifecycle.py` (~441) keeps `POST /api/sessions/{id}/rename`
  and writes the title alone (Q3).
- No code reads or writes the column after these edits. Migration 459 drops
  it (below).

**tmux window naming (formerly 3.3).** `tmux_window_naming.py` consumers: `_session_start/__init__.py`
(~11, ~27), `flow.py` (~46, ~614), `materialize.py` (~111-135, ~390),
`hooks/session_lookup.py` (~24, ~345), `runner_maintenance/isolation.py`
(~24-28) and `_TitleFieldMixin._run_title_change_side_effects` (`_title_fields.py`
~134-157, tmux call ~141-143). Remove every call and import.

The repair loop keeps a job. `tmux_window_name_repair_loop` (`isolation.py`
~53-172) probes each tmux-backed session. On `TmuxProbeState.SERVER_MISSING`
it calls `expire_tmux_socket_sessions`, and on a missing pane it calls
`expire_tmux_pane_sessions`. The tmux fallback (#21565) still needs that
expiry. Keep both branches and delete only the rename branch
(`resolve_tmux_repair_owner`, `enforce_window_name_if_unmanaged`,
`release_window_name_if_unowned`) with its `renamed` counter. Move
`probe_tmux_pane` and `_tmux_manager_for_session` (`tmux_window_naming.py`
~90-105) into `src/gobby/runner_tmux_repair.py` (92 lines), which already holds
`_select_tmux_repair_sessions` and `_tmux_repair_pane_key`. Rewrite the loop
docstring to describe liveness expiry. The loop keeps its name and its wiring
in `runner_lifecycle.py`, `runner_lifecycle_periodic.py` and
`runner_maintenance/__init__.py`. Rejected: renaming the loop, which changes
four files and the runner task attribute for no behavior.

Title side effects: delete `_run_title_change_side_effects` and the title
listener registry (`register_title_listener` and `unregister_title_listener`
in `_bootstrap.py` ~25-36, plus the `TitleChangeCallback` type). No production
code registers a listener. `update_title` keeps its session-change
notification. Move the probe cases from `tests/sessions/test_tmux_window_naming.py`
into `tests/test_runner_maintenance_tmux_repair.py`, which already patches the
probe at `gobby.runner_maintenance.isolation.probe_tmux_pane`.

**Migration 459 (formerly 3.4).** Template: commit `b9303f8183` ([gobby-#22740] migration
450 drops `sessions.heuristic_title`) changed exactly this carrier set. It
added the SQL file, an `EmbeddedMigration` entry in `assets.rs` with the file's
checksum, a removal in `verify.rs::is_live_mutable_seed_field` (~622 lists
`"title_source"`), cases in `verify_tests.rs`, the column removal in
`catalog.manifest.json` (the sessions table, ~3928-3934), and refreshed identity
values in `grant/bundle.rs`, `schema_contract.rs`, `cli_contract.rs` and
`schema_expected_identity.json`. `baseline.sql` (~3459) is not edited:
migrations after canonical baseline@420 land as numbered files.

Migration body:

```sql
-- Session titles are optional names a person sets (#23337). Automatic titles
-- go first, then the column that ranked them. Communications sessions keep
-- their "Comms: <user>" names. The title-write removals go live only through
-- this migration's cutover, so every name a person set before it is 'manual'.
UPDATE sessions SET title = NULL
WHERE title_source IS DISTINCT FROM 'manual'
  AND source IS DISTINCT FROM 'comms';
ALTER TABLE sessions DROP COLUMN title_source;
```

The UPDATE is irreversible on the live hub: it destroys every automatic title,
and no copy is kept. That is intended (Q4). This leaf ships in a package whose
cutover Josh approves. The PD runs it from the main checkout outside quiet
hours with `global` notices before and after. `gobby cutover` refuses
uncommitted schema inputs. None of this leaf's code is live before the cutover
(Decision 13): the old daemon stops, 459 runs, and the new daemon starts with
code that never reads the column.

**Migration test (3.4.4).** The new
`tests/storage/test_session_title_source_migration.py` follows
`tests/storage/test_validation_system_prompt_migration.py` (migration 445). It
reads `459_drop_session_title_source.sql` from
`crates/gcore/assets/schema/migrations/`, creates a TEMP `sessions` table with
`id`, `title`, `title_source` and `source` inside a `force_rollback`
transaction on the isolated test hub (`DATABASE_URL`), and runs the file.
Seeds: a person-set title stamped `manual`, as the pre-cutover code stores it
up to the stop (this leaf deletes that writer, so the test seeds its row); a
`task` title; a `provisional` title; a title with a NULL source; and a
communications session (`source = 'comms'`) titled "Comms: <user>" with a
non-`manual` source. Asserts: the manual and communications titles survive;
the other three are NULL; the TEMP table has no `title_source` column; and of
every `*.sql` file in the migrations directory, only 459 contains
`DROP COLUMN title_source`.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_session_title_source_migration.py tests/mcp_proxy/tools/tasks/test_create_task.py tests/mcp_proxy/tools/tasks/test_close_task_flow.py tests/mcp_proxy/tools/spawn_agent/test_execution.py tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py tests/sessions/test_handoff.py tests/sessions/test_clear_continuation.py tests/sessions/test_acp_lifecycle_service.py tests/storage/sessions tests/storage/test_sessions_import.py tests/storage/test_local_model_flags.py tests/hooks tests/servers/test_session_control.py tests/servers/test_http_models.py tests/servers/routes/test_agent_spawn_routes.py tests/servers/routes/test_servers_routes_sessions_routes.py tests/servers/routes/test_sessions_acp_routes.py tests/servers/websocket/chat/test_stream_persistence.py tests/test_runner_maintenance_tmux_repair.py -q`.
Then `gcode grep -w -E 'title_lifecycle|normalize_automatic_title_refs|format_provisional_session_title|tmux_window_naming|register_title_listener|schedule_tmux_window_rename' src tests`
must return nothing. `gcode grep -w -E 'title_source|set_title|apply_title_mutation' src`
must return nothing outside `src/gobby/install/shared/skills/`, which 5.1
updates. Then `cargo nextest run -p gobby-core`,
`cargo nextest run -p gobby-daemon --test cli_contract`, and
`uv run gobby db schema-identity --check` if the checkout provides it, else the
`gdaemon schema plan` read-only check that `restart` runs.

**Acceptance:**

- 3.4.1 - Migration 459 nulls every title not marked `manual` outside
  communications sessions and drops `sessions.title_source`. behavior:
  "IS DISTINCT FROM 'manual'" in
  `crates/gcore/assets/schema/migrations/459_drop_session_title_source.sql`.
- 3.4.2 - `title_source` is no longer a live mutable seed field and migration
  459 is embedded. test:
  `crates/gcore/src/schema/verify_tests.rs::title_source_is_not_a_live_mutable_seed_field`.
- 3.4.3 - The schema identity carriers match the migrated schema. file:
  `src/gobby/storage/schema_expected_identity.json`.
- 3.4.4 - Migration 459, run in isolation against seeded `sessions` rows,
  keeps a title stamped `manual` before the stop and a communications title,
  nulls the `task`, `provisional` and NULL-source titles, and drops
  `sessions.title_source`. No other migration drops that column. test:
  `tests/storage/test_session_title_source_migration.py::test_migration_459_keeps_manual_titles_and_drops_title_source`.
- 3.4.5 - Creating a task with `claim=true`, claiming it and closing it leave
  the session title unchanged. test:
  `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_claim_and_close_leave_session_title`.
- 3.4.6 - Registering a session without a title stores no title, and a
  registration with an explicit title stores it. test:
  `tests/storage/sessions/test_storage_sessions_registration.py::test_register_writes_only_explicit_titles`.
- 3.4.7 - A spawned child and a materialized session start untitled. test:
  `tests/hooks/test_session_materialize.py::test_session_start_leaves_title_unset`.
- 3.4.8 - A web-chat `/clear` successor carries its predecessor's title
  verbatim, and an untitled predecessor yields an untitled successor. test:
  `tests/sessions/test_clear_continuation.py::test_clear_successor_copies_title_verbatim`.
- 3.4.9 - `SessionManager` no longer exposes `normalize_automatic_title_refs`,
  and the first grep in Verification planned returns nothing. test:
  `tests/storage/test_sessions_import.py::test_session_manager_public_method_signatures_are_stable`.
- 3.4.10 - `build_task_tree` lives in `_crud_tree.py` and the web-chat clear
  successor commit lives in `clear_web_chat_successor.py`. file:
  `src/gobby/sessions/clear_web_chat_successor.py`.
- 3.4.11 - `gobby-sessions` registers no `set_title` tool. test:
  `tests/sessions/test_handoff.py::test_set_title_tool_is_not_registered`.
- 3.4.12 - `POST /api/sessions/{id}/rename` stores the stripped title, a blank
  value clears it, and the response carries no `title_source`. test:
  `tests/servers/routes/test_servers_routes_sessions_routes.py::test_rename_session_writes_title_only`.
- 3.4.13 - `Session` has no `title_source` field. test:
  `tests/storage/sessions/test_storage_sessions_models.py::test_session_has_no_title_source`.
- 3.4.14 - `register_session` takes no `title_source` argument. test:
  `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py::test_register_session_has_no_title_source`.
- 3.4.15 - The repair loop expires sessions on a missing tmux server or pane and
  renames no window. test:
  `tests/test_runner_maintenance_tmux_repair.py::test_missing_pane_expires_sessions_without_rename`.
- 3.4.16 - `update_title` runs no tmux rename and no title listener. test:
  `tests/storage/sessions/test_title_fields.py::test_update_title_has_no_side_effects`.
- 3.4.17 - `probe_tmux_pane` lives in `runner_tmux_repair.py`, and
  `tmux_window_naming.py` no longer exists. file:
  `src/gobby/runner_tmux_repair.py`.

## P4: Read-time display name
`kind: framing`

**Goal:** every session list names a session the way gclient names its seat.

### 4.1 One display_name helper for the API, Telegram and the CLI [category: code] (depends: 3.4)
`kind: deliverable`

Targets:
- `src/gobby/sessions/display_name.py`
- `src/gobby/storage/sessions/_title_defaults.py::*` — operation: delete — scope-reason: provider_title_label moves to the new display_name module
- `src/gobby/storage/sessions/_query.py::*` — scope-reason: add the bulk pane-label query beside fetch_task_refs_by_session
- `src/gobby/servers/routes/sessions/core.py::*` — scope-reason: serve display_name on every listed session
- `src/gobby/communications/agent_labels.py::*` — scope-reason: label agents by display_name
- `src/gobby/communications/manager.py::*` — scope-reason: pass display names to the agent label
- `src/gobby/communications/telegram_actions.py::*` — scope-reason: pass display names to the agent menu
- `src/gobby/communications/telegram_fallback.py::*` — scope-reason: pass display names to the agent label
- `src/gobby/cli/sessions.py::*` — scope-reason: list and show print display_name; summarize_session moves out
- `src/gobby/cli/sessions_summary.py`
- `tests/sessions/test_display_name.py`
- `tests/storage/sessions/test_task_refs.py::*` — scope-reason: bulk pane-label query cases
- `tests/storage/test_sessions_import.py::*` — scope-reason: session manager surface gains the pane-label query
- `tests/servers/routes/test_servers_routes_sessions_routes.py::*` — scope-reason: list serves display_name
- `tests/communications/test_agent_labels.py::*` — scope-reason: labels from display names
- `tests/communications/test_telegram_actions.py::*` — scope-reason: menu labels from display names
- `tests/cli/test_cli_sessions.py::*` — scope-reason: list and show print display_name
- `tests/cli/test_cli_sessions_coverage.py::*` — scope-reason: import the moved summary helpers
- `tests/utils/test_daemon_git_inventory.py::*` — scope-reason: the summarize git boundary entry names its new module

**Granularity:** nine production files, one behavior: one read-time name for
every session list. The helper, its query and its three readers ship together
so no surface keeps a second naming rule.

**Research context:** New module `src/gobby/sessions/display_name.py`:

- `provider_label(source: str) -> str`: `provider_title_label` and
  `_PROVIDER_TITLE_LABELS`, moved from `storage/sessions/_title_defaults.py`
  (~11-22, ~41-46), which is then deleted.
- `display_name(*, pane_label, title, source, newest_task_seq) -> str`: the
  stripped pane label when non-blank, else the stripped title when non-blank,
  else `f"{provider_label(source)} · #{newest_task_seq}"`, else
  `provider_label(source)`.
- `display_names(manager, sessions) -> dict[str, str]`: one call to
  `fetch_pane_labels_by_session` and one to `fetch_task_refs_by_session` for
  all ids. The newest claimed task is the highest seq in `claimed`, which that
  query returns ordered by `seq_num` (`storage/sessions/_query.py` ~321-370).

New `fetch_pane_labels_by_session(session_ids) -> dict[str, str | None]` on the
`_query.py` mixin, so `SessionManager` exposes it beside
`fetch_task_refs_by_session`. One query:

```sql
SELECT DISTINCT ON (t.session_id) t.session_id, p.label
FROM terminals t
LEFT JOIN workspace_panes p ON p.terminal_id = t.id
WHERE t.session_id = ANY(%s) AND t.state IN ('pending', 'live')
ORDER BY t.session_id, t.updated_at DESC
```

This follows the newest-live-terminal rule of `get_live_for_session`
(`storage/terminals.py` ~633-644, read-only). The `LEFT JOIN` keeps a newer
unplaced terminal (the tmux fallback), so it yields `None` and never falls
back to an older placed terminal. `idx_workspace_panes_terminal` (migration
440) makes `terminal_id` unique, so each terminal joins at most one pane.

Readers:

- `/api/sessions` list (`servers/routes/sessions/core.py` ~396-582) already
  bulk-loads task refs (~530). Add `display_name` to each session dict next to
  `to_dict` (~550). Web listeners refetch this route on session events.
- Telegram: `agent_label` (`communications/agent_labels.py`) parses titles today.
  Replace it with lookups in a `display_names(...)` mapping. `agent_menu_labels(sessions, names)`
  keeps its 46-character bound and duplicate suffix. Callers compute the
  mapping once per render with their session manager:
  `communications/manager.py` (~331), `telegram_actions.py` (~230-232,
  ~378-387) and `telegram_fallback.py` (~28).
- CLI: `list_sessions` (`cli/sessions.py` ~236-292) prints `display_name` in the
  title column (~273-291). `show_session` (~298-338) prints
  `Name: <display_name>` in place of the `Title:` line (~320-321).

Split: `src/gobby/cli/sessions.py` is 879 lines. Move `summarize_session`
(~512-751, with its inner `_gen_summary`), `_format_turns_for_llm` (~167-187)
and `_append_summary_notes` (~190-199) into the new
`src/gobby/cli/sessions_summary.py`. The moved command is a plain
`click.command`. `cli/sessions.py` imports it and registers it on the
`sessions` group with `add_command`, so `gobby sessions summarize` keeps its
name. The moved code reads the session manager through a function-local import
to avoid an import cycle. Retarget the git boundary entry
(`tests/utils/test_daemon_git_inventory.py` ~290) and the
`_format_turns_for_llm` import (`tests/cli/test_cli_sessions_coverage.py` ~11).

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_display_name.py tests/storage/sessions/test_task_refs.py tests/storage/test_sessions_import.py tests/servers/routes/test_servers_routes_sessions_routes.py tests/communications tests/cli/test_cli_sessions.py tests/cli/test_cli_sessions_coverage.py tests/utils/test_daemon_git_inventory.py -q`.

**Acceptance:**

- 4.1.1 - `display_name` returns the pane label, else the title, else
  `<Provider> · #<newest claimed seq>`, else `<Provider>`. test:
  `tests/sessions/test_display_name.py::test_display_name_ladder`.
- 4.1.2 - `fetch_pane_labels_by_session` returns, in one query, the label of the
  pane bound to each session's newest pending or live terminal. It returns
  `None` when that terminal has no pane, even if an older terminal has one,
  and when the session has no such terminal. test:
  `tests/storage/sessions/test_task_refs.py::test_fetch_pane_labels_by_session`.
- 4.1.3 - Every session in the `/api/sessions` list carries `display_name`.
  test:
  `tests/servers/routes/test_servers_routes_sessions_routes.py::test_list_sessions_serves_display_name`.
- 4.1.4 - Telegram agent buttons and labels use display names. test:
  `tests/communications/test_agent_labels.py::test_menu_labels_use_display_names`.
- 4.1.5 - `gobby sessions list` and `show` print the display name. test:
  `tests/cli/test_cli_sessions.py::test_list_and_show_print_display_name`.
- 4.1.6 - `gobby sessions summarize` is registered from `cli/sessions_summary.py`.
  test: `tests/cli/test_cli_sessions_coverage.py::test_summarize_command_registered`.

### 4.2 Web session lists read display_name [category: code] (depends: 3.4, 4.1)
`kind: deliverable`

Targets:
- `web/src/lib/sessionTitle.ts::*` — scope-reason: activity titles render the ref then display_name
- `web/src/types/sessions.ts::*` — scope-reason: add display_name and drop title_source
- `web/src/components/activity/SessionsTab.entries.ts::*` — scope-reason: caller of the activity title helpers
- `web/src/components/activity/terminal/terminalSessions.ts::*` — scope-reason: caller of the activity title helpers
- `web/src/components/chat/CommandBar.tsx::*` — scope-reason: caller of the session title helpers
- `web/src/components/chat/CommandPalette.tsx::*` — scope-reason: caller of the session title helpers
- `web/src/components/chat/ResumeSessionModal.tsx::*` — scope-reason: caller of the session title helpers
- `web/src/lib/__tests__/sessionTitle.test.ts::*` — scope-reason: display_name cases
- `web/src/components/activity/terminal/__tests__/terminalSessions.test.ts::*` — scope-reason: display_name cases
- `web/src/components/activity/__tests__/SessionsTab.phase1-red.test.tsx::*` — scope-reason: drop title_source fixtures

**Granularity:** seven production files, one behavior: web session rows render
the server's display name. The helper and its five importers change together.

**Research context:** `web/src/lib/sessionTitle.ts` reads `title` and
`title_source`: `getActivitySessionTitleParts` hides a `provisional` title and
strips a ref prefix, and `getActivitySessionTitle` renders `${ref}: ${title}`
or the bare ref. Its importers are `SessionsTab.entries.ts`,
`terminalSessions.ts`, `CommandBar.tsx`, `CommandPalette.tsx` and
`ResumeSessionModal.tsx`. Change: `Session` in `types/sessions.ts` (~24-25)
gains `display_name: string` and loses `title_source`. The activity helpers
render `${ref}: ${display_name}` and keep a ref-only fallback for a row without
`display_name`. Remove the provisional check and the prefix stripping
(`SESSION_PREFIX`, `stripSessionTitlePrefix`) once no caller needs them.
Chat surfaces that hold a local chat name (`DEFAULT_SESSION_TITLE`, rename)
keep reading `title`. The web chat rename hook keeps its route call as is.

Verification planned: `npm --prefix web run test -- sessionTitle terminalSessions SessionsTab`,
`npm --prefix web run typecheck`, `npm --prefix web run lint`.

**Acceptance:**

- 4.2.1 - Activity rows render `<ref>: <display_name>`, and a row without
  `display_name` renders the ref alone. test:
  `web/src/lib/__tests__/sessionTitle.test.ts::renders ref then display_name`.
- 4.2.2 - The web `Session` type carries `display_name` and no `title_source`.
  file: `web/src/types/sessions.ts`.
- 4.2.3 - The web tests cover a labelled session, a titled session and the
  provider fallback. file: `web/src/lib/__tests__/sessionTitle.test.ts`.

## P5: Documentation
`kind: framing`

**Goal:** the guides, the session skill and the shared role text describe the
pane-label model.

### 5.1 Session naming docs [category: docs] (depends: 1.1, 2.1, 3.4, 4.2)
`kind: deliverable`

Targets:
- `docs/guides/sessions.md`
- `docs/contracts/session-boundary.md`
- `docs/guides/http-endpoints.md`
- `docs/reference-audit/sessions.json::*` — scope-reason: drop the set_title entries and restate the title contract
- `src/gobby/install/shared/skills/gobby/references/sessions/discovery.md`
- `.gobby/roles/_common.md`

**Research context:** Text to replace:

- `docs/guides/sessions.md` (~90-97 title contract, ~238 `set_title` tool row):
  replace the deterministic title contract with the pane-label model
  (Decisions 1-7). Remove the `set_title` row.
- `docs/contracts/session-boundary.md` (~281-293): the `/clear` successor copies
  the predecessor's title verbatim, and nothing else titles a session.
- `docs/guides/http-endpoints.md` (~204, ~238): the session list serves
  `display_name`. Rename writes the optional name.
- `docs/reference-audit/sessions.json`: the `set_title` entries (~259, ~267)
  go, and the title contract result (~1321) states the new model. The audit
  is hand-maintained and loaded by `tests/skills/reference_library_helpers.py`.
- `references/sessions/discovery.md` (~6, ~28): name the pane label and
  `rename_workspace_item` for seat names, and the rename route for chat names.
- `.gobby/roles/_common.md` (~4): replace the `set_title` instruction with
  "your pane label names your seat".
- Spawn placement: document `title` as optional, defaulting to the agent
  definition name, wherever these files describe placement.

After the last leaf lands, the PD supersedes memories 0f664595 (gclient title
ladder) and a3e246d0 (title precedence) with the pane-label model. That step
has no file artifact.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/skills -q -k reference`,
then `gcode grep -w -E 'set_title|title_source' docs/guides docs/contracts docs/reference-audit src/gobby/install/shared .gobby/roles`,
which must return nothing.

**Acceptance:**

- 5.1.1 - The sessions guide describes the pane label as the seat name and the
  read-time display name. behavior: "pane label" in `docs/guides/sessions.md`.
- 5.1.2 - The session boundary contract states that a `/clear` successor copies
  the title verbatim. behavior: "verbatim" in
  `docs/contracts/session-boundary.md`.
- 5.1.3 - The discovery reference names `rename_workspace_item` for seat names.
  behavior: "rename_workspace_item" in
  `src/gobby/install/shared/skills/gobby/references/sessions/discovery.md`.
- 5.1.4 - The shared role text names the pane label as the seat name. behavior:
  "pane label" in `.gobby/roles/_common.md`.
- 5.1.5 - The HTTP guide documents `display_name` on the session list. behavior:
  "display_name" in `docs/guides/http-endpoints.md`.

## V1 Plan Changelog
`kind: verification`

- 2026-10-02: Initial draft for #23337 on the PD's 15:04 rulings (Q1-Q6), the
  15:28 render format (cbf258a3) and the 15:47 clear-successor ruling.
- 2026-10-02: Adversary round 1 (gobby#14550) and L1's 1.1 prep
  (gobby#14909). B1 and B2: 3.1 drops the provisional-title tests and the
  imports of `format_provisional_session_title` and `provider_title_label`,
  and targets `test_agent_spawn_routes.py`. N2 (PD ruling 16:06): 4.1 and 4.2
  depend on 3.4. N3: the pane-label query left-joins panes. N4: 1.1.5, 3.1.5
  and 4.2.1 name tests. N1, N5, N6 and L1's items revise 1.1. Decision 12
  makes the project prefix unconditional, and the pane label leads the
  gclient name ladder (Decision 2, 1.1.7). Found work: the 1.1, 3.4 and V2
  cargo commands use the nextest form.
- 2026-10-02: Adversary round 2 (gobby#14550): consensus on `86db544`. The
  PD accepted Decision 12 and the name-ladder call at 16:59. The 1.1
  granularity nit is non-blocking and left as-is.
- 2026-10-02: Close review `cfd753e3` (run `0c2c56ba`) was invalid: migration
  458 nulled a name written between 3.2 going live and the cutover. PD ruling
  19:48: sequencing. Decision 13, the Constraints, 3.1, 3.2 and 3.4 (3.4.4)
  hold 3.1-3.3 until 3.4's cutover. The Writer extended the hold to 3.1,
  whose clear-successor copy writes no `title_source`. M1 withdrawn (memory
  `f5577ae0`); the Adversary (gobby#14550) derives a fresh M1.
- 2026-10-02: Close review `173e8399` was invalid: 3.4.4 checked a comment
  phrase, so the Decision 13 hold had no real acceptance. PD ruling 22:51,
  option A: 3.1, 3.2 and 3.3 fold into 3.4 as one deliverable, so a partial
  landing cannot happen. 3.4 keeps both splits (`_crud_tree.py` and
  `clear_web_chat_successor.py`). 3.4.4 is now an isolated migration 458
  test. Old 3.1.1-3.1.6 are 3.4.5-3.4.10, 3.2.1-3.2.4 are 3.4.11-3.4.14,
  and 3.3.1-3.3.3 are 3.4.15-3.4.17. Options B (wind-down hold code) and C
  (per-writer stamping) are rejected. Decisions 9 and 13, the Overview, the
  Constraints and one 1.1 sentence follow. Found work: the title-source grep
  now excludes `src/gobby/install/shared/skills/`, which 5.1 updates. M1
  withdrawn (memory `f5577ae0`); the Adversary (gobby#14550) derives a fresh
  M1 after round 4.
- 2026-10-03: Close review `061775b8` (run `eb2d5d6c`) was invalid: #23271
  '`api_keys`, key format, issuance routes, and local-key adoption' owns
  migration 458 (`458_add_api_keys.sql`, `48470a3a9f`). PD ruling 00:26 and
  GO 00:41: #23271 keeps 458, and this plan renumbers to 459, free on 0.5.0
  and every branch. Every earlier 458 site, including 3.4.4's test name, is
  now 459 (`f9fcdad`). M1 withdrawn (memory `f5577ae0`); the Adversary
  (gobby#14579) derives a fresh M1.

## V2: Verification
`kind: verification`

Run after each leaf's final edit and again before the PD lands the branch:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_workspace_agent_panes.py tests/mcp_proxy/tools/spawn_agent tests/mcp_proxy/tools/tasks/test_create_task.py tests/mcp_proxy/tools/tasks/test_close_task_flow.py tests/mcp_proxy/tools/sessions tests/sessions tests/storage/sessions tests/storage/test_sessions_import.py tests/storage/test_local_model_flags.py tests/storage/test_session_title_source_migration.py tests/servers/routes tests/servers/test_http_models.py tests/servers/test_session_control.py tests/servers/websocket/chat/test_stream_persistence.py tests/hooks tests/test_runner_maintenance_tmux_repair.py tests/communications tests/cli/test_cli_sessions.py tests/cli/test_cli_sessions_coverage.py tests/utils/test_daemon_git_inventory.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
cargo nextest run -p gobby-client
cargo nextest run -p gobby-core
npm --prefix web run test -- sessionTitle terminalSessions SessionsTab
uv run gobby plans validate .gobby/plans/pane-seat-titles.md -p /Users/josh/Projects/gobby
```

Live checks after the PD-owned restart and the gclient install: a pane labelled
"L3 hooks" shows `gobby#<seq>: L3 hooks` in its header and Agents row; a
placed spawn without `title` gets the definition name as its pane label;
claiming and closing a task leaves the session title unset; the web session
list and Telegram agent menu show the pane label for a bound session. After
the 3.4 cutover, `sessions.title_source` no longer exists. Do not run the full
pytest suite.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: gclient renders the project ref and pane label
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: A pane labelled "L3 hooks" running session `#15011`
    in project `gobby` renders `gobby#15011: L3 hooks` in its header. test: `crates/gclient/src/ui/pane_chrome/tests.rs::header_names_project_ref_and_pane_label`.

    1.1.2: An unlabelled pane renders `gobby#15011: Claude` for a Claude session.
    test: `crates/gclient/src/ui/pane_chrome/tests.rs::header_falls_back_to_provider`.

    1.1.3: Agents line 1 renders the same `<project>#<seq>: <seat label>` text as
    the header in every sort and grouping (Decision 12), and line 2 keeps the task
    ticker. test: `crates/gclient/src/ui/sidebar_rows/tests.rs::agent_line_one_names_seat`.

    1.1.4: After a `pane.renamed` event, the next frame''s header and Agents line
    1 carry the new label. test: `crates/gclient/tests/sidebar_model.rs::pane_rename_moves_seat_label`.

    1.1.5: A session''s title and its run''s agent name no longer name the seat: an
    unlabelled pane running a titled session from a named definition gets the seat
    label `Claude`, and the 1.1 grep for `manual_title`, `title_source` and `definition_label`
    returns nothing. test: `crates/gclient/tests/sidebar_model.rs::session_title_does_not_name_seat`.

    1.1.6: The gclient version is bumped in its manifest, the lockfile and the install
    pin. file: `src/gobby/install/version_pins.py`.

    1.1.7: Attention chrome names a labelled seat `#<seq>: <pane label>`. test: `crates/gclient/tests/attention_flow.rs::attention_names_seat_by_pane_label`.'
  labels:
  - covers:pane-seat-titles:1.1:1.1.1
  - covers:pane-seat-titles:1.1:1.1.2
  - covers:pane-seat-titles:1.1:1.1.3
  - covers:pane-seat-titles:1.1:1.1.4
  - covers:pane-seat-titles:1.1:1.1.5
  - covers:pane-seat-titles:1.1:1.1.6
  - covers:pane-seat-titles:1.1:1.1.7
  tdd: true
  source_section: '1.1'
  implementation_domain: fullstack
- title: Spawn writes the placement title as the pane label
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.1.1: A tab placement''s new pane carries the placement title
    as its label, as a split placement''s pane already does. test: `tests/terminals/test_workspace_agent_panes.py::test_tab_placement_labels_pane`.

    2.1.2: A placement without `title` is filled with the agent definition name, and
    a blank `title` is still refused. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_missing_title_defaults_to_agent_name`.

    2.1.3: A second placement with the same title into one workspace is refused as
    a held seat. test: `tests/terminals/test_workspace_agent_panes.py::test_same_title_seat_is_refused`.'
  labels:
  - covers:pane-seat-titles:2.1:2.1.1
  - covers:pane-seat-titles:2.1:2.1.2
  - covers:pane-seat-titles:2.1:2.1.3
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Automatic titles, the title source and tmux naming retire with migration
    459
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '3.4.1: Migration 459 nulls every title not marked `manual`
    outside communications sessions and drops `sessions.title_source`. behavior: "IS
    DISTINCT FROM ''manual''" in `crates/gcore/assets/schema/migrations/459_drop_session_title_source.sql`.

    3.4.2: `title_source` is no longer a live mutable seed field and migration 459
    is embedded. test: `crates/gcore/src/schema/verify_tests.rs::title_source_is_not_a_live_mutable_seed_field`.

    3.4.3: The schema identity carriers match the migrated schema. file: `src/gobby/storage/schema_expected_identity.json`.

    3.4.4: Migration 459, run in isolation against seeded `sessions` rows, keeps a
    title stamped `manual` before the stop and a communications title, nulls the `task`,
    `provisional` and NULL-source titles, and drops `sessions.title_source`. No other
    migration drops that column. test: `tests/storage/test_session_title_source_migration.py::test_migration_459_keeps_manual_titles_and_drops_title_source`.

    3.4.5: Creating a task with `claim=true`, claiming it and closing it leave the
    session title unchanged. test: `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_claim_and_close_leave_session_title`.

    3.4.6: Registering a session without a title stores no title, and a registration
    with an explicit title stores it. test: `tests/storage/sessions/test_storage_sessions_registration.py::test_register_writes_only_explicit_titles`.

    3.4.7: A spawned child and a materialized session start untitled. test: `tests/hooks/test_session_materialize.py::test_session_start_leaves_title_unset`.

    3.4.8: A web-chat `/clear` successor carries its predecessor''s title verbatim,
    and an untitled predecessor yields an untitled successor. test: `tests/sessions/test_clear_continuation.py::test_clear_successor_copies_title_verbatim`.

    3.4.9: `SessionManager` no longer exposes `normalize_automatic_title_refs`, and
    the first grep in Verification planned returns nothing. test: `tests/storage/test_sessions_import.py::test_session_manager_public_method_signatures_are_stable`.

    3.4.10: `build_task_tree` lives in `_crud_tree.py` and the web-chat clear successor
    commit lives in `clear_web_chat_successor.py`. file: `src/gobby/sessions/clear_web_chat_successor.py`.

    3.4.11: `gobby-sessions` registers no `set_title` tool. test: `tests/sessions/test_handoff.py::test_set_title_tool_is_not_registered`.

    3.4.12: `POST /api/sessions/{id}/rename` stores the stripped title, a blank value
    clears it, and the response carries no `title_source`. test: `tests/servers/routes/test_servers_routes_sessions_routes.py::test_rename_session_writes_title_only`.

    3.4.13: `Session` has no `title_source` field. test: `tests/storage/sessions/test_storage_sessions_models.py::test_session_has_no_title_source`.

    3.4.14: `register_session` takes no `title_source` argument. test: `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py::test_register_session_has_no_title_source`.

    3.4.15: The repair loop expires sessions on a missing tmux server or pane and
    renames no window. test: `tests/test_runner_maintenance_tmux_repair.py::test_missing_pane_expires_sessions_without_rename`.

    3.4.16: `update_title` runs no tmux rename and no title listener. test: `tests/storage/sessions/test_title_fields.py::test_update_title_has_no_side_effects`.

    3.4.17: `probe_tmux_pane` lives in `runner_tmux_repair.py`, and `tmux_window_naming.py`
    no longer exists. file: `src/gobby/runner_tmux_repair.py`.'
  labels:
  - covers:pane-seat-titles:3.4:3.4.1
  - covers:pane-seat-titles:3.4:3.4.2
  - covers:pane-seat-titles:3.4:3.4.3
  - covers:pane-seat-titles:3.4:3.4.4
  - covers:pane-seat-titles:3.4:3.4.5
  - covers:pane-seat-titles:3.4:3.4.6
  - covers:pane-seat-titles:3.4:3.4.7
  - covers:pane-seat-titles:3.4:3.4.8
  - covers:pane-seat-titles:3.4:3.4.9
  - covers:pane-seat-titles:3.4:3.4.10
  - covers:pane-seat-titles:3.4:3.4.11
  - covers:pane-seat-titles:3.4:3.4.12
  - covers:pane-seat-titles:3.4:3.4.13
  - covers:pane-seat-titles:3.4:3.4.14
  - covers:pane-seat-titles:3.4:3.4.15
  - covers:pane-seat-titles:3.4:3.4.16
  - covers:pane-seat-titles:3.4:3.4.17
  tdd: true
  source_section: '3.4'
  implementation_domain: backend
- title: One display_name helper for the API, Telegram and the CLI
  category: code
  task_type: feature
  depends_on:
  - '3.4'
  validation_criteria: "4.1.1: `display_name` returns the pane label, else the title,\
    \ else `<Provider> \xB7 #<newest claimed seq>`, else `<Provider>`. test: `tests/sessions/test_display_name.py::test_display_name_ladder`.\n\
    4.1.2: `fetch_pane_labels_by_session` returns, in one query, the label of the\
    \ pane bound to each session's newest pending or live terminal. It returns `None`\
    \ when that terminal has no pane, even if an older terminal has one, and when\
    \ the session has no such terminal. test: `tests/storage/sessions/test_task_refs.py::test_fetch_pane_labels_by_session`.\n\
    4.1.3: Every session in the `/api/sessions` list carries `display_name`. test:\
    \ `tests/servers/routes/test_servers_routes_sessions_routes.py::test_list_sessions_serves_display_name`.\n\
    4.1.4: Telegram agent buttons and labels use display names. test: `tests/communications/test_agent_labels.py::test_menu_labels_use_display_names`.\n\
    4.1.5: `gobby sessions list` and `show` print the display name. test: `tests/cli/test_cli_sessions.py::test_list_and_show_print_display_name`.\n\
    4.1.6: `gobby sessions summarize` is registered from `cli/sessions_summary.py`.\
    \ test: `tests/cli/test_cli_sessions_coverage.py::test_summarize_command_registered`."
  labels:
  - covers:pane-seat-titles:4.1:4.1.1
  - covers:pane-seat-titles:4.1:4.1.2
  - covers:pane-seat-titles:4.1:4.1.3
  - covers:pane-seat-titles:4.1:4.1.4
  - covers:pane-seat-titles:4.1:4.1.5
  - covers:pane-seat-titles:4.1:4.1.6
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Web session lists read display_name
  category: code
  task_type: feature
  depends_on:
  - '3.4'
  - '4.1'
  validation_criteria: '4.2.1: Activity rows render `<ref>: <display_name>`, and a
    row without `display_name` renders the ref alone. test: `web/src/lib/__tests__/sessionTitle.test.ts::renders
    ref then display_name`.

    4.2.2: The web `Session` type carries `display_name` and no `title_source`. file:
    `web/src/types/sessions.ts`.

    4.2.3: The web tests cover a labelled session, a titled session and the provider
    fallback. file: `web/src/lib/__tests__/sessionTitle.test.ts`.'
  labels:
  - covers:pane-seat-titles:4.2:4.2.1
  - covers:pane-seat-titles:4.2:4.2.2
  - covers:pane-seat-titles:4.2:4.2.3
  tdd: true
  source_section: '4.2'
  implementation_domain: frontend
- title: Session naming docs
  category: docs
  task_type: feature
  depends_on:
  - '1.1'
  - '2.1'
  - '3.4'
  - '4.2'
  validation_criteria: '5.1.1: The sessions guide describes the pane label as the
    seat name and the read-time display name. behavior: "pane label" in `docs/guides/sessions.md`.

    5.1.2: The session boundary contract states that a `/clear` successor copies the
    title verbatim. behavior: "verbatim" in `docs/contracts/session-boundary.md`.

    5.1.3: The discovery reference names `rename_workspace_item` for seat names. behavior:
    "rename_workspace_item" in `src/gobby/install/shared/skills/gobby/references/sessions/discovery.md`.

    5.1.4: The shared role text names the pane label as the seat name. behavior: "pane
    label" in `.gobby/roles/_common.md`.

    5.1.5: The HTTP guide documents `display_name` on the session list. behavior:
    "display_name" in `docs/guides/http-endpoints.md`.'
  labels:
  - covers:pane-seat-titles:5.1:5.1.1
  - covers:pane-seat-titles:5.1:5.1.2
  - covers:pane-seat-titles:5.1:5.1.3
  - covers:pane-seat-titles:5.1:5.1.4
  - covers:pane-seat-titles:5.1:5.1.5
  tdd: false
  source_section: '5.1'
  assigned_agent: tech-writer
```

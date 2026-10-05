# Plugin system research and design notes (#20201, 2026-10)

Task: #20201 (Plan Gobby plugin system on the public CLI and daemon API).
Status: deferred until after 1.0. Josh, 2026-10-05: "The plugin system draft is
premature. We need to defer until after 1.0". No plan was drafted. These notes
are the research and the pre-draft design as far as the Lane 7 Plan Writer
gobby#15429 took them, kept for whoever resumes the task.

Unless a section says otherwise, line references were verified against 0.5.0
at `3e2aca2950`. Herdr references point at the fork point, herdr v0.8.0
(`346411fa21afd297f5ed3b3fa56f9e3fbf7654b7`, Apache-2.0); gcode cannot serve
the clone, so cite git objects. Decisions marked "A" were the recommended
answers to five questions sent to Josh. He deferred the task instead of
answering, so they are proposals, not rulings.

Found work fixed under #20201 before the deferral: `a5b48e523e`
(`docs/guides/http-endpoints.md` /ws auth; stale ROADMAP plan paths).

---

## Part 1: Research notes (before the claim)

### Task obligations (validation_criteria)
- A plan under .gobby/plans/ that validates. It covers the manifest schema, trust model, host surface (actions, event hooks, panes), install and registry lifecycle, and the public-API-only constraint.
- It records the relationship to the herdr-terminal-client epic (which needs gclient and the daemon API).
- herdr-client-completion adopted it as D3 (items 3.1.4, 5.1.3). The plugin-menu keymap entry stays reserved and hidden behind crates/gclient/tests/ui_carve_guard.rs::render_workspace_composes_imported_chrome until this plan lands it.
- ROADMAP.md "Side quests" names #20201 (true at ROADMAP.md:297).
- Labels: deferred-post-1.0, sidequest. Parent path 22949.20201. task_type architecture_doc.

### Gobby side
- D1: .gobby/plans/completed/herdr-terminal-client.md:1731-1744 (D1.1 label only, no body).
- herdr-client-completion (completed/): constraints :44, :59-61; 3.1 keymap :1360-1362, :1385-1387; 3.1.4 :1425; 5.1.3 :2335; D3 :2383-2404.
- ROADMAP: principle at :116-118. Decision 7 (:367-368): public API only, client-side Rust. Decision 16 (:405-410): no dylib plugins.
- gclient has only ONE reserved entry: custom_command at prefix+m (crates/gclient/src/ui/keymap/names.rs:296-304). Action::is_reserved :374-376; filtered in keymap.rs:191-263; ReservedAction errors :70-71, :163-164; the dispatch no-op is app/live_loop/actions.rs:404-407.
- Guard test ui_carve_guard.rs:251-318 (help excludes custom_command and reserved entries). Its FORBIDDEN list (:15-29) includes crate::plugin and plugin_command, but the scan walks only src/ui (:155-171). Also tests/keymap.rs:194-207, :285-316; tests/parity/chrome.rs:996-1000 is deferred with TODO(#20201).
- gclient transport: daemon_url (GOBBY_DAEMON_URL, GOBBY_PORT, endpoint file). Token via gobby_core::local_token (env GOBBY_AGENT_API_TOKEN, then the 0600 file); do not read the token file. REST bearer (daemon/rest.rs:208); WS /ws bearer (daemon/live_reader.rs:80-103).
- Event source: WS subscribe and unsubscribe (servers/websocket/server.py:522-523). Filters live in broadcast.py `_is_subscribed`: "*", exact type, type:key=value, plus hook_event narrowing. gclient subscribes to 5 types today (daemon/live.rs:32-38). Hook webhooks are a second push surface (docs/guides/webhooks-and-plugins.md:20-178).
- CLI groups: src/gobby/cli/__init__.py:20-75 includes panes and workspaces; there is no plugins group. gclient CLI verbs: command/verbs.rs:14-36.
- Retired: Python hook plugins (src/gobby/hooks/plugins.py removed; workflows-v2.md:1118-1119, 1533-1535); webhooks-and-plugins.md:9-10, 251-266; skill reference integrations/plugins.md. docs/plans/plugins-v2-draft.md is an orphan Python daemon-side bundler that conflicts with ROADMAP decisions 7 and 16; do not use it.
- Possibly relevant: completed/herdr-interface-backend-foundation.md:322 treats the skill marketplace as the distribution counterpart.

### Herdr side (plugin modules at v0.8.0)
- Manifest parser: src/app/api/plugins/manifest.rs. Fields:
  - id, name, version; min_herdr_version (required, semver gate); description; platforms.
  - [[build]], [[startup]].
  - [[actions]] {id, title, description, contexts, platforms, command}.
  - [[events]] {on, platforms, command}.
  - [[panes]] {id, title, description, platforms, placement overlay|popup|split|tab|zoomed, width, height (popup only), command}.
  - [[link_handlers]] {id, title, pattern regex, action, platforms}.
  - Rules: id charset, unique ids, non-empty argv. No schema version; unknown keys are ignored.
- Exec:
  - src/plugin_command.rs builds argv with no shell; cwd is the plugin root.
  - src/app/api/plugins/runtime.rs runs on a detached thread with 64 KiB stdout/stderr capture, inherited stdin, no timeout or kill, at most 32 concurrent commands, and logs in a 200-entry in-memory ring.
  - Env: HERDR_SOCKET_PATH, HERDR_BIN_PATH, HERDR_PLUGIN_ID/ROOT/CONFIG_DIR/STATE_DIR, CONTEXT_JSON, ACTION_ID, EVENT/EVENT_JSON, WORKSPACE/TAB/PANE_ID, CLICKED_URL.
  - Build commands run CLI-side with stdin null and HERDR_* stripped.
- Paths: src/plugin_paths.rs uses config_dir/plugins/github/<hash>, config_dir/plugins/config/<id> and state_dir/plugins/<id> (XDG). Registration is explicit only.
- Registry: src/persist/plugin_registry.rs keeps config_dir/plugins.json plus .plugins.lock and writes by atomic rename.
  - Each entry holds the cached manifest, enabled, paths, warnings and source {local|github, owner, repo, subdir, ref, resolved_commit, managed_path, installed_ms}.
  - There is no trust or hash field. Manifests are re-parsed on load.
- CLI: src/cli/plugin.rs.
  - install owner/repo[/subdir] --ref --yes: preview, then y/N, then build, with rollback.
  - Also: uninstall, link/unlink, list, config-dir, enable/disable, action list/invoke, log(s), pane open/focus/close.
  - There is no update verb. An offline fallback edits the registry directly.
- Host surface:
  - Actions run from the CLI, the socket, the plugin_action keybinding and link clicks.
  - 22 hookable events fire async and fire-and-forget; they cannot veto.
  - Startup hooks.
  - Panes are real terminal panes; a popup is a singleton.
  - Link handlers match by regex, first match wins.
- Trust: an install-time preview and y/N only. No signatures, pins, sandbox or permissions.
- API boundary: no SDK; "the entire Herdr CLI is the plugin API".
- Port or redesign: the manifest, paths, registry and command builder port cleanly. The install CLI, runtime and panes depend on herdr's App, PTY and socket, so they are redesigned onto gclient and the daemon API.
- Adv2 (gobby#15414) evidence excerpts:
  - manifest.rs:118-229 (e601547a…).
  - runtime.rs:15-200 (df44f1c5…; thread at 121, wait at 142, joins at 147-163) and :284-311 (1c8b3730…; drains to EOF after the cap).
  - plugin_registry.rs:19-96 (6a616804…; lock at 30, strict mutation read at 63, rename at 48, a startup read failure becomes an empty registry at 77-83).
  - cli/plugin.rs:154-263 (6ba3073b…; --yes at 188-190, preview before build at 205-210, manifest recheck after build at 214-215, rollback at 241-250).
  - Keybinds: src/config/keybinds.rs:85-130, 284-295, test 2213 (`[[keys.command]] key, command, type shell|pane|popup|plugin_action, description`).

---

## Part 2: Design notes (after the claim, before the draft)

### Decision Record (A = recommended, never ruled)
- **Q1, event host.** The live gclient hosts event hooks.
  - One host per machine, via `libc::flock(LOCK_EX|LOCK_NB)` on `$GOBBY_HOME/plugins/.host.lock`.
  - Clients that are not the host retry the lock on the 5 s registry poll, so one takes over when the host exits.
  - `gclient plugin host` runs the same loop headless until SIGINT or SIGTERM. It fails fast while the lock is held.
  - ROADMAP:107 ("gobby is permanently a separate interactive process. Zero-to-N viewers") motivates the explicit zero-or-many ownership rule.
- **Q2, trust.** Consent only.
  - Install, reinstall and enable-after-change print a preview: every argv of build, actions, events, panes and link handlers. A reinstall shows added, removed and changed entries.
  - The user answers y/N. Non-TTY runs require `--yes` (herdr cli/plugin.rs@v0.8.0:188-190).
  - The registry pins the resolved commit and the manifest sha256. On a load-time hash mismatch the plugin goes inactive with the warning "manifest changed; `gclient plugin enable <id>` to review".
  - No sandbox. The SRT chokepoint is the daemon TerminalRuntime spawn seam (ROADMAP decision 6), and client-spawned commands never pass it. Plugin panes do pass it, because they are daemon panes.
  - No token is injected. Strip `GOBBY_AGENT_API_TOKEN` from the child env.
- **Q3, link handlers in v1.** Yes.
  - Schema: `[[link_handlers]] id, pattern (regex), action (an action id in the same manifest)`.
  - Seam: `MouseOutcome::OpenLink(url)` in `app/live_loop/actions.rs::apply_live_mouse_outcome` (:101-105), via `open_link` (:202-213), `Chrome::link_opener` and `DEFAULT_LINK_OPENER` (ui/chrome.rs:34).
  - First match wins, by registry order (id sort) then manifest order. Otherwise the default opener runs.
- **Q4, distribution.**
  - `gclient plugin install owner/repo[/subdir] [--ref R]` uses system git.
  - `gclient plugin link <dir>` registers a local directory.
  - Re-running install is the update path, with a diff preview.
  - No marketplace; herdr's workers/plugin-marketplace is rejected.
- **Q5, custom command keybindings.**
  - The user keymap gains `[[commands]] key, action = "<plugin>/<action>" | pane = "<plugin>/<pane>", description`, which binds chords to plugin actions or panes.
  - Help shows them in a "custom" group, falling back to the description "custom command".
  - A shell command needs a linked local plugin; there are no inline shell commands.
  - The deferred parity test `tests/parity/chrome.rs::keybind_help_shows_custom_command_descriptions` gets rewritten onto a gclient fixture keymap. Herdr expects prefix+alt+g "open lazygit" and a no-description "custom command".
  - Also update the UPSTREAM.md Deferred row and the `keybind_help_groups` doc (tests/parity/chrome.rs:290-292).
- **Technical calls** (the Orchestrator confirmed the first three stand under ROADMAP decision 7):
  - The registry is a file under GOBBY_HOME.
  - Event filters use the WS subscription grammar.
  - Panes support split-right, split-down and tab only. There is no public API for popup, overlay or zoomed.
  - The module is `crates/gclient/src/plugins/`, never imported from src/ui.
  - The manifest file is `gobby-plugin.toml`.
  - `min_client_version` is checked against gclient's CARGO_PKG_VERSION with a numeric triple compare (no semver dependency), before the strict parse.
  - Unknown manifest keys are denied.
  - Dropped from herdr: `[[startup]]`, the `selection` and `tab` contexts, and popup sizes.

### Facts
- ROADMAP decision 7 (:367-368): plugins use the public API only, as client-side Rust. Decision 15: `gobby` takes the name from gclient at S3.2, so plugins must call `$GOBBY_CLIENT_BIN`. Decision 16 (:405-410): no dylibs.
- gclient verbs:
  - Parsing is the hand-rolled `command.rs::parse`, with VALUE_FLAGS (:89-102) and SWITCH_FLAGS (:103). New flags `--ref`, `--limit` and `--yes` must be added there.
  - `dispatch_inner` (:169-221): the plugin verb must branch before the token read, because lifecycle verbs need no daemon.
  - Help lives in `command/help.rs::VERBS` (:12-86).
- `split --cmd` precedent: `PaneSplit{pane, axis, terminal_id: None, cwd: None, node: None}`, then `PaneSendText{pane: new, text: cmd, submit: Some(true)}`.
  - The daemon's pane_split spawns `PANE_SHELL_COMMAND=("zsh",)` (src/gobby/terminals/workspace_ops.py:82) and has no argv API.
  - So plugin panes send `exec env K=V... <shlex-quoted argv>`, on the local node only.
  - Rejected alternative: extending PaneSplit and TabCreate with argv and env, a daemon API change during Stage 2 absorption.
- WorkspaceOp (`crates/gclient/src/daemon/workspace.rs:178`): label a plugin pane `plugin:<id>/<pane>` with PaneRename.
  - Find it again by `pane.label`, focus it with WorkspaceSelect, close it with PaneClose.
  - Open is a singleton: if the label exists, focus that pane.
- The daemon pane env contract: `GOBBY_PANE_REF`, `GOBBY_WORKSPACE_ID`, `GOBBY_TAB_ID` (`command.rs::CommandEnv::from_process` :18-28).
- WS details:
  - subscribe accumulates (`src/gobby/servers/websocket/handlers/core.py:187-189`); unsubscribe discards (:221).
  - Gated types: hook_event, session_message, session_event, session_usage_updated, token_event, agent_event, agent_message, worktree_event, workspace_event, autonomous_event, pipeline_event, terminal_output, terminal_event, skill_event, mcp_event, workflow_event, project_event, cron_event, trace_event.
  - Filters: `type:key=value` is top-level string equality.
  - Manifest `on` grammar: `<gated>` or `<gated>:<key>=<value>`. Reject `*`, non-gated types and the types the reader makes typed (Part 3).
  - The host must re-match locally, because the socket also carries gclient's own subscriptions.
- gclient live WS:
  - `daemon/live.rs::SUBSCRIBED_EVENTS` (:32-38) lists terminal_event, agent_event, project_event, worktree_event and session_event.
  - `daemon/live_connect.rs::LiveDaemon::subscribe_events` (:135-157) sends that fixed list on every connect and reconnect.
  - Plan: add a plugin filter set to `LiveState` (live.rs:49-73) plus a setter that subscribes added filters and unsubscribes removed ones, never a base type.
  - `Daemon::subscribe` gives the host its own EventReceiver; Lagged arrives as Lagged.
- Keymap symbols:
  - `ui/keymap/names.rs`: BindingSpec {name, description, defaults, reserved, indexed} (:83-90), BINDINGS (:122-306), `Action::is_reserved` (:374-376).
  - `ui/keymap.rs`: Binding.reserved (:52), KeymapError::ReservedAction (:70-71), build (:155-189), check_collisions (:193-214), lookup_direct (:225), lookup_prefix (:234), help_entries (:248-258), active (:260-265).
  - Callers: `key_input.rs:77,83`; `ui/keybind_help.rs::filtered_entries` (:35).
- Every consumer of the reserved mechanism has to change when it is removed:
  - names.rs: the custom_command spec, the name map, is_reserved.
  - keymap.rs: as listed above.
  - tests/keymap.rs: :129, :194-211, :285-316.
  - tests/ui_carve_guard.rs: :297-316.
  - tests/parity/chrome.rs: :290-292, :996-1000.
  - UPSTREAM.md: :57-58, :178-179.
  - NOTICE.md: :58.
  - keybind_help.rs: :2, :34.
- Carve guard: FORBIDDEN is a lowercase substring scan of src/ui. "crate::plugin" means no src/ui file may name crate::plugins. Rename the binding to `plugin_menu` ("Open the plugin menu", prefix+m).
- Live loop:
  - `app/live_loop.rs::run_live_loop` (:126) has a biased select! with arms for control_rx, jobs.rx and input. `route_live_input` dispatches actions to `handle_live_action`.
  - Wiring: a `tokio::task_local!` plugin session, scoped in `views/mod.rs::run_ready` around `run_live_loop`. That avoids signature churn through the dispatch chain and the integration tests.
  - Notices (action failures, inactive plugins) come through an mpsc receiver as a new select arm and become `chrome.notify(Toast::warning(..))`.
- Dependencies (crates/gclient/Cargo.toml):
  - Already present: libc, toml.
  - sha2 is dev-only and must move to [dependencies].
  - regex and shlex are in the lockfile (gcode, gterminal); add them.
  - tokio lacks the "process" feature; add it.
  - MSRV 1.88: `File::try_lock` needs 1.89, so use libc::flock.
- Tests: integration files under crates/gclient/tests/ (e.g. tests/plugin_manifest.rs); inline tests in `<module>/tests.rs` (crates/AGENTS.md).

### Runtime, registry and install specifics
- Paths:
  - `$GOBBY_HOME/plugins/{registry.json, .registry.lock, .host.lock}`.
  - `checkouts/<owner>/<repo>/<commit>/`, `config/<id>/`, `state/<id>/commands.jsonl`.
- Registry JSON: `{plugins: [{id, enabled, source: {kind: github, owner, repo, subdir, ref, commit, checkout} | {kind: local, path}, manifest_sha256, installed_at}]}`.
  - Mutators hold `.registry.lock` for the whole operation. Readers are lock-free (temp file, fsync, rename).
  - A corrupt registry makes every command error, naming the file. It is never treated as empty: herdr's empty-on-failure is rejected because the next mutation would drop entries.
  - A host with a corrupt registry runs no plugins and posts one notice.
- Install sequence:
  1. Clone into `checkouts/.staging-<uuid>` with `git clone`.
  2. `git -C <checkout> checkout --detach <ref>`, then `rev-parse HEAD`.
  3. Parse `<subdir>/gobby-plugin.toml`; check platform and version.
  4. Preview, then consent.
  5. Run the build: `[[build]]` argv, stdin null, stdout and stderr inherited, 600 s timeout.
  6. Re-hash the manifest; it must equal the pre-build hash.
  7. Rename the checkout to its managed path, write the registry, remove the previous checkout.
- Rollback: a failure before the registry write removes staging; a registry write failure removes the managed path.
- link registers a local dir, with preview and consent and no build.
- uninstall removes the entry, the managed checkout (never a linked dir) and the state dir. It keeps the config dir and prints its path.
- No separate unlink or update verbs.
- Runner:
  - argv with no shell. A relative `./x` argv0 resolves against the plugin root, which is also the cwd.
  - stdin is null for actions and links. Events get the event JSON message on stdin, then EOF (this avoids env size limits).
  - stdout and stderr are each captured to 64 KiB, and drained past the cap.
  - `process_group(0)`.
  - `timeout_seconds` defaults to 60, maximum 3600; builds 600. On timeout: SIGTERM the group, 2 s grace, SIGKILL the group, status timed_out.
  - After the leader exits, drain the pipes for at most 2 s, then abandon escaped descendants. Always reap.
  - Concurrency cap: 8 per host process. Overflow events are dropped and counted (`dropped_before` on the next record).
  - Host shutdown sends SIGTERM, then SIGKILL, to in-flight commands; status cancelled.
- Log record (JSONL): {ts, kind action|event|link|build, entry, argv, status succeeded|failed|timed_out|cancelled|spawn_failed, exit_code, duration_ms, stdout, stderr, truncated, dropped_before}. When the log passes 400 records, trim it to the last 200.
- Env set by the runner:
  - Always: GOBBY_PLUGIN_ID, GOBBY_PLUGIN_ROOT, GOBBY_PLUGIN_CONFIG_DIR, GOBBY_PLUGIN_STATE_DIR (created), GOBBY_CLIENT_BIN (current_exe), GOBBY_DAEMON_URL, GOBBY_PLUGIN_KIND, GOBBY_PLUGIN_ENTRY.
  - Events: GOBBY_PLUGIN_EVENT (the matched filter).
  - When known: GOBBY_WORKSPACE_ID, GOBBY_TAB_ID, GOBBY_PANE_REF. These replace inherited values.
  - Links: GOBBY_CLICKED_URL.
  - The parent env is otherwise inherited.
- Action contexts ⊆ {global, workspace, pane}, default global. The plugin menu offers the actions that match the current focus.
- Registry reload: poll the registry's mtime and size every 5 s, recompute the filters, and push the filter-set delta.

### Deliverable map (draft)
- **P1, core library:**
  - 1.1 manifest (`plugins/manifest.rs`, `plugins.rs` module root, lib.rs `pub mod plugins`, Cargo regex and sha2).
  - 1.2 paths and registry (`plugins/paths.rs`, `plugins/registry.rs`).
  - 1.3 runner (`plugins/runner.rs`, Cargo tokio process).
- **P2, CLI:**
  - 2.1 the plugin verb plus list, enable, disable, uninstall and logs (`command/plugin.rs`, `command.rs::dispatch_inner` and the flag tables, `help.rs::VERBS`).
  - 2.2 install and link (`plugins/install.rs`). A `GOBBY_PLUGIN_GITHUB_BASE` test seam lets tests clone from local bare repos.
  - 2.3 action and pane verbs (`plugins/panes.rs`, shlex).
- **P3, live client:** see the Part 3 chain.
- **P4, docs:**
  - `docs/guides/plugins.md` (new).
  - Pointers from `docs/guides/webhooks-and-plugins.md` and the gclient user guide (`docs/guides/gclient-user-guide.md:772-779` keymap section).
  - UPSTREAM.md and NOTICE.md rows.
  - ROADMAP side-quest line.
- Write the manifest schema as a TOML block, not a table.
- Relationship to name in the plan: herdr-terminal-client D1 (.gobby/plans/completed/herdr-terminal-client.md:1731-1744) and herdr-client-completion D3 (completed/herdr-client-completion.md:2383-2404; items 3.1.4, 5.1.3).

---

## Part 3: Re-verification after the #23528 Sidebar landing (`3e2aca2950`)

- Line counts:
  - `app/mod.rs` 883 and `app/live_loop/actions.rs` 852. Both are at or above the 850 `production-size-growth` trigger.
  - `app/live_loop.rs` 781, `menu.rs` 390, `ui/keymap.rs` 694 (tests from 655).
  - gclient version 0.1.22.
- `MenuItem.label` is `&'static str` (`app/live_loop/menu.rs:164-169`).
  - Plugin menu rows need dynamic labels, so the label becomes `Cow<'static, str>`.
  - The readers that copy it out are tests: `menu/tests.rs:11,434,944`, `tests/info_dialogs.rs:275`, `tests/menu_bar.rs:161,188,234,441` and `tests/parity/dialogs.rs:681`.
  - `ui/context_menu.rs:54` formats the label and needs no change.
- `HelpEntry` and `Binding` carry `&'static str` names and descriptions (`ui/keymap.rs:48-62`).
  - Custom command rows need a dynamic description, so `description` becomes `Cow<'static, str>`.
  - Append one `Binding` per `[[commands]]` entry, with `Action::Command(u16)` indexing the keymap's command list. Lookup, collision and help then reuse the existing machinery.
  - Consumers: `ui/keybind_help.rs` (wrap and width), `tests/keybind_help.rs:59,175`, `tests/parity/chrome.rs:342,376,387`.
- `OverrideFile` (`ui/keymap.rs:87-92`) has no `deny_unknown_fields`, so a `[[commands]]` table is silently ignored today.
- `live_reader.rs::daemon_event` (:362) turns these into typed variants:
  - `terminal_event`, `terminal_output` and `workspace_event`.
  - The attention `agent_event`s (`attention_changed`, `attention_metadata_changed`).

  The host sees only raw `DaemonEvent::Message`, so none of these are plugin-visible in v1.
- `actions.rs` is 852 lines. Each deliverable that targets it needs its own split or move paragraph naming a new bare-path `.rs` Target (`docs/contracts/plan-coverage.md:189`, :193-198). Proposed P3 chain, each step depending on the one before:
  1. 3.1 live session wiring: `plugins/live.rs`, `app/live_loop/plugins.rs`, the `views/mod.rs::run_ready` task-local scope, and the `run_live_loop` notice arm.
  2. 3.2 link handlers: move `open_link` and the `OpenLink` arm into a new `app/live_loop/links.rs`.
  3. 3.3 plugin menu: retire the reserved mechanism; add `plugin_menu` at prefix+m, `ContextMenuKind::Plugins`, `MenuAction::Plugin`, and a new `app/live_loop/menu/plugins.rs`.
  4. 3.4 event host: the live and headless host, `LiveDaemon` plugin filters, and the `plugin host` verb.
  5. 3.5 custom commands: a new `app/live_loop/commands.rs`, and un-defer the parity test.
- The production entry point is `views/mod.rs::run_ready`, which calls `run_live_loop` at :83. Integration tests call `run_live_loop` directly, so they see no plugins.
- The daemon sets pane identity env (`src/gobby/terminals/workspace_contract.py::_identity_env`: `GOBBY_WORKSPACE_ID`, `GOBBY_TAB_ID`, `GOBBY_PANE_ID`, `GOBBY_PANE_REF`). Plugin runs must replace any inherited values with the plugin's own context.
- `gobby_core::gobby_home()` honors `GOBBY_HOME`, which isolates the tests. `tests/mock_daemon/` serves `workspace_op` for the pane-verb tests.
- Manifest rule: argv[0] must not contain `=`. Pane exec lines are `exec env K=V ... argv`, so `env` would read such an argv[0] as an assignment.

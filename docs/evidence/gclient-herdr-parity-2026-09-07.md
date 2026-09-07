# gclient vs herdr parity review (2026-09-07)

Research spike #21971 under epic #21334 (`.gobby/plans/gclient-mouse-parity.md`).
The user compared the live gclient (0.5.0 @ ba07053, the 4.1 build) against herdr
side by side and rejected the live client as "nowhere near parity". This file records
what was compared, the confirmed causes, the user's decisions, and the follow-up cut.

## Setup

- gclient: `~/.gobby/bin/gclient` rebuilt from ba07053, launched in Ghostty at 160x48
  against the local daemon (`GOBBY_DAEMON_URL=http://127.0.0.1:60887`), project gobby.
- herdr oracle: `~/.gobby/clones/herdr`. It started the review at master @ 952729ee
  (Cargo 0.8.0). At the user's direction the clone was fast-forwarded to origin/master
  (a9f3ad5f) and checked out at **v0.9.0** (b99002ac, released 2026-09-07); the release
  binary was built to `/private/tmp/herdr-target` and the old 0.8.0 server stopped with
  `herdr server stop`. The user also installed herdr 0.8.2 from Homebrew
  (`/opt/homebrew/opt/herdr/bin/herdr`); the comparison target from here on is 0.9.0.
- Captures: `peekaboo see --window-id <id> --no-elements --capture-engine classic`.
  Window ids come from `peekaboo window list --pid <ghostty pid> --json`; they change
  per launch, and `--app Ghostty` is ambiguous because each direct launch is its own
  app instance.

## Screenshot pairs

| # | herdr | gclient |
|---|-------|---------|
| 00 (herdr 0.8.0, 15:11) | Sidebar `spaces` [~, gobby 0.5.0 ↑722 focused], footer `new · menu`, `agents grouped` [gobby · 1 / fake-codex], tabs [1, review2, +], one shell pane, no pane border when single. | Sidebar `terminals` (7 rows, three "blocked", four "idle") and `attention` (10 rows, all "blocked"), tabs [15, %604, %605], one shell pane, toast "No actionable attention prompt". The user's own earlier capture (14:51) of the first screen showed one tab with 8 cascading panes and a blank focused pane. |
| 01 (gclient only, 15:16) | – | Same chrome; the pane shows the user's `brew install herdr` output. Stray green `f` glyphs render on three otherwise blank rows at one column. |
| 02 (herdr 0.9.0, 15:27) | Startup after the server swap: `spaces` [~ collapsed, gobby 0.5.0 ↑722 focused], footer `new · menu`, `agents grouped` empty, tab `1`, one shell pane. | User-made state: tab %604 with a horizontal split, left %604 held (shell prompt) and right `gobby-codex-d0 · tmux · observe` (codex transcript). Sidebar unchanged. Stray glyphs (`B`, `h`, `(`, `p`, `(`, `r`) on blank rows of the left pane. |

## Confirmed causes (code references)

1. **Pane cascade at startup.** `crates/gclient/src/app/live_loop/actions.rs::sync_live_chrome`
   opens a pane for every roster terminal that is not shown, through `Chrome::open_pane`,
   which is `open_split(.., Direction::Horizontal)` on the focused slot
   (`crates/gclient/src/ui/chrome.rs`). Eight terminals produce seven halvings. It runs
   on every reconcile, so a closed pane comes back. herdr never auto-opens panes.
2. **Every row "blocked".** `/api/attention/roster` is the session roster: entries carry
   `attention` (null unless a prompt is pending), `lifecycle_status`, `provider`,
   `model`, `task`, `terminal`, `tmux`. `crates/gclient/src/ui/sidebar_rows.rs::attention_rows`
   hardcodes `RowState::Attention`, and `crates/gclient/src/ui/chrome.rs::row_state` marks
   any terminal with a roster entry as Attention. At 15:16 the daemon roster had 4
   entries, all `attention=null`; the sidebar showed 10, so 6 rows were stale entries
   the client never pruned.
3. **Wrong nouns.** The `terminals` section lists daemon terminal rows titled by tmux
   session name (`15`, `42`, `[tmux]`, `gobby-codex-d0`, `%604`); no project grouping,
   no session identity, no provider or task.
4. **Blank pane.** Terminal a4ca03b7 (tmux %15) renders blank in observe and held mode
   while %42 and %0 render. No evidence in `~/.gobby/logs/gclient.log` or `gterm.log`.
   Plan 4.4 ("Every attached pane renders its latest frame") already targets this (F3).
5. **Stray glyphs.** Single characters from another frame appear on blank rows of a
   pane (pairs 01 and 02). This is a damage/compositing defect in the pane renderer,
   distinct from 4, and not yet in the plan.
6. **Right-click does nothing.** `crates/gclient/src/app/live_loop/mouse/pointer.rs::down`
   routes a right press on a pane to `right_down`, which returns `Handled` (no menu)
   unless passthrough is configured or flagged; right presses on tabs, the new-tab
   button and sidebar rows are `Ignore`. No context-menu type exists. Close pane and
   close tab exist only as keymap actions (`close_pane`, `close_tab`, dispatched at
   `actions.rs` 161/167). Plan 5.1/5.2 specify the menus and depend on 4.4.
7. **No persisted client state.** `~/.gobby/client/` does not exist, so everything above
   is startup behaviour, not a restored snapshot.

## herdr 0.9.0 facts that shape the target

- Sidebar model: `spaces` (workspaces: repo name, branch, ahead count; linked worktrees
  as child rows, groups collapse/expand) and `agents` (grouped under spaces; only
  detected agents, never plain shells). Footer `new · menu`.
- Context menus (`src/client/shell/context_menu.rs::items`): Pane = Rename pane,
  [Clear pane name], [Swap with focused pane], Split right, Split down, Zoom,
  Send right-clicks to pane / Use Herdr right-click menu, Close pane. Tab = New tab,
  Rename, Close. Workspace row = Rename, Close, plus New worktree / Open worktree… for
  git repos, Delete worktree checkout… for linked worktrees, Close group and
  Expand/Collapse for groups.
- 0.9.0 changes relevant to gclient (CHANGELOG.md): multiple clients view different
  workspaces and tabs independently (#3526); the TUI runs in each client with
  presentation state local to the viewer (#3487); `--no-session` removed, every launch
  attaches to a background server (detach leaves panes running); Local and SSH
  machines in one window with a combined agent list (#3670); sidebar tokens colored by
  value rules (#3693); `ui.pane_borders = always|auto|off` (#3234); lifecycle
  subscriptions start live (#1270); Claude Code MCP questions and Bash approvals stay
  blocked until answered (#3283).

## User decisions

1. Sidebar nouns: herdr `spaces` become gobby **projects**; herdr `agents` become gobby
   **interactive and autonomous sessions**. The `terminals` and `attention` sections are
   the wrong model and go away.
2. "I think this is salvageable, but we need to rework the lefthand menu considerably."
3. "We also need to fix right-click because I can't close panes or tabs in gclient."
4. Compare against herdr 0.9.0 from here on (clone and tmp build moved to v0.9.0).
5. Plan sections 4.2–5.2 stay paused until the sidebar model is agreed.

## Open questions (answers decide the sidebar task cut)

- Does a project row group by project only, or project plus worktrees, as herdr does
  with branch and ahead count? Recommended: project with worktree child rows.
- Which sessions appear under agents: only gobby sessions from the roster
  (interactive Claude/Codex plus spawned runs), or every daemon terminal? Recommended:
  roster sessions only; plain shells appear as tabs and panes, never in the sidebar.
  The three identical idle `gobby-codex-d0` rows were stale tmux panes on a second tmux
  server (pid 38273).
- Startup: empty workspace with the sidebar (herdr) or restore the last snapshot?
  Recommended: restore when a snapshot exists, otherwise empty; never auto-open.

## Proposed follow-up cut (not yet filed)

1. Right-click context menus for pane and tab (plan 5.1 + 5.2 minus the roster and
   attention row menus), with `close pane` and `close tab` first. Needs the modal input
   router from 4.2 and can run ahead of the sidebar work.
2. Stop auto-opening panes in `sync_live_chrome` (reap stale slots only) and restore
   from the snapshot.
3. Sidebar rework: `projects` (from the daemon's project list, branch and ahead count)
   and `agents` (roster sessions grouped by project; state from `attention` plus
   `lifecycle_status`; label from provider, task and tmux session). Replaces 2.x's
   terminals and attention sections; 5.1's row menus follow the new rows.
4. Pane renderer: 4.4 (blank attached panes) plus the stray-glyph compositing defect.
5. Prune stale roster entries from client state on every reconcile.

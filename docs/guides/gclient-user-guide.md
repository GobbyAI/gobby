# gclient User Guide

`gclient` is Gobby's terminal workspace client. It runs in your terminal, attaches
to a workspace the Gobby daemon owns, shows its tabs and split panes of hosted
terminals, lists the agents that need your attention, and lets you take or hand
back keyboard control of any terminal. This guide covers the client as a user: how
to launch it, what is on screen, how workspaces work, every default keybinding,
the mouse, and what happens across daemon restarts.

For building, installing, and the wire protocols, read
[gterminal-development-guide.md](gterminal-development-guide.md).

## Launching

`gobby install` places the binary at `~/.gobby/bin/gclient`. Run it from any
directory:

```bash
gclient
```

```text
Usage: gclient [--project PROJECT] [--node NODE] [--workspace WORKSPACE] [--daemon-url URL] [--token-file PATH] [--frame-delivery auto|direct|proxy] [--no-mouse] [--version]
```

| Flag | Meaning |
| --- | --- |
| `--project PROJECT` | A project UUID or a checkout path. Without it the client walks up from the current directory to the nearest `.gobby/project.json`; outside every checkout it opens the project the workspace's rows say was focused last, or the personal project with one shell in the directory it was launched from. |
| `--workspace WORKSPACE` | The workspace to attach: a ref (`w1`, or `n2:w1`, whose node overrides `--node`) or a name. Defaults to `default`, which is created on first use. See [Workspaces](#workspaces). |
| `--node NODE` | The node that owns the workspace: a ref (`n2`), a node id, a hostname, or a label. Defaults to the daemon's own node. |
| `--daemon-url URL` | Daemon endpoint. Defaults to the local daemon's configured URL. |
| `--token-file PATH` | Bearer token file. Defaults to `~/.gobby/local_cli_token`. |
| `--frame-delivery auto\|direct\|proxy` | How terminal frames arrive. `auto` tries the local frame socket first and falls back to the daemon's WebSocket proxy per pane. |
| `--no-mouse` | Leave mouse events to your terminal emulator. Same as turning off `mouse capture` in settings, for this run only. |
| `--version`, `-V` | Print the version and exit. |
| `--help`, `-h` | Print the usage line and exit. |

Startup checks, in order: an explicit `--project` resolves (a directory
outside every checkout is fine), `~/.gobby/client/prefs.toml`
parses, the keymap override file parses, the daemon answers `/api/health`, and the
health report shows a running gterm host speaking the client's protocol version.
Each failure prints one message naming the fix (`gobby start`, `gobby init`,
`--token-file`, or the offending config line). Remote daemons are covered in the
development guide's [Remote use](gterminal-development-guide.md#remote-use)
section.

Logs go to `~/.gobby/logs/gclient.log`.

## Layout

```text
┌ sidebar ───────────┬ tab bar: 1  2  build Z  +  ─────────────────┐
│ [Menu]         [+] │ ┌ ▸ zsh ──────────┐┌ claude ──────────────┐ │
│ Machines           │ │  pane (focused) ││   pane               │ │
│ ▶ mbp · local      │ │                 ││                      │ │
│ └─ ○ studio        │ └─────────────────┘└──────────────────────┘ │
│ Projects [working] │                                             │
│ ▶ gobby (0.5.0 ↑2)▾│                                             │
│   ├─ ○ fix-y · #12 │                                             │
│ Sessions    [view] │                                             │
│ ⍾ #123: fix y      │                                             │
│   codex · gpt-5    │                                             │
│ ○ zsh %3           │                                             │
│   tmux             │                                             │
│                [«] │                                             │
├────────────────────┴─────────────────────────────────────────────┤
│ [● held] │ zsh %3 │ direct │ prefix ctrl+] │ message             │
└──────────────────────────────────────────────────────────────────┘
```

**Sidebar.** A menu band, three sections, and a footer band. Every clickable
control is bracketed. Machines and Projects together never take more than the
top half of the sidebar (each scrolls inside its cap); Sessions takes the rest.

- *Menu band* (accent colour): `[Menu]` opens the global menu, `[+]` registers
  a project.
- *Machines* lists this machine (the hub, by host name) first, then every other
  machine the daemon knows nested under it with `├─`/`└─`, each with the most
  urgent state of the terminals running there. At most four rows show before
  the list scrolls. The rows are the machine filter for the two sections below:
  clicking a remote machine shows that machine's terminals, clicking it again
  returns to `local`; clicking the hub row toggles `all`. The current filter is
  marked on the row (`local` or `all`).
- *Projects* lists registered projects as one-line cards:
  `glyph name (branch ↑ahead ↓behind)` with a `▸`/`▾` fold mark at the right
  edge. Only one project is expanded at a time: selecting a project expands it
  and folds the others, and its worktrees appear under the card as
  `├─ glyph branch · #task`, with `~` for the branch of a detached worktree.
  The band's control toggles `[working]` (projects
  with a live session, run, or terminal on the current machine filter, plus the
  focused one) and `[all]`.
- *Sessions* lists sessions as two-line rows: `glyph #ref: title` over
  `provider · model-effort · task ref or pane title · remote machine`. Bare
  terminals with no session are listed by pane name with their backend. The
  band's `[view]` control opens a menu with both axes: the scope, `this
  project` (the focused project only) or `all projects` (every project,
  grouped under dim project rows), and the order, `grouped` (tab order, with
  agent runs nested under the session that spawned them) or `priority`
  (flattened urgency order). A `✓` marks the value in force, and choosing it
  again closes the menu unchanged.
- *Footer band*: `[«]` collapses the sidebar.

State glyphs, on machine, project, and session rows alike:

| Glyph | State |
| --- | --- |
| `▶` | working |
| `⍾` | needs you (an attention prompt is waiting; the row's first line also carries the words `needs you` when they fit beside the title) |
| `◆` | unseen (new output since you last looked) |
| `○` | idle |
| `◌` | orphaned (the terminal's host is gone; see *Orphaned terminals*) |
| `·` | unknown |

Only needs-you rows carry a word. A font without U+237E shows a box in place of
`⍾`; the words still identify the row.

Collapse the sidebar with `prefix+b` or `[«]`. Collapsed, it becomes a narrow
rail: a dot per machine, then numbered project cards, then numbered sessions,
each list under a `─` rule, with `»` on the last row to expand.

**Tab bar.** One row of tabs for the focused project; each project keeps its own
tab set, and every tab is a row of the attached workspace (see
[Workspaces](#workspaces)). Auto-named tabs show their index, renamed tabs their
name, and a zoomed tab adds ` Z`. A new-tab button follows the last tab; scroll
arrows appear when tabs overflow. The bar hides when only one tab is open if you
turn on `hide tab bar with one tab` in settings.

**Panes.** A tab holds one or more terminals in nested splits; each pane is a
workspace row with a ref such as `n1:w1:t2:p3`, and its name is that row's label.
The focused pane's border title starts with `▸`. A pane that has not yet received
a frame says `waiting for frames`; a pane whose size another viewer set says
`sized by <viewer>` on its bottom row. An empty tab area shows `no pane open`.

**Status line.** From left to right: the focused pane's control indicator
(`[● held]`, `[○ observe]`, `[▲ take-back]`, `[◌ lease lost]`, or
`[◌ read-only]`), the pane's name and tmux address, its transport (`direct` or
`proxy`), the current mode when it is not plain terminal mode, the prefix chord,
shifted when the client runs inside tmux, `daemon unreachable` during an
outage, and the latest status message. Clicking the control indicator takes, releases, or takes
back control.

## Workspaces

A workspace is the daemon's record of a layout: its tabs, the panes in them, and
the terminal each pane shows. The client attaches one workspace at startup
(`--workspace`, default `default`, created on first use) and renders its rows; it
keeps no layout of its own. Every window attached to the same workspace shows the
same tabs, panes, and names, whether a change came from another `gclient`, from
`gobby panes split`, or from an agent calling the `gobby-workspaces` MCP tools.

**Refs.** Nodes, workspaces, tabs, and panes are addressed by short refs:
`n1:w1:t2:p3` is pane 3 of tab 2 of workspace 1 on node 1. Each number is the
lowest free one in its scope, so a closed pane's number is reused by the next
split. Underneath, every row also has a UUID.

**Several windows.** More than one `gclient` may attach the same workspace. Each
window keeps its own focus, zoom, active tab, scrollback position, copy mode, and
sidebar filters. The rows hold focus hints (the focused project, tab, and pane)
that every window writes as its focus moves and that the next window, or the next
launch, opens on. Two windows typing into one pane are arbitrated by the control
lease described under [Attach and control](#attach-and-control).

**Pane environment.** A shell the workspace starts inherits `GOBBY_TERMINAL_ID`,
`GOBBY_NODE_ID`, `GOBBY_NODE_REF`, `GOBBY_WORKSPACE_ID`, `GOBBY_TAB_ID`,
`GOBBY_PANE_ID`, and `GOBBY_PANE_REF`. A `gclient` launched inside such a pane
opens no shell of its own and shifts its prefix to `ctrl+]`. Hooks see the same
binding as `gobby_terminal_id` and `gobby_pane_ref`; see the ghook guide's
[Terminal Context](ghook-development-guide.md#terminal-context).

**Other surfaces.** The same rows are reachable from the CLI (`gobby workspaces`,
`gobby panes`, and `gobby nodes` in [cli-commands.md](cli-commands.md#workspaces)),
from the `gobby-workspaces` MCP registry ([mcp-tools.md](mcp-tools.md)), and over
the WebSocket workspace messages in
[gterm-protocols.md](../contracts/gterm-protocols.md#workspace-messages).

### Bringing a bare terminal in

A Ghostty tab, or any terminal window you opened yourself, runs a plain shell the
daemon knows nothing about, and it cannot be adopted into a workspace later. Work
the daemon should track starts in one of three places:

- A `gclient` pane: open a tab or a split and run the command there.
- A tmux session you start by hand on the default socket. The daemon lists it as
  an external terminal, and `gclient` opens it in a pane with ownership
  `external`, so `close_pane` and closing the tab never kill it.
- A provider session resumed inside a `gclient` pane, for example
  `claude --resume <id>`. The session start binds it to the pane's terminal
  through `GOBBY_TERMINAL_ID`, it appears on the sidebar roster with backend
  `native`, quitting the provider leaves the pane's shell live, and restarting
  the provider there rebinds it.

## The prefix key

Most chords start with a prefix, exactly like tmux. Press the prefix, release it,
then press the second key.

| Situation | Prefix |
| --- | --- |
| Running in a plain terminal | `ctrl+b` |
| Running inside a tmux client | `ctrl+]` |

The client detects tmux by asking the tmux server for its identity, not by the
`TMUX` variable alone, so a stale variable does not switch prefixes. Inside tmux
the outer tmux keeps `ctrl+b`. The status line always names the active prefix
(`prefix ctrl+b`, or `prefix ctrl+]` inside tmux). Override either default with a `prefix = "..."` line in the keymap file
(see [Customising the keymap](#customising-the-keymap)).

Pressing the prefix twice sends a literal prefix chord to the focused terminal.
Any other unbound key after the prefix is forwarded to the terminal as itself.

`ctrl+\` is the escape hatch: it releases control of the focused terminal in
every mode, without the prefix, so a held pane is never a trap.

## Default keybindings

The table below is the client's default keymap. Names in the last column are the
keys you use in the override file. Bindings marked *unset* have no default chord
and only work after you bind them; every action also appears in the help popup
(`prefix+?`), which lists the live bindings after any overrides.

### Tabs

| Chord | Action | Name |
| --- | --- | --- |
| `prefix+c` | Open a new tab with a fresh shell | `new_tab` |
| `prefix+n` / `prefix+p` | Next / previous tab | `next_tab` / `previous_tab` |
| `prefix+1` … `prefix+9` | Switch to tab 1–9 | `switch_tab` |
| `prefix+shift+t` | Rename the active tab | `rename_tab` |
| `prefix+shift+x` | Close the active tab | `close_tab` |

### Panes

| Chord | Action | Name |
| --- | --- | --- |
| `prefix+v` | Split side by side (new shell to the right) | `split_vertical` |
| `prefix+-` | Split stacked (new shell below) | `split_horizontal` |
| `prefix+x` | Close the focused pane | `close_pane` |
| `prefix+z` | Toggle zoom on the focused pane | `zoom` |
| `prefix+h` / `j` / `k` / `l` | Focus the pane to the left / below / above / right | `focus_pane_*` |
| `prefix+shift+h` / `j` / `k` / `l` | Swap with the pane in that direction | `swap_pane_*` |
| `prefix+tab` / `prefix+shift+tab` | Cycle to the next / previous pane | `cycle_pane_next` / `cycle_pane_previous` |
| `prefix+shift+p` | Rename the focused pane | `rename_pane` |
| `prefix+r` | Enter resize mode | `resize_mode` |
| `prefix+[` | Enter copy mode | `copy_mode` |
| *unset* | Open a new terminal (splits right) | `new_terminal` |
| *unset* | Focus the last focused pane | `last_pane` |

### Terminals and control

| Chord | Action | Name |
| --- | --- | --- |
| `prefix+t` | Take control of the focused terminal | `take_control` |
| `prefix+u` | Release control of the focused terminal | `release_control` |
| `prefix+shift+a` | Accept the take-back prompt | `take_back` |
| `ctrl+\` | Release control, in any mode, no prefix | (built in) |
| `prefix+a` | Answer the current attention prompt | `respond` |
| `prefix+w` | Open the terminal picker (the navigator) | `terminal_picker` |
| `prefix+g` | Open the navigator with search focused | `goto` |
| `prefix+shift+w` | Rename the selected terminal | `rename_terminal` |
| `prefix+shift+d` | Close the selected terminal | `close_terminal` |
| `prefix+o` | Focus the terminal a notification names | `open_notification_target` |
| *unset* | Select the next / previous terminal | `next_terminal` / `previous_terminal` |
| *unset* | Focus the next / previous attention prompt | `next_attention` / `previous_attention` |
| *unset* | Focus attention prompt 1–9 | `focus_attention` |

### Sidebar and projects

| Chord | Action | Name |
| --- | --- | --- |
| `prefix+b` | Toggle the sidebar | `toggle_sidebar` |
| `up` / `down` | Select the previous / next sidebar row (enters navigate mode) | `navigate_up` / `navigate_down` |
| `h` / `j` / `k` / `l` | Focus the neighbouring pane (navigate mode only) | `navigate_pane_*` |
| `prefix+shift+n` | Add a project | `new_project` |
| *unset* | Focus the next / previous project | `next_project` / `previous_project` |
| *unset* | Focus project 1–9 | `switch_project` |
| *unset* | Collapse or expand the project's worktrees | `toggle_group` |
| *unset* | Cycle the machine filter (`local`, `all`, each machine) | `cycle_machine_filter` |
| *unset* | Toggle `[working]` / `[all]` projects | `toggle_projects_filter` |
| *unset* | Toggle the sessions scope (this project / all projects) | `toggle_sessions_scope` |
| *unset* | Toggle the session order (grouped / priority) | `toggle_agent_sort` |

`up`, `down`, `h`, `j`, `k`, and `l` are direct chords: they act only when the
client is already in navigate mode. In terminal mode every unprefixed key goes to
the focused terminal.

### Client

| Chord | Action | Name |
| --- | --- | --- |
| `prefix+?` | Keybinding help | `help` |
| `prefix+s` | Settings | `settings` |
| `prefix+shift+r` | Reload `prefs.toml` and the keymap file | `reload_config` |
| `prefix+q` | Release control of the focused terminal (same as `release_control`; it does not exit) | `detach` |
| `prefix+shift+q` | Quit the client | `quit` |

`prefix+m` is reserved for a future command menu. It does nothing today and
cannot be rebound.

## Tabs and panes

A new tab starts its shell in the focused project's checkout; a split starts a
fresh shell for the same project. Worktree rows
in the sidebar open a tab whose shell starts in that worktree, or reveal the tab
that already shows it.

**Closing kills gobby's terminals, not yours.** `close_pane`, `close_terminal`,
and `close tab` ask the daemon to kill a terminal gobby started. The terminal is
not backgrounded and does not reappear in the sidebar; use `release_control` or
`detach` if you only want to stop typing into it. A tmux session you started
yourself (the sidebar lists it because the daemon found it, ownership
`external`) is never killed by `close_pane` or `close tab`: the pane leaves the
tab, its control lease is released, and the session stays in the sidebar to
reopen later. Only `close_terminal` kills an external session. Closing a tab
kills every gobby-owned pane in it. With `confirm close` on (the default),
closing a tab first opens a dialog that names the tab and counts its panes; `y`
or `enter` confirms, `n` or `esc` cancels. If the daemon refuses a kill, that
pane stays, and so does its tab.

A tab name and a pane name are workspace row state: rename one and every window
on the workspace shows it, it survives daemon restarts, and
`gobby panes rename REF [NAME]` sets or clears the same label from the CLI.
Neither changes the terminal's own title. A project label is yours alone; the
client keeps it in `prefs.toml`.

## Attach and control

Every pane you open is *attached*: it receives frames. Whether your keystrokes
reach it depends on the control lease, shown in the status line and the pane
title.

| Indicator | Meaning |
| --- | --- |
| `● held` | You hold the lease. Keys, pastes, and mouse reports go to the terminal. |
| `○ observe` | You are watching; the first keystroke takes control and is delivered once the lease is granted. |
| `▲ take-back` | Someone else holds the lease. `prefix+shift+a` or the indicator asks for it back. |
| `◌ lease lost` | The daemon revoked your lease, typically because another viewer took over. Typing is refused until you take control again. |
| `◌ read-only` | A write's outcome is unknown after a disconnect. Typing is refused; take control again to continue. |

Focusing a pane takes control of it automatically, whether you focus it by
keyboard, by click, or through the navigator. Typing into an observed pane also
requests control first and delivers the pending keystrokes once the lease is
granted; keys typed while that request is still pending are dropped, and the
status line says `acquiring control`. To look at a pane without taking it,
`alt+click` it.

`prefix+u`, `prefix+q`, and `ctrl+\` release the lease. The daemon can refuse a
take: the pane then shows `▲ take-back` and the reason lands in the status line.
On quit, the client releases every held lease and detaches every pane; the
terminals keep running.

## Attention prompts and respond

When an agent blocks on a question, its sidebar row shows `⍾` and the words
`needs you`. Clicking the row focuses its terminal, where the question is
already on screen; answer it there like any other input. `prefix+a` opens the
respond dialog for the first actionable prompt among the focused project's
agents, and *respond* in a needs-you row's right-click menu opens it for that
row. The dialog grows with the
terminal (64 to 120 columns), shows every line of the prompt up to twelve, and
offers either its options or a free-text field:

- `up` / `down` pick an option, `enter` submits it.
- With no options, type your answer, `backspace` edits, `enter` submits.
- `esc` cancels.

`mark seen` in the agent menu acknowledges the row's attention entry without
opening the terminal.

## Modes

The status line names the active mode. `esc` leaves every mode.

**Copy mode (`prefix+[`).** Mouse selection is the copy gesture: left-drag over
pane text and release to copy the selection to your clipboard through OSC 52. Any
other key leaves copy mode and goes to the terminal. Outside copy mode the same
gesture works in the focused pane whenever its application is not tracking the
mouse; `shift+drag` forces it even when the application is. Double-click selects
a word, triple-click a line, each copying immediately.

**Resize mode (`prefix+r`).** `h` / `j` / `k` / `l` or the arrow keys move the
focused pane's border one step. `enter` or `esc` leaves. Dragging a split border
with the mouse resizes without entering the mode.

**Navigator (`prefix+w` or `prefix+g`).** A popup listing the focused project's
terminals followed by its attention entries, each with its state and control
indicator. `j` / `k` or the arrows move, `enter` switches to the row's pane,
`/` focuses the search box (`ctrl+u` clears it, `esc` returns to the list), and
`a` / `b` / `w` / `i` (or `tab` to cycle) filter to all / blocked / working /
idle rows. `prefix+g` opens with the search box already focused.

**Navigate mode (`up` / `down`).** Moves the selection through the project cards
and worktree rows. `enter` focuses the selected project or opens the selected
worktree; `esc` returns to the terminal.

**Keybinding help (`prefix+?`).** The live keymap with names. `j` / `k`, the
arrows, and `PageUp` / `PageDown` scroll, `/` searches by key, description, or
name, `enter` or `esc` closes.

**Settings (`prefix+s`).** See below.

**Rename dialogs.** Type, `enter` commits, `esc` cancels.

## Settings

`prefix+s` opens the settings popup. `j` / `k` or the arrows move, `space`
toggles the row, `left` / `right` steps a value, `enter` or `esc` closes, as do
the `done` and `close` buttons. Rows are clickable. Every change is written to
`~/.gobby/client/prefs.toml` at once; there is nothing to apply.

| Row | Default | Effect |
| --- | --- | --- |
| theme | `dark` | `dark` or `light` |
| mouse capture | on | Off leaves selection and scrolling to your terminal emulator |
| pane borders | on | Draw borders around panes |
| pane scrollbars | on | Draw a scrollbar lane beside scrolled panes |
| pane gaps | on | Leave a gap between split panes |
| confirm close | on | Ask before closing a tab or project |
| hide tab bar with one tab | off | Hide the tab bar when a project has a single tab |
| sidebar width | 26 | Columns; also set by dragging the sidebar edge |
| right-click passthrough | none | Modifier that sends a right-click to the pane's application instead of opening the pane menu (`shift`, `alt`, `ctrl`, or none) |
| agent sort | `grouped` | `grouped` or `priority` order in the Sessions section |

The file is optional and every key in it is optional; an unknown key is a
startup error that names the line. The sidebar writes three keys of its own as
you use it: `sidebar_collapsed`, `project_order` (project ids in the order you
dragged them; projects it does not name follow in the daemon's order), and the
`[ui.project_labels]` table (your label per project id).

```toml
[ui]
theme = "dark"
mouse_capture = true
pane_borders = true
pane_scrollbars = true
pane_gaps = true
confirm_close = true
hide_tab_bar_when_single_tab = false
sidebar_width = 26
sidebar_collapsed = false
project_order = ["4b1c…", "9e2f…"]

[ui.project_labels]
"4b1c…" = "api"

[keymap]
path = "client/keymap.toml"
```

`prefix+shift+r` reloads the file and the keymap without restarting.

## Customising the keymap

Overrides live in `~/.gobby/client/keymap.toml`, or wherever `[keymap] path` in
`prefs.toml` points (relative paths resolve under `~/.gobby`). A missing file
means the defaults.

```toml
prefix = "ctrl+a"

[bindings]
new_terminal = "prefix+i"
zoom = ["prefix+z", "prefix+f"]
```

- Keys are the names from the tables above. A chord is `prefix+<key>` or a bare
  `<key>` for a direct chord. Modifiers are `ctrl`, `alt` (also `option`),
  `shift`, and `super` (also `cmd`).
  Special keys include `enter`, `esc`, `tab`, `space`, `backspace`, the arrows,
  `f1`–`f12`, and spelled-out punctuation such as `minus`, `slash`, `comma`,
  `period`, `backslash`, `semicolon`, `quote`, and `backtick`.
- Indexed actions (`switch_tab`, `switch_project`, `focus_attention`) take a
  range such as `"prefix+1..9"` or `"alt+1..9"`.
- A single string or a list of strings binds one or several chords. An empty
  list unbinds the action.
- Two actions cannot share a chord. A collision, an unknown action, an invalid
  chord, or a binding for the reserved `custom_command` is rejected at startup
  with the file and line; on `prefix+shift+r` the previous keymap stays loaded
  and a warning toast explains why.

## Mouse

Mouse support is on by default; turn it off with `--no-mouse` or the
`mouse capture` setting to use your terminal emulator's own selection.

| Gesture | Effect |
| --- | --- |
| Left-click a pane | Focus it and take control |
| `alt+click` a pane | Focus it without taking control (observe) |
| Left-drag in the focused pane | Select and copy text (see copy mode) |
| Double-click / triple-click | Select a word / a line |
| `ctrl+click` a link | Open the URL with `open` on macOS, `xdg-open` elsewhere |
| Wheel over a pane | Scroll its scrollback; on an alternate screen the wheel sends arrow keys instead |
| Wheel over the tab bar | Switch tabs |
| Wheel over the sidebar | Scroll the section under the pointer |
| Click a tab, the new-tab button, or the scroll arrows | Switch, open, or scroll tabs |
| Drag a tab | Reorder tabs |
| Click `[Menu]` / `[+]` on the menu band | Open the global menu / add a project |
| Click a project card / worktree row | Focus the project (expanding its card) / open the worktree |
| Drag a project card | Reorder projects |
| Click a `▸`/`▾` fold mark | Fold or unfold the card's worktrees |
| Click `[working]` / `[all]` on the Projects band | Switch the projects filter |
| Click `[view]` on the Sessions band | Open the menu holding the sessions scope and order |
| Click a session row | Focus its pane (a needs-you row's question is already on screen) |
| Click `[«]` on the footer band | Collapse the sidebar |
| Click the control indicator | Take, release, or take back control |
| Drag the sidebar edge or a split border | Resize |
| Click or drag a scrollbar | Jump or scroll |
| Right-click | Context menu for the target (see below) |

When a pane's application tracks the mouse (a TUI, `vim`, `less` with mouse on),
clicks, drags, and the wheel are forwarded to it as mouse reports. Hold `shift`
to keep a gesture for the client instead.

**Context menus.** Right-click opens a menu with `j` / `k` or the arrows to move,
`enter` or `space` to activate, and `esc` to close.

| Target | Items |
| --- | --- |
| Pane | rename pane, clear pane name, swap with focused pane, split right, split down, zoom / unzoom, take / release control, respond (when it needs you), copy mode, send right-clicks to pane / use gclient menu, close pane |
| Tab | new tab, rename tab, close tab |
| Project card | rename, close, new worktree, open worktree…, collapse / expand |
| Worktree row | rename, close, delete worktree checkout… |
| Session row | focus, open in new tab, respond (when it needs you), mark seen, take / release control, close terminal / destroy orphaned terminal (when orphaned) |
| Empty tab bar, empty sidebar, or `[Menu]` | new terminal, new tab, new project, settings, keybinding help, reload config, toggle sidebar, destroy orphaned terminals…, detach, quit |
| `[view]` on the Sessions band (left click) | this project / all projects, grouped / priority |

`send right-clicks to pane` flips a per-pane flag so the pane's application gets
right-clicks; the `right-click passthrough` setting does the same for every pane
while its modifier is held. `close` on a project card kills every terminal in the
project's tabs (after a confirm-close dialog) but leaves the project registered.
`delete worktree checkout…` kills the worktree's terminals and removes the
checkout through the daemon. `destroy orphaned terminals…` opens the dialog
described under *Orphaned terminals*.

## Workspace persistence

The layout is the daemon's, not the client's. Tabs, splits, names, and the focus
hints live in the workspace rows on the daemon, and the gterm host keeps the
terminals themselves, so the same workspace comes back in the next window and
after a daemon restart. The client writes nothing about layout.

| Where | What it holds |
| --- | --- |
| Workspace rows on the daemon | Tabs, split layout, tab and pane names, and the focus hints (focused project, tab, and pane) |
| `~/.gobby/client/prefs.toml` | Settings, plus `sidebar_collapsed`, `project_order`, and `[ui.project_labels]`, which the sidebar writes as you change them |
| The window's own memory | Zoom, scrollback position, copy mode, and the machine, projects, and sessions filters |

On launch the client attaches the workspace and opens the focused project. A
project with no tabs gets one shell in its checkout (for the personal project,
the directory `gclient` was launched from). A pane whose terminal died is dropped
from its split by the daemon's restart sweep, and a tab with no surviving panes
is dropped with it; a project that lost every tab starts again with that one
shell. There is no snapshot file to move aside.

## Daemon restarts and reconnects

The gterm host that runs your terminals outlives the daemon: `gobby stop`,
`gobby start`, and `gobby restart` leave every terminal running by default, and
the restarted daemon adopts the surviving host. `gobby stop --terminals` and
`gobby restart --terminals` explicitly drain it.

The client rides through the gap. When the daemon's connection drops:

1. The status line shows `daemon unreachable` and the error that broke the
   connection.
2. The client retries with backoff until the daemon answers, however the
   connection was lost: a deliberate stop or restart shows as *going away*, any
   other loss as *unavailable*. It never gives up on its own; `prefix+shift+q`
   quits a client whose daemon is not coming back.
3. On reconnect it re-attaches the workspace and takes the daemon's fresh
   snapshot of its rows: a pane whose terminal was killed during the restart is
   gone, and so is a tab that lost every pane.
4. It then re-attaches every surviving pane to the same terminal ids, retakes
   control of the focused pane, clears the stale failure banner, and refreshes
   the sidebar.

Panes, tabs, and held control therefore survive `gobby restart`. A write whose
outcome the daemon never confirmed leaves the pane `◌ read-only` until you take
control again. If the host itself was drained or replaced, the affected terminals
are gone and their panes disappear on the next roster refresh.

### Orphaned terminals

Two kinds of terminal row outlive their usefulness, and the client can destroy
both from one place:

- A native terminal whose host epoch is gone (the daemon marks the row
  `orphaned`). Its session row carries the `◌` glyph in the sidebar (the glyph
  alone marks it; no word is printed), and its context menu offers
  `destroy orphaned terminal` in place of `close terminal`.
- A tmux session on the default or gobby socket with no attached client, for
  example one you started by hand and detached from. Gobby-owned agent sessions
  are always detached and are never listed.

`destroy orphaned terminals…` on the global menu (right-click empty chrome, or
the sidebar's `[Menu]` control) fetches the current candidates from the daemon and
opens a checklist with every row checked. Each row shows the session name or
title, the backend, the Gobby session that still owns it (or `no session`), and
the time it was last seen. `j` / `k` or the arrows move, `space` toggles a row,
`a` checks or clears all, `enter` destroys the checked rows, and `esc` cancels.
The status line then reads `destroyed N of M orphaned terminals`, naming any row
the daemon refused; with nothing to clean up it reads `no orphaned terminals`.

A Ghostty tab is a plain shell and never appears here; see
[Bringing a bare terminal in](#bringing-a-bare-terminal-in) for where
daemon-tracked work starts.

_Last verified: 2026-09-18_

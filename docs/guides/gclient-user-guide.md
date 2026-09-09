# gclient User Guide

`gclient` is Gobby's terminal workspace client. It runs in your terminal, attaches
to the terminals the Gobby daemon hosts, lays them out in tabs and split panes,
lists the agents that need your attention, and lets you take or hand back
keyboard control of any terminal. This guide covers the client as a user: how to
launch it, what is on screen, every default keybinding, the mouse, and what
happens across daemon restarts.

For building, installing, and the wire protocols, read
[gterminal-development-guide.md](gterminal-development-guide.md).

## Launching

`gobby install` places the binary at `~/.gobby/bin/gclient`. Run it from inside a
Gobby project checkout:

```bash
gclient
```

```text
Usage: gclient [--project PROJECT] [--daemon-url URL] [--token-file PATH] [--frame-delivery auto|direct|proxy] [--no-mouse] [--version]
```

| Flag | Meaning |
| --- | --- |
| `--project PROJECT` | A project UUID or a checkout path. Without it the client walks up from the current directory to the nearest `.gobby/project.json`. |
| `--daemon-url URL` | Daemon endpoint. Defaults to the local daemon's configured URL. |
| `--token-file PATH` | Bearer token file. Defaults to `~/.gobby/local_cli_token`. |
| `--frame-delivery auto\|direct\|proxy` | How terminal frames arrive. `auto` tries the local frame socket first and falls back to the daemon's WebSocket proxy per pane. |
| `--no-mouse` | Leave mouse events to your terminal emulator. Same as turning off `mouse capture` in settings, for this run only. |
| `--version`, `-V` | Print the version and exit. |
| `--help`, `-h` | Print the usage line and exit. |

Startup checks, in order: the project resolves, `~/.gobby/client/prefs.toml`
parses, the keymap override file parses, the daemon answers `/api/health`, and the
health report shows a running gterm host speaking the client's protocol version.
Each failure prints one message naming the fix (`gobby start`, `gobby init`,
`--token-file`, or the offending config line). Remote daemons are covered in the
development guide's [Remote use](gterminal-development-guide.md#remote-use)
section.

Logs go to `~/.gobby/logs/gclient.log`.

## Layout

```text
┌ sidebar ──────┬ tab bar: 1  2  build Z  +  ─────────────────────┐
│ projects      │ ┌ ▸ zsh ─────────────┐┌ claude ────────────────┐ │
│ ● gobby    ▾  │ │                    ││                        │ │
│   0.5.0 ↑2    │ │   pane (focused)   ││   pane                 │ │
│   ├─ feat-x   │ │                    ││                        │ │
│   └─ fix-y    │ └────────────────────┘└────────────────────────┘ │
│  new   menu   │                                                  │
│───────────────│                                                  │
│ agents grouped│                                                  │
│ ● claude #123 │                                                  │
│   blocked     │                                                  │
├───────────────┴──────────────────────────────────────────────────┤
│ [● held] │ zsh %3 │ direct │ prefix ctrl+] │ message             │
└──────────────────────────────────────────────────────────────────┘
```

**Sidebar.** Two sections split by a draggable rule.

- *Projects* lists each registered project as a card: a state dot, the name, a
  second line with the branch and ahead/behind counts, and worktree rows indented
  under the card. Cards with worktrees carry a `▸`/`▾` fold toggle at the right
  edge. The footer has ` new` (register a project) and `menu` (the global menu).
- *Agents* lists the focused project's terminals that run a Gobby session, each
  with a state label: `blocked` (an attention prompt is waiting), `working`,
  `done` (new output since you last looked), or `idle`. The header shows the sort
  order (`grouped` or `priority`) and, when more than one machine is registered,
  the machine filter (`local`, `all`, or a machine id).

Collapse the sidebar with `prefix+b` or the `«` toggle. Collapsed, it becomes a
narrow rail of numbered project dots.

**Tab bar.** One row of tabs for the focused project; each project keeps its own
tab set. Auto-named tabs show their index, renamed tabs their name, and a zoomed
tab adds ` Z`. A new-tab button follows the last tab; scroll arrows appear when tabs
overflow. The bar hides when only one tab is open if you turn on
`hide tab bar with one tab` in settings.

**Panes.** A tab holds one or more terminals in nested splits. The focused pane's
border title starts with `▸`. A pane that has not yet received a frame says
`waiting for frames`; a pane whose size another viewer set says `sized by <viewer>`
on its bottom row. An empty tab area shows `no pane open`.

**Status line.** From left to right: the focused pane's control indicator
(`[● held]`, `[○ observe]`, `[▲ take-back]`, `[◌ lease lost]`, or
`[◌ read-only]`), the pane's name and tmux address, its transport (`direct` or
`proxy`), the current mode when it is not plain terminal mode, the prefix chord
when the client runs inside tmux, `daemon unreachable` during an outage, and the
latest status message. Clicking the control indicator takes, releases, or takes
back control.

## The prefix key

Most chords start with a prefix, exactly like tmux. Press the prefix, release it,
then press the second key.

| Situation | Prefix |
| --- | --- |
| Running in a plain terminal | `ctrl+b` |
| Running inside a tmux client | `ctrl+]` |

The client detects tmux by asking the tmux server for its identity, not by the
`TMUX` variable alone, so a stale variable does not switch prefixes. Inside tmux
the outer tmux keeps `ctrl+b`, and the status line shows `prefix ctrl+]` as a
reminder. Override either default with a `prefix = "..."` line in the keymap file
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
| *unset* | Cycle the agents section's machine filter | `cycle_machine_filter` |
| *unset* | Toggle grouped / priority agent order | `toggle_agent_sort` |

`up`, `down`, `h`, `j`, `k`, and `l` are direct chords: they act only when the
client is already in navigate mode. In terminal mode every unprefixed key goes to
the focused terminal.

### Client

| Chord | Action | Name |
| --- | --- | --- |
| `prefix+?` | Keybinding help | `help` |
| `prefix+s` | Settings | `settings` |
| `prefix+shift+r` | Reload `prefs.toml` and the keymap file | `reload_config` |
| `prefix+q` | Detach: releases control of the focused terminal | `detach` |
| `prefix+shift+q` | Quit the client | `quit` |

`prefix+m` is reserved for a future command menu. It does nothing today and
cannot be rebound.

## Tabs and panes

A new tab starts its shell in the focused project's checkout; a split starts a
fresh shell for the same project. Worktree rows
in the sidebar open a tab whose shell starts in that worktree, or reveal the tab
that already shows it.

**Closing kills the terminal.** `close_pane`, `close_terminal`, and `close tab`
ask the daemon to kill the terminal process. The terminal is not backgrounded and
does not reappear in the sidebar; use `release_control` or `detach` if you only
want to stop typing into it. Closing a tab kills every pane in it. With
`confirm close` on (the default), closing a tab first opens a dialog that names
the tab and counts its panes; `y` or `enter` confirms, `n` or `esc` cancels. If
the daemon refuses the kill, the pane stays.

Renames apply locally: a tab name, a pane name, or a project label is yours and
does not change the daemon's terminal title.

## Attach and control

Every pane you open is *attached*: it receives frames. Whether your keystrokes
reach it depends on the control lease, shown in the status line and the pane
title.

| Indicator | Meaning |
| --- | --- |
| `● held` | You hold the lease. Keys, pastes, and mouse reports go to the terminal. |
| `○ observe` | You are watching. Input is not sent. |
| `▲ take-back` | Someone else holds the lease. `prefix+shift+a` or the indicator asks for it back. |
| `◌ lease lost` | The daemon revoked your lease, typically because another viewer took over. |
| `◌ read-only` | A write's outcome is unknown after a disconnect. Take control again to continue. |

Focusing a pane takes control of it automatically, whether you focus it by
keyboard, by click, or through the navigator. Typing into an observed pane also
requests control first and delivers the pending keystrokes once the lease is
granted. To look at a pane without taking it, `alt+click` it.

`prefix+u`, `prefix+q`, and `ctrl+\` release the lease. The daemon can refuse a
take: the pane then shows `▲ take-back` and the reason lands in the status line.
On quit, the client releases every held lease and detaches every pane; the
terminals keep running.

## Attention prompts and respond

When an agent blocks on a question, its sidebar row turns `blocked`. `prefix+a`
opens the respond dialog for the first actionable prompt among the focused
project's agents; clicking a blocked row, or choosing *respond* from its
right-click menu, opens it for that row. The dialog shows the prompt and either
its options or a free-text field:

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

`prefix+s` opens the settings popup. `j` / `k` or the arrows move, `enter` or
`space` toggles the row, `left` / `right` steps a value, `esc` closes. Rows are
clickable. Every change is written to `~/.gobby/client/prefs.toml` at once.

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
| agent sort | `grouped` | `grouped` or `priority` order in the agents section |

The file is optional and every key in it is optional; an unknown key is a
startup error that names the line.

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
| Wheel over the sidebar | Scroll that section |
| Click a tab, the new-tab button, or the scroll arrows | Switch, open, or scroll tabs |
| Drag a tab | Reorder tabs |
| Click a project card / worktree row | Focus the project / open the worktree |
| Drag a project card | Reorder projects |
| Click a `▸`/`▾` toggle | Fold or unfold the card's worktrees |
| Click an agent row | Focus its pane, or open the respond dialog when it is blocked |
| Click the control indicator | Take, release, or take back control |
| Drag the sidebar edge, the section rule, or a split border | Resize |
| Click or drag a scrollbar | Jump or scroll |
| Right-click | Context menu for the target (see below) |

When a pane's application tracks the mouse (a TUI, `vim`, `less` with mouse on),
clicks, drags, and the wheel are forwarded to it as mouse reports. Hold `shift`
to keep a gesture for the client instead.

**Context menus.** Right-click opens a menu with `j` / `k` or the arrows to move,
`enter` or `space` to activate, and `esc` to close.

| Target | Items |
| --- | --- |
| Pane | rename pane, clear pane name, swap with focused pane, split right, split down, zoom / unzoom, take / release control, respond (when blocked), copy mode, send right-clicks to pane / use gclient menu, close pane |
| Tab | new tab, rename tab, close tab |
| Project card | rename, close, new worktree, open worktree…, collapse / expand |
| Worktree row | rename, close, delete worktree checkout… |
| Agent row | focus, open in new tab, respond (when blocked), mark seen, take / release control, close terminal |
| Empty tab bar, empty sidebar, or the `menu` button | new terminal, new tab, new project, settings, keybinding help, reload config, toggle sidebar, detach |

`send right-clicks to pane` flips a per-pane flag so the pane's application gets
right-clicks; the `right-click passthrough` setting does the same for every pane
while its modifier is held. `close` on a project card kills every terminal in the
project's tabs (after a confirm-close dialog) but leaves the project registered.
`delete worktree checkout…` kills the worktree's terminals and removes the
checkout through the daemon.

## Workspace persistence

The client saves its layout as it changes and restores it on the next launch.

| File | Contents |
| --- | --- |
| `~/.gobby/client/<project-id>/workspace.json` | That project's tabs, split layout, focused pane, and worktree tags |
| `~/.gobby/client/session.json` | The focused project, sidebar width and collapse, section split, machine filter, project order, and project labels |
| `~/.gobby/client/prefs.toml` | Settings, as above |

On launch the client restores the focused project's tabs to the terminals that
still exist; a terminal that has gone is dropped from its split, and a tab with no
surviving panes is dropped. If no snapshot exists, one shell opens in the project
checkout. Layout is never written to `prefs.toml`; a corrupt snapshot is moved
aside and the client starts from an empty layout.

## Daemon restarts and reconnects

The gterm host that runs your terminals outlives the daemon: `gobby stop`,
`gobby start`, and `gobby restart` leave every terminal running by default, and
the restarted daemon adopts the surviving host. Only `gobby stop --terminals`
drains it.

The client rides through the gap. When the daemon's connection drops:

1. The status line shows `daemon unreachable` and the error that broke the
   connection.
2. The client retries with backoff. A daemon shutdown is recognised as *going
   away* and is retried without limit; other losses stop after a bounded budget,
   after which the client exits with the reason.
3. On reconnect it re-attaches every pane to the same terminal ids, retakes
   control of the focused pane, clears the stale failure banner, and refreshes
   the sidebar.

Panes, tabs, and held control therefore survive `gobby restart`. A write whose
outcome the daemon never confirmed leaves the pane `◌ read-only` until you take
control again. If the host itself was drained or replaced, the affected terminals
are gone and their panes disappear on the next roster refresh.

_Last verified: 2026-09-09_

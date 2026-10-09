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
| `--project PROJECT` | A project UUID or a checkout path. Without it the client walks up from the current directory to the nearest `.gobby/project.json`; outside every checkout it opens the project the workspace's rows say was focused last, or the personal project. No shell starts until you ask for one. |
| `--workspace WORKSPACE` | The workspace to attach: a ref (`1`, or `2:1`, whose node overrides `--node`) or a name. Defaults to `default`, which is created on first use. See [Workspaces](#workspaces). |
| `--node NODE` | The node that owns the workspace: a ref (`2`), a node id, a hostname, or a label. Defaults to the daemon's own node. |
| `--daemon-url URL` | Daemon endpoint. Defaults to the local daemon's configured URL. |
| `--token-file PATH` | Explicit plaintext API-key file. Without this flag, the client reads `api_key` from `~/.gobby/bootstrap.yaml`. |
| `--frame-delivery auto\|direct\|proxy` | How terminal frames arrive, and with them your keystrokes. `auto` tries the local frame socket first and falls back to the daemon's WebSocket proxy per pane. Forcing `proxy` puts typing back on the daemon for every pane. |
| `--no-mouse` | Leave mouse events to your terminal emulator. Same as turning off `Mouse capture` in settings, for this run only. |
| `--version`, `-V` | Print the version and exit. |
| `--help`, `-h` | Print the usage line and exit. |

An explicit `--project` must resolve (a directory outside every checkout is
fine), and `~/.gobby/client/prefs.toml` and the keymap override file must parse.
Config errors name the offending line before the window opens. The client then
draws the window and checks daemon health, attaches the workspace, loads the
roster, and waits for the first terminal frame. A stopped daemon keeps the
window open for retries; see [Daemon restarts and reconnects](#daemon-restarts-and-reconnects).
Remote daemons are covered in the development guide's
[Remote use](gterminal-development-guide.md#remote-use) section.

Logs go to `~/.gobby/logs/gclient.log`.

## Layout

```text
 0 | Gobby  File  Edit  View  Window  Agent  Help
 1 | tab-0:0:0  tab-0:0:1 Z │ +
 2 |┌ ○ zsh · Focused ─────────────┐┌ ○ #1742: Codex ──────────────────┐
 3 |│  pane (focused)              ││  pane                            │
 4 |│                              ││                                  │
 5 |│                              ││                                  │
 6 |└────────────── tmux · 0:0:1:1 ┘└───────────────────────── 0:0:1:2 ┘
 7 |                                              prefix ctrl+b

    The sidebar starts pinned beside the tabs and panes. Unpin it to use
    an overlay on the configured side without moving the panes.

       ┌ sidebar overlay ───┐
 1–6   │ Machines           │ left-side example; the side is configurable
       │ Projects           │
       │ Agents             │
       │ Terminals          │
       └────────────────────┘
```

The menu bar occupies row 0, the tab bar row 1, the panes the middle,
and the status bar the last row. With default pane gaps, every pane draws all
four edges. This example shows the sidebar hidden for clarity. By default it
starts pinned in a saved column beside the content. `prefix+b` unpins it; a
later `prefix+b` opens the overlay on the configured side without resizing
tabs or panes. `esc` or `prefix+b` rolls the overlay up. **View › Sidebar ›
Pin sidebar** pins it again. The menus remain usable while the client connects.

**Menus.** Click a title on row 0 or move between open menus with the keyboard.
Items that need a focused pane, an attention prompt, or control are disabled
when those conditions are absent. Slashes below separate alternative labels:

| Menu | Items |
| --- | --- |
| Gobby | Settings, Reload config, Quit |
| File | New terminal, New tab, New project…, Open project…, Rename tab, Close tab, Destroy orphaned terminals…, Detach |
| Edit | Copy mode, Rename pane, Rename tab, Rename terminal, Clear pane name (when named), Send right-clicks to pane / Use gclient menu |
| View | Appearance: Dark / Light / System ▸, Theme: Restored ▸, Monochrome, Sidebar ▸ |
| Window | Split right, Split down, Zoom / Unzoom, Close pane, Resize mode, Arrange ▸ (Even horizontal, Even vertical, Main horizontal, Main vertical, Tiled, New grid…) |
| Agent | Respond, Mark seen, Take control, Release control, Take back, Detach, Open alert target, Next attention, Previous attention |
| Help | Keybinds, Alerts…, Daemon, About Gobby |

**File › New project…** creates a directory (or uses an existing empty one),
initializes Git, registers the checkout with Gobby, and attaches its default
workspace. Enter the full directory path; `tab` completes directories. A
nonempty directory is refused. Initialization or registration errors stay in
the dialog and preserve the directory. If registration fails after Git
initialization, retry with **Open project…**.

**File › Open project…** accepts an existing Git checkout, registers it if
needed, and attaches its default workspace. Registered checkouts open without
another registration. A path inside the checkout resolves to its root.

A row ending in `▸` opens a submenu beside its menu, which stays drawn with
that row lit. `→` or `l` opens the submenu under the cursor, `←` or `h`
returns to the menu it came from, a click on a row of any open menu acts on
it, and `esc` closes them all. **View › Appearance** holds Dark, Light, and
System. **View › Theme** lists the themes the appearance in force offers
(**Themes**, below); a pick redraws at once and is saved. **View ›
Monochrome** toggles monochrome (below). **View › Sidebar** holds:

| Sidebar row | Items |
| --- | --- |
| Show sidebar | toggles the sidebar |
| Pin sidebar | pins it beside the content, or unpins it to an overlay |
| Machines ▸ | This machine / All machines: the machine filter for the sections below |
| Projects ▸ | Working projects / All projects |
| Agents ▸ | This project / All projects, then Grouped / Priority |
| Terminals ▸ | New terminal, Destroy orphaned terminals… |

A `✓` marks the value or toggle in force. The attention legend lives in
**Help › Keybinds**, where it opens first.

**Monochrome.** **View › Monochrome**, or `Monochrome` in settings, draws the
client's own chrome in grays: every token of the theme in force keeps its
lightness and drops its hue. Pane contents keep their own colours. The choice is saved as
`monochrome = true` under `[ui]` in `~/.gobby/client/prefs.toml`. The glyph
carries the state: several states share a gray (in Dark, active and needs
you sit at the same lightness), so read `⍾`, `▶`, `○`, and the rest, and
the `needs you` words, rather than the shade.

The Dark and Light appearances paint their own ground: every cell and every
pane's default colours take the theme's text and background, whatever background and
foreground the hosting terminal (Ghostty included) configures. Those cells are
explicit colours, so a terminal setting that applies opacity to explicit cells
(Ghostty's `background-opacity-cells`) still applies. System keeps the hosting
terminal's own background and foreground and only picks the palette from the
OS appearance.

**Themes.** A theme sets two fills and a model colour. The header fill takes
the sidebar's section headings and the tab row. The menu bar sits on its own
bar fill, a lightness step off the header fill toward the selection. The
selection fill takes the selected and active sidebar rows, the active tab,
and the open menu's title. The model colour takes the model line of the
Agents rows. In Dark and Light a theme also sets its own ground. Restored is
the default.
Each appearance offers its own list under **View › Theme** and the `Theme`
setting:

| Appearance | Themes |
| --- | --- |
| Dark, Light | Restored, Moss, Midnight moss, Staircase, Inverse bar, Gobby bar, Contrast chrome, Moss chrome, Moss band |
| System | Restored, Moss, Midnight moss, Inverse bar, Gobby bar, Contrast chrome, Moss chrome, Ink, Host-matched |

While the OS appearance is light, System draws each theme's Light fills and
does not offer Ink or Host-matched. A saved theme that the appearance in
force does not offer draws as Restored and stays saved, so it returns when
an appearance that offers it does. Host-matched tints its fills with the host
terminal's own background hue at chroma 0.03 at most, or less where the
host's own chroma is lower. Until the host reports its background, those
fills are gray.

In Dark and Light, an unfocused pane sits on its own fill, a step in
lightness off the ground with the ground's hue, and its text keeps full
colour. The fill runs under the pane's scrollbar lane too. System marks focus
by the pane border alone. A tab with one pane draws no fill.

The five layouts under **Arrange ▸** redistribute a tab's panes into the
chosen layout. Window › Arrange ▸ arranges the active tab; the same submenu in
a tab's or a pane's right-click menu arranges that tab, bringing it forward
first. A choice whose tab has closed, or whose pane has left it, is refused
with a notice. **Arrange ▸ › New grid…** asks for rows and columns
and starts a terminal in each new cell. **Help › Daemon** shows the daemon URL, client and daemon
versions, health, last roster refresh, and completed startup timings. **Help ›
About Gobby** shows the versions, URL, and machine.

**Sidebar.** Shown and pinned by default. Its side and pinned state are saved
in preferences; unpinned it opens as an overlay on the saved side.
Four sections, each under a bold heading on the theme's header fill:
Machines, Projects, Agents, and Terminals. The selected row and the active
row sit on the selection fill, and the selected row also leads with the `▸`
marker. The Terminals heading is hidden
while no bare terminal is open. Each section's view options live in its own
**View › Sidebar** submenu. Machines and Projects together
never take more than the top half of the sidebar (each scrolls inside its cap);
Agents and Terminals share the rest. A section with more rows than room draws a
scrollbar thumb and no track. The thumb is dim at rest, and brightens for a
second after the section scrolls or while the navigate cursor is in it.

- *Machines* lists this machine (the hub, by host name) first, then every other
  machine the daemon knows nested under it with `├─`/`└─`, each with the most
  urgent state of the terminals running there. At most four rows show before
  the list scrolls. The rows are the machine filter for the sections below:
  clicking a remote machine shows that machine's terminals, clicking it again
  returns to this machine; clicking the hub row toggles all machines.
  **View › Sidebar › Machines** sets the same filter to This machine or All
  machines.
- *Projects* lists registered projects as one-line cards:
  `glyph name (branch ↑ahead ↓behind)` with a `▸`/`▾` fold mark at the right
  edge. Only one project is expanded at a time: selecting a project expands it
  and folds the others, and its worktrees appear under the card as
  `├─ glyph branch · #task`, with `~` for the branch of a detached worktree.
  The glyph rolls up the agents running in that worktree; a worktree with no
  agent draws a blank in its place, since it has no state to show.
  A branch too long for its row drops the task and scrolls on the same clock
  as the Agents titles.
  **View › Sidebar › Projects** chooses Working projects (projects
  with a live session, run, or terminal on the current machine filter, plus the
  focused one) or All projects.
- *Agents* lists active agents and sessions with their state, reference, name,
  task title, and model. A session named by hand shows that name where the
  definition goes (`#14069: Assistant`). Otherwise the agent definition names
  the row, or the provider when there is none; the provider is not repeated
  beside the title. The third line is the model, led by its family
  (`claude-opus-5.5`, `gpt-6.1-sol`) with the effort appended, in the
  theme's model colour, a muted teal or clay that no state uses. The provider
  and model are identified by their words: the model colour is the same for
  every provider.
  Runs can nest under their
  parent session. Selecting one in another workspace switches to that workspace
  and focuses its existing pane. If the
  terminal has gone away, the row refreshes and a warning explains that it is
  unavailable. In the all-projects view the fixed prefix is `project#ref:`;
  in the current-project view it is `#ref:`. Only the title after that prefix
  scrolls. By default it rests at the start,
  walks left to its end, parks, and jumps home; every scrolling row and pane
  header shares one clock. Settings can reverse that direction or turn it off.
  **View › Sidebar › Agents** holds both axes: the scope, This project (the
  focused project only) or All projects (every project, grouped under dim
  project rows), and the order, Grouped (tab order, with agent runs nested
  under the session that spawned them) or Priority (flattened urgency order).
  A `✓` marks the value in force, and choosing it again closes the menu
  unchanged.
- *Terminals* lists bare terminals without an agent row by their given pane
  name or foreground command. The pane's address sits at the right edge, as
  the pane's corner prints it, and gives way when the name needs the room.
  The working directory sits below, with `~` for your home, once the daemon
  reports one. The client relists every five seconds, so the directory and
  command follow a `cd` or a new program within that time; a directory the
  daemon can no longer read drops the line. Click or right-click one as you
  would an agent row.

State glyphs, on machine, project, and session rows alike. The same table opens
first in the keybinding help as the attention legend:

| Glyph | State | Counts as | Means |
| --- | --- | --- | --- |
| `⍾` | needs you | need you | waiting on you: an approval, a question, an error it cannot pass; the row's first line also carries the words `needs you` when they fit beside the title |
| `▶` | active | active | running a turn |
| `○` | idle | idle | at its prompt, nothing pending |
| `◆` | output unseen | idle | finished since you last looked; clears when its pane is focused |
| `‖` | held | idle | a run someone paused on purpose; never an agent idle at its prompt |
| `◌` | gone | gone | process or relay missing (see *Orphaned terminals*) |
| `·` | no state yet | idle | the first seconds after a spawn, before the daemon has a state |

A machine, project, or worktree row shows the most urgent state among its
agents: needs you, then gone, then held, then active, then idle. A held run
still waits on someone, so it outranks work. Output unseen and no state yet
roll up as idle. A machine or project with no agents is idle; a worktree with
no agents shows no glyph. The seven states display on agent rows and pane
corners; machine, project, and worktree rows show the rolled-up five, and
tabs show only `⍾`.

Only needs-you rows carry a word. A font without U+237E shows a box in place of
`⍾`; the words still identify the row.

**Tab bar.** One row of tabs for the focused project. A registered project opens
as its own workspace, so the address is hub, then workspace, then tab, then pane:
Gobby on workspace 0 and Game Goblins on the next workspace is `0:1:0`, not a
second tab of workspace 0. `prefix+c` still opens a tab inside the attached
workspace. `prefix+shift+g` switches to the next workspace on this node. See
[Workspaces](#workspaces). A tab you have not renamed shows its number and
`Untitled` (`6: Untitled`), which stays put while the panes inside it change
what they run; a renamed tab shows its name alone, and a zoomed tab adds ` Z`.
A tab, the active tab included, leads with `⍾` while an agent on one of its
panes needs you; any other state shows no glyph, since work and new output
stay on the sidebar rows. A `│` rule sets the new-tab button `+` apart from
the last tab. When the tabs overflow, each shows whole or not at all, the
new-tab button stays at the right end, and each edge counts the tabs hidden
beyond it (`‹ 3`, `2 ›`). A count takes `⍾` and the needs-you colour when one of
those tabs needs you, and a click on it pages the bar one screen. The bar hides
when only one tab is open if you turn on `Hide tab bar with one tab` in
settings.

**Panes.** A tab holds one or more terminals in nested splits; each pane is a
workspace row with a ref such as `0:0:1:2`, and its name is that row's label.
A pane's top-left corner names who sits in it: the agent's state glyph, its
session reference, and its definition, as in `○ #1742: Codex`. A session named
by hand shows that name in the definition's place, as in `○ #14069: Assistant`,
without repeating the `project#seq:` prefix the title carries. The project
leads the reference (`○ gobby#1742: Codex`) only in the all-projects priority
view. A seat with no definition names its provider, such as `○ Claude Code`,
and a bare shell shows its name, such as `○ zsh`. The focused pane appends
` · Focused`, or ` · Read-only` / ` · Uncertain` when typing is unsafe;
unfocused panes carry no condition word. The focused title is bold in the
accent, a seat that needs you reads in the warning hue, and a read-only or
uncertain focused pane is dimmed. The task title stays on the Agents row.
Over-long pane titles share the Agents ticker. The bottom-left corner is
empty. The bottom-right corner reads the sandbox lock on an SRT pane, then
`tmux` for a tmux pane, then its address, joined by ` · `: `0:0:1:2` for a
workspace pane, `tmux · 0:0:1:1` for a tmux pane the workspace holds,
`tmux · %16` for a tmux pane it does not (its own tmux id), or the backend
alone (`gclient`, `tmux`) until the address is known (see
[Attach and control](#attach-and-control)). A red closed lock
(`` U+F023, in the destructive hue) leads the address only when Gobby
launched the pane under its SRT sandbox, meaning the agent run records the
sandbox as enforced or the managed session's launch contract enabled it.
Every other pane (a seat you start yourself, a bare shell, or a pane with no
agent row) shows no mark, so the lock's presence carries the state in any
colour setting. A provider's own sandbox settings never lock a pane. The lock
is a Nerd Font symbol, which Ghostty's default font includes; set
`nerd_glyphs = false` under `[ui]` in `~/.gobby/client/prefs.toml` to draw
`sbx` instead. Add `sandbox` to `[status] left` or `right` to name the
focused pane's state in words: `sandboxed` or `unrestricted`. With `Pane gaps` off, a pane above
another shares that pane's top line and has no bottom edge of its own; its
address moves to the top-right of its own title row, unless that would leave the
title fewer than four cells. A pane that has not yet received
a frame says `waiting for frames`; a pane whose size another viewer set says
`sized by <viewer>` on its bottom row. An empty tab shows a dimmed goblin mark
above **No pane open.** when there is room; a small window keeps the text and
omits the mark. From there, use the live `prefix+w` binding to attach a
terminal, **File › New terminal** to start one, or `prefix+b` to open the
sidebar. If you changed the keymap, these hints show your current chords.

**A Codex prompt that stays on the first row.** Codex (checked on 0.159.0) runs
on the alternate screen and pins your latest prompt to the top row of its
pane while the response scrolls beneath it. In a Gobby pane that prompt is
often the daemon's wake text (`Message from Gobby daemon: New activity
available.`), so it can look stuck under the pane header. That row is Codex's
own drawing: the same bytes replayed into plain tmux, gterm, and gclient give
identical screens, and Claude Code, Codex run with `--no-alt-screen`, and a
shell all scroll their first row normally. gclient shows the pane as Codex
drew it and leaves Codex's launch settings unchanged.

**Status line.** The left counts every agent on this machine by its legend
state, whatever the sidebar shows: `⍾ 1 needs you` or `⍾ N need you`, then
`N idle`, `N active`, and `◌ N gone`, separated by `│`. Output unseen, held, and
no state yet count as idle, so the counts sum to the agents on this machine. A
count of zero draws nothing. Clicking the needs-you count opens the sidebar in
navigate mode. An outage leads the line with
`× Daemon unreachable · retrying in <n> s`, and the last known counts stay
beside it. The idle, active, and gone counts give way to the focused pane's title
or drop whole when the window is too narrow for the right-hand hints.
The right end shows the prefix chord,
shifted when the client runs inside tmux, and the current mode when it is not
plain terminal mode. Pane-local title, address, and control state stay on that
pane's borders. Every pane draws a top edge; a focused pane four cells wide or
narrower has no room for its title there and hands it here, where it leads the
line. The title is clickable only while it names an exception: clicking a title
that ends `Read-only` or `Uncertain`, on the border or here, takes control, and
hovering it underlines the title's words. `Focused` is a
condition, not a button.

**Alerts.** Messages that used to sit in the status line are toasts: they
stack at the top-right corner of the pane area, newest at the bottom, up to
three at once, and leave after six seconds or at the next keypress. Every
toast is kept in the alert log (**Help › Alerts…**; the last 200),
newest first; `j` / `k` or the arrows scroll it, `enter`, `esc`, or `q` closes
it. A condition rather than an event, such as `Daemon unreachable`, stays in
the status line.

### What a terminal is called

When no running session title is available, surfaces that need a terminal name
ask these questions in order and stop at the first answer:

1. **The name you gave the pane.** Rename it from the pane's context menu or
   the rename key; the name is stored on the workspace row, so it survives a
   restart and every other viewer sees it too.
2. **The command in its foreground** — `zsh` at an idle prompt, `nvim` or
   `cargo` while a job holds the terminal, `claude` or `codex` while an agent
   runs. The daemon reads this when it serves the terminal list, so it follows
   what you are actually running.

If a terminal has neither a name you gave it nor a foreground command, its
short terminal address alone identifies it rather than a long UUID.

Where two terminals share a name — most panes on a machine are running a shell
— the address is what tells them apart: the pane's workspace ref (`0:0:0:1`),
or the tmux pane id of an external tmux pane, which you can type straight into
tmux. The address sits at the right edge of a Terminals row and leads the
navigator's detail.
A session's provider
(`claude`, `codex`, `droid`) is a token beside the address rather than the
terminal's name.

The daemon also reports a `title` for each terminal, which you will see as
secondary text on a bare terminal row. It is not used as a name: tmux fills it
from the window name, the pane title or the session name, so it is `zsh` for
one pane and `75`, `[tmux]` or a whole session banner for the next.

## Workspaces

A workspace is the daemon's record of a layout: its tabs, the panes in them, and
the terminal each pane shows. Addressing is hub, workspace, tab, pane. A
registered project has one default workspace on the machine; opening that project
attaches it, creating it at the next free workspace ref when it does not exist
yet. A launch with no project attaches the projectless scratch named `default`.
An explicit `--workspace` wins and may hold tabs from more than one project.
`prefix+shift+g` cycles every workspace on the node without quitting; `prefix+c`
opens a tab in the workspace you are already on. This reverses the earlier rule
that projects stayed inside one workspace. The client attaches one workspace at
startup and renders its rows; it keeps no layout of its own. Every window
attached to the same workspace shows the
same tabs, panes, and names, whether a change came from another `gclient`, from
`gobby panes split`, or from an agent calling the `gobby-workspaces` MCP tools.

**Refs.** Nodes, workspaces, tabs, and panes are addressed by short refs, all
digits and zero-based: `0:1:2:1` is pane 1 of tab 2 of workspace 1 on node 0.
A shorter ref is read from the left: `1` is a workspace, `2:1` is workspace 1
on node 2, `0:1:2` is a tab. Each number is the lowest free one in its scope,
so a closed pane's number is reused by the next split. Underneath, every row
also has a UUID.

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
  `external`, so `close_pane`, closing the tab, and `close terminal` on its
  sidebar row all release it rather than kill it.
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
| `prefix+c` | Open an empty tab without starting a shell | `new_tab` |
| `prefix+shift+g` | Switch to the next workspace on this node | `next_workspace` |
| `prefix+n` / `prefix+p` | Next / previous tab | `next_tab` / `previous_tab` |
| `prefix+1` … `prefix+9` | Switch to tab 1–9 | `switch_tab` |
| `prefix+shift+left` / `prefix+shift+right` | Move the active tab one position left / right | `move_tab_left` / `move_tab_right` |
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
| `prefix+shift+1` … `prefix+shift+9` | Move the focused pane to tab 1–9 in this workspace | `move_pane_to_tab` |
| `prefix+shift+c` | Move the focused pane to a new tab in this workspace | `move_pane_to_new_tab` |
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
| `prefix+b` | Open or close the sidebar overlay; unpin a pinned sidebar | `toggle_sidebar` |
| `up` / `down` | Select the previous / next sidebar row (enters navigate mode) | `navigate_up` / `navigate_down` |
| `h` / `j` / `k` / `l` | Focus the neighbouring pane (navigate mode only) | `navigate_pane_*` |
| `prefix+shift+n` | Create project | `new_project` |
| *unset* | Open project | `open_project` |
| *unset* | Focus the next / previous project | `next_project` / `previous_project` |
| *unset* | Focus project 1–9 | `switch_project` |
| *unset* | Collapse or expand the project's worktrees | `toggle_group` |
| *unset* | Cycle the machine filter (this machine, all machines, each machine) | `cycle_machine_filter` |
| *unset* | Toggle working / all projects | `toggle_projects_filter` |
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

The **Help › Keybinds** overlay shows the current bindings after overrides. In a
narrow window it hides binding names, keeping chords and descriptions readable;
you can still search by a binding name.

`prefix+m` is reserved for a future command menu. It does nothing today and
cannot be rebound.

## Tabs and panes

A new tab opens empty. Use New Terminal or a split to start a shell in the
focused project's checkout; that shell and its pane are created together in the
daemon workspace. The tab stays `Untitled` until you rename it. Worktree rows
in the sidebar open a tab whose shell starts in that worktree, or reveal the tab
that already shows it. An empty tab is a draft in this window; once you open a
shell in it, the daemon-owned tab takes its place and survives reattachment.
Splitting a tab of bare terminals moves its current pane layout into the daemon
workspace. Those existing terminals stay adopted; the new shell is owned.

**Closing kills gobby's terminals, not yours.** `close_pane`, `close_terminal`,
and `close tab` ask the daemon to kill a terminal gobby started. The terminal is
not backgrounded and does not reappear in the sidebar; use `release_control` or
`detach` if you only want to stop typing into it. A tmux session you started
yourself (the sidebar lists it because the daemon found it, ownership
`external`) is never killed by `close_pane` or `close tab`: the pane leaves the
tab, its control lease is released, and the session stays in the sidebar to
reopen later. Only `close_terminal` kills an external session. Closing a tab
kills every gobby-owned pane in it. With `Confirm close` on (the default),
closing a tab first opens a dialog that names the tab and counts its panes; `y`
or `enter` confirms, `n` or `esc` cancels. If the daemon refuses a kill, that
pane stays, and so does its tab.

A tab name and a pane name are workspace row state: rename one and every window
on the workspace shows it, it survives daemon restarts, and
`gobby panes rename REF [NAME]` sets or clears the same label from the CLI.
Neither changes the terminal's own title. A project label is yours alone; the
client keeps it in `prefs.toml`.

Moving a pane to another tab keeps its running process and scrollback, and focus
follows it. The source layout closes the gap; if it was the last pane, the
empty source tab disappears. Tab order and pane placement are saved in the
workspace and survive detach and reattach.

## Attach and control

Every pane you open is *attached*: it receives frames. Whether your keystrokes
reach it depends on the control lease, summarized at the pane's bottom-right.

| Pane title ends | Meaning |
| --- | --- |
| ` · Focused` | This is the focused pane. It holds the lease or is acquiring it; keys, pastes, and mouse reports are delivered in order. |
| (no condition word) | An ordinary unfocused pane, including one deliberately observed with `alt+click`. |
| ` · Read-only` | Another viewer took the lease, or the host refused this pane's input. Typing is refused; click the title or use `prefix+shift+a` to take control. |
| ` · Uncertain` | A proxied write's outcome is unknown after a disconnect. Typing is refused; click the title or take control again to continue. |

### Where your keystrokes go

A local native pane you hold types straight to the terminal host over the same
socket its frames arrive on. Nothing waits on the daemon between the key and the
PTY, so a busy or slow daemon no longer stalls the window. The daemon still
decides who may type: taking the lease makes it grant your attachment input on
the host, and releasing it, losing it to a takeover, or detaching revokes that
grant.

Every other pane keeps typing through the daemon — tmux panes, remote panes,
and any pane whose direct connection failed and fell back to `proxy`. Those
are the panes that can show `· Uncertain`, because only a write the daemon
acknowledges can have an unknown outcome. No indicator tells the two apart; a
pane that typed instantly and then went sluggish fell back, and
`~/.gobby/logs/gclient.log` records it as `direct-fallback`. Under `auto`, a
fallen-back pane keeps trying the direct connection beside its proxy, first
after 5 seconds and then backing off to once a minute. The proxy keeps
delivering until the host accepts and the direct stream is in place. Then the
pane switches back, a lease you held follows it, and the log records
`direct-promotion`.

On hosts that support the kitty keyboard protocol, gclient enables disambiguated
key reporting while the client is active. That lets the host distinguish modified
keys such as `ctrl+enter`; unsupported hosts retain their legacy input behavior.
Key encoding then follows the focused pane's latest terminal mode. When an
application enables the kitty keyboard protocol, modified Enter and other extended
keys reach it as distinct keys; a pane that has not enabled the protocol keeps
legacy terminal encoding, where modified Enter is indistinguishable from Enter.

Three messages belong to the direct path:

| Status line | What happened |
| --- | --- |
| `terminal did not grant input; take control again` | The daemon gave you the lease but the host did not accept the matching grant. The pane drops to take-back; asking again is the only honest offer, since there is no daemon path to fall back to. |
| `terminal refused input (<code>); take control again` | The host refused a key, normally `input_not_granted` after your grant was revoked. Output keeps flowing; the pane returns to observing. |
| `terminal input backlog; key dropped` | You typed faster than the terminal drained. That one keystroke is gone and is not retried, because a retried keystroke is the wrong keystroke. |

Keyboard and navigator focus claim a free pane. A completed left click does the
same, including when its previous holder released it. Pressing alone does not
take control, so dragging to select text leaves the lease alone. Clicking a pane
you already control sends no new take request. If another live agent holds the
pane, the click focuses it and shows the read-only takeover control; use that
indicator or `prefix+t` for a deliberate take-back.

Asking the daemon who may type costs one round trip, and that round trip belongs
to the focus change or completed click, never to a key. For a free pane, keys,
pastes, and forwarded mouse reports you produce before the grant lands
are held in order and written the instant it does. Nothing is dropped and nothing
announces a mode — the pane's title ends `· Focused` throughout. Only a daemon
that stops answering can overrun that queue, and then a warning toast says
`too much typed while acquiring control; the rest was dropped`.

Moving focus away before the grant lands discards whatever that pane was holding,
rather than replaying it whenever the pane next wins control — a command that
runs long after you typed it is worse than one that never ran.

To look at a pane without taking it, `alt+click` it. An observed pane still
types: the first key takes control for you. A forwarded mouse report does not,
so a pane you deliberately left observing stays that way under the pointer.

`prefix+u`, `prefix+q`, and `ctrl+\` release the lease. The daemon can refuse a
take: the pane's title then ends `· Read-only`, the reason arrives
as a toast, and clicking the title takes control back.
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

The `Send` and `Cancel` buttons act as `enter` and `esc`.

`mark seen` in the agent menu acknowledges the row's attention entry without
opening the terminal.

## Modes

The status line names the active mode. `esc` leaves every mode.

**Copy mode (`prefix+[`).** Mouse selection is the copy gesture: left-drag over
pane text, scroll while selecting to extend through native scrollback, and
release to copy the whole selection to your clipboard through OSC 52. Any other
key leaves copy mode and goes to the terminal. Outside copy mode the same gesture
works in the focused pane whenever its application is not tracking the mouse;
`shift+drag` forces it even when the application is. Double-click selects a word,
triple-click a line, each copying immediately. Direct native attachments read the
off-screen range from gterm; proxy attachments and tmux panes copy the visible
frame or attach history available to the client.

**Resize mode (`prefix+r`).** `h` / `j` / `k` / `l` or the arrow keys move the
focused pane's border one step. `enter` or `esc` leaves. Dragging a split border
with the mouse resizes without entering the mode.

**Navigator (`prefix+w` or `prefix+g`).** A popup listing the focused project's
terminals followed by its attention entries, each with its address, backend,
state, and control indicator; the search matches the address too, so `0:0:1`
finds the panes of one tab. `j` / `k` or the arrows move, `enter` switches to the row's pane,
`/` focuses the search box (`ctrl+u` clears it, `esc` returns to the list), and
`a` / `b` / `w` / `i` (or `tab` to cycle) filter to all / blocked / working /
idle rows. `prefix+g` opens with the search box already focused.

**Navigate mode (`up` / `down`).** Moves the selection through the project cards
and worktree rows. `enter` focuses the selected project or opens the selected
worktree; `esc` returns to the terminal.

**Keybinding help (`prefix+?`).** The attention legend, then the live keymap
with names. A search hides the legend. `j` / `k`, the
arrows, and `PageUp` / `PageDown` scroll, `/` searches by key, description, or
name, `enter` or `esc` closes. The `Close` button closes too; while the search
has focus it reads `Back` and returns to the list, as `esc` does.

**Settings (`prefix+s`).** See below.

**Rename dialogs.** Type, `enter` commits, `ctrl+c` clears the name, `esc`
cancels. The `Save`, `Clear`, and `Cancel` buttons do the same.

## Settings

`prefix+s` opens the settings popup. `j` / `k` or the arrows move, `space`
toggles the row, `left` / `right` steps a value, `enter` or `esc` closes, as do
the `Done` and `Close` buttons. Rows are clickable. Every change is written to
`~/.gobby/client/prefs.toml` at once; there is nothing to apply.

| Row | Default | Effect |
| --- | --- | --- |
| Appearance | `dark` | `dark`, `light`, or `system`; system follows OS appearance while the client is open (dark if the OS does not specify one); saved as `theme`; also changed by **View › Appearance** |
| Theme | Restored | The named theme drawn in that appearance, stepping through the themes it offers (see **Themes**); saved as `palette` (`classic`, from earlier versions, loads as Restored); also changed by **View › Theme** |
| Monochrome | off | Draw the client's chrome in grays; states keep their glyphs; also changed by **View › Monochrome** |
| Mouse capture | on | Off leaves selection and scrolling to your terminal emulator |
| Pane scrollbars | on | Draw a scrollbar lane beside scrolled panes; its thumb is dim at rest and brightens while the pane is focused or for a second after it scrolls |
| Pane gaps | on | Leave a gap between split panes |
| Confirm close | on | Ask before closing a pane, terminal, tab, or project |
| Hide tab bar with one tab | off | Hide the tab bar when a project has a single tab |
| Sidebar width | 26 | Columns, up to 540 of the pixels the terminal reports for its window; that cap is never under 36 columns, and is 36 when the terminal reports no pixels. Also set by dragging the sidebar edge, which saves the width on release |
| Sidebar side | `left` | Put the overlay or pinned sidebar on the left or right |
| Sidebar pinned | on | Keep the sidebar in its own column; also changed by **View › Sidebar › Pin sidebar** |
| Right-click passthrough | none | Modifier that sends a right-click to the pane's application instead of opening the pane menu (`shift`, `alt`, `ctrl`, or none) |
| Agent sort | `grouped` | `grouped` or `priority` order in the Agents section |
| Title scrolling | `left` | `off`, `left`, or `right`: which way over-long session titles and pane headers scroll, on one shared ticker |

The file is optional and every key in it is optional; an unknown key is a
startup error that names the line. The retired keys `pane_borders` and
`sidebar_collapsed` still load and are dropped on the next save. The sidebar
writes two keys of its own as you use it: `project_order` (project ids in the
order you dragged them; projects it does not name follow in the daemon's
order) and the `[ui.project_labels]` table (your label per project id).

```toml
[ui]
theme = "dark"
palette = "restored"
mouse_capture = true
pane_scrollbars = true
pane_gaps = true
confirm_close = true
hide_tab_bar_when_single_tab = false
sidebar_width = 26
title_scrolling = "left"
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
`Mouse capture` setting to use your terminal emulator's own selection.

| Gesture | Effect |
| --- | --- |
| Left-click a free pane | Focus it and take control on release; an agent-held pane focuses and shows the takeover indicator |
| `alt+click` a pane | Focus it without taking control (observe) |
| Left-drag in a pane | Select and copy text without taking control (see copy mode) |
| Double-click / triple-click | Select a word / a line |
| `ctrl+click` a link | Open the URL with `open` on macOS, `xdg-open` elsewhere |
| Wheel over a pane | Scroll its scrollback; on an alternate screen the wheel sends arrow keys instead |
| Wheel over the tab bar | Switch tabs |
| Wheel over the sidebar | Scroll the section under the pointer |
| Wheel over the keybinding help, the alert log, the navigator, or the Open worktree or Destroy orphaned terminals list | Do what its arrow keys do: the help and the log scroll three rows a notch, the navigator and the two lists move their selection one row; a notch outside the popup does nothing |
| Click a tab, the new-tab button, or an edge count | Switch tabs, open one, or page the bar one screen |
| Drag a tab onto another tab | Reorder tabs, including when the terminal delivers only press and release events |
| Click a project card / worktree row | Focus the project (expanding its card) / open the worktree |
| Drag a project card | Reorder projects |
| Click a `▸`/`▾` fold mark | Fold or unfold the card's worktrees |
| Click a session or bare terminal row | Focus its pane (a needs-you row's question is already on screen) |
| Click the control indicator | Take, release, or take back control |
| Drag the sidebar edge or a split border | Resize |
| Click or drag a scrollbar | Jump or scroll |
| Click a dialog or overlay button | Act as the key its hint names (`↵`, `esc`, `^c`, ...) |
| Right-click | Context menu for the target (see below) |

When a pane's application tracks the mouse (a TUI, `vim`, `less` with mouse on),
clicks, drags, and the wheel are forwarded to it as mouse reports. Hold `shift`
to keep a gesture for the client instead.

**Menus.** Right-click opens a context menu; clicking a title on row 0 opens
that menu. Use `j` / `k` or the arrows to move, `enter` or `space` to activate,
`→` / `l` to open a `▸` submenu, `←` / `h` to return from one, and `esc` to
close.

| Target | Items |
| --- | --- |
| Pane | Rename pane, Clear pane name, Swap with focused pane, Split right, Split down, Zoom / Unzoom, Arrange ▸ (for the pane's tab), Take / Release control, Respond (when it needs you), Copy mode, Send right-clicks to pane / Use gclient menu, Close pane |
| Tab | New tab, Rename tab, Arrange ▸ (for that tab), Close tab |
| Project card | Rename, Close, New worktree, Open worktree…, Collapse / Expand |
| Worktree row | Rename, Close, Delete worktree checkout… |
| Agent or bare terminal row | Focus, Open in new tab, Respond (when it needs you), Mark seen, Take / Release control, Close terminal / Destroy orphaned terminal (when orphaned) |
| Empty tab bar or empty sidebar | New terminal, New tab, New project…, Open project…, Settings, Keybinding help, Reload config, Toggle sidebar, Destroy orphaned terminals…, Detach, Quit |
| **Gobby** on the menu bar (click) | Settings, Reload config, Quit |
| **File** on the menu bar (click) | New terminal, New tab, New project…, Open project…, Rename tab, Close tab, Destroy orphaned terminals…, Detach |
| **Edit** on the menu bar (click) | Copy mode, Rename pane, Rename tab, Rename terminal, Clear pane name, Send right-clicks to pane / Use gclient menu |
| **View** on the menu bar (click) | Appearance ▸, Theme ▸, Monochrome, Sidebar ▸ (Show sidebar, Pin sidebar, Machines ▸, Projects ▸, Agents ▸, Terminals ▸) |
| **Window** on the menu bar (click) | Split right, Split down, Zoom / Unzoom, Close pane, Resize mode, Arrange ▸ (five layouts, New grid…) |
| **Agent** on the menu bar (click) | Respond, Mark seen, Take / Release control, Take back, Detach, Open alert target, Next / Previous attention |
| **Help** on the menu bar (click) | Keybinds, Alerts…, Daemon, About Gobby |

`Send right-clicks to pane` flips a per-pane flag so the pane's application gets
right-clicks; the `Right-click passthrough` setting does the same for every pane
while its modifier is held. `Close terminal` acts on the row you right-clicked
rather than on the focused pane, and on an external terminal (a tmux pane
`gclient` never created) it releases the lease and drops the pane instead of
killing it. `Close` on a project card kills every terminal in the
project's tabs (after a confirm-close dialog) but leaves the project registered.
`Delete worktree checkout…` kills the worktree's terminals and removes the
checkout through the daemon. `Destroy orphaned terminals…` opens the dialog
described under *Orphaned terminals*.

## Workspace persistence

The populated layout is the daemon's, not the client's. Tabs, splits, names, and the focus
hints live in the workspace rows on the daemon, and the gterm host keeps the
terminals themselves, so the same workspace comes back in the next window and
after a daemon restart. An empty New Tab draft exists only in its current window
until a terminal is opened in it. The client writes nothing about layout.

| Where | What it holds |
| --- | --- |
| Workspace rows on the daemon | Tabs, split layout, tab and pane names, the project's default workspace (distinct from the focused project), and the focus hints (focused project, tab, and pane) |
| `~/.gobby/client/prefs.toml` | Settings, plus `project_order` and `[ui.project_labels]`, which the sidebar writes as you change them |
| The window's own memory | Zoom, whether the sidebar is pinned, scrollback position, copy mode, and the machine, projects, and sessions filters |

On launch the client attaches the workspace — the project's own workspace when
the launch names a registered project and no `--workspace` was given — and opens
the focused project without starting a shell. A pane whose terminal died is dropped
from its split by the daemon's restart sweep, and a tab with no surviving panes
is dropped with it; a project that lost every tab remains empty until you open
another terminal. There is no snapshot file to move aside.

## Daemon restarts and reconnects

The gterm host that runs your terminals outlives the daemon: `gobby stop`,
`gobby start`, and `gobby restart` leave every terminal running by default, and
the restarted daemon adopts the surviving host. `gobby stop --terminals` and
`gobby restart --terminals` explicitly drain it.

The client rides through the gap. When the daemon's connection drops:

1. A direct native pane keeps receiving frames from its gterm host. If you
   already hold its host input grant, you can keep typing while the daemon is
   unreachable; host refusal still ends that authority. Taking control of an
   ungranted pane waits for the daemon to return. Panes that use the daemon's
   proxy keep their last visible frame frozen.
   A toast names the URL and cause (`Daemon unreachable at <url>: <error>`),
   while the status bar's left slot shows
   `× Daemon unreachable · retrying in <n> s` and counts down to the next attempt.
2. The client retries with backoff until the daemon answers, however the
   connection was lost: a deliberate stop or restart shows as *going away*, any
   other loss as *unavailable*. It never gives up on its own; `prefix+shift+q`
   quits a client whose daemon is not coming back.
3. On reconnect it re-attaches the workspace and takes the daemon's fresh
   snapshot of its rows: a pane whose terminal was killed during the restart is
   gone, and so is a tab that lost every pane.
4. It then re-attaches every surviving pane to the same terminal ids, retakes
   control of the focused pane, refreshes the sidebar, and resumes live frames.
   The retry segment clears itself after the connection succeeds; the event
   toast expires normally or clears at the next keypress.

Panes, tabs, and held control therefore survive `gobby restart`. On a pane that
types through the daemon, a write whose outcome the daemon never confirmed leaves
it `· Uncertain` until you take control again; a direct native pane has no such
write to lose, and retaking control re-grants its input on the host. If the host
itself was drained or replaced, the affected terminals are gone and their panes
disappear on the next roster refresh.

On launch, the goblin and the wordmark stand alone on the selected ground
while four stages complete: **daemon health**, **workspace attach**,
**roster**, and **first frame**. The splash has no menu bar, tabs, sidebar, or
status line; they arrive with the first frame, and `prefix+shift+q` quits
meanwhile. **Help › Daemon** then reports health and stage details. Each
completed stage's timing and the final summary are logged to
`~/.gobby/logs/gclient.log`.

With the daemon stopped, the first failed health check ends the splash: the
chrome opens and the status bar shows `× Daemon unreachable · retrying in <n> s`.
Once the daemon answers, the remaining stages run, the status segment clears, and the
workspace opens. A malformed daemon URL, refused token, unusable gterm host,
or broken prefs file still reports its own error.

A hangup signal does not end the client either: a closed terminal still ends it
through its input, and `prefix+shift+q` remains the way to quit.

### Orphaned terminals

Two kinds of terminal row outlive their usefulness, and the client can destroy
both from one place:

- A native terminal whose host epoch is gone (the daemon marks the row
  `orphaned`). Its session row carries the `◌` glyph in the sidebar (the glyph
  alone marks it; no word is printed), and its context menu offers
  `Destroy orphaned terminal` in place of `Close terminal`.
- A tmux session on the default or gobby socket with no attached client, for
  example one you started by hand and detached from. Gobby-owned agent sessions
  are always detached and are never listed.

Choose **File › Destroy orphaned terminals…** or the same item on the empty
chrome context menu. The client fetches the current candidates from the daemon
and opens a checklist with every row checked. Each row shows the session name or
title, the backend, the Gobby session that still owns it (or `no session`), and
the time it was last seen. `j` / `k` or the arrows move, `space` toggles a row,
`a` checks or clears all, `enter` destroys the checked rows, and `esc` cancels.
A toast then reports `destroyed N of M orphaned terminals`, naming any row
the daemon refused; with nothing to clean up it says `No orphaned terminals.`

A Ghostty tab is a plain shell and never appears here; see
[Bringing a bare terminal in](#bringing-a-bare-terminal-in) for where
daemon-tracked work starts.

_Last verified: 2026-09-18_

# Workspaces and panes

Load when arranging workspaces, tabs, and panes, or when typing into or watching a
pane's terminal. Fetch the `gobby-workspaces` schemas you need. For cross-session
messages, use `gobby-agents:send_message` instead.

A node is one of the operator's machines. It holds workspaces whose tabs hold
panes, and every ref is a number counting from zero. Address rows by ref, such as
`0:1:0:2`, or by id. `node` resolves a node by its ref, hostname, or label; changing another node's rows is
refused `invalid_op` because that node's daemon owns them. Start from `list_nodes`,
`list_workspaces`, and `get_workspace`, which returns every tab and pane.

`create_workspace` returns the named workspace, creating it on first use.
`create_tab` and `split_pane` open a shell in the project or `worktree_id`, or
adopt a live `terminal_id`. A split lands second, to the right on `horizontal` and
below on `vertical`; `swap_panes` moves it into first place. `move_pane`,
`swap_panes`, and `rename_workspace_item` rearrange; `rename_workspace_item`
without `name` clears a tab title or pane label. `close_pane`, `close_tab`, and
`close_workspace` kill the live terminals they own. Changes publish to the
workspace's WebSocket subscribers, so an attached client redraws.

`rebalance_tab(tab, rows, columns=80)` preserves the tab's pane and terminal IDs and
rebuilds its layout into evenly sized rows and columns. Pass the current tab's
viewport width and height in cells. When the viewport fits them, panes keep at
least 80 columns and 12 rows, plus divider cells; the default width uses one
column. A tab too crowded for that floor is tiled across the whole viewport, as
gclient's Arrange > Tiled does: ceil(sqrt N) even rows. No pane is restarted or
killed. A viewport under 80x12 or too small for even the tiled grid, missing
height, or an in-flight spawn refuses the operation. Hidden PTY dimensions can
be stale; supply known viewport dimensions. Crew-lane uses this same layout
through spawn placement's
`axis: balanced`, `columns` and required `rows` budgets. Its guard reads them
from the gclient window showing the lane's workspace: the focused tab's live
pane sizes plus divider cells, since every tab shares the window. Without one
live size per pane it refuses before launching; pass `lane_columns` and
`lane_rows` to override.
The operator CLI is
`gobby workspaces rebalance-tab TAB --columns WIDTH --rows HEIGHT --json`.

Pane input follows `send_keys` policy: `send_text` types, `send_pane_keys` presses,
a trailing newline submits, and nonliteral input names keys such as `enter` or
`c-c`. An `indeterminate` write may have landed: read the pane before retrying,
and retry with the same `idempotency_key`.
`read_pane` returns the screen's last `lines`. `wait_for_pane_output` polls for a
regex and reports `matched`, `timeout`, or `pane_lost`; wait once instead of looping
reads. Its timeout is capped at 300 seconds.

Calls act as your session, or as the operator for the local CLI token; no argument
chooses the actor. An agent API token is refused `forbidden` on every tool, whatever
session it names. Autonomous agent sessions and terminals outside the actor's project
and agent tree are refused `forbidden` for pane input, spawns, adopts, and closes.
Failures return `success: false` with a `code`: `not_found`, `invalid_ref`,
`invalid_op`, `terminal_failed`, `busy`, or `forbidden`.

`gobby workspaces list|show|create|delete`, `gobby panes
split|close|move|swap|rename|send|read|wait`, and `gobby nodes list` are the
operator's CLI for the same tools, each with `--node` and `--json`. `panes split`
takes exactly one of `--right`, `--left`, `--above`, `--below`; `panes move` takes
`--tab` and one direction naming the pane to sit beside.

Guide: [Internal registries](../../../../../../../../docs/guides/mcp-tools.md#internal-registries).

_Last verified: 2026-09-17_

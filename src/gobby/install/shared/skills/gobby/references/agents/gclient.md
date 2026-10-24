# Place agent panes in gclient workspaces

Load before placing a spawned agent's terminal, reading workspace refs, or typing
into another session's terminal.

## Workspaces, tabs and panes

A workspace is the daemon's record of one layout: its tabs, the panes in each tab,
and the terminal each pane shows. Every `gclient` window attached to a workspace
renders the same rows, so a placement shows up for everyone working in it.

Refs are digits, zero-based, read from the left: `1` is workspace 1 on this node,
`0:1` is workspace 1 on node 0, `0:1:2` is tab 2 of it, and `0:1:2:1` is pane 1
of that tab. A closed row's number is reused by the next one, so read refs just
before you use them. Every row also has a UUID that never changes.

Read refs with `gobby-workspaces:get_workspace(workspace=...)`, which returns the
workspace's tabs with their titles and panes, or `list_workspaces` for a node's
workspaces. Never derive a ref from a session's terminal ID or from another
seat's output.

## Place a spawned agent

`spawn_agent(placement=...)` binds the agent's terminal into a workspace before
the agent starts. Use one of three forms:

- New tab: `{"tab": {"workspace": REF, "title": TEXT}}`. `REF` is a workspace
  ref, UUID or name.
- Split beside a pane: `{"split": {"pane": REF, "axis": AXIS, "title": TEXT}}`.
- Split beside a named tab's focused pane:
  `{"split": {"workspace": REF, "tab_title": TEXT, "axis": AXIS, "title": TEXT}}`.
  Use this form when the project names its tabs; it needs no pane ref. It
  refuses with `invalid_ref` unless exactly one tab in that workspace has the
  title. `pane` and `tab_title` are mutually exclusive.

`AXIS` is `right`, `down` or `balanced`. `balanced` needs the viewport's `rows`
(and accepts `columns`, default 80). It keeps every pane at least 80 columns by
12 rows when they fit, else tiles the tab evenly; a viewport too small for
either refuses. `title` names the agent's tab or pane.

A refusal carries `placement_error` and changes nothing: `invalid_placement` for
a malformed shape, `invalid_ref` or `not_found` for a ref that names nothing
usable, `forbidden` for another project's pane or a caller out of scope, and
`busy` while another spawn into that pane is still in flight. Fix the input
and spawn again.

## Keep operator tabs clear

Operator and admin tabs belong to the people and coordinators working in them.
Never split an agent pane into one of them, and never move, close or rename a
pane you did not place. Place agents in the tabs the project assigns to them, or
in a new tab.

## Read the layout from project policy

Tab titles and which seats go in which tab are project choices. Read them from
project policy (`get_variable(name="project_policy")`) before placing a pane.
When the policy names no layout, open a new tab titled for the agent's role.

## Terminal control limits

- Message other sessions with `gobby-agents:send_message`. Keystrokes are not a
  messaging transport.
- `gobby-sessions:send_keys` types into a session's terminal. It reaches only
  the caller, sessions in the same project, or the caller's agent tree;
  autonomous agent runs and definitions that select `no-send-keys` cannot use
  it. Use it to resume a stalled session, such as answering a waiting prompt;
  never to steer another seat's work or to enter credentials.
- `gobby-sessions:capture_output` takes one diagnostic snapshot of a session's
  last terminal lines, for state hooks do not report, such as a permission
  dialog. It is not a monitor: wait for a terminal pattern with
  `gobby-agents:wait_for_output`, and for a run's end with
  `gobby-agents:wait_for_agent`.

Guide: [gclient workspaces](../../../../../../../../docs/guides/gclient-user-guide.md#workspaces).

_Last verified: 2026-10-23_

# gclient command mode

Load when an agent needs to inspect or operate daemon-owned gclient workspaces
from a shell. `gclient help` lists command verbs; `gclient --help` describes the
interactive terminal client. This reference does not authorize terminal writes
or closes. Prefer `capture-pane` for read-only pane inspection.

- `gclient list [--workspace REF]` lists a workspace's tabs and panes.
- `gclient new-tab --project NAME|ID [--workspace REF] [--name TEXT]` creates
  a tab for the project.
- `gclient split [REF] --right|--down [--cmd TEXT]` splits a pane; `--cmd`
  runs text in the new pane.
- `gclient resize [REF] RATIO` sets a split ratio greater than 0 and less
  than 1.
- `gclient title [REF] TEXT [--kind tab|pane]` renames a tab or pane.
- `gclient select [REF] [--workspace REF] [--tab-ref TAB]` stores the focus and
  switches every running gclient window on the workspace to that tab and pane;
  a tab REF keeps the tab's own focused pane.
- `gclient send-keys [REF] TEXT [--enter]` sends pane text, optionally
  submitting Enter. With `--enter` the daemon answers only after it reads the
  composer back, which can take tens of seconds: exit 1 with
  `command_not_submitted` means the CLI kept the text, and a reply with
  `indeterminate: true` means nobody could verify the submit.
- `gclient capture-pane [REF] [--lines N]` reads pane text; `N` must be
  positive.
- `gclient wait-for-output [REF] --pattern REGEX [--timeout S] [--interval S]`
  waits for matching pane output; default timeout is 30 seconds.
- `gclient kill [REF] [--kind tab|pane]` closes a tab or pane.
- `gclient help` prints the command-mode table without contacting the daemon.

`--json`, `--daemon-url URL`, and `--token-file PATH` apply to action verbs.
`--workspace REF` applies to every action verb. `list` and `new-tab` use
`GOBBY_WORKSPACE_ID` when the option is omitted, and otherwise require it
explicitly. For verbs targeting a pane or tab, it scopes short IDs and rejects
a full ref outside the selected workspace. `select` derives the workspace from
a full numeric tab or pane ref; a shorter or UUID ref needs `--workspace` or
`GOBBY_WORKSPACE_ID`. Discover the current workspace ref from daemon workspace
listings rather than assuming a project name or fixed number.

Omitted pane `REF` uses `GOBBY_PANE_REF`, which a Gobby pane shell inherits.
Outside a pane, pass an explicit ref. Numeric refs have the form
`hub:workspace:tab:pane`; a three-part ref denotes a tab for `title` and `kill`.
For a UUID tab ref with those verbs, pass `--kind tab`. To focus a UUID pane with
`select`, supply `--tab-ref TAB` or run inside a pane with `GOBBY_TAB_ID`.

Without `--json`, `list` prints tab and pane identifiers, `new-tab` and `split`
print created refs or IDs, and `capture-pane` prints captured text. Other action
verbs print the daemon result as JSON. `--json` prints the daemon result JSON
for every action verb. Exit status 0 means success (including a matched wait),
1 means daemon refusal or wait timeout, 2 means invalid usage or a wait whose
pane was lost, and 3 means token, connection, or invalid daemon-reply failure.
Read the error text for the specific reason.

For interactive startup, workspace navigation, and pane control, see the
[gclient user guide](../../../../../../../../docs/guides/gclient-user-guide.md).

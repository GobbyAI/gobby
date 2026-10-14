# Node commands

Version 1 of the hub-node command contract. The golden messages live in
`crates/gdaemon/tests/fixtures/node_commands/corpus.json`. The Rust types in
`crates/gdaemon/src/node_commands.rs` and the Python models in
`src/gobby/hub/node_commands.py` parse every message and re-encode it to the
same bytes. Payloads, results, and node bodies reject unknown fields.

A new command or field bumps `v`. A node answers an unknown `v` with
`unsupported_version`. The hub answers a node body with an unknown `v` the
same way. The models accept any `v` of 1 or greater so that refusal can be
produced by the handler rather than by the parser.

## Frames

Hub to node:

```json
{"type":"command","v":1,"request_id":"<uuid>","command":"<name>","payload":{}}
```

Node to hub, success:

```json
{"type":"reply","v":1,"request_id":"<uuid>","ok":true,"result":{}}
```

Node to hub, or gdaemon when it must answer for the node:

```json
{"type":"reply","v":1,"request_id":"<uuid>","ok":false,"error":{"code":"<code>","message":"<text>","stage":"none"}}
```

`stage` is `"none"`, `"partial"`, or `null`. Every error carries it. A node
sets `"none"` when the command changed nothing and `"partial"` when it may
have. gdaemon sets `"none"` on `node_offline`, because it writes nothing
without a live channel, and `null` on `node_timeout` and `channel_closed`,
because the frame may have reached the node. Only `"none"` proves a command
had no effect.

## Error codes

gdaemon produces these. A node never sends them.

| Code | Stage |
| --- | --- |
| `node_offline` | `"none"` |
| `node_timeout` | `null` |
| `channel_closed` | `null` |

A node produces these.

| Code | Stage |
| --- | --- |
| `unsupported_version` | `"none"` when the node rejects the frame before an effect |
| `unknown_command` | `"none"` |
| `invalid_payload` | `"none"` when validation fails before an effect |
| `not_found` | `"none"` |
| `cancelled` | `"none"` |
| `command_failed` | `"none"` or `"partial"` |

`not_found` for `agent.cancel` also proves that nothing runs for that
`run_id` now or later. `{stopped:false}` means the agent may still run.

### Hub-client codes

These do not cross the wire. Section 1.3's Python client raises them.

| Code | Stage |
| --- | --- |
| `node_routing_unavailable` | `"none"` |
| `node_reply_lost` | `null` |

### Machine routing

The hub refuses these before any command, each with `stage` `"none"`.
Routing also reuses `node_offline`.

| Code |
| --- |
| `checkout_unresolved` |
| `machine_not_found` |
| `machine_not_owned` |

### Hub-flow result codes

These stay with the flows that produce them. They are not node-command
frames. `machine_cap_reached` is section 2.5's per-machine
`max_active_agents` ceiling.

| Code |
| --- |
| `not_stopped` |
| `spawn_in_doubt` |
| `remote_resume_unsupported` |
| `machine_cap_reached` |

### Hub route codes

Section 1.4's routes answer these. `stale_report` uses the body below.

| Code | HTTP |
| --- | --- |
| `node_identity_required` | 403 |
| `machine_is_local` | 403 |
| `unsupported_version` | 422 |
| `invalid_body` | 422 |
| `project_not_found` | 404 |
| `project_deleted` | 409 |
| `checkout_conflict` | 409 |
| `checkout_root_taken` | 409 |
| `checkout_rejected` | 422 |
| `stale_report` | 409 |

## v1 commands

### `agent.spawn`

Payload, in order:

| Field | JSON |
| --- | --- |
| `run_id`, `session_id`, `project_id`, `terminal_id`, `provider`, `prompt`, `source_root` | string |
| `parent_session_id`, `workflow_name`, `model`, `reasoning_effort`, `title` | string or null |
| `agent_depth`, `max_agent_depth`, `timeout_seconds` | number |
| `provider_args` | array of strings |
| `sandbox` | `{enabled, network, extra_write_paths}` |
| `workspace` | `{mode, branch_name, base_branch}` |

`sandbox.network` is `"none"` or `"trusted"`. `sandbox.extra_write_paths` is
an array of strings. `workspace.mode` is `"none"`, `"worktree"`, or
`"clone"`. With `"none"` the agent runs in `source_root`, and `branch_name`
and `base_branch` are null. With `"worktree"` or `"clone"`, `branch_name` is
a string, and a null `base_branch` means `source_root`'s current branch.

The payload has no `env` and no `cwd`. The node sets the seven identity
variables from the typed fields: `GOBBY_SESSION_ID`, `GOBBY_AGENT_RUN_ID`,
`GOBBY_PROJECT_ID`, `GOBBY_AGENT_DEPTH`, `GOBBY_MAX_AGENT_DEPTH`,
`GOBBY_PARENT_SESSION_ID`, `GOBBY_WORKFLOW_NAME`. It sets `GOBBY_DAEMON_URL`
to its own front door and `GOBBY_PROMPT_FILE` to a node-local file holding
`prompt`.

Result: `{"accepted":true}`.

### `agent.cancel`

Payload: `{run_id, terminal_id, signal, close_terminal}`. `signal` is
`"TERM"` or `"KILL"`. `close_terminal` is a boolean.

Result: `{"stopped":true}` or `{"stopped":false}`.

### `terminal.write`

Payload: `{terminal_id, kind, data, submit}`. `kind` is `"text"`, `"key"`,
`"input"`, or `"paste"`. `data` is a string. `submit` is a boolean.

Result: `{}`. A failure after the write may have reached the pane uses
`command_failed` with `stage` `"partial"`.

## Node bodies

Both go to the hub's front door with the node's API key. Neither carries a
machine id. A `machine_id` field fails validation.

### `POST /api/nodes/checkouts`

Request: `{v, project_id, root_path}`.

Result: `{root_path, created}`. `created` is a boolean.

### `POST /api/nodes/report`

Request:

| Field | JSON |
| --- | --- |
| `v` | number, 1 or greater |
| `hub_epoch` | number or null |
| `revision` | number, 1 or greater |
| `host_epoch` | number |
| `accepted` | array of `run_id` strings, at most 1,000 |
| `agents` | array of agent objects, at most 1,000 |
| `exits` | array of exit objects, at most 1,000 |

An agent object is `{run_id, terminal_id, pid, locator, locator_key, workspace}`.
An exit object is `{run_id, terminal_id, launched, exit_code, reason, workspace}`.
`exit_code` is a number or null. `reason` is `prepare_failed`,
`prepare_timeout`, `launch_failed`, `cancelled`, or `exited`.

`workspace` is `{mode, path, branch_name, base_branch}` or null. On a report,
`mode` is `"worktree"` or `"clone"`. `base_branch` is a string or null.

`hub_epoch` is the epoch the node last learned from a `stale_report`
refusal, or null. `revision` is a positive integer the node keeps in durable
state. `host_epoch` names the terminal host's current incarnation.

Result: `{}`.

A list of 1,001 entries fails validation. That bound is what one request can
make the hub hold.

### `stale_report` refusal

Body: `{code, hub_epoch}` with `code` equal to `"stale_report"` and
`hub_epoch` the hub's current epoch. The node parses this body, adopts the
epoch, and sends a fresh report.

## Node duties

`agent.spawn`:

- The node validates the payload, records the run as accepted, and replies
  `{accepted:true}` before it prepares anything. It answers `stage` `"none"`
  only when it recorded nothing.
- While its accepted and live runs already total 1,000, the node answers
  `command_failed` with `stage` `"none"` and records nothing. The hub's
  ceiling normally refuses first. This rule covers agents whose session end
  reached the hub before their process exited.
- It then prepares the workspace from `source_root`. A worktree or clone path
  and branch embed `run_id`, so no other machine's row names the same path.
  It launches the provider there, builds the provider command, applies its
  SRT chokepoint, enforces `timeout_seconds`, and closes the pane when the
  agent process exits.
- A failure before launch removes what it prepared and reports an exit with
  `launched` false. Preparation has a node-side deadline. Passing it is
  `prepare_timeout`.
- Before its first filesystem effect, the node writes a preparation record
  for the run, naming the workspace path and branch it will create, to the
  same node-local durable state as its cancel records. Removal is idempotent:
  a path and branch already gone count as removed. A failed removal, or a
  node daemon restart during preparation or removal, leaves the record. The
  node retries the removal at startup and on each report interval until the
  path is gone, and only then deletes the record. Reporting the
  `launched:false` exit does not clear the record. A cancel record for the
  same run leaves it in place.
- At launch the record becomes the launched workspace's record. The node
  keeps it until a report carrying the run's exit gets 200, so every report
  until then can name the workspace.

Commands that name the same `run_id` run one at a time in arrival order.

`agent.cancel`:

- The node records the `run_id` as cancelled in node-local durable state
  before it replies, so the record survives a node daemon restart. A later
  `agent.spawn` for it answers `cancelled` with `stage` `"none"`.
- For an accepted run that has not launched, it stops preparation, removes
  what it prepared under the preparation record's retry rule, answers
  `{stopped:true}`, and reports an exit with `launched` false and reason
  `cancelled`.
- A cancel for a run with nothing accepted or live answers `not_found`.

Checkouts: the node registers each checkout it serves before the hub can
route a spawn of that project to it.

Reports:

- The node posts a report every 30 seconds and immediately after any change.
  `accepted` and `agents` are complete in every report.
- The node sends one report at a time. It builds each one fresh from its
  current state with the next `revision`. A node daemon restart never repeats
  a revision. It never resends a report's bytes. A retry is a new report.
- A `stale_report` refusal carries the hub's current `hub_epoch`. The node
  adopts it and sends a fresh report.
- `accepted` lists runs accepted and not yet launched or exited. A run leaves
  `accepted` only by moving to `agents` or `exits`.
- `agents` lists the live agents the node launched, read from its terminal
  host, so agents that outlive a node daemon restart stay listed. A workspace
  appears only after its agent launched.
- `exits` lists each run's end, oldest first and at most 1,000 per report.
  Each repeats in every report until a report carrying it gets 200. A
  `launched:true` exit names the run's workspace. A `launched:false` exit
  names none.

The node-duty test cases owned by #24148 are out-of-order and retried
reports, a `stale_report` refusal, more than 1,000 pending exits, a spawn at
1,000 accepted and live runs, a failed removal, and a node daemon restart at
each preparation and removal boundary.

## Internal route

gdaemon serves this to the hub in D2. It is not a node body.

`GET /api/internal/nodes/connected` returns `{"machine_ids":[...]}`, the
machines with a live channel.

`POST /api/internal/nodes/{machine_id}/commands` takes
`{"command","payload","timeout_ms"}`. `timeout_ms` defaults to 30000 and is
at most 600000. gdaemon mints `request_id`, sends the command frame, and
answers 200 with the reply frame, or with a gdaemon error frame for
`node_offline`, `node_timeout`, or `channel_closed`.

gdaemon admits a request only from a loopback peer whose
`X-Gobby-Backend-Secret` equals `GOBBY_FRONT_DOOR_SECRET`, compared in
constant time. Every other request, and every request to a standalone or node
front door, gets 404. gdaemon answers 404 only before it sends any frame.

A pending command ends on its reply, its timeout, its channel's close or
replacement, or its HTTP caller going away. A reply that arrives after that
is dropped and logged.

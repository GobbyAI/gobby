# Node commands

Version 2 of the hub-node command contract. The golden messages live in
`crates/gdaemon/tests/fixtures/node_commands/corpus.json`. The Rust types in
`crates/gdaemon/src/node_commands.rs` and the Python models in
`src/gobby/hub/node_commands.py` parse every message and re-encode it to the
same bytes. Payloads, results, and node bodies reject unknown fields.

Both sides define `CONTRACT_VERSION` as 2: a Rust `pub const` and a Python
module constant. Every frame and every node body carries it as `v`; answers
carry no `v`. A new command or field bumps `v`. Every handler and route
refuses a `v` other than `CONTRACT_VERSION` with `unsupported_version`: a node
answers a frame that way, and the hub answers a node body that way. The
models accept any `v` of 1 or greater so that the refusal can name its
request rather than fail in the parser.

## Frames

Hub to node:

```json
{"type":"command","v":2,"request_id":"<uuid>","command":"<name>","payload":{}}
```

Node to hub, success:

```json
{"type":"reply","v":2,"request_id":"<uuid>","ok":true,"result":{}}
```

Node to hub, or gdaemon when it must answer for the node:

```json
{"type":"reply","v":2,"request_id":"<uuid>","ok":false,"error":{"code":"<code>","message":"<text>","stage":"none"}}
```

A command frame's fields are `type` (`"command"`), `v`, `request_id`,
`command`, and `payload`. A reply's are `type` (`"reply"`), `v`,
`request_id`, `ok`, and then `result` when `ok` is true or `error` when it is
false. An `error` is `{code, message, stage}`: `code` and `message` are
strings.

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
`max_active_agents` ceiling. `remote_loopback_endpoint` refuses, with
`stage` `"none"` and before any row, a remote spawn whose resolved generation
endpoint is loopback: a loopback `api_base` names a different server on each
machine. A non-loopback endpoint reaches the node unchanged.

| Code |
| --- |
| `not_stopped` |
| `spawn_in_doubt` |
| `remote_loopback_endpoint` |
| `machine_cap_reached` |

### Hub route codes

Section 1.4's routes and the run routes below answer these. `stale_report`
uses the body below. A run route answers `run_not_found` when no run has the
body's `run_id` or the run belongs to another machine.

| Code | HTTP |
| --- | --- |
| `node_identity_required` | 403 |
| `machine_is_local` | 403 |
| `unsupported_version` | 422 |
| `invalid_body` | 422 |
| `project_not_found` | 404 |
| `run_not_found` | 404 |
| `project_deleted` | 409 |
| `checkout_conflict` | 409 |
| `checkout_root_taken` | 409 |
| `checkout_rejected` | 422 |
| `stale_report` | 409 |
| `report_too_large` | 413 |
| `report_backlog_full` | 429 |

## Commands

### `agent.spawn`

Payload, in order:

| Field | JSON |
| --- | --- |
| `run_id`, `session_id` | string |
| `parent_session_id` | string or null |
| `project_id`, `terminal_id` | string |
| `agent_depth`, `max_agent_depth` | number |
| `workflow_name` | string or null |
| `provider` | string |
| `model` | string or null |
| `prompt` | string |
| `provider_args` | array of strings |
| `reasoning_effort` | string or null |
| `sandbox` | sandbox object |
| `timeout_seconds` | number |
| `title` | string or null |
| `source_root` | string |
| `workspace` | `{mode, path, branch_name, base_branch}` |
| `agent_name`, `api_base`, `api_token`, `codex_oss_provider`, `resume_session_id`, `execution_mode`, `code_index_preflight`, `task_id` | string or null |
| `extra_env` | object of strings |
| `codex_config_overrides` | array of strings |
| `droid_mode` | `"exec"` or `"interactive"` |
| `prewarm_pre_commit_store` | boolean |
| `monitor` | `{auto_enter_agent_terminals, auto_enter_agent_interval_seconds, auto_enter_approval_prompts, init_timeout_seconds}` |

`sandbox` is the hub's resolved `SandboxConfig`
(`src/gobby/agents/sandbox.py`), all thirteen fields in order: `enabled`
(boolean), `backend` (`"srt"` or `"provider-native"`), `mode`
(`"permissive"` or `"restrictive"`), `allow_network` (boolean),
`extra_read_paths`, `extra_write_paths`, `extra_deny_read_paths`,
`extra_deny_write_paths`, `allowed_domains` and `denied_domains` (arrays of
strings), `allow_git_network` and `allow_package_registries` (booleans), and
`allow_unix_sockets` (array of strings). Version 1's `sandbox.network` is
gone and fails validation.

`workspace.mode` is `"none"`, `"worktree"`, `"clone"`, or `"existing"`. With
`"none"` the agent runs in `source_root`, and `branch_name` and
`base_branch` are null. With `"worktree"` or `"clone"`, `branch_name` is a
string, and a null `base_branch` means `source_root`'s current branch.
`"existing"` names, by `path`, a workspace the node prepared for an earlier
run of the resumed session; the node runs there in place. `path` is required
with `"existing"` and null with every other mode; either mismatch fails
validation. With `"existing"`, `branch_name` is the workspace row's branch
and `base_branch` is null.

`resume_session_id` is the provider-native thread id, never a Gobby session
reference. `code_index_preflight` is the managed code-index preflight mode,
or null for none. `task_id` names the run's task, or null. `extra_env` holds
the spawn's extra environment variables.

`monitor` carries the hub's lifecycle-monitor settings for the run:
`auto_enter_agent_terminals` and `auto_enter_approval_prompts` are booleans,
and `auto_enter_agent_interval_seconds` and `init_timeout_seconds` are
numbers.

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

Result: `{"stopped":true}` or `{"stopped":false}`; `stopped` is a boolean.

### `terminal.write`

Payload: `{terminal_id, kind, data, submit}`. `kind` is `"text"`, `"key"`,
`"input"`, or `"paste"`. `data` is a string. `submit` is a boolean.

Result: `{}`. A failure after the write may have reached the pane uses
`command_failed` with `stage` `"partial"`.

## Node bodies

Each goes to the hub's front door with the node's API key. None carries a
machine id. A `machine_id` field fails validation.

### `POST /api/nodes/checkouts`

Request: `{v, project_id, root_path}`.

Result: `{root_path, created}`. `root_path` is a string and `created` a
boolean.

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

An agent object is
`{run_id, terminal_id, pid, locator, locator_key, workspace, pane, sandbox}`.
An exit object is
`{run_id, terminal_id, launched, exit_code, reason, workspace, sandbox}`.
`pid` is a number, and `locator` and `locator_key` are strings naming the
agent's pane on the terminal host.
`exit_code` is a number or null. `reason` is `prepare_failed`,
`prepare_timeout`, `launch_failed`, `cancelled`, or `exited`.

`workspace` is `{mode, path, branch_name, base_branch}` or null. On a report,
`mode` is `"worktree"` or `"clone"`. `base_branch` is a string or null.

#### Pane

An agent's `pane` is null before the node's first pass on the run, otherwise
`{attention, injections, composer, idle, tail}`:

- `attention` is `{reason, kind, fingerprint, payload, dismissed}` or null.
  `reason` and `kind` are strings and `dismissed` a boolean. `payload` is the
  detected prompt, `{kind, excerpt, options, fingerprint}`, where `options` is
  an array of `{option, label}` with `option` a number and `label` a string,
and `excerpt` is the prompt's text. Both `fingerprint`
  values are SHA-256 hex digests, as `src/gobby/agents/prompt_detector.py`
  produces them; the Python model refuses anything but 64 lowercase
  hexadecimal characters, so the route answers 422 `invalid_body` before it
  stores or logs anything.
- `injections` is a number, the run's automatic sends so far.
- `composer` is `{state, line}`. `state` is `"empty"`, `"draft"`, or
  `"unknown"`; a pane read never reads `"held"`. `line` is a string or null.
- `idle` is
  `{status, turn_in_flight, unsubmitted_input, queued_message_prompt, capacity_message, quota}`
  or null. `status` is `"unknown"`, `"context_full"`, `"idle"`, or
  `"active"`. `turn_in_flight`, `unsubmitted_input`, and
  `queued_message_prompt` are booleans. `capacity_message` is a string or
  null. `quota` is `{provider, reset_time}` or null, with `reset_time` a
  string or null.
- `tail` is the last 15-line read, at most 2,000 characters.

#### Sandbox record

An agent's or exit's `sandbox` is null when the run never reached the SRT
chokepoint, otherwise
`{runtime_version, policy_schema_version, policy_hash, violation_count, violation_count_truncated, denial_operations, recent_violations}`:

- `runtime_version` is the SRT version, which the Python model requires to
  be `<digits>.<digits>.<digits>`. `policy_schema_version` is a number.
  `policy_hash` is the policy's SHA-256 hex digest, 64 lowercase hexadecimal
  characters.
- `violation_count` is at most 10,000, with `violation_count_truncated` true
  past that, as `_count_violation_lines` counts
  (`src/gobby/storage/agents/_sandbox_records.py`).
- `denial_operations` maps an operation name to its count, at least 0. Each
  name is `unclassified` or matches `^[a-z][a-z0-9*-]*$` and comes back
  unchanged from `redact_terminal_output`, so a name shaped like a secret
  fails the parse.
- `recent_violations` is null in an agent entry. In an exit it is an array
  of at most the last 100 events, each a JSON object, as `_recent_violations`
  projects them with each `command` cut to 2,048 characters.

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

### Report budget refusals

`report_too_large` (413) means the request body exceeds the hub's
`node_reports.max_request_bytes`. The hub stops reading at that cap, before any
parse, and writes nothing. `report_backlog_full` (429) means the report would
push the machine's pending bytes past `node_reports.max_pending_bytes_per_machine`.
The hub writes nothing and does not move the revision mark. Every 429 carries
`Retry-After: 30`, the node's report interval, so the node retries the same
fresh report after one interval. Nothing accepted is dropped.

### `POST /api/nodes/runs/prompt-delivery-failure`

Request: `{v, run_id, code, detail, pane}`. `code` and `detail` are strings.
`pane` is the last non-empty pane snapshot text, or null. The node posts it
when prompt delivery fails and never signals the run itself.

Result: `{failed}`. `failed` is a boolean: whether the hub failed the run.

### `POST /api/nodes/runs/code-index-warning`

Request: `{v, run_id, warning, prompt}`. `warning` is
`{preflight, cwd, message}`: `preflight` is the payload's
`code_index_preflight` mode, `cwd` the workspace path, and `message` the
failure, all strings. `prompt` is the payload's
`prompt`, unchanged.

Result: `{prompt}`: that prompt with the sanitized warning appended. The node
launches with it.

### `POST /api/nodes/runs/terminate`

Request: `{v, run_id, cause, detail, pane_tail, checkpoint}`. `cause` is
`"doom_loop"`, `"provider_stall"`, `"timeout"`, or `"init_timeout"`. `detail`
and `pane_tail` are strings. `checkpoint` is
`{ref_name, commit_sha, parent_sha, files_changed, message}` or null:
`ref_name`, `commit_sha`, `parent_sha`, and `message` are strings and
`files_changed` a number.

Result: `{terminated, retry}`. `terminated` and `retry` are booleans. `retry` true asks the node to
post again on its next pass; false ends the asks for that cause and run.

### `GET /api/nodes/detection-manifests`

Result: `{manifests}`. `manifests` is an array of `{provider_id, content}`,
where `provider_id` and `content` are strings,
and an `ETag`. A request whose `If-None-Match` matches gets 304.

## Node duties

`agent.spawn`:

- The node validates the payload, records the run as accepted, and replies
  `{accepted:true}` before it prepares anything. It answers `stage` `"none"`
  only when it recorded nothing.
- While its accepted and live runs already total 1,000, the node answers
  `command_failed` with `stage` `"none"` and records nothing. The hub's
  ceiling normally refuses first. This rule covers agents whose session end
  reached the hub before their process exited.
- The node refuses a spawn whose `sandbox` is not `enabled` with `backend`
  `"srt"` with `invalid_payload`, stage `"none"`, and the message
  `spawn_agent requires managed SRT (enabled=<enabled>, backend=<backend>)`.
- It then prepares the workspace from `source_root`. A worktree or clone path
  and branch embed `run_id`, so no other machine's row names the same path.
  An `existing` workspace is used in place at `workspace.path`; the node never
  removes an `existing` workspace. With a `code_index_preflight` mode, a
  failed preflight posts its warning to
  `POST /api/nodes/runs/code-index-warning`, and the node launches with the
  prompt that route answers. It launches the provider there, builds the
  provider command, applies its SRT chokepoint, and closes the pane when the
  agent process exits. It removes the prompt file once the chokepoint has
  copied it.
- The node observes `timeout_seconds` and the run's pane states. It never
  kills a run on its own for a timeout or a stall: it asks the hub through
  `POST /api/nodes/runs/terminate`, and the hub decides.
- When a run ends, the node reaps the run's sandbox processes and sandbox
  roots.
- A failure before launch removes what it prepared and reports an exit with
  `launched` false. Preparation has a node-side deadline of 300 seconds from
  acceptance to the host's spawn commit. Passing it is `prepare_timeout`.
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
  appears only after its agent launched. Each agent entry carries the run's
  latest `pane` and its `sandbox` record's counts.
- `exits` lists each run's end, oldest first and at most 1,000 per report.
  Each repeats in every report until a report carrying it gets 200. A
  `launched:true` exit names the run's workspace. A `launched:false` exit
  names none. Each exit carries the run's `sandbox` record with its recent
  violations.

The node-duty test cases owned by #24148 are out-of-order and retried
reports, a `stale_report` refusal, more than 1,000 pending exits, a spawn at
1,000 accepted and live runs, a failed removal, and a node daemon restart at
each preparation and removal boundary.

## Fields not carried

Each `SpawnRequest` field (`src/gobby/agents/spawn_models.py`) that the
`agent.spawn` payload does not carry, with the reason:

- `cwd`, `project_path`, `worktree_id`, `clone_id`: the node computes its
  workspace from `source_root` and `workspace`, and a resume names its earlier
  workspace by `workspace.path`. The hub's worktree and clone rows belong to
  hub-mode P4 (Remote workspaces and the machine-scoped project view).
- `agent_run_id`: it equals `run_id`.
- `workflow`: it is `workflow_name`.
- `branch_name`: it is inside `workspace`.
- `retry_terminal_id`: it is the payload's `terminal_id`, which a hub retry
  repeats.
- `effective_reasoning_effort`: it is `reasoning_effort`.
- `sandbox_config`: it is `sandbox`.
- `code_index_preflight_mode`: it is `code_index_preflight`.
- `initial_variables`: the hub writes them to the child session in
  `prepare_terminal_spawn` before it sends, and agy's second merge is
  redundant on that path.
- `claimed_session_id`, `machine_id`, `is_local`: run-row attributes the hub
  records. A loopback endpoint is refused with `remote_loopback_endpoint`,
  and node bodies name no machine.
- `requested_reasoning_effort`, `reasoning_required`, `reasoning_status`,
  `reasoning_message`: the hub resolves reasoning and records it; the node
  gets the effective effort as `reasoning_effort`.
- `auto_approve`: always true for spawned agents.
- `sandbox_args`, `sandbox_env`: only the resume path reads them, and a
  resume reaches the node as a fresh `agent.spawn`.
- `resume_metadata_json`: launch bookkeeping that follows from the payload,
  which the node does not report.
- `code_index_preflight_warning`: the node produces it and posts it to
  `POST /api/nodes/runs/code-index-warning`.
- `terminal_lifetime`: the executor does not read it.
- `session_manager`, `run_manager`, `cleanup_agent`, `daemon_config`,
  `terminal_manager`, `terminal_runtime_registry`, `write_coordinator`,
  `prepared_spawn`, `phase_timings_ms`, `spawn_started_at`, `cancel_event`,
  `placement_binder`: in-process handles and timings, not data.

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

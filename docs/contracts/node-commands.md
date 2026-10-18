# Node commands

Version 3 of the hub-node command contract. The golden messages live in
`crates/gdaemon/tests/fixtures/node_commands/corpus.json`. The Rust types in
`crates/gdaemon/src/node_commands.rs` and the Python models in
`src/gobby/hub/node_commands.py` parse every message and re-encode it to the
same bytes. Payloads, results, and node bodies reject unknown fields. The
corpus's `invalid` list holds messages that both sides must refuse.

Every field is required, including a nullable one: a field this contract
gives as "or null" accepts an explicit `null` and fails when it is missing.
Values are not coerced: a string never parses as a number, and a number never
parses as a boolean.

Both sides define `CONTRACT_VERSION` as 4: a Rust `pub const` and a Python
module constant. Every frame and every node body carries it as `v`; answers
carry no `v`. A new command or field bumps `v`. Every handler and route
refuses a `v` other than `CONTRACT_VERSION` with `unsupported_version`: a node
answers a frame that way, and the hub answers a node body that way. The
models accept any `v` of 1 or greater so that the refusal can name its
request rather than fail in the parser.

## Frames

Hub to node:

```json
{"type":"command","v":4,"request_id":"<uuid>","command":"<name>","payload":{}}
```

Node to hub, success:

```json
{"type":"reply","v":4,"request_id":"<uuid>","ok":true,"result":{}}
```

Node to hub, or gdaemon when it must answer for the node:

```json
{"type":"reply","v":4,"request_id":"<uuid>","ok":false,"error":{"code":"<code>","message":"<text>","stage":"none"}}
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
| `no_project_root` | `"none"` |
| `outside` | `"none"` |

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
| `checkout_too_large` | 413 |
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

## v3 commands

Version 3 adds three hub-to-node commands. They name no run. A node merges
and removes only its own workspaces and reads only its own checkouts.
Expected refusals are ok results with an `outcome`, so callers branch on
data. Error replies are `invalid_payload`, `not_found`, and
`command_failed`, each with the stage rule below.

### `workspace.merge`

Payload, in order:

| Field | JSON |
| --- | --- |
| `project_id` | string |
| `mode` | `"worktree"` or `"clone"` |
| `path` | string |
| `branch_name`, `target_branch` | string |
| `source_root` | string |

`path` is the node's workspace, `<home>/worktrees/<project_id>/<run_id>`
or `<home>/clones/<project_id>/<run_id>`. `branch_name` is the workspace's
branch, and the merge lands on `target_branch` in `source_root`, the node's
checkout the workspace branched from.

Result, in order:

| Field | JSON |
| --- | --- |
| `outcome` | `"merged"`, `"already_merged"`, `"conflict"`, `"target_dirty"`, or `"busy"` |
| `merge_sha` | string or null |
| `target_head_sha` | string or null |
| `conflicted_files` | array of strings |
| `dirty_paths` | array of strings |

`merge_sha` is set only with `merged`. `target_head_sha` is set with
`merged` and `already_merged`. `conflicted_files` is non-empty only with
`conflict`, and `dirty_paths` only with `target_dirty`. Every other
combination carries null and empty arrays.

Stage rule: every check runs before any effect, so `invalid_payload`,
`not_found`, and a `command_failed` for a detached HEAD or a refused
checkout of `target_branch` carry `stage` `"none"`. A `command_failed`
from the merge itself carries `"partial"`.

### `workspace.remove`

Payload, in order:

| Field | JSON |
| --- | --- |
| `project_id` | string |
| `mode` | `"worktree"` or `"clone"` |
| `path` | string |
| `branch_name` | string |
| `source_root` | string |
| `force`, `force_delete_branch` | boolean |
| `merged_into` | string or null |

`merged_into` names the branch the workspace's branch must already be
merged into, or null for any other local branch in `source_root`.

Result: `{outcome}`, where `outcome` is `"removed"`, `"dirty"`,
`"unmerged"`, or `"busy"`.

Stage rule: `invalid_payload` and `not_found` carry `stage` `"none"`. A
`command_failed` from a failed prune, branch deletion, or workspace removal
carries `"partial"`.

### `checkout.status`

Payload: `{project_id, root_path}`, both strings. `root_path` is the node's
registered checkout.

Result, in order:

| Field | JSON |
| --- | --- |
| `current_branch` | string or null |
| `branch_count` | number |
| `ahead`, `behind` | number or null |

The fields are the ones `get_status`
(`src/gobby/servers/routes/source_control.py`) computes locally.
`current_branch` is null on a detached HEAD, and `ahead` and `behind` are
null when the current branch has no upstream.

Stage rule: the command is read-only, so every error it answers carries
`stage` `"none"`.

## v4 commands

### `checkout.read`

Read-only. The hub sends `checkout.read` with a `cwd` and one `query`. The node
replies with a result, or fails with an error whose `stage` is `"none"`.

Payload:

```json
{"cwd":"<path>","query":{"kind":"files","paths":["<path>"],"read_limit":1024}}
{"cwd":"<path>","query":{"kind":"git","args":["<arg>"],"stdin":null,"timeout_ms":5000}}
```

- `query.kind` is `files` or `git`. A query carries only the fields of its form.
- `files`: `paths` holds at most 1,000 entries. `read_limit` is `null` or a
  number from 0 to 8,388,608.
- `git`: `args` lists the git arguments. `stdin` is base64 or `null`.
  `timeout_ms` is from 1 to 600,000.

The result is `{root, project_id, home, temp_dir, files, git}`. `root` is R0,
and `project_id` is its marker id. `home` and `temp_dir` are the node's
canonical home and temporary directories. `files` is a list of entries or
`null`, and `git` is a result or `null`. Every nullable field is required.

Each `files` entry has the fields `path`, `resolved`, `kind`, `checkout_root`,
`git_common_dir`, `project_root`, `project_id`, `data`, and `truncated`. `kind`
is `file`, `dir`, `other`, `missing`, or `outside`. For an admitted path, the four location fields are
set, and `data` is base64 of the first `read_limit` bytes of a `file`, or
`null`. An `outside` entry carries `path`, `resolved`, and `kind`; its other
fields are `null`, and `truncated` is `false`.

The git result has the fields `exit_code`, `timed_out`, `stdout`, `stderr`, and
`truncated`. `stdout` is base64. `stderr` is UTF-8 with replacement, at most 64 KiB.

Errors, each with `stage` `"none"`: `invalid_payload`, `no_project_root`, and
`outside`. Node duties below say when each applies.

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

`report_too_large` (413) means a `POST /api/nodes/report` body exceeds the hub's
`node_reports.max_request_bytes`. `checkout_too_large` (413) is the same cap on
`POST /api/nodes/checkouts`. The hub stops reading at that cap, before any
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

`workspace.merge` and `workspace.remove`:

- The node runs them outside the per-run queues, after its `v`, command,
  and payload checks. No handler treats a successful parse as
  authorization: every path the hub names is checked against the node's
  own roots.
- Checks before any effect, each answering `invalid_payload` with `stage`
  `"none"`: `path` names a directory directly under
  `<home>/worktrees/<project_id>/` for `"worktree"` or
  `<home>/clones/<project_id>/` for `"clone"`. An existing path must
  canonicalize to a directory directly under the canonical root, so a
  symlink cannot escape it. A missing path's parent must canonicalize to
  that root and its file name must be one plain component, which admits an
  already-removed workspace. `source_root` is absolute and equals its
  `git rev-parse --show-toplevel`. `branch_name`, `target_branch`, and
  `merged_into` pass `git check-ref-format --branch`.
- `busy`: the node answers `busy` while an accepted or live run's workspace
  record or a launched workspace record names `path`, while a run with mode
  `"none"` runs in `source_root`, or while another merge or removal holds
  `path` or `source_root`. Otherwise it holds both until the command ends.
  A spawn whose `existing` path is held, or named by a pending removal
  record, fails with `prepare_failed`.

`workspace.merge`, in order:

1. Clone mode fetches `branch_name` from `path` into
   `refs/heads/clone-merge/<branch_name>` in `source_root` and merges that
   ref; worktree mode merges `branch_name`. A missing branch or clone path
   answers `not_found`. The temporary ref is deleted on every path after the
   fetch.
2. A source that is already an ancestor of `target_branch` answers
   `already_merged`.
3. Staged entries in `source_root`, or unstaged and untracked paths that
   `git diff --name-only <target_branch>...<source>` names, answer
   `target_dirty` with those paths. `.gobby/` paths count too. The node
   stashes nothing.
4. The node records the current branch and checks out `target_branch` when
   it differs. A detached HEAD or a refused checkout answers
   `command_failed` with `stage` `"none"`.
5. `git merge --no-ff --no-edit <source>` answers `merged` with `merge_sha`
   and `target_head_sha`. A conflict reads the conflicted files, runs
   `git merge --abort`, and answers `conflict` with them. Any other failure
   aborts a merge in progress and answers `command_failed` with `stage`
   `"partial"`.
6. In every case after step 4, the node checks out the recorded branch
   again.

`workspace.remove`, in order:

1. Without `force`, an existing path with `git status --porcelain` output
   answers `dirty`.
2. Without `force_delete_branch`, a branch that still exists must have its
   head be an ancestor of `merged_into` in `source_root`, or, with
   `merged_into` null, of another local branch there; otherwise `unmerged`.
   A worktree's branch lives in `source_root`; a clone's lives in the clone,
   and a head that `source_root` does not hold is unmerged. This check runs
   for a missing path too, so a missing directory never authorizes deleting
   an unmerged branch.
3. A missing path removes what remains: worktree mode prunes worktrees and
   deletes the branch if it exists. It answers `removed`.
4. An existing path commits a pending removal record for the path and
   branch, then removes the workspace under the preparation record's retry
   rule. Success deletes the record and answers `removed`.

Steps 1 and 2 change nothing, so `dirty` and `unmerged` leave the workspace
and branch as they were. A failed prune, branch deletion, or removal
answers `command_failed` with `stage` `"partial"`; a failed removal keeps
its record for the retry.

`checkout.status`:

- The node runs it outside the per-run queues. `root_path` must be absolute
  and equal its `git rev-parse --show-toplevel`, else `invalid_payload`.
- It runs the same `git for-each-ref` as `get_status`, with a 10-second
  deadline, and answers the fields as `get_status` computes them. A failed
  or late git answers `command_failed`.
- The command is read-only: every error it answers carries `stage`
  `"none"`.

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

`checkout.read`:

- It is read-only. Every error carries `stage` `"none"`.
- It computes R0 from the canonical `cwd`: the nearest ancestor holding
  `.gobby/project.json`. With none, it answers `no_project_root`.
- It classifies each path in this order: under R0 is admitted; a temp agent
  scratchpad is outside; a path whose nearest checkout shares R0's git common
  dir is admitted; every other path is outside. A path under the node's gobby
  home is outside unless it is under `<home>/worktrees/` or `<home>/clones/`.
  It returns no content or git result for an outside path.
- It runs `git` only with the global options `-c core.fsmonitor=false`,
  `--literal-pathspecs`, `--no-optional-locks`, and `--no-pager`, plus `-C`,
  `--git-dir`, and `--work-tree` when they name an admitted path. The
  subcommand is one of `rev-parse`, `diff`, `ls-files`, `ls-tree`, `status`,
  `log`, `check-ignore`, `merge-base`, or `rev-list`.
- It refuses `--output` and `--output=*`, `--no-index`, `--ext-diff`,
  `--textconv`, `-O*`, `--orderfile*`, `--exclude-from*`, and
  `--open-files-in-pager` anywhere in the arguments, with `invalid_payload`. It
  answers `outside` for a `-C`, `--git-dir`, or `--work-tree` path it does not
  admit.
- It removes every `GIT_*` variable from git's environment, sets
  `GIT_OPTIONAL_LOCKS=0`, and kills git at `timeout_ms`.
- It fills the 8 MiB reply budget in request order over `data` and git
  `stdout`. An entry cut by the budget has `truncated` `true`.
- A `files` query with more than 1,000 paths, or a `cwd` that is not a
  directory, is `invalid_payload`.

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
`X-Gobby-Backend-Secret` equals the per-boot front-door secret, compared in
constant time. Every other request, and every request to a standalone or node
front door, gets 404. gdaemon answers 404 only before it sends any frame.

A pending command ends on its reply, its timeout, its channel's close or
replacement, or its HTTP caller going away. A reply that arrives after that
is dropped and logged.

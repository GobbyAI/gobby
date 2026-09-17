# Rules

Rules are Gobby's reactive enforcement layer. They evaluate normalized hook
events and decide whether to block, rewrite, annotate, or trigger follow-up
actions. The model is **CLI agnostic**: Claude, Codex, AGY, and Qwen sessions all
feed the same rule engine once their events are normalized. The same model also
covers other supported sources as their adapters emit normalized events.

For the larger system model, see [Workflows Overview](./workflows-overview.md).

Rules govern agent actions through provider hooks and MCP bridges. Programmatic
MCP calls from `gobby mcp-proxy call-tool`, ordinary REST clients, pipelines, and
internal code skip before/after tool rules. Session identity and agent credentials
still provide attribution and authorization; they do not turn a programmatic call
into an agent action. Argument validation and metrics remain active.

Programmatic calls also leave agent enforcement state untouched: they neither
grant schema leases nor create or clear agent tool errors or discovery state.
Agent bridges opt into enforcement using their existing wrapper protocol marker.
To restrict an agent's access to the Gobby CLI, block its shell invocation with a
tool rule.

## What Rules Are Good For

Use rules when you need behavior that should happen automatically at hook time:

- block a tool call or stop attempt
- rewrite unsafe or non-compliant tool input
- inject dynamic context into the next turn
- seed or mutate session variables
- trigger MCP side effects when a condition becomes true
- emit an explicit skill/reference fetch directive for a workflow state

Rules are not the right tool for long-running control flow. Use pipelines or
agent step workflows for that.

## Bundled Memory Gates

The `memory-lifecycle` rule group keeps memory use explicit and bounded across
providers. `search-memories-on-claim` prompts an agent to search for relevant
project knowledge after a successful task claim. `guard-plan-memory-writes`
keeps draft findings in the plan artifact unless they are durable preferences
or finalized decisions. The `review-closed-task-memories-before-handoff` and
`review-closed-task-memories-on-stop` gates request one
`review_task_memories` pass for each closure batch. These rules guide and gate
agent behavior; they do not inject project memories automatically. See
[Memory System Guide](./memory.md#lifecycle-rules) for the complete installed
rule table.

## YAML Format

Bundled and project rule files are grouped YAML documents:

```yaml
group: worker-safety
tags: [default, safety]

rules:
  no-push:
    description: "Block git push from worker sessions"
    event: before_tool
    priority: 50
    when: "variables.get('_agent_type') is not None"
    effects:
      - type: block
        tools: [Bash]
        command_pattern: "git\\s+push"
        reason: "Do not push from worker sessions."
```

At sync time, each named entry under `rules:` becomes an individual row in
`rule_definitions`. Bundled YAML may still use a single `effect` shorthand;
sync wraps it into the stored `effects` array. The MCP and HTTP authoring
APIs validate the stored `effects` shape directly.

## Rule Shape

### File-Level Fields

| Field | Purpose |
| --- | --- |
| `group` | Logical grouping for related rules |
| `tags` | Discovery and selector tags applied to rules in the file |
| `sources` | Optional source metadata for installed rule rows |
| `audience` | Optional default audience for rules in the file |

### Rule-Level Fields

| Field | Purpose |
| --- | --- |
| `description` | Human-readable summary |
| `event` | Hook event that triggers the rule |
| `enabled` | Default enabled state |
| `priority` | Lower runs earlier |
| `when` | Rule-level condition |
| `match` | Accepted metadata field; current runtime filtering uses `tools`, `when`, and effect selectors |
| `tools` | Optional pre-filter on native tool name |
| `audience` | Limit the rule to `all`, `interactive`, `autonomous`, or a concrete audience |
| `agent_scope` | Limit the rule to specific agent types |
| `effects` | One or more effect definitions |

Current rules are validated as `RuleDefinitionBody`. `effects` is required in
stored definitions and must contain at least one effect. Each effect can also
have its own `when`. A rule can contain at most one `block` effect.

## Condition Expressions

Rule-level and per-effect `when` expressions are evaluated by
`SafeExpressionEvaluator`, an AST-based evaluator. Session variables are
available through `variables` and are also flattened into the top-level
context. Tool input is available as `tool_input`; for MCP `call_tool` events,
the inner `arguments` object is unwrapped while `server_name` and `tool_name`
remain available.

Supported expression features include:

- boolean logic: `and`, `or`, `not`
- comparisons: `==`, `!=`, `<`, `<=`, `>`, `>=`, `is`, `is not`, `in`, `not in`
- arithmetic: `+`, `-`, `*`, `//`, `%`
- literals: strings, numbers, booleans, `None`, lists, tuples, and dicts
- attribute and subscript access
- ternary expressions: `a if condition else b`
- list and generator comprehensions
- safe method calls on dict, str, and list values

Allowed helper functions include `len`, `bool`, `str`, `int`, `list`, `dict`,
`any`, `all`, `normalize_path`, `skill_loaded`, MCP-result helpers such as
`mcp_called` and `mcp_failed`, task helpers such as `task_state_in`, and
tool-policy helpers such as `is_discovery_tool`, `is_operator_tool`, and
`requires_task_for_any_touched_file`.

Use defensive variable access in block rules:

```yaml
when: "variables.get('task_claimed', False) and not variables.get('plan_mode')"
```

A rule-level condition error fails closed when any sibling is a `block`, so
eligible non-block siblings can also execute. A non-block-only rule fails open.
Per-effect condition errors independently fail closed for blocks and skip other
effects. Selectors still apply. Database cancellation/deadline errors propagate.
Within one rule, use `variables.get(...)` after a sibling mutation: flattened
scalar aliases are captured when the rule context is built. Later rules receive
fresh context. A block condition is checked when encountered, before its
application is deferred.

## Events

Rules should usually target semantic workflow events first. Raw normalized hook
events remain available as escape hatches when you need provider-specific
timing.

### Common Events

| Event | When it fires |
| --- | --- |
| `session_start` | Session bootstrap, resume, clear, or compaction re-entry |
| `turn_start` | Semantic start-of-turn boundary across supported CLIs |
| `turn_end` | Semantic end-of-turn boundary across supported CLIs |
| `before_tool` | Before a native tool or MCP tool runs |
| `after_tool` | After a tool call finishes |
| `session_end` | Session teardown |
| `task_created` | A task row has been created |
| `task_completed` | A task row has completed |
| `teammate_idle` | A teammate/agent idle signal was emitted |
| `instructions_loaded` | Runtime instructions were loaded |
| `config_change` | Configuration changed |
| `cwd_changed` | Session working directory changed |
| `file_changed` | A watched file changed |
| `worktree_create` | A worktree was created |
| `worktree_remove` | A worktree was removed |

### Raw Escape-Hatch Events

| Event | When it fires |
| --- | --- |
| `before_agent` | Raw pre-turn hook |
| `after_agent` | Raw post-turn hook |
| `stop` | Raw stop hook |
| `stop_failure` | A turn ended with an API/runtime failure |
| `pre_compact` | Before context compaction |
| `post_compact` | After context compaction, where supported |
| `before_tool_selection` | Before a model chooses tools |
| `before_model` | Before a model call |
| `after_model` | After a model call |
| `subagent_start` | Child agent starts |
| `subagent_stop` | Child agent stops |
| `permission_request` | A permission/approval request is being evaluated |
| `permission_denied` | A permission request was denied |
| `notification` | A notification-style event is emitted |
| `elicitation` | An elicitation request is being evaluated |
| `elicitation_result` | An elicitation result was received |

### Additional Accepted Events

`RuleTriggerEvent` also accepts `setup`, `user_prompt_expansion`,
`post_tool_batch`, `message_display`, and `directory_added`. Accepted names do
not imply every provider emits the event; inspect the adapter payload before
using a provider-specific event.

### About `turn_start`

`turn_start` is the portability event you usually want for prompt-entry,
turn-start context injection, and reset logic. The engine emits it alongside
the raw `before_agent` hook.

### About `turn_end`

`turn_end` is the portability event you usually want for stop gates and
turn-final checks. The engine emits it alongside the raw hook when a session
finishes a turn, so one rule can cover CLIs that surface the boundary as
`after_agent`, `stop`, or `stop_failure`. Separate raw events can each evaluate
the semantic boundary; do not infer cross-event once-only delivery. Manual
compaction bypasses one subsequent semantic turn end, while raw rules still run.
User-interrupt handling suppresses configurable turn-end blocks and keeps
non-block effects live. Durable agent/task waits consume no ordinary stop count.

`turn_end` is only the rule-authoring boundary for the current turn. Spawned
agent-run termination is a separate lifecycle action and is signaled through
`gobby-agents:end_agent_run`.

## Effects

Gobby currently supports these effect types:

| Effect | Purpose |
| --- | --- |
| `block` | Prevent the action and return a reason |
| `set_variable` | Update session state in-place |
| `inject_context` | Append text to the session context |
| `mcp_call` | Queue an MCP call as part of rule evaluation |
| `observe` | Append structured observations to session state |
| `rewrite_input` | Modify tool input before execution |
| `set_permission_response` | Set permission decision metadata on the hook response |
| `set_retry` | Mark an auto-denied tool call as retryable |
| `set_watch_paths` | Update dynamic file watchers |
| `set_worktree_path` | Override a generated worktree path |
| `set_elicitation` | Programmatically answer or override elicitation results |
| `load_skill` | Emit an explicit skill/reference fetch directive |
| `set_display_content` | Set display-content response metadata |
| `run_command` | Run an argv command with hook JSON on stdin, failing open |
| `proxy_hook` | Apply a trusted registered input transformation on `before_tool` |

## Effect Notes

### `block`

Use this to stop a tool call, stop attempt, or other action.

Supported match fields include:

- `tools`
- `mcp_tools`
- `command_pattern`
- `command_not_pattern`

`command_pattern` is matched against each executable segment of a shell
command — the raw text of one pipeline between unquoted `&&`, `||`, `;`, `&`,
and newlines, quotes and substitutions intact — so an anchored pattern sees
one command list at a time (`curl … | sh` stays whole). Heredoc bodies are
stdin data and are left out when every pipeline stage is `cat`, `tee`, `git`,
`gh`, or a bare redirection; a body reaching anything else (shells,
interpreters, `ssh`, unknown tools), process-substituted onward, expanded
through an unquoted delimiter with `$(` or a backtick, or never terminated
stays attached to its segment. A command substitution is resolved by the same
rules before it rejoins its segment, so a commit message built from
`"$(cat <<'EOF' … EOF)"` drops its body while `"$(uv run pytest)"` keeps its
invocation; a segment that runs what a substitution prints (`sh -c`, `eval`)
keeps the body whole. A
`command_not_pattern` exempts the command when it matches the executable text
as a whole, so an environment exported in an earlier segment still counts.
`mask_quoted: true` blanks quoted string data before `command_pattern` runs,
which keeps a commit message or echo mentioning a command from reading as one.
The exemption always reads the unmasked text, so a quoted target such as
`pytest 'tests/x.py'` still exempts.

Only one `block` effect is allowed per rule. With `rules.aggregate_blocks=false`,
the first blocking rule ends evaluation. With aggregation enabled (the default),
later matching rules contribute block gates but no non-block side effects.

Native `tools` and `mcp_tools` are alternative selectors. Native shell aliases
match as shell tools and use command selectors. Non-shell native and MCP tool
matches do not additionally apply command regexes; with no tool selectors,
command selectors still apply. Use the selector appropriate to the payload.

### `set_variable`

`set_variable` mutates the session variables immediately. Later rules in the
same evaluation pass see the updated value. Expression-like strings evaluate;
Jinja values render/coerce first. Failed expressions skip the write. Custom rules
cannot write runtime-reserved variables. The default value is `None`, so specify
the intended value rather than relying on an omitted field.

### `inject_context`

Multiple `inject_context` effects accumulate. This is how rule bundles append
reminders, handoff text, or recovery guidance without replacing the whole
system prompt.

### `mcp_call`

Use `mcp_call` when a hook needs to trigger a tool automatically. Current
effect options include:

- `background`
- `inject_result`
- `block_on_failure`
- `block_on_success`

Top-level string arguments are templated; nested containers are not recursively
rendered by this effect. Inline dispatch requires `inject_result=true`,
`background=false`, and an available dispatcher. `success_variable` requires
inline result injection. An inline failure can contribute a block reason, but
does not automatically stop later sibling effects: gate dependent work
explicitly. Deferred/background calls cannot establish an inline prerequisite.

### `rewrite_input`

`rewrite_input` changes the pending tool input before it runs. uv enforcement is
implemented as a `block` rule, not a rewrite.

For MCP `call_tool` events, rewrite updates are merged into the inner
`arguments` object so routing fields stay intact.

### Response-Metadata Effects

`set_permission_response`, `set_retry`, `set_watch_paths`, `set_worktree_path`,
and `set_elicitation` write response metadata consumed by hook adapters or
runtime handlers. Use these only when the hook surface expects that metadata.

### `load_skill`

`load_skill` emits a fetch directive. It does not resolve or inject the body and
does not satisfy a loading gate. The agent must complete the requested skill or
exact reference retrieval, including every cursor page. Router/menu-only loads
do not load references.

### Executable and Delivery Effects

`run_command` runs an argv list with hook-event JSON on stdin. A skill and script
must be supplied together when using a skill asset; script paths are validated.
Failures, missing executables, timeouts, and unparseable output fail open. Do not
use it as a blocking safety primitive or assume an installed script is authorized
for arbitrary execution.

`proxy_hook` is restricted to `before_tool` and registered trusted handlers. It
cannot configure arbitrary commands, scripts, or background execution. Original
input is checked before transformation; changed input is checked again against
agent, step, and declarative block policy.

`delivery` defaults to `eager`. Supported `on_receipt` tracking writes are staged
with the payload rather than committed before delivery. A block's
`acknowledge_variable` is consumed only when its block is delivered; a displaced
block stays armed. Use this mechanism rather than clearing a trigger in a sibling
`set_variable` effect. See [override precedence](./workflow-rules.md#override-precedence-and-acknowledge-variables).

## Example: Multi-Effect Rule

```yaml
group: tool-hygiene

rules:
  prefer-uv-rewrite:
    event: before_tool
    when: "event.data.get('tool_name') == 'Bash'"
    effects:
      - type: rewrite_input
        input_updates:
          command: >-
            {{
              tool_input.get('command', '')
              | regex_replace('^python\\s+', 'uv run python ')
            }}
      - type: inject_context
        template: "Prefer `uv run ...` for Python commands in this repo."
```

For `before_tool` events the command lives under `tool_input`, which the
template context exposes unwrapped; `event.data.get('command', '')` would
resolve to an empty string.

## Evaluation Rules

The engine evaluates rules like this:

1. Resolve raw and semantic events for the incoming hook.
2. Load enabled rules for those events.
3. Apply event-project scope and agent-scope filtering.
4. Filter by audience and the session's active rule selectors.
5. Run hard-coded agent and step tool enforcement for `before_tool`.
6. Evaluate each rule's `tools`, `when`, and effect selectors.
7. Apply matching non-block effects in order, then apply a deferred block if present.
8. Stop after the first blocking rule, or collect later blocks without their side effects when aggregation is enabled.

Important runtime semantics:

- `set_variable` effects are visible to later rules in the same pass.
- `inject_context` effects accumulate.
- `mcp_call` effects are collected and dispatched after evaluation.
- Inline `mcp_call` effects can inject formatted results and contribute a block; later siblings still require explicit dependency conditions.
- In a multi-effect rule, non-block effects run before the rule's block effect.
- Rule conditions skip rules; they do not stop evaluation.
- Some universal safety behavior is hard-coded in the engine, not expressed in YAML.
- On `turn_start`, the engine resets transient stop/tool-block state and may seed progressive MCP discovery.
- On ordinary `turn_end`, the engine increments `stop_attempts` before configurable rules; active durable waits do not consume attempts.

## Activation Model

A rule only affects a session when all of the following are true:

1. The definition exists in `rule_definitions`.
2. The rule is enabled.
3. Active `rules.enforcement_enabled` is true, and row scope is global or the event project.
4. Its `agent_scope`, if present, matches the session's agent type.
5. Its `audience`, if present, matches the current runtime audience.
6. The current session's active rule selectors include it.

That means bundled YAML is only the template source. The database plus active
selectors determine what actually runs. Current agent-definition selectors take
precedence over fallback `_active_rule_names`; missing fallback applies no
additional filter. There is no general per-session rule-override management API.

### Export and Import

`gobby rules export` emits one YAML document, preserving each rule's body, group,
description, enabled state, priority, tags, and sources. Per-rule tags/sources
override file defaults; the importer retains its ownership tag. Invalid metadata
is rejected. Duplicate names from different scopes fail rather than silently
losing a row. Import uses destination project scope, not original row IDs or
scope: this is a definition export, not a full database backup.

### Customization and Scope

Keep bundled definitions Gobby-owned: customize using a distinct named rule and
intentional agent selectors, not by editing an installed bundled body. Public
low-level updates accepting a body are not authorization to change bundled
policy. Current rule sync skips user rules that collide with bundled names; do not
assume another domain's `override: true` format enables same-name rule overrides.

Current rule-file roots are `.gobby/workflows/rules/` and
`~/.gobby/workflows/rules/`. Operator imports within a registered checkout use
that project's ID; outside one they are global. MCP `create_rule` creates a
global custom row, so file sync never refreshes or prunes it. Its `project_path`
controls export location only, and `make_template` selects global export. It is
not a scope selector.

Managed sync applies enabled defaults until the user pins a toggle. It refreshes
managed definitions, protects live custom rows, and prunes absent managed rows
within scanned scopes. A soft-deleted managed row can return on sync. Read back
the installed state rather than assuming restore resets every user choice.
Row changes are read without restart; engine code changes need a coordinated
restart from the daemon checkout.

## Public Tooling

Use the `gobby-workflows` MCP server to manage standalone rules:

- `list_rules`
- `get_rule`
- `create_rule`
- `update_rule`
- `toggle_rule`
- `delete_rule`

The CLI also exposes operator commands under `gobby rules`, including `list`,
`show`, `enable`, `disable`, `import`, `export`, and `audit`.

Use `create_rule(name, definition)` with `group` inside the definition and a
nonempty `effects` array. Creation accepts the body, not embedded row metadata;
use `update_rule` for supported metadata. Updates replace a supplied body rather
than merging it; explicit metadata wins over embedded update metadata. Read back
successful writes. Auto-export can be skipped in dev mode or fail after the row
write succeeds, so verify files separately.

MCP `list_rules` has no public cursor: event takes precedence over group if both
are supplied. Brief mode returns name/event/group/enabled. Malformed bodies can
be skipped with a warning; use exact lookup and logs before declaring absence.
Large proxy results are separately retrievable via `gobby-results` pagination.

`toggle_rule` changes installed state, not one session. `delete_rule` soft-deletes;
bundled `gobby` tags require `force=true`, and sync can restore them. The CLI is
operator tooling: enable/disable call the daemon, while list/show/import/export/
audit access CLI storage. A multi-rule import may partially succeed. Audit output
is bounded history, not a complete record of every allow or skipped rule.

HTTP has nine rule routes: list/create, groups, tags, bulk-toggle, and per-name
get/update/delete/toggle. There is no collection `PUT /api/rules`. Bulk-toggle
filters by source (`installed` or `project`), not project ID; inspect `partial`
and `failures` even when the response says success. See the
[HTTP inventory](./http-endpoints.md#memory-skills-workflows-and-rules).

For authoring caveats and engine behavior that matters when designing rules,
see [Rule Authoring Guide](./workflow-rules.md).

_Last verified: 2026-09-12_

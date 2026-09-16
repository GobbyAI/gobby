# Hook Schemas

This guide is the native hook transport reference for Gobby-managed CLI
integrations. It describes the provider hook names, request envelopes,
normalization model, and response fields that adapters translate into Gobby's
workflow engine.

## Overview

Gobby receives native hooks from each CLI, normalizes them to `HookEvent`, runs
workflow rules, then translates `HookResponse` back to the provider-specific
response shape.

Rule authors should target semantic workflow events such as `turn_start` and
`turn_end`. Raw events such as `before_agent`, `after_agent`, and `stop` are
transport/runtime details that exist so adapters can normalize provider hooks.
Agent process termination is separate from hook delivery; automation agents
must still finish by calling `gobby-agents:end_agent_run`.

| CLI | Native Format | Session Field | Integration |
| --- | --- | --- | --- |
| Claude Code | Settings hook names with Gobby kebab-case `ghook --type` values | `session_id` | HTTP hook command |
| AGY CLI | PascalCase (`PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, `Stop`) | `session_id` | HTTP hook command |
| Qwen CLI | Current PascalCase (`SessionStart`, `PreToolUse`, `Stop`) | `session_id` | HTTP hook command |
| Codex CLI | hooks.json PascalCase (`SessionStart`, `PreToolUse`) | `session_id` | HTTP hook command |
| Droid CLI | PascalCase (`PreToolUse`) | `session_id` | HTTP hook command |
| Grok CLI | snake_case (`session_start`, `pre_tool_use`) | `sessionId` normalized to `session_id` | HTTP hook command |

## HTTP Request Envelope

All Gobby-managed hook commands post to:

```text
POST /api/hooks/execute
```

The endpoint accepts ONLY the schema-versioned envelope. Requests without
`schema_version: 1` are rejected with HTTP 400 (`Unsupported schema_version`);
the older flat shape without `schema_version` is no longer accepted.

```json
{
  "schema_version": 1,
  "response_capability": "hook-response.v1",
  "critical": false,
  "enqueued_at": "2026-05-07T15:00:00Z",
  "source": "claude",
  "hook_type": "pre-tool-use",
  "input_data": {
    "session_id": "provider-session-id",
    "tool_name": "Bash",
    "tool_input": {
      "command": "git status --short"
    }
  }
}
```

`source` is required and must be one of `claude`, `grok`, `qwen`, `agy`,
`codex`, or `droid`. `hook_type` is the provider hook name that the selected
adapter understands.

The request must also advertise `response_capability: "hook-response.v1"`.
Missing or older capabilities are rejected before adapter evaluation even when
the installed binary's runtime stamp is compatible. Managed ghook supplies this
field, the local bearer credential, and the envelope identity. A custom client
must implement the current response and delivery-receipt protocol before
advertising the capability; adding the string alone is not an implementation.

## HTTP Response Semantics

Gobby hook delivery is at-least-once. Clients should treat HTTP 409 with
`reason: "duplicate envelope already processing"` as an in-flight duplicate for
the same `X-Gobby-Envelope-Id`, not as a terminal failure. Retry with
exponential backoff and the same envelope ID; the daemon replays a stored
terminal response once the original delivery finishes.

Managed ghook settles a delivered envelope only after mapping and flushing the
provider response. When a receipt is returned it replaces the original inbox
file with a delivery receipt for acknowledgement. A plain 2xx or a successful
POST is not proof that context reached the host. Retry backpressure is HTTP 503
with `status: "retry"`; ghook retains the envelope and continues even for a
critical hook. Other 503 responses follow the failure matrix.

### Daemon-Down Critical Posture

"Daemon down" means that `ghook` cannot deliver the envelope to the Python
daemon because the connection fails or times out. The envelope remains queued
for replay, but only `ghook` and the host CLI can decide whether the current
invocation blocks. A planned Gobby stop or restart is a separate case: a fresh
shutdown marker makes `ghook` return a continue response for Stop hooks so an
intentional daemon shutdown cannot strand an agent.

Turn-level `Stop` is never critical. A daemon outage fails open on every
provider's Stop, PreToolUse, and other turn-level hooks so an unreachable
daemon cannot freeze the CLI on every turn. Session-lifecycle hooks still fail
closed:

| Source / CLI | Terminal Hook | Daemon-Down Posture |
| --- | --- | --- |
| `claude` | `stop` | Fail open; non-critical transport failure |
| `agy` | `Stop` | Fail open; non-critical transport failure |
| `qwen` | `Stop` | Fail open; non-critical transport failure |
| `codex` | `Stop` | Fail open; non-critical transport failure |
| `droid` | `Stop` | Fail open; non-critical transport failure |
| `grok` | `stop` | Fail open; non-critical transport failure |

Non-critical transport failures use `ghook`'s fail-open path (exit 1 with an
error JSON for most CLIs; AGY exits 0 with per-event skip JSON). Critical
transport failures use exit 2 and do not emit a continue response. The
critical set is session-lifecycle only: Claude `session-start`, `session-end`,
and `pre-compact`; Codex, Qwen, and Droid `SessionStart`, `SessionEnd`, and
`PreCompact`; Grok `session_start`, `session_end`, and `pre_compact`. AGY has
no critical native event: PreInvocation, PreToolUse, PostToolUse,
PostInvocation, and Stop are all non-critical.

Evaluation exceptions fail closed for `Stop` / `stop` and envelopes carrying
`critical: true`. Evaluation timeouts on capability-bearing requests instead
return HTTP 503 with `status: "retry"` and `retry_kind: "adapter_timeout"`.
Managed ghook retains that envelope and lets the current invocation continue,
including critical hooks. An outstanding adapter worker retains its processing
lease until finalization. This retry behavior is implemented in the hooks route
and covered by `tests/servers/routes/test_hooks_agy_dispatch.py`; it must not be
described as a synchronous fail-closed timeout guarantee.

`ghook` is versioned and released from the authoritative
[`GobbyAI/gobby`](https://github.com/GobbyAI/gobby/tree/0.5.0/crates/ghook)
monorepo. The retired `gobby-cli` checkout and repository preserve audit history
only. Turn-level `Stop` fails open on daemon outage so a down daemon cannot
block every turn; Claude's existing fail-open Stop behavior is unchanged.
### Runtime Schema Compatibility

Running `ghook --version` writes
`$GOBBY_HOME/bin/.ghook-runtime.json` with the binary's `schema_version` and
`ghook_version`. The daemon reads this stamp for `/api/health` and
`/api/admin/status`, and `gobby status` renders incompatible states as health
issues. Diagnostics use explicit `absent`, `compatible`, `malformed`,
`schema_mismatch`, and `stale_version` states.

An absent stamp does not degrade health so existing installations continue to
run until ghook has emitted runtime metadata. A malformed stamp, envelope schema
mismatch, or ghook version below the managed minimum does degrade health. The
daemon currently accepts envelope schema `1`; its ghook floor comes directly
from the managed binary version policy, so a
pin update also updates the runtime compatibility threshold.

## Native To Workflow Mapping

### Semantic Rule Events

The workflow engine resolves raw provider events into semantic authoring events:

| Semantic Event | Raw Events That Trigger It | Use For |
| --- | --- | --- |
| `turn_start` | `before_agent` | Prompt-time context injection, per-turn setup |
| `turn_end` | `after_agent`, `stop`, `stop_failure` | Stop gates, task/commit checks, end-of-turn cleanup and failed-stop recovery |

Use provider-specific names only when writing adapter code, testing native hook
transport, or documenting low-level payloads.

### Claude Code

Claude settings use PascalCase hook keys, but Gobby's installed `ghook` command
passes kebab-case hook types to the daemon.

| Native Hook Type | Hook Event Name | Raw Workflow Event | Semantic Event |
| --- | --- | --- | --- |
| `session-start` | `SessionStart` | `session_start` | `session_start` |
| `setup` | `Setup` | `setup` | `setup` |
| `instructions-loaded` | `InstructionsLoaded` | `instructions_loaded` | raw only |
| `user-prompt-submit` | `UserPromptSubmit` | `before_agent` | `turn_start` |
| `user-prompt-expansion` | `UserPromptExpansion` | `user_prompt_expansion` | `user_prompt_expansion` |
| `pre-tool-use` | `PreToolUse` | `before_tool` | `before_tool` |
| `permission-request` | `PermissionRequest` | `permission_request` | `permission_request` |
| `post-tool-use` | `PostToolUse` | `after_tool` | `after_tool` |
| `post-tool-use-failure` | `PostToolUseFailure` | `after_tool` | `after_tool` |
| `post-tool-batch` | `PostToolBatch` | `post_tool_batch` | `post_tool_batch` |
| `permission-denied` | `PermissionDenied` | `permission_denied` | `permission_denied` |
| `notification` | `Notification` | `notification` | `notification` |
| `message-display` | `MessageDisplay` | `message_display` | `message_display` |
| `subagent-start` | `SubagentStart` | `subagent_start` | `subagent_start` |
| `subagent-stop` | `SubagentStop` | `subagent_stop` | `subagent_stop` |
| `task-created` | `TaskCreated` | `task_created` | `task_created` |
| `task-completed` | `TaskCompleted` | `task_completed` | `task_completed` |
| `stop` | `Stop` | `stop` | `turn_end` |
| `stop-failure` | `StopFailure` | `stop_failure` | `turn_end` |
| `teammate-idle` | `TeammateIdle` | `teammate_idle` | `teammate_idle` |
| `config-change` | `ConfigChange` | `config_change` | `config_change` |
| `cwd-changed` | `CwdChanged` | `cwd_changed` | `cwd_changed` |
| `directory-added` | `DirectoryAdded` | `directory_added` | `directory_added` |
| `file-changed` | `FileChanged` | `file_changed` | `file_changed` |
| `worktree-create` | `WorktreeCreate` | `worktree_create` | `worktree_create` |
| `worktree-remove` | `WorktreeRemove` | `worktree_remove` | `worktree_remove` |
| `pre-compact` | `PreCompact` | `pre_compact` | `pre_compact` |
| `post-compact` | `PostCompact` | `post_compact` | `post_compact` |
| `session-end` | `SessionEnd` | `session_end` | `session_end` |
| `elicitation` | `Elicitation` | `elicitation` | `elicitation` |
| `elicitation-result` | `ElicitationResult` | `elicitation_result` | `elicitation_result` |

### AGY

AGY hooks are installed to `~/.gemini/config/hooks.json`, which is the AGY
vendor config path. These are the only five native events; AGY does not expose
`SessionStart` or `UserPromptSubmit`. Gobby separately supports managed spawning
and web chat through AGY's custom stream-json subprocess transport on 1.1.18+.

| Native Hook | Raw Workflow Event | Semantic Event |
| --- | --- | --- |
| `PreInvocation` | `before_agent` | `turn_start` |
| `PostInvocation` | `after_agent` | `turn_end` |
| `PreToolUse` | `before_tool` | `before_tool` |
| `PostToolUse` | `after_tool` | `after_tool` |
| `Stop` | `stop` | `turn_end` |

### Qwen

Qwen uses a dedicated Claude-shaped adapter with Qwen-specific contracts.

| Native Hook | Raw Workflow Event | Semantic Event |
| --- | --- | --- |
| `SessionStart` | `session_start` | `session_start` |
| `SessionEnd` | `session_end` | `session_end` |
| `UserPromptSubmit` | `before_agent` | `turn_start` |
| `PreToolUse` | `before_tool` | `before_tool` |
| `PermissionRequest` | `permission_request` | `permission_request` |
| `PostToolUse` | `after_tool` | `after_tool` |
| `PostToolUseFailure` | `after_tool` | `after_tool` |
| `Stop` | `stop` | `turn_end` |
| `StopFailure` | `stop_failure` | `turn_end` |
| `SubagentStart` | `subagent_start` | `subagent_start` |
| `SubagentStop` | `subagent_stop` | `subagent_stop` |
| `PreCompact` | `pre_compact` | `pre_compact` |
| `PostCompact` | `post_compact` | `post_compact` |
| `Notification` | `notification` | `notification` |
| `TodoCreated` | `task_created` | `task_created` |
| `TodoCompleted` | `task_completed` | `task_completed` |

### Codex

Gobby-managed Codex terminal installs use hooks.json-style PascalCase hook
events posted through `ghook`. Older app-server/WebSocket paths exist for chat
integration, but they are not the installed terminal hook contract.

| Native Hook | Raw Workflow Event | Semantic Event |
| --- | --- | --- |
| `SessionStart` | `session_start` | `session_start` |
| `UserPromptSubmit` | `before_agent` | `turn_start` |
| `PreToolUse` | `before_tool` | `before_tool` |
| `PermissionRequest` | `permission_request` | `permission_request` |
| `PostToolUse` | `after_tool` | `after_tool` |
| `PreCompact` | `pre_compact` | `pre_compact` |
| `PostCompact` | `post_compact` | `post_compact` |
| `Stop` | `stop` | `turn_end` |

Codex `PreToolUse` and `Stop` responses use `systemMessage` for context; Codex
does not accept `additionalContext` for those hooks.

Codex also sends `Interrupt`, normalized as `interrupt`. This is interruption
evidence, not a turn-end gate. ghook emits no stdout for this event, including
disabled, unmanaged, malformed, timeout and stale-daemon response paths.
The adapter also maps `SubagentStart`, `SubagentStop`, and `SessionEnd` to
`subagent_start`, `subagent_stop`, and `session_end`, respectively.

### Grok

Grok uses lowercase snake-case native hook names and camelCase payload fields.

| Native Hook | Raw Workflow Event | Semantic Event |
| --- | --- | --- |
| `session_start` | `session_start` | `session_start` |
| `session_end` | `session_end` | `session_end` |
| `user_prompt_submit` | `before_agent` | `turn_start` |
| `pre_tool_use` | `before_tool` | `before_tool` |
| `post_tool_use` | `after_tool` | `after_tool` |
| `post_tool_use_failure` | `after_tool` | `after_tool` |
| `stop` | `stop` | `turn_end` |
| `stop_failure` | `stop_failure` | `turn_end` |
| `pre_compact` | `pre_compact` | `pre_compact` |
| `post_compact` | `post_compact` | `post_compact` |
| `notification` | `notification` | `notification` |
| `permission_denied` | `permission_denied` | `permission_denied` |
| `subagent_start` | `subagent_start` | `subagent_start` |
| `subagent_stop` | `subagent_stop` | `subagent_stop` |
| `pending_interaction` | `notification` | wait evidence |
| `interaction_resolved` | `notification` | wait-resolution evidence |
| `stop_cancelled` | `stop` or `interrupt` | disposition-dependent |

Grok registers `SubagentStart` and `SubagentStop` in `hooks-template.json`.
Grok 1.0.30 dispatches `subagent_start` non-blocking with the parent's
`sessionId` (ACP `blockingEvents` lists only `pre_tool_use`, `stop`, and
`subagent_stop`); Gobby resolves it to the parent and increments the counter
without a hooks-log row. Older builds sent the child `sessionId`. One start can
cover a batch of parallel children, so after the first `subagent_stop` the
counter can read zero while children still run. Child hooks carry their own
`sessionId` but run inside the parent's process: same tmux pane, `parent_pid`,
and `parent_create_time`. Gobby binds a Grok hook whose session is unknown to
the live pane owner only when `terminal_process_contexts_match` proves that
shared process, then derives `is_subagent` / `subagent_count` from the bind; a
different process in the same pane registers as a new session. A parent
`user_prompt_submit` still resets the counter; a process-bound child
`user_prompt_submit` does not. Evidence:
`tests/fixtures/provider_contracts/grok/subagent-start-debug-trace.json` and
`tests/fixtures/provider_contracts/grok/subagent-start-drop-summary.json`.

For `stop_cancelled`, `stop_reason: user_interrupt` together with
`cancelled_by: user` becomes `interrupt`; other cancellations carry the
`ended_non_user` disposition. These lifecycle fields distinguish interruption
and provider waits from a completed user turn.

### Droid

| Native Hook | Raw Workflow Event | Semantic Event |
| --- | --- | --- |
| `PreToolUse` | `before_tool` | `before_tool` |
| `PostToolUse` | `after_tool` | `after_tool` |
| `UserPromptSubmit` | `before_agent` | `turn_start` |
| `Notification` | `notification` | `notification` |
| `Stop` | `stop` | `turn_end` |
| `SubagentStop` | `subagent_stop` | `subagent_stop` |
| `PreCompact` | `pre_compact` | `pre_compact` |
| `SessionStart` | `session_start` | `session_start` |
| `SessionEnd` | `session_end` | `session_end` |

Droid declares all nine events, and the table above is the translation contract used
whenever one arrives. Only five were observed firing in non-interactive `droid exec`
runs on 0.219.0: `PreToolUse`, `PostToolUse`, `Stop`, `SessionStart`, and `SessionEnd`.
`UserPromptSubmit`, `Notification`, `SubagentStop`, and `PreCompact` never fired, so a
spawned Droid agent never reaches `turn_start` and cannot be given context through
`UserPromptSubmit.additionalContext` -- `SessionStart` is its only context hook. This
was measured in `droid exec` only; it is not a claim about interactive Droid sessions
(#22402).

## Common Payload Fields

Adapters preserve provider payloads in `HookEvent.data`, then add normalized
fields where possible.

| Field | Meaning |
| --- | --- |
| `session_id` | Provider session/thread ID; becomes `HookEvent.session_id` |
| `machine_id` | Local machine identifier |
| `cwd` | Current working directory |
| `prompt` | Canonical user prompt field for turn-start handling |
| `tool_name` | Canonical tool name after provider-specific mapping |
| `tool_input` | Canonical tool input payload |
| `tool_output` | Canonical tool output payload |
| `mcp_server` | Extracted MCP server name when a tool call targets MCP |
| `mcp_tool` | Extracted MCP tool name when a tool call targets MCP |
| `is_error` | Normalized tool failure flag |

Shell-like tools normalize to `Bash`. Common Qwen/AGY tool names also map to
Claude-style names such as `Read`, `Write`, `Edit`, `Glob`, and `Grep`.

## Provider Payload Examples

These are provider payload fragments, not complete HTTP requests. Wrap them in
the versioned, authenticated envelope above for an actual transport request;
normally the installed ghook does that work.

### Claude Code

```json
{
  "source": "claude",
  "hook_type": "user-prompt-submit",
  "input_data": {
    "session_id": "claude-session-123",
    "machine_id": "machine-uuid",
    "cwd": "/path/to/project",
    "transcript_path": "/path/to/transcript.jsonl",
    "user_prompt": "Refresh the hook schema guide"
  }
}
```

```json
{
  "source": "claude",
  "hook_type": "pre-tool-use",
  "input_data": {
    "session_id": "claude-session-123",
    "tool_name": "Bash",
    "tool_input": {
      "command": "git status --short"
    }
  }
}
```

```json
{
  "source": "claude",
  "hook_type": "permission-request",
  "input_data": {
    "session_id": "claude-session-123",
    "tool_name": "Bash",
    "tool_input": {
      "command": "rm -rf /tmp/example"
    },
    "permission_suggestions": []
  }
}
```

### AGY

```json
{
  "source": "agy",
  "hook_type": "PreInvocation",
  "input_data": {
    "hook_event_name": "PreInvocation",
    "session_id": "agy-session-123",
    "cwd": "/path/to/project",
    "timestamp": "2026-05-07T15:00:00Z",
    "prompt": "Refresh the hook schema guide"
  }
}
```

### Qwen

```json
{
  "source": "qwen",
  "hook_type": "PreToolUse",
  "input_data": {
    "hook_event_name": "PreToolUse",
    "session_id": "qwen-session-123",
    "tool_name": "RunShellCommand",
    "tool_input": {
      "command": "git status --short"
    }
  }
}
```

### Codex

```json
{
  "source": "codex",
  "hook_type": "UserPromptSubmit",
  "input_data": {
    "session_id": "codex-session-123",
    "cwd": "/path/to/project",
    "prompt": "Refresh the hook schema guide"
  }
}
```

```json
{
  "source": "codex",
  "hook_type": "PreToolUse",
  "input_data": {
    "session_id": "codex-session-123",
    "tool_name": "exec_command",
    "tool_input": {
      "cmd": "git status --short"
    }
  }
}
```

### Droid

Captured live from Droid `0.219.0` in a non-interactive `droid exec` run. Every event
carries `session_id`, `cwd`, `transcript_path`, and `permission_mode` (`auto-high` under
`--auto high`, `off` at session start):

```json
{
  "source": "droid",
  "hook_type": "PreToolUse",
  "input_data": {
    "hook_event_name": "PreToolUse",
    "session_id": "ac313540-534b-48a7-96fb-06e72acdda33",
    "cwd": "/path/to/project",
    "transcript_path": "/path/to/.factory/sessions/<project>/<session>.jsonl",
    "permission_mode": "auto-high",
    "tool_name": "Execute",
    "tool_input": {
      "command": "echo gobby-probe-ok",
      "riskLevel": "low",
      "riskLevelReason": "This echo command only prints a fixed string and has no side effects.",
      "summary": "Echo gobby probe marker"
    }
  }
}
```

Fields beyond that common set, by event:

| Event | Additional fields |
| --- | --- |
| `PreToolUse` | `tool_name`, `tool_input` (`Execute`: `command`, `riskLevel`, `riskLevelReason`, `summary`) |
| `PostToolUse` | `tool_name`, `tool_input`, `tool_response` |
| `Stop` | `elapsed_time` (ms), `message_id`, `stop_hook_active`, `tool_execution_count` |
| `SessionStart` | `source` (e.g. `startup`), `CLAUDE_ENV_FILE` |
| `SessionEnd` | `message_id`, `message_count`, `reason`, `session_duration_ms` |

Droid also exports `CLAUDE_*`, `DROID_*`, and `FACTORY_*` project-directory variables into
the hook process environment, including `CLAUDE_ENV_FILE` and `CLAUDE_PROJECT_DIR`.

### Grok

Grok `0.2.67` normal `PostToolUse` payloads include a definitive shell exit code:

```json
{
  "source": "grok",
  "hook_type": "post_tool_use",
  "input_data": {
    "hookEventName": "post_tool_use",
    "sessionId": "grok-session-123",
    "toolName": "run_terminal_command",
    "toolInput": {
      "command": "uv run pytest tests/workflows/test_hooks.py -q"
    },
    "toolResult": {
      "exit_code": 7,
      "output_for_prompt": "exit: 7\n"
    }
  }
}
```

The adapter normalizes `toolInput` and `toolResult` to `tool_input` and
`tool_output`. Task close reads these normalized outcomes from the transcript:
exit `0` is clean, a nonzero exit is failed, and a missing definitive exit code
is unknown. Unknown outcomes cannot satisfy the checklist. Rerun the command
through a CLI shell tool that reports a definitive exit code.

## Unified HookEvent Model

All adapter events are normalized to the internal dataclass in
`src/gobby/hooks/events.py`. Its core fields are shown below; the implementation
also carries turn disposition, wait identity/resolution, provider turn key,
request ID and protocol cursor.

```python
@dataclass
class HookEvent:
    event_type: HookEventType
    session_id: str
    source: SessionSource
    timestamp: datetime
    data: dict[str, Any]

    machine_id: str | None = None
    cwd: str | None = None
    user_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

`session_id` is the provider's external session ID. Gobby's platform session ID
is attached through request headers or adapter metadata when available.

## Unified HookResponse Model

Workflow evaluation returns a provider-neutral `HookResponse`:

```python
@dataclass
class HookResponse:
    decision: Literal["allow", "deny", "ask", "block", "modify"] = "allow"
    context: str | None = None
    system_message: str | None = None
    reason: str | None = None
    display_content: str | None = None

    modified_input: dict[str, Any] | None = None
    auto_approve: bool = False
    permission_decision: Literal["allow", "deny"] | None = None
    updated_permissions: list[dict[str, Any]] | None = None

    retry: bool = False
    watch_paths: list[str] | None = None
    worktree_path: str | None = None
    elicitation_action: Literal["accept", "decline", "cancel"] | None = None
    elicitation_content: dict[str, Any] | None = None
    elicitation_error: str | None = None

    modify_args: dict[str, Any] | None = None
    trigger_action: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

Adapters translate these fields into the native response schema for each CLI.

## Response Translation

### Context Fields

| HookResponse Field | Claude Code | Qwen | AGY | Codex | Droid |
| --- | --- | --- | --- | --- | --- |
| `context` | `hookSpecificOutput.additionalContext` when supported | `hookSpecificOutput.additionalContext` when supported | `injectSteps.ephemeralMessage` on `PreInvocation` and `PostInvocation` | `additionalContext` or `systemMessage`, depending on hook | `hookSpecificOutput.additionalContext` when supported |
| `system_message` | Top-level `systemMessage`, except startup context is injected once | Top-level `systemMessage`, except startup context is injected once | `injectSteps.userMessage` on `PreInvocation` and `PostInvocation` | `systemMessage` for `PreToolUse` and `Stop` | Top-level `systemMessage`, except startup context is injected once |

### Blocking And Tool Control

| HookResponse Field | Native Effect |
| --- | --- |
| `decision="block"` or `decision="deny"` | Provider-specific block/deny response |
| `permission_decision` | Pre-tool permission decision (`allow`, `deny`, or adapter-supported ask behavior) |
| `modified_input` | Provider-supported tool input rewrite; Codex applies rewrites through dispatch enforcement |
| `auto_approve` | Converts to provider permission allow where supported |
| `updated_permissions` | Claude permission-request permission updates |
| `retry` | Claude `PermissionDenied` retry response |
| `watch_paths` | Claude dynamic `FileChanged` watch paths |
| `worktree_path` | Claude `WorktreeCreate` output |
| `elicitation_*` | Claude elicitation response fields |
| `display_content` | Claude `MessageDisplay` replacement delta |
| `modify_args` | ACP adapter `BeforeModel.llm_request` or `BeforeToolSelection.toolConfig`; not Qwen's native terminal hook surface |

For AGY `PreToolUse`, the supported decisions are `allow`, `deny`, `ask`, and
`deny_unless_prior_grant`; `modified_input` maps to `overwrite`.
`PostInvocation` can return `terminationBehavior`, while Stop blocking maps to
`decision: continue`. Gobby never emits schema-present `force_ask` because it is
unmeasured, never emits `permissionOverrides` because headless auto-deny wins, and
never emits `injectSteps.toolCall` because the provider treats it as fatal.

## Integration Example

Use the managed dispatcher in provider hook configuration:

```text
ghook --gobby-owned --cli=claude --type=pre-tool-use
```

The provider supplies its native JSON on stdin. Do not replace this with a
minimal HTTP script: authentication, spool durability, capability negotiation,
duplicate handling, provider exit/output mapping and delivery receipts all belong
to the transport contract. See [ghook](./ghook-user-guide.md) for installation,
diagnose mode and failure recovery. Operator `gobby hooks test` is a synthetic
diagnostic and does not establish that a real host rendered context.

_Last verified: 2026-09-13_

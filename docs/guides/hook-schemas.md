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
| Gemini CLI | PascalCase (`SessionStart`) | `session_id` | HTTP hook command |
| Qwen CLI | Gemini-compatible PascalCase | `session_id` | HTTP hook command |
| Codex CLI | hooks.json PascalCase (`SessionStart`, `PreToolUse`) | `session_id` | HTTP hook command |
| Droid CLI | PascalCase (`PreToolUse`) | `session_id` | HTTP hook command |

## HTTP Request Envelope

All Gobby-managed hook commands post to:

```text
POST /api/hooks/execute
```

The legacy flat request shape is:

```json
{
  "source": "claude",
  "hook_type": "pre-tool-use",
  "input_data": {
    "session_id": "provider-session-id",
    "machine_id": "machine-uuid",
    "cwd": "/path/to/project"
  }
}
```

`source` is required and must be one of `claude`, `gemini`, `qwen`, `codex`, or
`droid`. `hook_type` is the provider hook name that the selected adapter
understands.

The endpoint also accepts an explicit schema-versioned envelope:

```json
{
  "schema_version": 1,
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

`schema_version` is the discriminator. If it is present, it must be `1`;
otherwise the request is treated as the legacy flat shape.

## Native To Workflow Mapping

### Semantic Rule Events

The workflow engine resolves raw provider events into semantic authoring events:

| Semantic Event | Raw Events That Trigger It | Use For |
| --- | --- | --- |
| `turn_start` | `before_agent` | Prompt-time context injection, per-turn setup |
| `turn_end` | `after_agent`, `stop` | Stop gates, task/commit checks, end-of-turn cleanup |

Use provider-specific names only when writing adapter code, testing native hook
transport, or documenting low-level payloads.

### Claude Code

Claude settings use PascalCase hook keys, but Gobby's installed `ghook` command
passes kebab-case hook types to the daemon.

| Native Hook Type | Hook Event Name | Raw Workflow Event | Semantic Event |
| --- | --- | --- | --- |
| `session-start` | `SessionStart` | `session_start` | `session_start` |
| `instructions-loaded` | `InstructionsLoaded` | `instructions_loaded` | raw only |
| `user-prompt-submit` | `UserPromptSubmit` | `before_agent` | `turn_start` |
| `pre-tool-use` | `PreToolUse` | `before_tool` | `before_tool` |
| `permission-request` | `PermissionRequest` | `permission_request` | `permission_request` |
| `post-tool-use` | `PostToolUse` | `after_tool` | `after_tool` |
| `post-tool-use-failure` | `PostToolUseFailure` | `after_tool` | `after_tool` |
| `permission-denied` | `PermissionDenied` | `permission_denied` | `permission_denied` |
| `notification` | `Notification` | `notification` | `notification` |
| `subagent-start` | `SubagentStart` | `subagent_start` | `subagent_start` |
| `subagent-stop` | `SubagentStop` | `subagent_stop` | `subagent_stop` |
| `task-created` | `TaskCreated` | `task_created` | `task_created` |
| `task-completed` | `TaskCompleted` | `task_completed` | `task_completed` |
| `stop` | `Stop` | `stop` | `turn_end` |
| `stop-failure` | `StopFailure` | `stop_failure` | `stop_failure` |
| `teammate-idle` | `TeammateIdle` | `teammate_idle` | `teammate_idle` |
| `config-change` | `ConfigChange` | `config_change` | `config_change` |
| `cwd-changed` | `CwdChanged` | `cwd_changed` | `cwd_changed` |
| `file-changed` | `FileChanged` | `file_changed` | `file_changed` |
| `worktree-create` | `WorktreeCreate` | `worktree_create` | `worktree_create` |
| `worktree-remove` | `WorktreeRemove` | `worktree_remove` | `worktree_remove` |
| `pre-compact` | `PreCompact` | `pre_compact` | `pre_compact` |
| `post-compact` | `PostCompact` | `post_compact` | `post_compact` |
| `session-end` | `SessionEnd` | `session_end` | `session_end` |
| `elicitation` | `Elicitation` | `elicitation` | `elicitation` |
| `elicitation-result` | `ElicitationResult` | `elicitation_result` | `elicitation_result` |

### Gemini And Qwen

Qwen uses the Gemini-compatible adapter and hook template.

| Native Hook | Raw Workflow Event | Semantic Event |
| --- | --- | --- |
| `SessionStart` | `session_start` | `session_start` |
| `SessionEnd` | `session_end` | `session_end` |
| `BeforeAgent` | `before_agent` | `turn_start` |
| `AfterAgent` | `after_agent` | `turn_end` |
| `BeforeTool` | `before_tool` | `before_tool` |
| `AfterTool` | `after_tool` | `after_tool` |
| `BeforeToolSelection` | `before_tool_selection` | `before_tool_selection` |
| `BeforeModel` | `before_model` | `before_model` |
| `AfterModel` | `after_model` | `after_model` |
| `PreCompress` | `pre_compact` | `pre_compact` |
| `Notification` | `notification` | `notification` |

### Codex

Gobby-managed Codex terminal installs use hooks.json-style PascalCase hook
events posted through `ghook`. Older app-server/WebSocket paths exist for chat
integration, but they are not the installed terminal hook contract.

| Native Hook | Raw Workflow Event | Semantic Event |
| --- | --- | --- |
| `SessionStart` | `session_start` | `session_start` |
| `UserPromptSubmit` | `before_agent` | `turn_start` |
| `PreToolUse` | `before_tool` | `before_tool` |
| `PostToolUse` | `after_tool` | `after_tool` |
| `Stop` | `stop` | `turn_end` |

Codex `PreToolUse` and `Stop` responses use `systemMessage` for context; Codex
does not accept `additionalContext` for those hooks.

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

Shell-like tools normalize to `Bash`. Common Gemini/Qwen tool names also map to
Claude-style names such as `Read`, `Write`, `Edit`, `Glob`, and `Grep`.

## Provider Payload Examples

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

### Gemini And Qwen

```json
{
  "source": "gemini",
  "hook_type": "BeforeAgent",
  "input_data": {
    "hook_event_name": "BeforeAgent",
    "session_id": "gemini-session-123",
    "cwd": "/path/to/project",
    "timestamp": "2026-05-07T15:00:00Z",
    "prompt": "Refresh the hook schema guide"
  }
}
```

```json
{
  "source": "qwen",
  "hook_type": "BeforeTool",
  "input_data": {
    "hook_event_name": "BeforeTool",
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

```json
{
  "source": "droid",
  "hook_type": "UserPromptSubmit",
  "input_data": {
    "hook_event_name": "UserPromptSubmit",
    "session_id": "droid-session-123",
    "cwd": "/path/to/project",
    "user_prompt": "Refresh the hook schema guide"
  }
}
```

## Unified HookEvent Model

All adapter events are normalized to this internal dataclass:

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

| HookResponse Field | Claude Code | Gemini/Qwen | Codex | Droid |
| --- | --- | --- | --- | --- |
| `context` | `hookSpecificOutput.additionalContext` when supported | `hookSpecificOutput.additionalContext` when supported | `additionalContext` or `systemMessage`, depending on hook | `hookSpecificOutput.additionalContext` when supported |
| `system_message` | Top-level `systemMessage`, except startup context is injected once | Top-level `systemMessage`, except startup context is injected once | `systemMessage` for `PreToolUse` and `Stop` | Top-level `systemMessage`, except startup context is injected once |

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
| `modify_args` | Gemini/Qwen `BeforeModel.llm_request` or `BeforeToolSelection.toolConfig` |

## Integration Example

Custom dispatchers should use the shared `/api/hooks/execute` endpoint and pass
the provider source explicitly:

```python
#!/usr/bin/env python3
import json
import sys

import requests

GOBBY_URL = "http://127.0.0.1:60887/api/hooks/execute"


def main() -> None:
    source = sys.argv[1]
    hook_type = sys.argv[2]
    input_data = json.load(sys.stdin)

    response = requests.post(
        GOBBY_URL,
        json={
            "source": source,
            "hook_type": hook_type,
            "input_data": input_data,
        },
        timeout=5,
    )
    result = response.json()
    print(json.dumps(result))

    if source in {"gemini", "qwen"}:
        sys.exit(0)
    sys.exit(0 if result.get("continue", True) else 1)


if __name__ == "__main__":
    main()
```

Gemini and Qwen communicate block decisions in JSON; their hook commands should
exit `0` so the CLI treats the hook response as successful rather than a hook
process failure.

_Last verified: 2026-05-07_

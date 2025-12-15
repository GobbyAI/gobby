# Hook Schemas

This document describes the hook event types, payloads, and response formats supported by Gobby across all CLI integrations.

## Overview

Gobby uses a unified internal event model (`HookEvent`) that normalizes hooks from multiple AI CLIs:

| CLI | Hook Format | Session ID Field | Integration |
|-----|-------------|------------------|-------------|
| Claude Code | kebab-case (`session-start`) | `session_id` | HTTP hooks |
| Gemini CLI | PascalCase (`SessionStart`) | `session_id` | HTTP hooks |
| Codex CLI | JSON-RPC (`thread/started`) | `threadId` | WebSocket events |

## Event Types

### Unified Event Types

| Internal Type | Claude Code | Gemini CLI | Codex CLI |
|--------------|-------------|------------|-----------|
| `SESSION_START` | `session-start` | `SessionStart` | `thread/started` |
| `SESSION_END` | `session-end` | `SessionEnd` | `thread/archive` |
| `BEFORE_AGENT` | `user-prompt-submit` | `BeforeAgent` | `turn/started` |
| `AFTER_AGENT` | `stop` | `AfterAgent` | `turn/completed` |
| `BEFORE_TOOL` | `pre-tool-use` | `BeforeTool` | `item/*/requestApproval` |
| `AFTER_TOOL` | `post-tool-use` | `AfterTool` | `item/completed` |
| `BEFORE_TOOL_SELECTION` | - | `BeforeToolSelection` | - |
| `BEFORE_MODEL` | - | `BeforeModel` | - |
| `AFTER_MODEL` | - | `AfterModel` | - |
| `PRE_COMPACT` | `pre-compact` | `PreCompress` | - |
| `SUBAGENT_START` | `subagent-start` | - | - |
| `SUBAGENT_STOP` | `subagent-stop` | - | - |
| `PERMISSION_REQUEST` | `permission-request` | - | - |
| `NOTIFICATION` | `notification` | `Notification` | - |

---

## Claude Code Hooks

### Request Format

All Claude Code hooks use this wrapper format:

```json
{
  "hook_type": "session-start",
  "input_data": {
    "session_id": "abc123-def456",
    "machine_id": "uuid-string",
    "cwd": "/path/to/project",
    "transcript_path": "/path/to/.claude/projects/.../transcript.jsonl",
    ...hook-specific fields
  }
}
```

### Response Format

```json
{
  "continue": true,
  "decision": "approve",
  "stopReason": "optional reason if blocked",
  "systemMessage": "optional user-visible message",
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "context injected into Claude"
  }
}
```

### Hook Payloads

#### session-start

Fired when a new Claude Code session begins.

```json
{
  "hook_type": "session-start",
  "input_data": {
    "session_id": "abc123-def456",
    "machine_id": "machine-uuid",
    "cwd": "/path/to/project",
    "transcript_path": "/path/to/.claude/projects/.../transcript.jsonl",
    "source": "startup|clear|resume"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Claude Code's session identifier |
| `machine_id` | string | Unique machine identifier |
| `cwd` | string | Current working directory |
| `transcript_path` | string | Path to JSONL transcript file |
| `source` | string | How session started: `startup`, `clear`, or `resume` |

#### session-end

Fired when a Claude Code session ends.

```json
{
  "hook_type": "session-end",
  "input_data": {
    "session_id": "abc123-def456",
    "transcript_path": "/path/to/.claude/projects/.../transcript.jsonl"
  }
}
```

#### user-prompt-submit

Fired before processing a user's prompt.

```json
{
  "hook_type": "user-prompt-submit",
  "input_data": {
    "session_id": "abc123-def456",
    "prompt": "User's message text",
    "transcript_path": "/path/to/.claude/projects/.../transcript.jsonl"
  }
}
```

#### stop

Fired when the agent stops responding.

```json
{
  "hook_type": "stop",
  "input_data": {
    "session_id": "abc123-def456",
    "stop_reason": "end_turn|tool_use|user_interrupt"
  }
}
```

#### pre-tool-use

Fired before a tool is executed. Can block tool execution.

```json
{
  "hook_type": "pre-tool-use",
  "input_data": {
    "session_id": "abc123-def456",
    "tool_name": "Bash",
    "tool_input": {
      "command": "ls -la"
    }
  }
}
```

Response can block:
```json
{
  "continue": false,
  "decision": "block",
  "stopReason": "Command not allowed by policy"
}
```

#### post-tool-use / post-tool-use-failure

Fired after tool execution completes.

```json
{
  "hook_type": "post-tool-use",
  "input_data": {
    "session_id": "abc123-def456",
    "tool_name": "Bash",
    "tool_input": {"command": "ls -la"},
    "tool_output": "file1.txt\nfile2.txt"
  }
}
```

`post-tool-use-failure` has the same schema but indicates the tool failed.

#### pre-compact

Fired before context compaction occurs.

```json
{
  "hook_type": "pre-compact",
  "input_data": {
    "session_id": "abc123-def456"
  }
}
```

#### permission-request

Fired when Claude Code requests permission for a sensitive operation. Can approve or deny the request.

```json
{
  "hook_type": "permission-request",
  "input_data": {
    "session_id": "abc123-def456",
    "resource": "file",
    "action": "write",
    "target": "/etc/hosts",
    "reason": "Need to add local DNS entry"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Claude Code's session identifier |
| `resource` | string | Resource type: `file`, `directory`, or `command` |
| `action` | string | Requested action: `read`, `write`, or `execute` |
| `target` | string | Path or identifier of the target resource |
| `reason` | string | (Optional) Explanation for why permission is needed |

Response can deny:
```json
{
  "continue": false,
  "decision": "block",
  "stopReason": "Access to system files not permitted"
}
```

#### subagent-start / subagent-stop

Fired when subagents (Task tool) spawn or complete.

```json
{
  "hook_type": "subagent-start",
  "input_data": {
    "session_id": "abc123-def456",
    "agent_id": "parent-agent-id",
    "subagent_id": "spawned-agent-id"
  }
}
```

#### notification

System notifications (e.g., waiting for user input).

```json
{
  "hook_type": "notification",
  "input_data": {
    "session_id": "abc123-def456",
    "notification_type": "waiting_for_input|error|info"
  }
}
```

---

## Gemini CLI Hooks

### Request Format

Gemini CLI hooks use this wrapper format (via hook_dispatcher.py):

```json
{
  "source": "gemini",
  "hook_type": "SessionStart",
  "input_data": {
    "hook_event_name": "SessionStart",
    "session_id": "gemini-session-123",
    "cwd": "/path/to/project",
    "timestamp": "2025-01-15T10:30:00Z",
    ...hook-specific fields
  }
}
```

### Response Format

```json
{
  "decision": "allow",
  "reason": "optional reason",
  "systemMessage": "optional user-visible message",
  "hookSpecificOutput": {
    "additionalContext": "context to inject",
    "llm_request": {},
    "toolConfig": {}
  }
}
```

Exit codes: `0` = allow, `2` = deny

### Hook Payloads

#### SessionStart / SessionEnd

```json
{
  "hook_event_name": "SessionStart",
  "session_id": "gemini-session-123",
  "cwd": "/path/to/project",
  "timestamp": "2025-01-15T10:30:00Z"
}
```

#### BeforeAgent / AfterAgent

```json
{
  "hook_event_name": "BeforeAgent",
  "session_id": "gemini-session-123",
  "prompt": "User's message"
}
```

#### BeforeTool / AfterTool

```json
{
  "hook_event_name": "BeforeTool",
  "session_id": "gemini-session-123",
  "tool_name": "RunShellCommand",
  "tool_input": {
    "command": "ls -la"
  }
}
```

**Tool Name Mapping:**
| Gemini Name | Normalized Name |
|-------------|-----------------|
| `RunShellCommand` | `Bash` |
| `ReadFile`, `ReadFileTool` | `Read` |
| `WriteFile`, `WriteFileTool` | `Write` |
| `EditFile`, `EditFileTool` | `Edit` |
| `GlobTool` | `Glob` |
| `GrepTool` | `Grep` |

#### BeforeToolSelection (Gemini-only)

Allows modifying tool configuration before selection.

```json
{
  "hook_event_name": "BeforeToolSelection",
  "session_id": "gemini-session-123",
  "available_tools": ["ReadFile", "WriteFile", "RunShellCommand"]
}
```

Response can modify tool config:
```json
{
  "decision": "allow",
  "hookSpecificOutput": {
    "toolConfig": {
      "disabled_tools": ["RunShellCommand"]
    }
  }
}
```

#### BeforeModel / AfterModel (Gemini-only)

Allows modifying LLM requests.

```json
{
  "hook_event_name": "BeforeModel",
  "session_id": "gemini-session-123",
  "llm_request": {
    "model": "gemini-pro",
    "messages": [...]
  }
}
```

Response can modify request:
```json
{
  "decision": "allow",
  "hookSpecificOutput": {
    "llm_request": {
      "temperature": 0.7
    }
  }
}
```

#### PreCompress

Fired before context compression.

```json
{
  "hook_event_name": "PreCompress",
  "session_id": "gemini-session-123"
}
```

---

## Codex CLI Events

Codex uses WebSocket JSON-RPC notifications (not HTTP hooks).

### Event Format

```json
{
  "method": "thread/started",
  "params": {
    "thread": {
      "id": "thr_abc123",
      "preview": "First message preview",
      "createdAt": 1705312200,
      "modelProvider": "openai"
    }
  }
}
```

### Approval Response Format

```json
{
  "decision": "accept"
}
```

Values: `accept` | `decline`

### Event Payloads

#### thread/started

New conversation thread created.

```json
{
  "method": "thread/started",
  "params": {
    "thread": {
      "id": "thr_abc123",
      "preview": "Help me write a function",
      "createdAt": 1705312200,
      "modelProvider": "openai"
    }
  }
}
```

#### thread/archive

Thread archived/ended.

```json
{
  "method": "thread/archive",
  "params": {
    "threadId": "thr_abc123"
  }
}
```

#### turn/started / turn/completed

Conversation turn lifecycle.

```json
{
  "method": "turn/started",
  "params": {
    "threadId": "thr_abc123",
    "turn": {
      "id": "turn_xyz789",
      "status": "in_progress"
    }
  }
}
```

```json
{
  "method": "turn/completed",
  "params": {
    "threadId": "thr_abc123",
    "turn": {
      "id": "turn_xyz789",
      "status": "completed",
      "error": null
    }
  }
}
```

#### item/commandExecution/requestApproval

Command execution requires approval.

```json
{
  "method": "item/commandExecution/requestApproval",
  "params": {
    "threadId": "thr_abc123",
    "itemId": "item_cmd123",
    "turnId": "turn_xyz789",
    "command": "rm -rf /tmp/test",
    "parsedCmd": "rm -rf /tmp/test",
    "reason": "Deleting temporary files",
    "risk": "medium"
  }
}
```

#### item/fileChange/requestApproval

File change requires approval.

```json
{
  "method": "item/fileChange/requestApproval",
  "params": {
    "threadId": "thr_abc123",
    "itemId": "item_file123",
    "turnId": "turn_xyz789",
    "changes": [
      {
        "path": "/path/to/file.py",
        "type": "modify",
        "content": "new content"
      }
    ],
    "reason": "Updating configuration"
  }
}
```

#### item/completed

Item (tool execution) completed.

```json
{
  "method": "item/completed",
  "params": {
    "threadId": "thr_abc123",
    "item": {
      "id": "item_cmd123",
      "type": "commandExecution",
      "status": "completed"
    }
  }
}
```

Item types: `commandExecution`, `fileChange`, `mcpToolCall`, `message`

---

## Unified HookEvent Model

All hooks are normalized to this internal model:

```python
@dataclass
class HookEvent:
    event_type: HookEventType      # SESSION_START, BEFORE_TOOL, etc.
    session_id: str                # CLI's session/thread ID
    source: SessionSource          # CLAUDE, GEMINI, CODEX
    timestamp: datetime            # When event occurred
    data: dict                     # Event-specific payload

    machine_id: str | None         # Machine identifier
    cwd: str | None                # Working directory
    user_id: str | None            # Platform user (future)
    project_id: str | None         # Platform project (future)
    workflow_id: str | None        # Workflow ID (future)
    metadata: dict                 # Adapter-specific data
```

## HookResponse Model

All responses use this unified model before CLI-specific translation:

```python
@dataclass
class HookResponse:
    decision: Literal["allow", "deny", "ask"] = "allow"
    context: str | None = None           # Inject into AI context
    system_message: str | None = None    # User-visible message
    reason: str | None = None            # Explanation for decision

    modify_args: dict | None = None      # Modify tool/model args
    trigger_action: str | None = None    # Trigger CLI action
    metadata: dict                       # Adapter-specific data
```

---

## Integration Examples

### Claude Code Hook Dispatcher

Location: `~/.claude/hooks/hook_dispatcher.py`

```python
#!/usr/bin/env python3
import json
import sys
import requests

GOBBY_URL = "http://localhost:8765/hooks/claude"

def main():
    hook_type = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    input_data = json.load(sys.stdin)

    response = requests.post(GOBBY_URL, json={
        "hook_type": hook_type,
        "input_data": input_data
    }, timeout=5)

    result = response.json()
    print(json.dumps(result))
    sys.exit(0 if result.get("continue", True) else 1)

if __name__ == "__main__":
    main()
```

### Gemini CLI Hook Dispatcher

Location: `~/.gemini/hooks/hook_dispatcher.py`

```python
#!/usr/bin/env python3
import json
import sys
import requests

GOBBY_URL = "http://localhost:8765/hooks/gemini"

def main():
    hook_type = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    input_data = json.load(sys.stdin)

    response = requests.post(GOBBY_URL, json={
        "source": "gemini",
        "hook_type": hook_type,
        "input_data": input_data
    }, timeout=5)

    result = response.json()
    print(json.dumps(result))

    # Exit code: 0 = allow, 2 = deny
    decision = result.get("decision", "allow")
    sys.exit(0 if decision == "allow" else 2)

if __name__ == "__main__":
    main()
```

### Codex Integration

Codex uses WebSocket events via `CodexAppServerClient`. No hook dispatcher needed.

```python
from gobby.adapters.codex import CodexAdapter
from gobby.codex.client import CodexAppServerClient

# Create client and adapter
client = CodexAppServerClient()
adapter = CodexAdapter(hook_manager=hook_manager)

# Attach adapter to receive events
adapter.attach_to_client(client)

# Start client (connects to codex app-server)
await client.start()

# Events are automatically forwarded to HookManager
```

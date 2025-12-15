# Claude Code Hook Schemas Reference

This document defines the input and output schemas for all Claude Code hooks.

## Table of Contents

- [SessionStart Hook](#sessionstart-hook)
- [SessionEnd Hook](#sessionend-hook)
- [UserPromptSubmit Hook](#userpromptsubmit-hook)
- [PreToolUse Hook](#pretooluse-hook)
- [PostToolUse Hook](#posttooluse-hook)
- [PreCompact Hook](#precompact-hook)
- [Stop Hook](#stop-hook)
- [SubagentStart Hook](#subagentstart-hook)
- [SubagentStop Hook](#subagentstop-hook)
- [Notification Hook](#notification-hook)

---

## SessionStart Hook

**Trigger**: When a Claude Code session starts (startup, resume, `/clear`, or compact)

### Input Schema

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "transcript_path": "/Users/josh/.claude/projects/my-project/session.jsonl",
  "hook_event_name": "SessionStart",
  "source": "startup",
  "machine_id": "847EB0AC-1A34-51F1-A15C-75F37C9ECE90"
}
```

**Fields:**

- `session_id` (string, optional): Unique session identifier (UUID format). **Note**: Only available when `source` is "resume", "clear", or "compact". On initial "startup", session_id is not yet generated.
- `transcript_path` (string, required): Path to the JSONL conversation transcript file
- `hook_event_name` (string, required): Always "SessionStart"
- `source` (string, required): How session started - one of:
  - `"startup"`: Normal Claude Code launch (session_id not yet available)
  - `"resume"`: From `--resume`, `--continue`, or `/resume` commands
  - `"clear"`: From `/clear` command (new session)
  - `"compact"`: From auto or manual compact operations
- `machine_id` (string, optional): Unique machine identifier

### Output Schema

**Option 1: Context Injection (Recommended)**

```json
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "## Session Started\n\n- **Session ID**: `550e8400-...`\n- Machine ID: `847EB0AC-...`"
  }
}
```

**Option 2: Simple Status**

```json
{
  "status": "session_started"
}
```

**Fields:**

- `hookSpecificOutput.additionalContext` (string, optional): Markdown text to inject into Claude's context
- `status` (string, optional): Hook execution status

**Exit Codes:**

- `0`: Success (stdout added to Claude's context)
- `1`: Error (stderr logged, hook continues)

---

## SessionEnd Hook

**Trigger**: When a Claude Code session ends

### Input Schema

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "hook_event_name": "SessionEnd",
  "reason": "clear",
  "machine_id": "847EB0AC-1A34-51F1-A15C-75F37C9ECE90"
}
```

**Fields:**

- `session_id` (string, required): Session identifier that is ending
- `hook_event_name` (string, required): Always "SessionEnd"
- `reason` (string, required): Why session ended - one of:
  - `"clear"`: User executed `/clear` command
  - `"logout"`: User logged out
  - `"prompt_input_exit"`: User exited via prompt
  - `"other"`: Other reasons
- `machine_id` (string, optional): Unique machine identifier

### Output Schema

```json
{
  "status": "session_ended"
}
```

**Fields:**

- `status` (string, optional): Hook execution status

**Exit Codes:**

- `0`: Success
- `1`: Error (logged but ignored)

---

## UserPromptSubmit Hook

**Trigger**: When user submits a prompt (before Claude processes it)

### Input Schema

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "prompt": "Write a function to calculate factorial",
  "transcript_path": "/Users/josh/.claude/projects/my-project/session.jsonl",
  "cwd": "/Users/josh/projects/my-project",
  "permission_mode": "default",
  "hook_event_name": "UserPromptSubmit",
  "machine_id": "847EB0AC-1A34-51F1-A15C-75F37C9ECE90"
}
```

**Fields:**

- `session_id` (string, required): Current session identifier
- `prompt` (string, required): The user's submitted text
- `transcript_path` (string, required): Path to conversation transcript
- `cwd` (string, required): Current working directory
- `permission_mode` (string, required): Permission mode (e.g., "default", "acceptEdits")
- `hook_event_name` (string, required): Always "UserPromptSubmit"
- `machine_id` (string, optional): Unique machine identifier

### Output Schema

**Option 1: Approve (default)**

```json
{
  "status": "approved"
}
```

**Option 2: Reject with feedback**

```json
{
  "status": "rejected",
  "message": "Prompt contains sensitive information"
}
```

**Option 3: Modify prompt**

```json
{
  "status": "approved",
  "modified_prompt": "Enhanced prompt with additional context..."
}
```

**Fields:**

- `status` (string, required): Either "approved" or "rejected"
- `message` (string, optional): Feedback message shown to user (if rejected)
- `modified_prompt` (string, optional): Replacement prompt text

**Exit Codes:**

- `0`: Success, prompt approved
- `2`: Block prompt and show stderr to user
- `1`: Error (logged)

---

## PreToolUse Hook

**Trigger**: Before Claude Code executes a tool

### Input Schema

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "tool_name": "Edit",
  "tool_input": {
    "file_path": "/path/to/file.py",
    "old_string": "foo",
    "new_string": "bar"
  },
  "machine_id": "847EB0AC-1A34-51F1-A15C-75F37C9ECE90"
}
```

**Fields:**

- `session_id` (string, required): Current session identifier
- `tool_name` (string, required): Name of tool about to execute (e.g., "Edit", "Bash", "Read")
- `tool_input` (object, optional): Tool parameters as key-value pairs
- `machine_id` (string, optional): Unique machine identifier

### Output Schema

**Option 1: Allow (default)**

```json
{
  "status": "allowed"
}
```

**Option 2: Block with reason**

```json
{
  "status": "blocked",
  "reason": "Tool not permitted in this context"
}
```

**Fields:**

- `status` (string, required): Either "allowed" or "blocked"
- `reason` (string, optional): Explanation if blocked

**Exit Codes:**

- `0`: Success, tool allowed
- `2`: Block tool execution and show stderr to user
- `1`: Error (logged)

---

## PostToolUse Hook

**Trigger**: After Claude Code executes a tool

### Input Schema

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "tool_name": "Edit",
  "tool_input": {
    "file_path": "/path/to/file.py",
    "old_string": "foo",
    "new_string": "bar"
  },
  "tool_output": {
    "success": true,
    "message": "File updated successfully"
  },
  "machine_id": "847EB0AC-1A34-51F1-A15C-75F37C9ECE90"
}
```

**Fields:**

- `session_id` (string, required): Current session identifier
- `tool_name` (string, required): Name of tool that executed
- `tool_input` (object, optional): Tool parameters that were used
- `tool_output` (object, optional): Result returned by the tool
- `machine_id` (string, optional): Unique machine identifier

### Output Schema

```json
{
  "status": "processed"
}
```

**Fields:**

- `status` (string, optional): Hook execution status

**Exit Codes:**

- `0`: Success
- `1`: Error (logged but tool execution continues)

---

## PreCompact Hook

**Trigger**: Before Claude Code compacts the conversation context

### Input Schema

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "transcript_path": "/Users/josh/.claude/projects/my-project/session.jsonl",
  "hook_event_name": "PreCompact",
  "trigger": "auto",
  "custom_instructions": null,
  "machine_id": "847EB0AC-1A34-51F1-A15C-75F37C9ECE90"
}
```

**Fields:**

- `session_id` (string, required): Current session identifier
- `transcript_path` (string, required): Path to conversation transcript
- `hook_event_name` (string, required): Always "PreCompact"
- `trigger` (string, required): How compact was triggered - one of:
  - `"auto"`: Automatic compact (context window limit)
  - `"manual"`: User manually triggered compact
- `custom_instructions` (string, optional): Custom instructions if manually triggered
- `machine_id` (string, optional): Unique machine identifier

### Output Schema

```json
{
  "status": "ready_to_compact"
}
```

**Fields:**

- `status` (string, optional): Hook execution status

**Exit Codes:**

- `0`: Success
- `1`: Error (logged but compact continues)

---

## Stop Hook

**Trigger**: When Claude Code session is stopping

### Input Schema

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "reason": "user_exit",
  "metadata": {},
  "machine_id": "847EB0AC-1A34-51F1-A15C-75F37C9ECE90"
}
```

**Fields:**

- `session_id` (string, required): Session identifier being stopped
- `reason` (string, optional): Reason for stopping
- `metadata` (object, optional): Additional context about the stop
- `machine_id` (string, optional): Unique machine identifier

### Output Schema

```json
{
  "status": "stopped"
}
```

**Fields:**

- `status` (string, optional): Hook execution status

**Exit Codes:**

- `0`: Success
- `1`: Error (logged)

---

## SubagentStart Hook

**Trigger**: When a subagent (spawned via Task tool) is starting

### Input Schema

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "subagent_id": "agent-abc123",
  "agent_id": "task-executor",
  "agent_transcript_path": "/Users/josh/.claude/projects/my-project/.claude/task-abc123.jsonl",
  "metadata": {},
  "machine_id": "847EB0AC-1A34-51F1-A15C-75F37C9ECE90"
}
```

**Fields:**

- `session_id` (string, required): Current session identifier (stored as `external_id` in Gobby)
- `subagent_id` (string, required): Unique identifier for the subagent instance
- `agent_id` (string, optional): Type/name of the subagent (e.g., "task-executor", "Explore", "Plan")
- `agent_transcript_path` (string, optional): Path to the subagent's conversation transcript
- `metadata` (object, optional): Additional context about the subagent
- `machine_id` (string, optional): Unique machine identifier

### Output Schema

```json
{
  "status": "subagent_started"
}
```

**Fields:**

- `status` (string, optional): Hook execution status

**Exit Codes:**

- `0`: Success
- `1`: Error (logged)

**Use Cases:**

- Track when subagents are created
- Initialize subagent-specific state
- Inject context into subagent's conversation
- Log subagent creation for analytics

---

## SubagentStop Hook

**Trigger**: When a subagent (spawned via Task tool) is stopping

### Input Schema

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "subagent_id": "agent-abc123",
  "agent_id": "task-executor",
  "agent_transcript_path": "/Users/josh/.claude/projects/my-project/.claude/task-abc123.jsonl",
  "reason": "task_completed",
  "metadata": {},
  "machine_id": "847EB0AC-1A34-51F1-A15C-75F37C9ECE90"
}
```

**Fields:**

- `session_id` (string, required): Current session identifier (stored as `external_id` in Gobby)
- `subagent_id` (string, required): Unique identifier for the subagent instance
- `agent_id` (string, optional): Type/name of the subagent (e.g., "task-executor", "Explore", "Plan")
- `agent_transcript_path` (string, optional): Path to the subagent's conversation transcript
- `reason` (string, optional): Why subagent is stopping
- `metadata` (object, optional): Additional context
- `machine_id` (string, optional): Unique machine identifier

### Output Schema

```json
{
  "status": "subagent_stopped"
}
```

**Fields:**

- `status` (string, optional): Hook execution status

**Exit Codes:**

- `0`: Success
- `1`: Error (logged)

---

## Notification Hook

**Trigger**: When Claude Code sends a notification

### Input Schema

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "notification_type": "info",
  "message": "Task completed successfully",
  "severity": "info",
  "metadata": {},
  "machine_id": "847EB0AC-1A34-51F1-A15C-75F37C9ECE90"
}
```

**Fields:**

- `session_id` (string, required): Current session identifier
- `notification_type` (string, required): Type of notification
- `message` (string, required): Notification message text
- `severity` (string, optional): Severity level - one of:
  - `"info"`: Informational message
  - `"warning"`: Warning message
  - `"error"`: Error message
- `metadata` (object, optional): Additional context
- `machine_id` (string, optional): Unique machine identifier

### Output Schema

```json
{
  "status": "notification_received"
}
```

**Fields:**

- `status` (string, optional): Hook execution status

**Exit Codes:**

- `0`: Success
- `1`: Error (logged)

---

## Common Patterns

### Exit Codes Summary

| Exit Code | Meaning | Behavior |
|-----------|---------|----------|
| `0` | Success | Hook output processed normally |
| `1` | Error | Error logged, execution continues |
| `2` | Block | Blocks operation and shows stderr to user |

### Context Injection

To inject text into Claude's conversation context, use:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "<HookName>",
    "additionalContext": "Your markdown text here..."
  }
}
```

The `additionalContext` string will be added to Claude's context window and appears in `<system-reminder>` tags.

### Machine ID

The `machine_id` field is a unique, persistent identifier for the machine running Claude Code. It's useful for:

- Tracking sessions across multiple machines
- Machine-specific configuration
- Analytics and logging

Generated using hardware identifiers (MAC address, disk serial, etc.) and cached in `/tmp/gobby_machine_id.txt`.

---

## Implementation Notes

### Hook Execution Flow

1. **Claude Code** → Sends JSON input to hook script via stdin
2. **Hook Script** → Reads stdin, processes data
3. **Hook Script** → Writes JSON output to stdout
4. **Hook Script** → Exits with appropriate exit code
5. **Claude Code** → Processes output based on exit code

### Error Handling

Hooks should handle errors gracefully:

- Log errors to stderr (will appear in hook logs)
- Return appropriate exit code
- Provide helpful error messages to user if needed

### Best Practices

1. **Always validate input**: Check required fields exist before using them
2. **Use exit codes correctly**:
   - `0` for normal operation
   - `2` only when you need to block and notify user
   - `1` for internal errors
3. **Keep processing fast**: Hooks block Claude Code execution
4. **Log useful information**: Use structured logging for debugging
5. **Test with sample data**: Use the schemas above to create test inputs

---

## Testing Hooks

Test hooks manually using echo and piping:

```bash
# Test SessionStart hook
echo '{"session_id": "test-123", "transcript_path": "/tmp/test.jsonl", "hook_event_name": "SessionStart", "source": "startup"}' | \
  python .claude/hooks/session-start.py

# Test UserPromptSubmit hook
echo '{"session_id": "test-123", "prompt": "Hello", "hook_event_name": "UserPromptSubmit"}' | \
  python .claude/hooks/user-prompt-submit.py
```

---

*Last Updated: 2025-11-17*
*Claude Code Version: Compatible with v2.0.43+ (SubagentStart hook support)*

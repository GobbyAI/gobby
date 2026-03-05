# Research: AskUserQuestion Response Mechanism for Web Chat UI

## Context

The web chat UI can display AskUserQuestion tool calls as interactive cards (header chip, question text, option buttons with descriptions) but users cannot respond — options are display-only. This research investigates how the Claude Agent SDK handles AskUserQuestion and how to wire up a response path from the web UI back to the SDK.

## Current Architecture

### Message Flow (no AskUserQuestion support)

```
Frontend (useChat.ts)
  → WebSocket: { type: 'chat_message', content, conversation_id }
    → websocket.py: _handle_chat_message()
      → ChatSession.send_message(content)
        → ClaudeSDKClient.query(content)
        → async for message in client.receive_response():
            AssistantMessage → ToolCallEvent → tool_status(calling)
            UserMessage      → ToolResultEvent → tool_status(completed)
            ResultMessage    → DoneEvent → chat_stream(done=true)
```

### Key Files

| File | Role |
|------|------|
| `src/gobby/servers/chat_session.py` | Wraps ClaudeSDKClient, streams ChatEvents |
| `src/gobby/servers/websocket.py` | WebSocket server, routes messages, streams to frontend |
| `web/src/hooks/useChat.ts` | WebSocket client, message state management |
| `web/src/components/Message.tsx` | Renders messages with tool calls |
| `web/src/components/ToolCallDisplay.tsx` | Renders tool calls; has dedicated AskUserQuestionDisplay |
| `web/src/components/ChatMessages.tsx` | Renders message list, passes props to Message |
| `web/src/App.tsx` | Top-level component, uses useChat hook |

### ChatSession Configuration (current)

```python
# src/gobby/servers/chat_session.py lines 141-150
options = ClaudeAgentOptions(
    system_prompt=system_prompt,
    max_turns=None,
    model=model or "claude-sonnet-4-5",
    allowed_tools=["mcp__gobby__*"],
    permission_mode="bypassPermissions",  # Auto-approves all tools
    cli_path=cli_path,
    mcp_servers=mcp_config if mcp_config is not None else {},
    cwd=cwd,
)
```

No `can_use_tool` callback. No SDK hooks. `bypassPermissions` means the CLI auto-approves everything.

## SDK Analysis

### Package Details

- **Package**: `claude-agent-sdk` (>= 0.1.18)
- **Location**: `.venv/lib/python3.13/site-packages/claude_agent_sdk/`
- **Architecture**: Thin Python wrapper around Claude CLI subprocess
- **Transport**: `SubprocessCLITransport` — bidirectional stdin/stdout with control protocol

### How Tool Execution Works

The SDK does NOT execute tools. The CLI subprocess does. The SDK only observes:

```
CLI subprocess          SDK (Python)
    |                       |
    | ← prompt + options -- | connect(options), query(prompt)
    |                       |
    | -- AssistantMessage → | ToolUseBlock (Claude wants to call a tool)
    |                       |
    | (CLI executes tool)   |
    |                       |
    | -- UserMessage -----→ | ToolResultBlock (tool result)
    |                       |
    | (repeat until done)   |
    |                       |
    | -- ResultMessage ---→ | Final completion signal
```

### AskUserQuestion Is a Built-in CLI Tool

AskUserQuestion is handled by the Claude CLI, not the SDK. In terminal mode, the CLI shows the question and waits for terminal input. In subprocess mode (SDK), the behavior depends on permission configuration.

### The `can_use_tool` Callback (Official SDK Mechanism)

**Source**: https://platform.claude.com/docs/en/agent-sdk/user-input

The `can_use_tool` callback is the official mechanism for handling both tool permissions AND user input (AskUserQuestion). It fires in two cases:

1. **Tool needs approval** — Claude wants to use a tool that isn't auto-approved
2. **Claude asks a question** — Claude calls AskUserQuestion

When `can_use_tool` is set, the SDK automatically sets `permission_prompt_tool_name="stdio"`, which tells the CLI to use the control protocol for permission prompts instead of terminal UI.

#### Official Example (from docs)

```python
async def handle_ask_user_question(input_data: dict) -> PermissionResultAllow:
    """Display Claude's questions and collect user answers."""
    answers = {}
    for q in input_data.get("questions", []):
        print(f"\n{q['header']}: {q['question']}")
        options = q["options"]
        for i, opt in enumerate(options):
            print(f"  {i + 1}. {opt['label']} - {opt['description']}")
        response = input("Your choice: ").strip()
        answers[q["question"]] = parse_response(response, options)

    return PermissionResultAllow(
        updated_input={
            "questions": input_data.get("questions", []),
            "answers": answers,
        }
    )

async def can_use_tool(tool_name: str, input_data: dict, context) -> PermissionResultAllow:
    if tool_name == "AskUserQuestion":
        return await handle_ask_user_question(input_data)
    return PermissionResultAllow(updated_input=input_data)  # Auto-approve others
```

**Key insight**: The answers are provided via `PermissionResultAllow.updated_input`. The callback returns the original questions plus the user's answers in a single dict. This is NOT a tool result — it's a modified tool input that the CLI then processes.

### Control Protocol Flow

When `can_use_tool` is configured:

```
1. Claude generates ToolUseBlock for AskUserQuestion
2. CLI sends AssistantMessage (regular message → SDK message stream)
3. CLI sends can_use_tool control request (control message → _handle_control_request)
4. SDK calls can_use_tool callback with (tool_name, input_data, context)
5. Callback responds with PermissionResultAllow or PermissionResultDeny
6. CLI processes the result
7. CLI sends UserMessage with ToolResultBlock (regular message → SDK message stream)
```

Steps 2 and 3 happen concurrently — regular messages go to the stream, control messages spawn handler tasks.

### SDK Control Protocol Types

```python
# From claude_agent_sdk/types.py
class SDKControlPermissionRequest(TypedDict):
    subtype: Literal["can_use_tool"]
    tool_name: str
    input: dict[str, Any]
    permission_suggestions: list[Any] | None
    blocked_path: str | None

# No tool_call_id in the control request — only tool_name and input
```

### SDK Hook Support

```python
# From claude_agent_sdk/types.py lines 161-170
# Supported hook events (SessionStart/SessionEnd NOT supported):
HookEvent = (
    Literal["PreToolUse"]
    | Literal["PostToolUse"]
    | Literal["UserPromptSubmit"]
    | Literal["Stop"]
    | Literal["SubagentStop"]
    | Literal["PreCompact"]
)
```

**SessionStart and SessionEnd are explicitly unsupported** in the Python SDK due to setup limitations.

### `can_use_tool` vs `permission_mode` Interaction

- `can_use_tool` and `permission_prompt_tool_name` are mutually exclusive (SDK raises ValueError)
- `can_use_tool` automatically sets `permission_prompt_tool_name="stdio"` internally
- `can_use_tool` and `permission_mode` are NOT checked against each other in the SDK code
- However, `permission_mode="bypassPermissions"` likely prevents `can_use_tool` from ever firing because the CLI auto-approves everything without sending control requests
- **Recommendation**: Remove `bypassPermissions` and use `can_use_tool` callback for all permission handling

## Hook System Interaction Analysis

### Session-Lifecycle Hooks and Chat Sessions

The CLI subprocess loads project hooks from `.claude/settings.json` (installed by `gobby install`). This means session-lifecycle hooks DO fire for chat session tool calls via:

```
CLI subprocess → PreToolUse hook → hook_dispatcher.py → Gobby daemon → HookManager → session-lifecycle.yaml
```

### The SessionStart Gap

Since the SDK doesn't fire SessionStart, session-lifecycle variables are never initialized:

| Variable | Default | Purpose |
|----------|---------|---------|
| `enforce_tool_schema_check` | `true` | Progressive discovery |
| `require_task_before_edit` | `true` | Task-before-edit enforcement |
| `unlocked_tools` | (unset) | Schema-unlocked tools list |
| `servers_listed` | (unset) | Whether list_mcp_servers was called |
| `stop_attempts` | (unset) | Stop hook counter |
| `task_claimed` | (unset) | Whether a task is active |

Without initialization, `when` conditions in `on_before_tool` rules evaluate against unset variables, effectively disabling all enforcement.

### Hook and `can_use_tool` Execution Order

Hooks and `can_use_tool` operate at different layers in the CLI's tool execution pipeline:

```
Claude → ToolUseBlock
  → CLI fires PreToolUse hook (session-lifecycle evaluation)
    → If hook blocks: tool blocked, can_use_tool never fires
    → If hook allows: CLI checks permissions
      → CLI sends can_use_tool control request to SDK
        → Our callback fires
```

**They are orthogonal** — hooks enforce workflow rules, `can_use_tool` handles SDK-level permissions and user input. No conflict.

### AskUserQuestion Block Rule

Session-lifecycle.yaml (line 292) blocks AskUserQuestion under specific conditions:

```yaml
- tools: [AskUserQuestion]
  when: "variables.get('stop_attempts', 0) > 0 and task_claimed"
  reason: "Do not ask — act on the hook directive..."
```

For chat sessions with uninitialized variables: `0 > 0 and undefined` → `False` → not blocked.

### SessionStart Failover via `on_before_agent`

The `on_before_agent` trigger maps to `UserPromptSubmit` (Claude) / `BeforeAgent` (Gemini). It fires on every user message. A failover can detect that `on_session_start` never ran and perform equivalent initialization:

**Guard condition**: `not variables.get('session_initialized')`

**Actions to mirror from `on_session_start`**:
- Variable resets: `plan_mode`, `unlocked_tools`, `servers_listed`, `listed_servers`
- `reset_memory_injection_tracking`
- `capture_baseline_dirty_files`
- `memory_sync_import`, `task_sync_import`
- `inject_context` for skills (always_apply), task context, pre-existing error policy
- Set `session_initialized: true`

**Actions to skip** (not applicable for SDK sessions):
- Context handoff (`previous_session_summary`, `compact_handoff`) — no previous Gobby session
- Plan mode recommendation prompt — web chat has own UX
- `pending_context_reset` flag — Gemini-specific

**For CLI sessions**: Add `set_variable session_initialized: true` at end of `on_session_start`. The failover's guard condition evaluates to False → skips.

## Proposed Architecture

### Backend: `can_use_tool` Callback with Async Waiting

```python
# ChatSession additions
_pending_question: dict | None = None
_pending_answer_event: asyncio.Event | None = None
_pending_answers: dict | None = None

async def _can_use_tool(self, tool_name, input_data, context):
    if tool_name == "AskUserQuestion":
        self._pending_question = input_data
        self._pending_answer_event = asyncio.Event()
        await self._pending_answer_event.wait()  # Block until frontend responds
        answers = self._pending_answers
        # Reset state
        self._pending_question = None
        self._pending_answer_event = None
        self._pending_answers = None
        return PermissionResultAllow(
            updated_input={"questions": input_data["questions"], "answers": answers}
        )
    return PermissionResultAllow(updated_input=input_data)

def provide_answer(self, answers: dict):
    self._pending_answers = answers
    if self._pending_answer_event:
        self._pending_answer_event.set()
```

### Synchronization Model

The `can_use_tool` callback runs in an anyio task (spawned by `Query._handle_control_request`). `provide_answer()` is called from the asyncio WebSocket handler. Since anyio on asyncio wraps asyncio tasks, `asyncio.Event` works across both contexts:

- `asyncio.Event.wait()` — works from anyio task (backed by asyncio)
- `asyncio.Event.set()` — works from asyncio WebSocket handler

### Full Event Flow

```
 1. User sends message → WebSocket → ChatSession.send_message()
 2. Claude generates AskUserQuestion ToolUseBlock
 3. CLI sends AssistantMessage → SDK stream → ChatSession yields ToolCallEvent
 4. WebSocket sends tool_status(calling, tool_name="AskUserQuestion", arguments={questions})
 5. Frontend renders interactive question UI with clickable options
 6. CLI sends can_use_tool control request → SDK spawns handler task
 7. Handler calls ChatSession._can_use_tool("AskUserQuestion", input_data)
 8. Callback creates asyncio.Event, awaits it (blocks)
 9. User selects options, clicks Submit
10. Frontend sends ask_user_response WebSocket message
11. WebSocket handler calls session.provide_answer(answers)
12. asyncio.Event fires → callback resumes
13. Callback returns PermissionResultAllow(updated_input={questions, answers})
14. CLI processes tool result → UserMessage with ToolResultBlock arrives
15. ChatSession yields ToolResultEvent → WebSocket sends tool_status(completed)
16. Claude continues conversation with user's answers
```

### WebSocket Protocol Addition

```json
// Inbound from frontend:
{
    "type": "ask_user_response",
    "conversation_id": "...",
    "tool_call_id": "...",
    "answers": {
        "Which authentication method?": "OAuth 2.0",
        "Which features?": "Linting, Type checking"
    }
}
```

### Frontend Prop Chain

```
App.tsx (useChat → respondToQuestion)
  → ChatMessages (onRespondToQuestion prop)
    → Message (onRespondToQuestion prop)
      → ToolCallDisplay (onRespond prop)
        → AskUserQuestionDisplay (onRespond prop, manages selection state)
```

## Key Design Decisions

1. **No new WebSocket message type for showing questions** — The existing `tool_status` with `status: "calling"` already delivers AskUserQuestion data to the frontend. The `can_use_tool` callback just blocks until answered.

2. **One pending question per session** — Claude processes sequentially; only one AskUserQuestion can be pending at a time per ChatSession. No queue needed.

3. **Remove `bypassPermissions`** — Switch to `can_use_tool` callback that auto-approves all tools except AskUserQuestion. Overhead is negligible (instant return for non-AskUserQuestion tools).

4. **Session-lifecycle failover** — Add `on_before_agent` bootstrap in session-lifecycle.yaml so SDK sessions get the same variable initialization as CLI sessions.

## Open Questions

1. **asyncio.Event vs anyio.Event** — The callback runs in anyio context. Since we're on asyncio backend, `asyncio.Event` should work, but needs runtime validation. If issues arise, may need anyio-native synchronization.

2. **Timeout handling** — What happens if the user never responds? The `can_use_tool` callback would block indefinitely. Consider adding a timeout (e.g., 5 minutes) that returns a denial message to Claude.

3. **Reconnection** — If the user disconnects and reconnects while a question is pending, the ChatSession persists but the frontend doesn't know there's a pending question. May need to send pending question state on reconnection.

4. **Step workflow interaction** — Step workflows may restrict which tools are available per step. If a step blocks AskUserQuestion, the hook blocks it before `can_use_tool` fires (correct behavior). Needs integration testing.

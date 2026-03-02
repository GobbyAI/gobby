# Plan: Unified SDK Agent Execution

## Context

Agent spawning has three broken or outdated execution paths:
- **Headless**: Subprocess with `--dangerously-skip-permissions -p` — no hooks, no MCP proxy, no session lifecycle
- **Embedded**: PTY fork — same problems as headless, unused
- **Terminal**: Tmux subprocess — hooks work via CLI adapter, but live observation requires stream-jsonl parsing from tmux output, which is fragile

The web chat UI already uses `ClaudeSDKClient` (in-process, proper hooks, MCP proxy). All agent modes should converge on the SDK as the unified execution engine. Session resumption (`--resume <id>`) works for all three CLIs (Claude, Gemini, Codex), enabling cross-mode handoffs without transcript parsing.

**New mode taxonomy:**
- **self** — activate workflow on caller session (unchanged)
- **autonomous** — SDK in-process, auto-approve all tools, configurable max_turns
- **interactive** — SDK in-process, human-gated (tool approval, observable via WebSocket) — future follow-up, currently web_chat

## This Plan Covers

1. `AutonomousRunner` — SDK-based fire-and-forget agent (replaces headless)
2. Session ID capture across all modes (foundation for cross-mode resume)
3. Mode taxonomy cleanup (drop headless/embedded, add autonomous)
4. Registry/lifecycle updates for asyncio.Task-based agents

## Deferred (follow-up work)

- Replace terminal (tmux) mode with SDK interactive mode
- Web chat resume from terminal sessions via `ClaudeAgentOptions.resume`
- Gemini/Codex SDK integration (currently CLI-only)

## Changes

### 1. New file: `src/gobby/agents/spawners/autonomous.py` (~180 lines)

**`AutonomousRunner`** class — in-process SDK runner:

```python
class AutonomousRunner:
    def __init__(
        self,
        session_id: str,
        run_id: str,
        project_id: str,
        cwd: str,
        prompt: str,
        model: str | None,
        system_prompt: str | None,
        max_turns: int | None,          # From agent definition
        agent_run_manager: Any,
        fire_lifecycle: Callable | None,  # async (event_type, data) -> dict | None
        seq_num: int | None = None,
        resume_session_id: str | None = None,  # For cross-mode resume
    ): ...

    async def run(self) -> None:
        # 1. _find_cli_path() + _find_mcp_config() from chat_session_helpers
        # 2. Build system prompt with env info
        # 3. Build simplified SDK hooks from fire_lifecycle
        # 4. ClaudeAgentOptions:
        #    - allowed_tools=["mcp__gobby__*"]
        #    - can_use_tool=lambda *_: True (autonomous)
        #    - max_turns from agent definition
        #    - resume=resume_session_id (if resuming)
        # 5. client.connect() → conversation.send_message(prompt) → accumulate text
        # 6. Capture SDK session ID from ResultMessage for future resume
        # 7. agent_run_manager.complete(run_id, result=text)
        # 8. finally: client.disconnect()
```

**Hook wiring** — simplified `_build_sdk_hooks()`:
- UserPromptSubmit, PreToolUse, PostToolUse, Stop
- No plan mode, no tool approval UI, no history injection, no compact
- Reuses response converters from `chat_session_helpers.py`

**`fire_lifecycle`** — callback provided by caller, bridges to workflow engine:
- Creates `HookEvent` with `source=SessionSource.AUTONOMOUS_SDK`
- Calls `workflow_handler.evaluate()` via `asyncio.to_thread()`
- Returns dict with decision/context/reason

### 2. `src/gobby/workflows/definitions.py` — Update mode taxonomy

```python
# Before:
mode: Literal["terminal", "embedded", "headless", "self", "inherit"] = "inherit"

# After:
mode: Literal["terminal", "autonomous", "self", "inherit"] = "inherit"
```

### 3. `src/gobby/agents/spawn_executor.py` — Replace headless/embedded

```python
async def execute_spawn(request: SpawnRequest) -> SpawnResult:
    if request.mode == "terminal":
        # Existing terminal dispatch (claude/gemini/codex)
        ...
    elif request.mode == "autonomous":
        return await _spawn_autonomous(request)
    # "self" handled upstream
```

`_spawn_autonomous()`:
- Resolves agent definition → system prompt (via `resolve_agent()` + `_inject_agent_skills()`)
- Extracts `max_turns` from agent definition body
- Builds `fire_lifecycle` callback from `request.workflow_handler`
- Creates `AutonomousRunner`, launches as `asyncio.create_task()`
- Returns `SpawnResult(process=task)`

Remove `_spawn_embedded()` and `_spawn_headless()`.

Update `SpawnRequest`:
- `mode: Literal["terminal", "autonomous", "self"]`
- Add `workflow_handler: Any | None = None`
- Add `agent_run_manager: Any | None = None`
- Add `db: Any | None = None`

### 4. `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`

- Update mode Literal to `["terminal", "autonomous", "self"]`
- Pass `workflow_handler`, `agent_run_manager`, `db` into `SpawnRequest`
- After autonomous spawn: store `spawn_result.process` (asyncio.Task) on `RunningAgent.task`

### 5. Session ID capture — `src/gobby/agents/spawners/autonomous.py`

Capture `ResultMessage.session_id` (the Claude CLI session ID) and store it:
- On the `agent_runs` DB record (new column `sdk_session_id`)
- Enables future `resume` from any mode

### 6. `src/gobby/hooks/events.py` — Add `AUTONOMOUS_SDK` to `SessionSource`

### 7. `src/gobby/agents/registry.py` — Update kill() (1 line)

`if agent.task and agent.mode in ("in_process", "autonomous"):`

### 8. `src/gobby/agents/lifecycle_monitor.py` — Task-based detection (~15 lines)

Detect done asyncio.Tasks for `mode == "autonomous"` agents in `check_dead_agents()`.

### 9. Update all references

Replace `"headless"` → `"autonomous"`, remove `"embedded"`:
- `src/gobby/tasks/external_validator.py:348`
- `src/gobby/mcp_proxy/tools/task_validation.py:598`
- `src/gobby/agents/spawners/embedded.py` → delete
- `src/gobby/agents/spawners/headless.py` → delete
- All test fixtures

## Files

| File | Change |
|------|--------|
| `src/gobby/agents/spawners/autonomous.py` | **NEW** — AutonomousRunner |
| `src/gobby/workflows/definitions.py` | Update mode Literal |
| `src/gobby/agents/spawn_executor.py` | autonomous dispatch, remove headless/embedded |
| `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` | Mode update, pass deps, store task |
| `src/gobby/hooks/events.py` | Add AUTONOMOUS_SDK source |
| `src/gobby/agents/registry.py` | kill() condition (1 line) |
| `src/gobby/agents/lifecycle_monitor.py` | Task-based detection (~15 lines) |
| `src/gobby/tasks/external_validator.py` | mode="autonomous" |
| `src/gobby/mcp_proxy/tools/task_validation.py` | mode="autonomous" |
| `src/gobby/agents/spawners/embedded.py` | **DELETE** |
| `src/gobby/agents/spawners/headless.py` | **DELETE** |
| Tests | Update modes, new AutonomousRunner tests |

## Key Reuse

| What | From |
|------|------|
| `_find_cli_path()` | `chat_session_helpers.py:79` |
| `_find_mcp_config()` | `chat_session_helpers.py:99` |
| `_response_to_*_output()` | `chat_session_helpers.py:123-213` |
| `resolve_agent()` | `workflows/agent_resolver.py` |
| `_inject_agent_skills()` | `websocket/chat.py:28` |
| `HookEvent`, `HookEventType` | `hooks/events.py` |
| `normalize_tool_fields()` | `hooks/normalization.py` |

## Verification

1. `uv run pytest tests/agents/spawners/test_autonomous.py -v` — new tests
2. `uv run pytest tests/agents/ -v -k "spawn"` — spawn tests pass with new mode
3. `uv run pytest tests/workflows/ -k "definition" -v` — mode validator works
4. `uv run pytest tests/servers/ -k "chat" -v` — web chat unchanged
5. `uv run ruff check src/` — clean after deleting files
6. Live: `spawn_agent(agent="default", mode="autonomous")` → session activates, `get_agent_result` returns text

## Follow-up Tasks (create via gobby-tasks after implementation)

Create these as detailed planning tasks with descriptions, linked as an epic:

### Epic: Unified SDK Agent Execution — Phase 2
Parent epic for the follow-up work that builds on the autonomous runner foundation.

### Task 1: Replace terminal (tmux) mode with SDK interactive mode
Replace tmux-based terminal spawning with an SDK-based interactive runner. The interactive runner is similar to AutonomousRunner but human-gated: tool approval via callback, observable via WebSocket streaming, multi-turn conversation. This eliminates the need for stream-jsonl parsing from tmux output for live session observation. Key files: `spawn_executor.py` (new `_spawn_interactive()` dispatch), `websocket/chat.py` (refactor to share runner logic with interactive mode). The `ChatSession` class already does most of this — the work is extracting the interaction model from the WebSocket transport.

### Task 2: Web chat resume from terminal/autonomous sessions
Enable web chat to resume a session that was previously running as terminal or autonomous agent. When a user connects via web chat and selects a session that has an `sdk_session_id`, create the ChatSession with `ClaudeAgentOptions.resume=sdk_session_id`. This eliminates JSONL transcript parsing for live session takeover. Key files: `websocket/chat.py` (`_create_chat_session` to accept resume parameter), `chat_session.py` (`start()` to pass `resume` to `ClaudeAgentOptions`). Depends on: session ID capture from Phase 1.

### Task 3: Gemini/Codex SDK integration for autonomous mode
Currently AutonomousRunner is Claude-only (uses ClaudeSDKClient). Gemini supports `gemini --resume <uuid>` and Codex has `thread/resume` + TypeScript SDK (`codex.resumeThread()`). Investigate whether these CLIs have Python SDK equivalents or if subprocess with `--resume` is sufficient. If subprocess: extend AutonomousRunner to shell out with resume flags while still capturing session IDs. If SDK: add provider-specific runner implementations.

### Task 4: Cross-mode session handoff (autonomous ↔ interactive)
Enable seamless handoff between autonomous and interactive modes for the same session. An autonomous agent that needs human input could escalate to interactive mode (attach WebSocket UI). An interactive session could be detached and continued autonomously. Uses `sdk_session_id` as the bridge. Key mechanism: kill current runner, create new runner with `resume=sdk_session_id` and different interaction model.

### Task 5: Auto-compaction for long-running autonomous agents
Long-running autonomous orchestrators (unlimited max_turns) need context management. The SDK supports `PreCompact` hooks — wire these in AutonomousRunner to persist compaction summaries. Ensure autonomous agents survive compaction without losing critical context. Compact notifications go through the same WebSocket channel as other events — if the user has the session open in web chat (via resume/observe), they see the compact. If they type `/compact` in web chat, the command routes to the SDK client for that session.

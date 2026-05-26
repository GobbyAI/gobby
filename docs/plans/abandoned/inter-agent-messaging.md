# Inter-Agent Messaging Overhaul

## Context

The current messaging system has 5 hierarchy-locked tools (`send_to_parent`, `send_to_child`, `broadcast_to_children`, `poll_messages`, `mark_message_read`) that only work between parent-child sessions and require polling. This is a foundational blocker for the party-time orchestration plans (`docs/plans/party-time.md`, `docs/plans/party-time-v2.md`).

This overhaul simplifies to 3 capabilities:
1. **`send_message`** — any session to any session in the same project (P2P)
2. **Push delivery** — messages injected via hook enrichment on next hook event, no polling needed
3. **`send_command`** — directed execution with temporary workflow tool restrictions

---

## Phase 1: Schema + Storage

### Migration 115

BASELINE_VERSION is currently 114. New migration adds columns to `inter_session_messages` and creates `agent_commands` table.

```sql
-- Extend inter_session_messages for P2P and push delivery
ALTER TABLE inter_session_messages ADD COLUMN message_type TEXT NOT NULL DEFAULT 'direct';
ALTER TABLE inter_session_messages ADD COLUMN metadata_json TEXT;
ALTER TABLE inter_session_messages ADD COLUMN delivered_at TEXT;
CREATE INDEX idx_ism_message_type ON inter_session_messages(message_type);
CREATE INDEX idx_ism_undelivered ON inter_session_messages(to_session, delivered_at)
    WHERE delivered_at IS NULL;

-- Command execution lifecycle
CREATE TABLE agent_commands (
    id TEXT PRIMARY KEY,
    from_session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    to_session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    command_text TEXT NOT NULL,
    allowed_tools TEXT,            -- JSON array or "all"
    allowed_mcp_tools TEXT,        -- JSON array of "server:tool" or "all"
    exit_condition TEXT,           -- Expression evaluated against workflow variables
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|active|completed|cancelled
    original_workflow_snapshot TEXT, -- JSON blob of saved workflow state for restoration
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    activated_at TEXT,
    completed_at TEXT
);
CREATE INDEX idx_agent_commands_active ON agent_commands(to_session, status)
    WHERE status = 'active';
```

### Files

| File | Change |
|------|--------|
| `src/gobby/storage/migrations.py` | Migration 115, update BASELINE_SCHEMA, bump BASELINE_VERSION to 115 |
| `src/gobby/storage/inter_session_messages.py` | Add `message_type`, `metadata_json`, `delivered_at` to dataclass. Add `get_undelivered_messages(to_session)` and `mark_delivered(ids)` methods to manager |
| `src/gobby/storage/agent_commands.py` | **New.** `AgentCommand` dataclass + `AgentCommandManager` with `create_command`, `get_active_command`, `get_pending_commands`, `activate_command`, `complete_command`, `cancel_command` |
| `src/gobby/storage/sessions.py` | Add `is_ancestor(ancestor_id, descendant_id, max_depth=10)` — walks `parent_session_id` chain |

---

## Phase 2: Unified `send_message` + Deprecated Wrappers

### Tools (on `gobby-agents`, replacing old messaging tools)

Clean break — remove `send_to_parent`, `send_to_child`, `broadcast_to_children`, `poll_messages`, `mark_message_read`. Replace with:

| Tool | Description |
|------|-------------|
| `send_message(from_session, to_session, content)` | P2P message to any session in same project. |
| `send_command(to_session, command_text, allowed_tools, allowed_mcp_tools, exit_condition)` | Directed execution (ancestor-only). No timeout — commands run until `complete_command` or `exit_condition` is met or session ends. |
| `complete_command(result)` | Signal command completion, restore original workflow. `result` sent as a message back to the command sender. |

The `agent_runs.result` bridge (current `agent_messaging.py:104-128`) is preserved — `send_message` auto-writes to `agent_runs.result` when recipient is sender's parent, so `get_agent_result` continues to work.

### Security boundaries

- `send_message`: Validate both sessions exist and share the same `project_id`
- `send_command`: Validate `from_session` is an ancestor of `to_session` (walk `parent_session_id` chain via new `is_ancestor()`). Reject if target already has an active command.

### Files

| File | Change |
|------|--------|
| `src/gobby/mcp_proxy/tools/agent_messaging.py` | Rewrite from scratch: `send_message`, `send_command`, `complete_command`. Delete all old tool implementations. |
| `src/gobby/mcp_proxy/registries.py` | Thread `AgentCommandManager` + broadcaster into `add_messaging_tools()` (lines 226-235) |
| `src/gobby/servers/websocket/broadcast.py` | Add `broadcast_agent_message()` and `broadcast_agent_command()`. Add `"agent_message"` / `"agent_command"` to high-volume subscription filter |
| `tests/mcp_proxy/tools/test_agent_messaging.py` | Rewrite from scratch for new tools |
| `tests/e2e/test_inter_agent_messages.py` | Rewrite for P2P semantics + push delivery + command lifecycle |

---

## Phase 3: Push Delivery via Hook Enrichment

On eligible hook events (PreToolUse, UserPromptSubmit), check for undelivered messages and inject into `response.context`.

### Mechanism

1. `EventEnricher.enrich()` calls new `_inject_pending_messages()` after existing enrichment (line 100)
2. Fetches `get_undelivered_messages(session_id)` — messages where `delivered_at IS NULL`
3. Formats as labeled block: `[Inter-Agent Messages (N pending)]` with sender refs, priority tags, content. If the message has `metadata_json`, include key-value pairs in the injection block.
4. Appends to `response.context` (becomes `additionalContext` in Claude Code adapter via `claude_code.py:227-228`)
5. Calls `mark_delivered(ids)` atomically — prevents re-injection on next hook
6. **Delivery acknowledgement**: For each delivered message, auto-send a `message_type = 'delivery_ack'` message back to the original sender with the delivered message ID in metadata. Sender receives this on their next hook event, confirming delivery. Ack messages (`delivery_ack`, `command_ack`) are excluded from generating their own acks to prevent infinite loops.
7. Token budget: truncate to ~2000 chars, urgent messages first, `"... N more messages"` indicator. Ack messages are system-generated and small (~50 chars each).

### Wiring

`InterSessionMessageManager` currently only exists on the MCP proxy side (`registries.py`). The hook factory needs its own instance pointing at the same DB.

### Files

| File | Change |
|------|--------|
| `src/gobby/hooks/event_enrichment.py` | Accept `InterSessionMessageManager` + `AgentCommandManager` in `__init__`. Add `_inject_pending_messages()` and `_inject_command_context()`. Only inject on PreToolUse and UserPromptSubmit event types. |
| `src/gobby/hooks/factory.py` | Add `InterSessionMessageManager` + `AgentCommandManager` to `_Storage` dataclass (line 55-64). Add to `HookManagerComponents` (line 94-127). Create in `_create_storage()` (line 302-314). |
| `src/gobby/hooks/hook_manager.py` | Pass new managers from components to `EventEnricher.__init__()` |

---

## Phase 4: `send_command` Workflow Override

### Activation flow (in `_inject_command_context`)

When `EventEnricher` detects a pending command for the session:

1. **Snapshot** current workflow state (`workflow_name`, `step`, `variables`) into `agent_commands.original_workflow_snapshot` as JSON
2. **Override** workflow state: `workflow_name = "__command__"`, `step = "__command__"`, variables include `_command_id`, `_command_tools`, `_command_mcp_tools`
3. **Inject** command text into `response.context`
4. **Mark** command `status = 'active'`, set `activated_at`
5. **Acknowledge**: Auto-send a `message_type = 'command_ack'` message back to the sender via `InterSessionMessageManager.create_message()`, confirming the command was received and activated. The sender gets this on their next hook event via push delivery.

### Tool enforcement (in `WorkflowEngine`)

Add branch for `state.workflow_name == "__command__"` in `handle_event()` (around line 283):

- On `BEFORE_TOOL`: only allow tools in `_command_tools` list + `EXEMPT_TOOLS` (line 80-94) + `complete_command`. Block everything else with clear reason.
- MCP tool restrictions: check `_command_mcp_tools` for `call_tool`/`get_tool_schema` events (reuse logic from lines 404-434)
- `complete_command` tool is always allowed

### Exit conditions (2 mechanisms)

1. **Explicit**: Agent calls `complete_command` (primary mechanism)
2. **Automatic**: `exit_condition` expression evaluated after each `AFTER_TOOL` event via the existing `ConditionEvaluator` in `workflows/unified_evaluator.py`. When the condition evaluates truthy, auto-complete the command and restore workflow state.

No timeout. The agent is blocked from doing anything except the command, so if they can't complete it they're stuck regardless. The acknowledgement message confirms activation to the sender.

### Restoration (on complete/cancel)

Deserialize `original_workflow_snapshot`, restore `WorkflowState` fields, set `context_injected = False` to force step context re-injection. Save via `state_manager.save_state()`.

### Edge cases

| Scenario | Behavior |
|----------|----------|
| New command while one active | Reject: "active command already exists" |
| Session ends with active command | `SessionCoordinator.complete_agent_run()` cancels active commands during cleanup |
| Daemon restart mid-command | Both `agent_commands` table and `WorkflowState` (`__command__` sentinel) are persisted — recoverable |

### Files

| File | Change |
|------|--------|
| `src/gobby/workflows/engine.py` | Add `_handle_command_mode()` for `__command__` workflow with tool restriction logic + exit condition evaluation |
| `src/gobby/hooks/session_coordinator.py` | Cancel active commands in `complete_agent_run()` cleanup (around line 296) |

---

## Verification

1. **P2P messaging**: `send_message` between two non-parent-child sessions in same project succeeds; different project fails
2. **Push delivery**: Send message, trigger hook on recipient, verify `additionalContext` contains message content + metadata and `delivered_at` is set
3. **agent_runs.result bridge**: `send_message` to parent session still writes to `agent_runs.result` so `get_agent_result` works
4. **send_command**: Send command with `allowed_tools: ["Read", "Grep"]`, verify child can't use `Edit`/`Write`, verify `complete_command` restores original workflow
5. **Delivery acknowledgement**: After `send_message`, verify sender receives a `delivery_ack` on their next hook event. After `send_command`, verify sender receives a `command_ack`.
6. **Exit condition**: Send command with `exit_condition: "task_completed == true"`, set that variable, verify auto-completion
7. **Ancestor validation**: `send_command` from sibling session is rejected
8. **WebSocket**: Subscribe to `agent_message`, verify events broadcast on send/deliver
9. **Tests**: `uv run pytest tests/storage/test_inter_session_messages.py tests/storage/test_agent_commands.py tests/mcp_proxy/tools/test_agent_messaging.py tests/hooks/test_event_enrichment.py tests/e2e/test_inter_agent_messages.py tests/workflows/test_engine.py -v`

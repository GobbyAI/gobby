# Session Management System

## Overview

Comprehensive session management for Gobby including MCP tools, CLI commands, and cross-system integration. This plan consolidates session-related features and fills gaps identified in the existing implementation.

**Status**: In Progress

**Related Plans**:
- `completed/SESSION_TRACKING.md` - Message tracking and parsing (DONE)
- `AUTONOMOUS_HANDOFF.md` - Handoff context extraction (mostly done, tests pending)

---

## Current State

### What Already Exists

| Component | Location | Status |
|-----------|----------|--------|
| **Session Storage** | `storage/sessions.py` | Complete |
| **Session Manager** | `sessions/manager.py` | Complete |
| **Session CLI** | `cli/sessions.py` | Complete |
| **Session Messages Storage** | `storage/session_messages.py` | Complete |
| **Session Messages MCP** | `mcp_proxy/tools/session_messages.py` | Partial |
| **Transcript Analyzer** | `sessions/analyzer.py` | Complete |
| **Handoff Workflow Actions** | `workflows/actions.py` | Complete |
| **Session Lifecycle** | `sessions/lifecycle.py` | Complete |

### Existing CLI Commands (`gobby session`)

```bash
gobby session list [--project] [--status] [--source] [--limit] [--json]
gobby session show <session_id> [--json]
gobby session messages <session_id> [--limit] [--role] [--offset] [--json]
gobby session search <query> [--session] [--project] [--limit] [--json]
gobby session delete <session_id>
gobby session stats [--project]
```

### Existing MCP Tools (`gobby-sessions`)

| Tool | Description |
|------|-------------|
| `get_session_messages` | Get messages for a session |
| `search_messages` | Full-text search across messages |

---

## What's Missing

### 1. Session CRUD MCP Tools

The `gobby-sessions` registry only has message tools. Need session management tools:

| Tool | Description |
|------|-------------|
| `get_session` | Get session details by ID |
| `get_current_session` | Get active session for current project |
| `list_sessions` | List sessions with filters |
| `session_stats` | Session statistics |

### 2. Handoff MCP Tools

| Tool | Description |
|------|-------------|
| `create_handoff` | Create handoff context with optional notes |
| `get_handoff_context` | Retrieve stored compact_markdown |

### 3. Cross-Reference Tools

| Tool | Description |
|------|-------------|
| `get_session_commits` | Git commits made during session timeframe |

Note: `get_session_tasks` and `get_task_sessions` already exist in `gobby-tasks`.

### 4. CLI Commands

| Command | Description |
|---------|-------------|
| `gobby session handoff [--session-id] [notes]` | Create handoff context |

### 5. Testing

- Unit tests for TranscriptAnalyzer (edge cases)
- MCP tools tests for session operations
- Integration tests for handoff flow

---

## Architecture

```text
                     +---------------------------+
                     |      CLI Interface        |
                     |    gobby session *        |
                     +---------------------------+
                              |
                              v
+------------------+    +---------------------------+    +------------------+
|  MCP Interface   |    |     HTTP Endpoints        |    |  Hook System     |
| gobby-sessions   |<-->|   /sessions/*             |<-->| on_session_*     |
+------------------+    +---------------------------+    +------------------+
         |                       |                              |
         v                       v                              v
+------------------+    +---------------------------+    +------------------+
| InternalRegistry |    |   LocalSessionManager     |    | WorkflowActions  |
| session tools    |    |   (storage/sessions.py)   |    | extract_handoff  |
+------------------+    +---------------------------+    +------------------+
         |                       |                              |
         +-----------------------+------------------------------+
                                 |
                                 v
                        +------------------+
                        |   PostgreSQL DB      |
                        |   sessions       |
                        |   session_tasks  |
                        |   session_messages|
                        +------------------+
```

---

## Implementation Phases

### Phase 1: Extend gobby-sessions MCP Registry

**Goal**: Add session CRUD tools to the existing registry.

**Files to Modify**:
- `src/gobby/mcp_proxy/tools/session_messages.py` - Add session tools
- `src/gobby/mcp_proxy/registries.py` - Pass `local_session_manager` to registry

**Tool Specifications**:

```python
@registry.tool(
    name="get_session",
    description="Get session details by ID.",
)
def get_session(session_id: str) -> dict[str, Any]:
    """Returns: id, status, source, project_id, title, created_at, updated_at,
    git_branch, parent_session_id, summary_markdown, compact_markdown"""

@registry.tool(
    name="get_current_session",
    description="Get the current active session for a project.",
)
def get_current_session(project_id: str | None = None) -> dict[str, Any]:
    """Find active session. project_id defaults to current project."""

@registry.tool(
    name="list_sessions",
    description="List sessions with optional filtering.",
)
def list_sessions(
    project_id: str | None = None,
    status: str | None = None,  # active, paused, expired, archived, handoff_ready
    source: str | None = None,  # claude, gemini, codex
    limit: int = 20,
) -> dict[str, Any]:
    """Returns: {sessions: [...], count: int}"""

@registry.tool(
    name="session_stats",
    description="Get session statistics for a project.",
)
def session_stats(project_id: str | None = None) -> dict[str, Any]:
    """Returns: {total: int, by_status: {...}, by_source: {...}}"""
```

**Checklist**:
- [ ] 1.1 Add `local_session_manager` parameter to `create_session_messages_registry()`
- [ ] 1.2 Implement `get_session` tool
- [ ] 1.3 Implement `get_current_session` tool
- [ ] 1.4 Implement `list_sessions` tool
- [ ] 1.5 Implement `session_stats` tool
- [ ] 1.6 Update `setup_internal_registries()` to pass session_manager
- [ ] 1.7 Write unit tests for new tools

---

### Phase 2: Add Handoff MCP Tools and CLI

**Goal**: Expose handoff functionality via MCP tools and CLI.

**Context**: The `extract_handoff_context` workflow action exists at `workflows/actions.py:1042-1108`. It uses `TranscriptAnalyzer` and saves to `session.compact_markdown`.

**Files to Modify**:
- `src/gobby/mcp_proxy/tools/session_messages.py` - Add handoff tools
- `src/gobby/cli/sessions.py` - Add `handoff` command

**Tool Specifications**:

```python
@registry.tool(
    name="create_handoff",
    description="Create handoff context by extracting structured data from the session transcript.",
)
async def create_handoff(
    session_id: str | None = None,  # Defaults to current active session
    notes: str | None = None,       # Additional notes to include
) -> dict[str, Any]:
    """
    Uses TranscriptAnalyzer to extract:
    - Active gobby-task
    - TodoWrite state
    - Files modified
    - Git commits and status
    - Initial goal
    - Recent activity

    Returns: {success: bool, markdown_length: int, context: {...}}
    """

@registry.tool(
    name="get_handoff_context",
    description="Get the handoff context (compact_markdown) for a session.",
)
def get_handoff_context(session_id: str) -> dict[str, Any]:
    """Returns: {session_id: str, compact_markdown: str | None, has_context: bool}"""
```

**CLI Command**:

```bash
gobby session handoff [--session-id <id>] [notes]
```

If `--session-id` not provided, uses the current project's most recent active session.

**Checklist**:
- [ ] 2.1 Implement `create_handoff` MCP tool
- [ ] 2.2 Implement `get_handoff_context` MCP tool
- [ ] 2.3 Add `gobby session handoff` CLI command
- [ ] 2.4 Wire handoff tools to use `TranscriptAnalyzer`
- [ ] 2.5 Write integration tests for handoff flow

---

### Phase 3: Cross-Reference Tools

**Goal**: Add tools to retrieve session-related data from other systems.

**New Tool**:

```python
@registry.tool(
    name="get_session_commits",
    description="Get git commits made during a session timeframe.",
)
async def get_session_commits(
    session_id: str,
    max_commits: int = 20,
) -> dict[str, Any]:
    """
    Uses session.created_at and session.updated_at to filter
    git log within that timeframe.

    Returns: {session_id: str, commits: [{hash, message, timestamp}], count: int}
    """
```

**Note**: This reuses git log parsing from `workflows/actions.py:1695-1722`.

**Checklist**:
- [ ] 3.1 Implement `get_session_commits` MCP tool
- [ ] 3.2 Extract git log parsing helper for reuse
- [ ] 3.3 Write unit tests

---

### Phase 4: Testing and Documentation

**Goal**: Complete test coverage and documentation.

**Test Files**:

| File | Purpose |
|------|---------|
| `tests/sessions/test_analyzer.py` | Add edge cases |
| `tests/mcp_proxy/test_mcp_tools_sessions.py` | NEW - Session MCP tools |
| `tests/sessions/test_handoff_flow.py` | NEW - Integration tests |

**Checklist**:
- [ ] 4.1 Add edge case tests to `test_analyzer.py`:
  - [ ] Empty TodoWrite todos list
  - [ ] Malformed tool blocks
  - [ ] Multiple Edit/Write calls
  - [ ] Git status extraction
- [ ] 4.2 Create `test_mcp_tools_sessions.py`:
  - [ ] Test `get_session`
  - [ ] Test `list_sessions` with filters
  - [ ] Test `get_current_session`
  - [ ] Test `create_handoff`
  - [ ] Test `get_handoff_context`
- [ ] 4.3 Create `test_handoff_flow.py`:
  - [ ] End-to-end handoff creation and retrieval
  - [ ] Handoff injection on session start
- [ ] 4.4 Update CLAUDE.md with session management guidance
- [ ] 4.5 Mark AUTONOMOUS_HANDOFF.md checklist items complete

---

## Tool Reference Summary

### gobby-sessions Registry (After Implementation)

| Tool | Args | Returns |
|------|------|---------|
| `get_session_messages` | session_id, limit?, offset?, role? | messages, total_count |
| `search_messages` | query, project_id?, limit? | results |
| `get_session` | session_id | session dict |
| `get_current_session` | project_id? | session dict or null |
| `list_sessions` | project_id?, status?, source?, limit? | sessions[], count |
| `session_stats` | project_id? | total, by_status, by_source |
| `create_handoff` | session_id?, notes? | success, markdown_length |
| `get_handoff_context` | session_id | compact_markdown |
| `get_session_commits` | session_id, max_commits? | commits[] |

### CLI Commands (Complete List)

| Command | Options | Description |
|---------|---------|-------------|
| `gobby session list` | --project, --status, --source, --limit, --json | List sessions |
| `gobby session show <id>` | --json | Show session details |
| `gobby session messages <id>` | --limit, --role, --offset, --json | Show messages |
| `gobby session search <query>` | --session, --project, --limit, --json | Search messages |
| `gobby session delete <id>` | (confirm) | Delete session |
| `gobby session stats` | --project | Show statistics |
| `gobby session handoff` | --session-id, [notes] | Create handoff context (NEW) |

---

## Database Schema

No new migrations needed. Uses existing tables:

- `sessions` - Core session data including `compact_markdown`
- `session_messages` - Message storage
- `session_message_state` - Processing state
- `session_tasks` - Session-task linking

---

## Critical Files

| File | Changes |
|------|---------|
| `src/gobby/mcp_proxy/tools/session_messages.py` | Extend with session CRUD + handoff tools |
| `src/gobby/mcp_proxy/registries.py` | Wire session_manager to registry |
| `src/gobby/cli/sessions.py` | Add handoff CLI command |
| `src/gobby/sessions/analyzer.py` | Reference - TranscriptAnalyzer |
| `src/gobby/storage/sessions.py` | Reference - LocalSessionManager |

---

## Migration Notes

### Relationship to Other Plans

**SESSION_TRACKING.md** (Completed):
- Implemented message storage, parsing, WebSocket broadcast
- Created `session_messages` and `session_message_state` tables
- All 6 phases completed

**AUTONOMOUS_HANDOFF.md** (Mostly Complete):
- Phases 1-4 implemented (TranscriptAnalyzer, workflow actions, triggers)
- Phase 5 partially complete (missing unit tests)
- Phase 6 (autonomous chaining) is separate feature

This plan fills the gaps:
- Session MCP tools (not in either plan)
- CLI handoff command (mentioned but not detailed)
- Test completion (Phase 5 of AUTONOMOUS_HANDOFF)

### Task References

Existing tasks to incorporate:
- `gt-5df42a` - Add handoff() MCP tool
- `gt-9a6808` - Write tests for session manager
- `gt-1c5ca4` - Create servers/routes/ directory and extract session routes

New task (current):
- `gt-e9e4c5` - Implement create_handoff MCP tool and CLI

---

## Success Criteria

- [ ] All session MCP tools available via `call_tool(server="gobby-sessions", ...)`
- [ ] `gobby session handoff` CLI creates handoff context
- [ ] Session-task cross-references work bidirectionally
- [ ] Test coverage > 80% for session MCP tools
- [ ] AUTONOMOUS_HANDOFF.md Phase 5 checklist complete

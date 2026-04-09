# Unified Web Chat (v1 — Deprecated)

> **Deprecated:** This plan has been superseded by [unified-web-chat-v2.md](unified-web-chat-v2.md). The v2 plan replaces the session management approach with CLI subprocesses and introduces `PendingInteractionManager` for approval flows. See v2 for the canonical `ChatSessionProtocol` design.

## Overview

Replace the SDK-backed and Codex-specific web chat with a unified architecture where all three CLIs (Claude, Gemini, Codex) are driven as subprocesses behind a single `ChatSessionProtocol`. Provider-switchable from the frontend. Source identity normalized to bare providers + `session_type` column.

## Constraints

- Codex web chat deletion must be atomic (one commit, tree stays compilable)
- Schema migrations must precede model/storage changes
- `PendingInteractionManager` is the Phase 2 critical path — most other Phase 2 tasks depend on it
- Phases 3 and 4 are parallel after Phase 2
- Phase 5 is deferrable
- Prior branch attempt was rejected — all code is written fresh, no cherry-picks

## Phase 0: Cleanup + Source Identity Migration

**Goal**: Remove old Codex web chat, normalize source identity to bare providers, add `session_type` column for web-vs-terminal distinction.

### 0.1 Remove Codex web chat implementation [category: refactor]

Delete `CodexChatSession` and `CodexChatSessionPermissionsMixin` and remove all references. This must be atomic — all deletions and import removals in one change.

**Delete these files:**
- `src/gobby/servers/codex_chat_session.py` (394 lines — `CodexChatSession` dataclass)
- `src/gobby/servers/codex_chat_session_permissions.py` (311 lines — `CodexChatSessionPermissionsMixin`)

**Remove imports and branches in:**

`src/gobby/servers/websocket/chat/_session.py`:
- Line 223: Remove `from gobby.servers.codex_chat_session import CodexChatSession` import
- Lines 200-227: Remove the `provider == "codex"` / `"codex_web_chat" in sources` branch in `_create_chat_session_inner()`. All sessions temporarily route to `ChatSession` (Claude SDK) until Phase 2 introduces `CLIChatSession`.

`src/gobby/servers/websocket/chat/_lifecycle.py`:
- Line 111: Remove `from gobby.servers.codex_chat_session import CodexChatSession`
- Lines 111-116: Remove `isinstance(session, CodexChatSession)` check in source detection. Temporarily hardcode source to `SessionSource.CLAUDE_SDK_WEB_CHAT` (cleaned up in 0.4).

`src/gobby/servers/websocket/chat/_messaging.py`:
- Remove any `CodexChatSession`-specific handling if present.

**Delete test files:**
- `tests/servers/test_codex_chat_session.py`
- `tests/servers/test_codex_chat_session_permissions.py`

**Verification:** `grep -r 'CodexChatSession\|codex_chat_session' src/ tests/` returns zero hits. Web chat still works (Claude SDK path).

### 0.2 Add session_type column via schema migration [category: code] (depends: 0.1)

Add `session_type` column to the sessions table and update the unique index to include it. This prevents web and terminal sessions from colliding once `source` becomes a bare provider name.

**Target:** `src/gobby/storage/migrations.py`

Add migration v200:

```python
def _migrate_v200(self, conn: Connection) -> None:
    """Add session_type column and update unique index."""
    conn.execute("ALTER TABLE sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'terminal'")
    conn.execute("UPDATE sessions SET session_type = 'web_chat' WHERE source LIKE '%web_chat%'")
    conn.execute("UPDATE sessions SET source = 'claude' WHERE source IN ('claude_sdk', 'claude_sdk_web_chat')")
    conn.execute("UPDATE sessions SET source = 'codex' WHERE source = 'codex_web_chat'")
    conn.execute("DROP INDEX IF EXISTS idx_sessions_unique")
    conn.execute(
        "CREATE UNIQUE INDEX idx_sessions_unique "
        "ON sessions(external_id, machine_id, source, project_id, session_type)"
    )
```

Update `CURRENT_VERSION` to 200. Add `_migrate_v200` to the migration dispatch.

**Target:** `src/gobby/storage/baseline_schema.sql`

Add `session_type TEXT NOT NULL DEFAULT 'terminal'` to the sessions CREATE TABLE statement. Update the unique index definition:

```sql
CREATE UNIQUE INDEX idx_sessions_unique ON sessions(external_id, machine_id, source, project_id, session_type);
```

**Verification:** Fresh DB has `session_type` column. Existing DB migrates cleanly — web chat sessions get `session_type='web_chat'`, source values normalized. Unique index includes `session_type`.

### 0.3 Update Session model and storage layer for session_type [category: code] (depends: 0.2)

Add `session_type` to the Session dataclass and update all storage methods that create or look up sessions.

**Target:** `src/gobby/storage/session_models.py`

Add field to Session dataclass:

```python
session_type: str = "terminal"
```

Update `from_row()` to read `session_type` from DB row. Update `to_dict()` and `to_brief()` to include `session_type` in output.

**Target:** `src/gobby/storage/sessions.py`

Update `register()` (line 30) — accept `session_type: str = "terminal"` parameter. Pass it to INSERT. Include in `find_by_external_id` lookup.

Update `find_by_external_id()` (line 178) — accept `session_type: str | None = None` parameter. When provided, add `AND session_type = ?` to the WHERE clause. This is backward-compatible — callers that don't pass it get existing behavior.

**Verification:** `register(source="claude", session_type="web_chat")` creates a session with correct values. `find_by_external_id()` with `session_type` parameter filters correctly. `to_dict()` output includes `session_type`.

### 0.4 Collapse SessionSource enum and migrate backend callsites [category: refactor] (depends: 0.3)

Remove `CLAUDE_SDK`, `CLAUDE_SDK_WEB_CHAT`, and `CODEX_WEB_CHAT` from the `SessionSource` enum. Update all backend code that references old source values.

**Target:** `src/gobby/hooks/events.py` (lines 58-66)

Remove these enum members:
- `CLAUDE_SDK = "claude_sdk"`
- `CLAUDE_SDK_WEB_CHAT = "claude_sdk_web_chat"`
- `CODEX_WEB_CHAT = "codex_web_chat"`

Only `CLAUDE`, `GEMINI`, `CODEX` remain.

**Target:** `src/gobby/servers/routes/mcp/hooks.py` (lines 120-141)

Collapse adapter branches. Remove `claude_sdk` and `claude_sdk_web_chat` cases — both map to `ClaudeCodeAdapter` with `SessionSource.CLAUDE`. Result:

```python
if source == "claude":
    adapter = ClaudeCodeAdapter(hook_manager=hook_manager)
elif source == "gemini":
    adapter = GeminiAdapter(hook_manager=hook_manager)
elif source == "codex":
    codex_adapter = getattr(request.app.state, "codex_adapter", None)
    adapter = codex_adapter if codex_adapter is not None else CodexHooksAdapter(hook_manager=hook_manager)
```

**Target:** `src/gobby/servers/websocket/chat/_lifecycle.py` (lines 111-116)

Replace `isinstance` source detection with provider attribute lookup. Source is now determined by session's `provider` attribute (or default `"claude"`), not by class type:

```python
source = SessionSource(getattr(session, "provider", "claude"))
```

**Target:** `src/gobby/servers/websocket/chat/_session.py` (lines 200-227)

Change `cli_source="claude_sdk_web_chat"` to `cli_source="claude"` in `resolve_agent()` call.

**Target:** `src/gobby/servers/chat_session.py`

- Line 206: Change `env["GOBBY_SOURCE"] = "claude_sdk_web_chat"` to `env["GOBBY_SOURCE"] = "claude"`
- Line 273: Change `"source": "claude_sdk_web_chat"` to `"source": "claude"`
- Lines 421, 438: Update any other source references.

**Target:** `src/gobby/workflows/agent_resolver.py` (lines 13-20)

Simplify `_SOURCE_TO_PROVIDER` mapping — remove `claude_sdk`, `claude_sdk_web_chat`, `codex_web_chat` entries. Only `claude`, `gemini`, `codex` remain.

**Target:** `src/gobby/servers/websocket/handlers/plan_approval.py` (line 139)

Update `for source in ("claude_sdk_web_chat", "codex_web_chat"):` to use bare provider names or query by `session_type='web_chat'` instead.

**Target:** `src/gobby/install/shared/hooks/hook_dispatcher.py` (lines 227-243)

Update `_detect_source()` — if it maps `GOBBY_SOURCE` values, ensure it handles bare provider names.

**Verification:** `grep -r 'claude_sdk_web_chat\|codex_web_chat\|claude_sdk' src/` returns zero hits (excluding test files). `SessionSource` enum has exactly 3 members. All hook routing works with bare provider names.

### 0.5 Expose session_type in REST API and migrate frontend [category: code] (depends: 0.4)

Add `session_type` to REST API session responses and update all frontend files that use old source strings for web/terminal distinction.

**Backend — already done:** `to_dict()` and `to_brief()` updated in 0.3 to include `session_type`. REST routes use these serializers, so `session_type` is already in API responses.

**Target:** `web/src/hooks/useSessions.ts`

Add `session_type` to `GobbySession` interface:

```typescript
export interface GobbySession {
  // ... existing fields ...
  session_type: string;  // 'terminal' | 'web_chat'
}
```

Update `KNOWN_SOURCES` — remove `claude_sdk_web_chat`:

```typescript
export const KNOWN_SOURCES = ["claude", "gemini", "codex"] as const;
```

Update session filtering to use `session_type` instead of `source`:

```typescript
// HIDDEN_SOURCES stays the same (pipeline, cron, system)
// No need for source-based web/terminal split anymore
```

**Target:** `web/src/components/sessions/SessionSidebar.tsx`

Lines 62-64: Change web/CLI split from `s.source === 'claude_sdk_web_chat'` to `s.session_type === 'web_chat'`:

```typescript
const webSessions = sessions.filter((s) => s.session_type === 'web_chat')
const cliSessions = sessions.filter((s) => s.session_type !== 'web_chat')
```

Lines 30-38: Remove `claude_sdk_web_chat` from `sourceLabel()`. Lines 40-48: Remove from `sourceDotClass()`. Line 206: Update `isResumable` check.

**Target:** `web/src/components/shared/SourceIcon.tsx`

Remove `claude_sdk_web_chat` from `SourceType`, `SOURCE_COLORS`, and the switch case. Add a `web_chat` icon case keyed on `session_type` (or keep the chat bubble icon under a different key).

**Target:** `web/src/components/chat/ResumeSessionModal.tsx`

Remove `claude_sdk_web_chat` from `SOURCE_LABELS` and `SOURCE_COLORS`.

**Target:** `web/src/components/sessions/SessionsPage.tsx`

Remove `claude_sdk_web_chat` from `sourceLabel()`.

**Target:** `web/src/components/sessions/SessionDetail.tsx`

Line 237: Change `session.source !== 'claude_sdk_web_chat'` to `session.session_type === 'terminal'`.

**Target:** `web/src/components/dashboard/SessionsCard.tsx`

Remove `claude_sdk`, `claude_sdk_web_chat` from `SOURCE_COLORS` and `SOURCE_LABELS`.

**Target:** `web/src/components/dashboard/UsageCard.tsx`

Remove `claude_sdk`, `claude_sdk_web_chat` from `SOURCE_LABELS`.

**Target:** `web/src/components/tasks/SessionViewer.tsx`

Remove `claude_sdk_web_chat` from `SOURCE_LABELS`.

**Target:** `web/src/App.tsx`

- Line 499: Change `"claude_sdk_web_chat"` to `"claude"` in `useAgentDefinitions` call
- Line 590: Change `s.source === "claude_sdk_web_chat"` to `s.session_type === "web_chat"` for `webChatSessions` filter

**Verification:** `grep -r 'claude_sdk_web_chat\|claude_sdk' web/src/` returns zero hits. Frontend correctly splits sessions by `session_type`. Source icons show bare provider names.

### 0.6 Migrate agent/skill configs to bare provider sources [category: config] (depends: 0.4)

Update YAML/markdown config files that reference old source values.

**Target:** `src/gobby/install/shared/workflows/agents/default-web-chat.yaml`

Line 3: Change `sources: [claude_sdk_web_chat]` to `sources: [claude, gemini, codex]`

**Target:** `src/gobby/install/shared/skills/canvas/SKILL.md`

Line 10: Change `sources: [claude_sdk_web_chat, gemini_sdk_web_chat]` to `sources: [claude, gemini, codex]`

**Target:** `src/gobby/skills/injector.py` (lines 214-217)

Verify `_matches_audience()` source matching works with bare provider names. The logic is `context.source not in config.sources` — as long as `context.source` is now `"claude"` (set in 0.4) and config sources are `["claude", "gemini", "codex"]`, this works without code changes. Verify only.

**Target:** `src/gobby/hooks/event_handlers/_session_start.py` (line 75)

Verify `cli_source = event.source.value` produces bare provider values after 0.4. No code change expected.

**Verification:** Agent definitions and skills match on bare provider sources. Skill injection works for web chat sessions. `grep -r 'claude_sdk_web_chat\|codex_web_chat\|gemini_sdk_web_chat' src/gobby/install/` returns zero hits.

### 0.7 Update test fixtures for source identity migration [category: refactor] (depends: 0.4)

Update all test files that reference old source values. ~33 references across 11 files.

**Test files to update:**
- `tests/hooks/test_hooks_events.py` — Update SessionSource references
- `tests/hooks/test_skill_manager.py` — Update source values in test data
- `tests/llm/test_claude.py` — Update source references
- `tests/llm/test_claude_provider.py` — Update source references
- `tests/mcp_proxy/test_proxy_server.py` — Update source references
- `tests/servers/test_chat_session_hooks.py` — Update source values
- `tests/skills/test_injector.py` — Update source matching test data
- `tests/skills/test_parser.py` — Update source values in test data
- `tests/workflows/test_agent_resolver.py` — Update `_SOURCE_TO_PROVIDER` test expectations

**Pattern:** Replace `"claude_sdk_web_chat"` with `"claude"` (+ `session_type="web_chat"` where needed), `"codex_web_chat"` with `"codex"`, `"claude_sdk"` with `"claude"`. Remove references to deleted `SessionSource.CLAUDE_SDK_WEB_CHAT` etc.

**Verification:** `uv run pytest tests/hooks/ tests/llm/ tests/mcp_proxy/ tests/servers/test_chat_session_hooks.py tests/skills/ tests/workflows/test_agent_resolver.py -v` passes. `grep -r 'claude_sdk_web_chat\|codex_web_chat\|CLAUDE_SDK_WEB_CHAT\|CODEX_WEB_CHAT' tests/` returns zero hits.

## Phase 1: ClaudeCLI Launcher + Stream Parser

**Goal**: Build CLI subprocess infrastructure for multi-turn web chat streaming.

### 1.1 Implement stream JSON parser for Claude CLI output [category: code] (depends: Phase 0)

Create a new module to parse Claude's `--output-format stream-json` NDJSON output into typed event objects.

**Create:** `src/gobby/llm/stream_json_parser.py` (~200 lines)

**StreamEvent hierarchy:**

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class StreamEvent:
    """Base class for all stream events."""
    raw: dict[str, Any] = field(repr=False)

@dataclass
class InitEvent(StreamEvent):
    """system/init — session initialized."""
    session_id: str = ""
    model: str = ""

@dataclass
class ContentBlockDelta(StreamEvent):
    """Content block delta — text, thinking, or tool_use."""
    block_type: str = ""  # "text", "thinking", "tool_use"
    text: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None

@dataclass
class MessageComplete(StreamEvent):
    """Full assistant message complete."""
    content: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""

@dataclass
class RateLimitEvent(StreamEvent):
    """Rate limit info from the stream."""
    retry_after: float = 0.0

@dataclass
class ResultEvent(StreamEvent):
    """Final result with usage stats."""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str = ""

@dataclass
class ErrorEvent(StreamEvent):
    """Error during streaming."""
    message: str = ""
    is_fatal: bool = False
```

**Async stream iterator:**

```python
import asyncio
import json

async def parse_stream(reader: asyncio.StreamReader) -> AsyncIterator[StreamEvent]:
    """Parse NDJSON lines from Claude CLI stdout into StreamEvents."""
    async for line in reader:
        line = line.decode().strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        yield _classify_event(data)

def _classify_event(data: dict[str, Any]) -> StreamEvent:
    """Route raw JSON to the appropriate StreamEvent subclass."""
    # Classify based on type field patterns from Claude CLI stream-json output:
    # system/init, assistant message with content blocks, rate_limit_event, result
    ...
```

**Expected Claude stream-json schema (to be verified during implementation):**

```text
system/init → system/hook_* → assistant{content:[{type:"thinking"|"text"|"tool_use"}]} → rate_limit_event → result
```

**Verification:** Unit tests with captured NDJSON samples covering: init, text delta, thinking delta, tool_use, rate_limit, result, error, malformed input, empty lines.

### 1.2 Extend ClaudeCLI with session launcher [category: code] (depends: 1.1)

Extend the existing `claude_cli.py` with a `ClaudeCLI` class that can spawn multi-turn CLI sessions.

**Target:** `src/gobby/llm/claude_cli.py`

Currently contains only `find_cli_path()` and `verify_cli_path()`. Add:

```python
class ClaudeCLI:
    """Claude CLI subprocess manager for multi-turn web chat sessions."""

    def __init__(self, cli_path: str | None = None):
        self._cli_path = cli_path

    async def _resolve_path(self) -> str:
        """Find and verify CLI path."""
        path = self._cli_path or find_cli_path()
        if not path:
            raise FileNotFoundError("Claude CLI not found")
        return path

    def session(
        self,
        session_id: str | None = None,
        model: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> "CLISession":
        """Create a new multi-turn CLI session."""
        return CLISession(
            cli_path_resolver=self._resolve_path,
            session_id=session_id,
            model=model,
            env_overrides=env_overrides,
        )


class CLISession:
    """Multi-turn Claude CLI session via stream-json I/O."""

    def __init__(self, cli_path_resolver, session_id, model, env_overrides):
        self._resolve_path = cli_path_resolver
        self._session_id = session_id
        self._model = model
        self._env_overrides = env_overrides or {}
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        """Spawn CLI subprocess with stream-json I/O."""
        path = await self._resolve_path()
        cmd = [path, "--output-format", "stream-json", "--verbose", "--input-format", "stream-json"]
        if self._session_id:
            cmd.extend(["--session-id", self._session_id])
        if self._model:
            cmd.extend(["--model", self._model])

        env = {**os.environ, **self._env_overrides}
        self._process = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )

    async def send(self, message: str) -> AsyncIterator[StreamEvent]:
        """Send a message and stream responses."""
        assert self._process and self._process.stdin
        payload = json.dumps({"type": "user", "content": message}) + "\n"
        self._process.stdin.write(payload.encode())
        await self._process.stdin.drain()
        async for event in parse_stream(self._process.stdout):
            yield event

    async def interrupt(self) -> None:
        """Send interrupt signal to CLI process."""
        if self._process:
            self._process.send_signal(signal.SIGINT)

    async def stop(self) -> None:
        """Terminate CLI process."""
        if self._process:
            self._process.terminate()
            await self._process.wait()
```

**Note:** `ClaudeCLI.query()` is NOT added — headless single-turn calls stay on the SDK.

**Verification:** Unit tests with mocked subprocess covering: start, send+stream, interrupt, stop, CLI not found error, env override passthrough.

### 1.3 Bump hook dispatcher timeout for hold-open pattern [category: config] (depends: Phase 0)

Increase HTTP timeouts in the hook dispatcher to support the hold-open approval pattern where CLI hook requests block waiting for user action in the web UI.

**Target:** `src/gobby/install/shared/hooks/hook_dispatcher.py`

Line 716 — httpx async path:

```python
# Before:
timeout=90.0,
# After:
timeout=httpx.Timeout(10.0, read=600.0),
```


This keeps a short 10s connect timeout (fail fast if daemon is down) but allows 600s read timeout for hold-open approvals.

Line 688 — curl fire-and-forget path:

```python
# Before:
"--max-time", "90",
# After:
"--max-time", "600",
```


Only used for `SessionEnd` (not approval-bearing) but should match for consistency.

**Add import if not present:** `import httpx` (may already be imported).

**Verification:** Hook dispatcher uses new timeout values. Existing hooks still fire normally (the longer timeout only matters when the server holds the response).

## Phase 2: CLIChatSession + Unified Pending Interactions

**Goal**: New `CLIChatSession` backed by `ClaudeCLI.session()`, unified approval model via `PendingInteractionManager`, provider routing from frontend.

### 2.1 Add pending_interactions table via schema migration [category: code] (depends: Phase 1)

Create the database table for durable pending interaction state (tool approvals, plan approvals, ask-user questions).

**Target:** `src/gobby/storage/migrations.py`

Add migration v201:

```python
def _migrate_v201(self, conn: Connection) -> None:
    """Add pending_interactions table."""
    conn.execute("""
        CREATE TABLE pending_interactions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            provider TEXT NOT NULL,
            tool_name TEXT,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            decision TEXT,
            response_json TEXT,
            timeout_seconds INTEGER NOT NULL DEFAULT 300,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX idx_pending_interactions_session "
        "ON pending_interactions(session_id, status)"
    )
```

Update `CURRENT_VERSION` to 201.

**Target:** `src/gobby/storage/baseline_schema.sql`

Add the `pending_interactions` CREATE TABLE and index to baseline DDL.

**Verification:** Fresh DB has the table. Migration from v200 adds it cleanly. Index exists on `(session_id, status)`.

### 2.2 Implement PendingInteractionManager [category: code] (depends: 2.1)

Central coordinator for durable pending interactions with in-memory waiters and timeout cleanup. Replaces the bifurcated approval state (`pending_plan_path` + in-memory `asyncio.Event` in `chat_session_permissions.py`).

**Create:** `src/gobby/servers/pending_interactions.py` (~200 lines)

```python
import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

@dataclass
class PendingInteraction:
    id: str
    session_id: str
    kind: str  # 'tool', 'plan', 'ask_user'
    provider: str
    tool_name: str | None
    payload: dict[str, Any]
    timeout_seconds: int
    status: str = "pending"
    decision: str | None = None
    response: dict[str, Any] | None = None

class PendingInteractionManager:
    """Coordinates durable pending interactions with in-memory waiters."""

    def __init__(self, db):
        self._db = db
        self._waiters: dict[str, asyncio.Event] = {}
        self._results: dict[str, dict] = {}
        self._timeouts: dict[str, asyncio.Task] = {}

    async def create(self, session_id: str, kind: str, provider: str,
                     payload: dict, tool_name: str | None = None,
                     timeout_seconds: int = 300) -> str:
        """Insert DB row, create asyncio.Event, start timeout task. Returns interaction_id."""
        await self.supersede(session_id, kind)
        interaction_id = str(uuid.uuid4())
        # Insert into DB
        # Create Event + timeout task
        return interaction_id

    async def wait(self, interaction_id: str) -> dict:
        """Block until resolved or timeout. Returns decision + response."""

    async def resolve(self, interaction_id: str, decision: str,
                      response: dict | None = None) -> bool:
        """Set decision, wake waiter, update DB. Returns False if expired/missing."""

    async def expire(self, interaction_id: str) -> None:
        """Mark expired in DB, wake waiter with timeout decision."""

    async def rebroadcast(self, session_id: str) -> list[dict]:
        """Return all pending (non-expired, non-resolved) interactions for session.
        Only latest non-expired per (session_id, kind) returned."""

    async def cleanup(self) -> None:
        """Cancel all timeout tasks. Called on daemon shutdown."""

    async def supersede(self, session_id: str, kind: str) -> None:
        """Expire any existing pending interaction of same (session_id, kind)."""

    async def expire_all_pending(self) -> None:
        """Mark all pending rows as expired. Called on daemon startup (fail-closed)."""
```

**Supersession rules:** Before creating a new interaction, `supersede()` expires any existing one of the same `(session_id, kind)`. This is safe because all three providers guarantee single-outstanding-blocking per kind.

**Daemon restart:** `expire_all_pending()` called on startup — all pending rows marked expired (fail-closed). Sessions get "approval lost, please retry" on next hook.

**WS message schemas (server → client):**

```typescript
// Pending interaction
{ type: "pending_interaction", interaction_id: string, kind: "tool" | "plan" | "ask_user",
  session_id: string, tool_name?: string, arguments?: object, question?: string,
  plan_path?: string, plan_content?: string }

// Resolved confirmation
{ type: "interaction_resolved", interaction_id: string, decision: string }

// Expired notification
{ type: "interaction_expired", interaction_id: string, kind: string }
```

**WS message schemas (client → server):**

```typescript
// Resolve interaction
{ type: "resolve_interaction", interaction_id: string,
  decision: "approve" | "reject" | "approve_always" | "answer",
  response?: { answers: Record<string, string> }, feedback?: string }
```

**Register on app.state:** Add `PendingInteractionManager` instance to `app.state` in `app_factory.py`. Call `expire_all_pending()` on startup, `cleanup()` on shutdown.

**Verification:** Create → wait → resolve cycle works. Timeout expires correctly. Supersession works. Rebroadcast returns only latest pending per kind. Daemon restart expires all pending.

### 2.3 Implement CLIChatSession for Claude [category: code] (depends: 1.2, 2.2)

New `ChatSessionProtocol` implementation backed by `ClaudeCLI.session()`. This is the Claude-specific CLI chat session for web chat.

**Create:** `src/gobby/servers/cli_chat_session.py` (~300 lines)

```python
from gobby.llm.claude_cli import ClaudeCLI, CLISession
from gobby.llm.stream_json_parser import StreamEvent, ContentBlockDelta, ResultEvent
from gobby.servers.chat_session_base import ChatEvent

class CLIChatSession:
    """Claude CLI-backed web chat session."""

    provider: str = "claude"

    def __init__(self, conversation_id: str, model: str | None = None,
                 session_id: str | None = None,
                 env_overrides: dict[str, str] | None = None):
        self.conversation_id = conversation_id
        self._model = model
        self._session_id = session_id
        self._env_overrides = env_overrides or {}
        self._cli = ClaudeCLI()
        self._cli_session: CLISession | None = None

    async def start(self, model: str | None = None) -> None:
        """Spawn CLI subprocess."""
        self._cli_session = self._cli.session(
            session_id=self._session_id,
            model=model or self._model,
            env_overrides=self._env_overrides,
        )
        await self._cli_session.start()

    async def send_message(self, content: str | list[dict]) -> AsyncIterator[ChatEvent]:
        """Send message to CLI, translate StreamEvents to ChatEvents."""
        text = content if isinstance(content, str) else _extract_text(content)
        async for event in self._cli_session.send(text):
            chat_event = _stream_to_chat_event(event)
            if chat_event:
                yield chat_event

    async def interrupt(self) -> None:
        if self._cli_session:
            await self._cli_session.interrupt()

    async def stop(self) -> None:
        if self._cli_session:
            await self._cli_session.stop()

    async def switch_model(self, new_model: str) -> None:
        self._model = new_model
        # CLI session may need restart with new model
```

**Key design:** Lifecycle events (PreToolUse, PostToolUse, etc.) arrive via HTTP hooks naturally — the CLI subprocess fires them through `hook_dispatcher.py` → `POST /api/hooks/execute`. `CLIChatSession` does NOT wire `_on_pre_tool` etc. callbacks. Approval resolution lives in `PendingInteractionManager`, not in this class.

**Local LLM support:** Pass `ANTHROPIC_BASE_URL` in `env_overrides` when configured.

**Verification:** Unit tests with mocked `ClaudeCLI` covering: start, send_message streaming, interrupt, stop, model switching.

### 2.4 Slim ChatSessionProtocol and migrate approval to PendingInteractionManager [category: refactor] (depends: 2.2, 2.3)

Remove approval-related methods from the protocol. Move approval resolution to `PendingInteractionManager`.

**Target:** `src/gobby/servers/chat_session_base.py`

Remove from `ChatSessionProtocol`:
- `provide_answer(answers: dict[str, str]) -> None` (line 87)
- `provide_approval(decision: str) -> None` (line 89)
- `provide_plan_decision(decision: str) -> None` (line 91)
- `has_pending_question` property (line 67)
- `has_pending_approval` property (line 70)
- `has_pending_plan` property (line 73)
- `approve_plan() -> None` (line 95)
- `set_plan_feedback(feedback: str) -> None` (line 97)

Protocol retains: `start()`, `send_message()`, `interrupt()`, `drain_pending_response()`, `switch_model()`, `set_chat_mode()`, `stop()`. Add required `provider: str` attribute.

**Target:** `src/gobby/servers/chat_session.py`

`ChatSession` (SDK fallback) keeps the old methods internally for its own use during transition, but they're no longer part of the protocol interface.

**Target:** `src/gobby/servers/websocket/chat/_messaging.py`

- `_handle_tool_approval_response()` (lines 849-869): Change from `session.provide_approval(decision)` to `PendingInteractionManager.resolve(interaction_id, decision)`. The `interaction_id` comes from the client's `resolve_interaction` message.
- `_handle_ask_user_response()` (lines 829-847): Change from `session.provide_answer(answers)` to `PendingInteractionManager.resolve(interaction_id, "answer", response={"answers": answers})`.
- Add handler for `resolve_interaction` WS message type that dispatches to `PendingInteractionManager.resolve()`.

**Target:** `src/gobby/servers/websocket/handlers/plan_approval.py`

Refactor `handle_plan_approval_response()` to use `PendingInteractionManager.resolve()` instead of `session.provide_plan_decision()`. Plan file path stored in `payload_json` of the pending interaction.

**Verification:** Existing SDK-backed chat sessions still work (ChatSession wraps old methods internally). CLIChatSession works without `provide_*` methods. All approval flows route through PendingInteractionManager.

### 2.5 Remove pending_plan_path column and find_pending_plans [category: refactor] (depends: 2.4)

Clean up the old plan approval persistence mechanism now that `PendingInteractionManager` handles it.

**Target:** `src/gobby/storage/migrations.py`

Add migration v202 — SQLite doesn't support DROP COLUMN before 3.35.0, so recreate the table without `pending_plan_path`:

```python
def _migrate_v202(self, conn: Connection) -> None:
    """Remove pending_plan_path from sessions table."""
    # Use SQLite table recreation pattern:
    # 1. Create new table without pending_plan_path
    # 2. Copy data
    # 3. Drop old table
    # 4. Rename new table
    # 5. Recreate indexes
```

Or if SQLite version >= 3.35.0 (Python 3.13 bundles 3.45+):
```python
conn.execute("ALTER TABLE sessions DROP COLUMN pending_plan_path")
```

**Target:** `src/gobby/storage/baseline_schema.sql`

Remove `pending_plan_path TEXT` from sessions CREATE TABLE.

**Target:** `src/gobby/storage/session_models.py`

Remove `pending_plan_path` field from Session dataclass. Remove from `from_row()`, `to_dict()`, `to_brief()`.

**Target:** `src/gobby/storage/sessions.py`

Delete `find_pending_plans()` method (lines 395-403). Delete `update_pending_plan()` method.

**Verification:** No references to `pending_plan_path` or `find_pending_plans` in codebase. Migration runs cleanly.

### 2.6 Wire hold-open permission gate in hook endpoint [category: code] (depends: 2.2)

Extend the hook execution endpoint to hold HTTP responses for web chat sessions that need user approval.

**Target:** `src/gobby/servers/routes/mcp/hooks.py`

In `execute_hook()`, after adapter processes the hook:

```python
# After existing hook processing...
session = await lookup_session(request, session_header)

if session and session.session_type == "web_chat":
    hook_type = payload.get("type", "")
    tool_name = payload.get("tool_name", "")

    if hook_type == "PreToolUse" and _is_gated_tool(tool_name, session):
        manager: PendingInteractionManager = request.app.state.pending_interaction_manager
        interaction_id = await manager.create(
            session_id=session.id,
            kind="tool",
            provider=session.source,
            payload={"tool_name": tool_name, "arguments": payload.get("arguments", {})},
            tool_name=tool_name,
        )
        # Broadcast pending_interaction to WS clients
        await broadcast_pending_interaction(request.app, session.id, interaction_id, ...)
        # Hold response until resolved
        result = await manager.wait(interaction_id)
        return {"decision": result["decision"]}

    if hook_type == "AskUserQuestion":
        # Similar hold-open for ask-user questions
        interaction_id = await manager.create(
            session_id=session.id, kind="ask_user", provider=session.source,
            payload={"question": payload.get("question", ""), "tool_call_id": payload.get("tool_call_id", "")},
        )
        await broadcast_pending_interaction(...)
        result = await manager.wait(interaction_id)
        return {"additionalContext": result.get("response", {}).get("answers", {})}

# Terminal sessions: existing adapter path, no hold-open
```

**Key design:** Terminal sessions (`session_type == 'terminal'`) continue through the existing adapter path with no hold-open and no persistence. Only web chat sessions get the hold-open treatment.

**Verification:** Terminal hooks process immediately (no regression). Web chat PreToolUse on gated tool → pending interaction created → WS notification sent → approve → hook returns → CLI proceeds. Timeout → auto-deny.

### 2.7 Wire provider routing in session creation and WS messaging [category: code] (depends: 2.3, 2.4)

Route web chat session creation to the appropriate `ChatSessionProtocol` implementation based on the `provider` field from the frontend.

**Target:** `src/gobby/servers/websocket/chat/_session.py`

Update `_create_chat_session_inner()` to read `provider` and route:

```python
async def _create_chat_session_inner(
    self, conversation_id: str, model: str | None = None,
    project_id: str | None = None, resume_session_id: str | None = None,
    provider: str | None = None,
) -> ChatSessionProtocol:
    # Provider precedence:
    # 1. Explicit UI provider (from chat_message.provider)
    # 2. Agent definition provider
    # 3. Resumed session's source
    # 4. None → existing SDK ChatSession fallback

    effective_provider = provider
    if not effective_provider and resume_session_id:
        # Look up resumed session's source
        ...
    if not effective_provider:
        agent = await resolve_agent(cli_source="claude", ...)
        effective_provider = getattr(agent, "provider", None)

    match effective_provider:
        case "claude":
            from gobby.servers.cli_chat_session import CLIChatSession
            session = CLIChatSession(conversation_id=conversation_id, model=model)
        case _:
            # Default: existing SDK path (backwards compat during transition)
            session = ChatSession(conversation_id=conversation_id)

    return session
```

**Target:** `src/gobby/servers/websocket/chat/_messaging.py`

Read `provider` from `chat_message` WS payload and pass through to session creation:

```python
provider = data.get("provider")  # None means default/SDK path
# Pass to _create_chat_session_inner
```

Pass `session_type="web_chat"` to `register()` for all web chat sessions.

**Target:** `src/gobby/servers/websocket/chat/_lifecycle.py`

Source detection now reads `session.provider` attribute instead of isinstance checks:

```python
source = SessionSource(getattr(session, "provider", "claude"))
```

**Verification:** No provider → SDK path works. `provider="claude"` → CLIChatSession. Streaming text works end-to-end. Session registered with correct source and session_type.

### 2.8 Add provider selection to frontend useChat hook [category: code] (depends: 2.7)

Add provider state to the frontend and include it in the WS chat_message payload.

**Target:** `web/src/hooks/useChat.ts`

Add state:

```typescript
const [selectedProvider, setSelectedProvider] = useState<string | null>(null);
```

Include in chat_message payload (around line 2227):

```typescript
if (selectedProvider) {
  payload.provider = selectedProvider;
}
```

Expose in return value:

```typescript
return {
  // ... existing returns ...
  selectedProvider,
  setSelectedProvider,
};
```

**Target:** `web/src/types/chat.ts`

Add to `ChatState` interface:

```typescript
export interface ChatState {
  // ... existing fields ...
  provider?: string | null;
  onProviderChange?: (provider: string | null) => void;
}
```

**Target:** `web/src/components/chat/ChatPage.tsx`

Add a provider picker UI element (dropdown or segmented control) in the header or command bar area. Show available providers. Wire `onProviderChange` to `setSelectedProvider`.

Initial implementation: hardcoded `["claude"]` as available providers (Gemini/Codex added in Phase 3). Provider picker only visible when at least 2 providers are available (hide for now, show after Phase 3/4).

**Verification:** Selecting a provider includes it in the WS payload. No provider selected → payload has no `provider` field (backwards compat). UI element renders correctly.

### 2.9 Wire reconnect rebroadcast for pending interactions [category: code] (depends: 2.2, 2.7)

On WebSocket reconnect, rebroadcast all pending interactions for the active conversation so the user can resume approvals.

**Target:** `src/gobby/servers/websocket/chat/_session.py` (or reconnect handler)

On WS reconnect with `conversation_id`:

```python
# Look up db_session_id from conversation_id
db_session = session_store.find_by_external_id(
    external_id=conversation_id, ..., session_type="web_chat"
)
if db_session:
    manager: PendingInteractionManager = app.state.pending_interaction_manager
    pending = await manager.rebroadcast(db_session.id)
    for interaction in pending:
        await ws.send_json({
            "type": "pending_interaction",
            **interaction,
        })
```

**Replace:** Remove `rebroadcast_pending_plans()` from `plan_approval.py` — its functionality is now handled by `PendingInteractionManager.rebroadcast()`.

**Frontend:** Handle `pending_interaction` messages on reconnect — render the appropriate approval UI (tool approval, plan approval, or ask-user dialog).

**Verification:** WS disconnect + reconnect → all pending interactions rebroadcast. Only the active conversation's interactions are sent. Expired interactions are not rebroadcast.

## Phase 3: Gemini + Codex Web Chat

**Goal**: All three CLIs get the same treatment through `ChatSessionProtocol`.

### 3.1 Implement Gemini ACP client [category: code] (depends: Phase 2)

Create a client for Gemini's ACP (Agent Communication Protocol) — bidirectional JSON-RPC over stdio for multi-turn sessions.

**Create:** `src/gobby/adapters/gemini_acp_client.py` (~250 lines)

```python
class GeminiACPClient:
    """Gemini ACP protocol client over subprocess stdio."""

    def __init__(self, cli_path: str | None = None):
        self._cli_path = cli_path
        self._process: asyncio.subprocess.Process | None = None

    async def start(self, session_id: str | None = None) -> None:
        """Spawn gemini --acp subprocess."""
        path = self._cli_path or shutil.which("gemini")
        if not path:
            raise FileNotFoundError("Gemini CLI not found")
        cmd = [path, "--acp"]
        if session_id:
            cmd.extend(["--resume", session_id])
        self._process = await asyncio.create_subprocess_exec(
            *cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE
        )

    async def send(self, message: str) -> AsyncIterator[StreamEvent]:
        """Send JSON-RPC message, yield normalized events."""
        # Write JSON-RPC request to stdin
        # Parse NDJSON response: init → message(role:assistant,delta:true) → result{stats}
        ...

    async def stop(self) -> None:
        """Terminate subprocess."""
```

**Gemini stream-json schema (verified April 2026):**
```
init → message(role:user) → message(role:assistant,delta:true) → result{stats}
```

Normalize to the same `StreamEvent` hierarchy from `stream_json_parser.py` via a Gemini-specific `_classify_event()`.

**Verification:** Unit tests with captured Gemini ACP NDJSON samples. Start, send, stream, stop lifecycle.

### 3.2 Implement GeminiCLIChatSession [category: code] (depends: 3.1)

`ChatSessionProtocol` implementation wrapping `GeminiACPClient`.

**Create:** `src/gobby/servers/gemini_cli_chat_session.py` (~200 lines)

```python
class GeminiCLIChatSession:
    """Gemini CLI-backed web chat session via ACP protocol."""

    provider: str = "gemini"

    def __init__(self, conversation_id: str, model: str | None = None,
                 session_id: str | None = None):
        self.conversation_id = conversation_id
        self._model = model
        self._session_id = session_id
        self._client = GeminiACPClient()

    async def start(self, model: str | None = None) -> None:
        await self._client.start(session_id=self._session_id)

    async def send_message(self, content) -> AsyncIterator[ChatEvent]:
        text = content if isinstance(content, str) else _extract_text(content)
        async for event in self._client.send(text):
            chat_event = _stream_to_chat_event(event)
            if chat_event:
                yield chat_event

    async def interrupt(self) -> None: ...
    async def stop(self) -> None: ...
    async def switch_model(self, new_model: str) -> None: ...
```

**Resume:** Gemini `--resume <id>` is provisional — verify actual behavior. If resume doesn't work, sessions are observable-only (no continue).

**Verification:** Unit tests with mocked `GeminiACPClient`. Integration test if Gemini CLI is available.

### 3.3 Implement CodexCLIChatSession [category: code] (depends: Phase 2)

Fresh `ChatSessionProtocol` implementation using the existing `CodexAppServerClient` for streaming deltas.

**Create:** `src/gobby/servers/codex_cli_chat_session.py` (~250 lines)

```python
class CodexCLIChatSession:
    """Codex CLI-backed web chat session via app-server protocol."""

    provider: str = "codex"

    def __init__(self, conversation_id: str, model: str | None = None,
                 thread_id: str | None = None):
        self.conversation_id = conversation_id
        self._model = model
        self._thread_id = thread_id
        # Use existing CodexAppServerClient infrastructure
```

**Codex stream schema (verified April 2026):**
```
thread.started → turn.started → item/agentMessage/delta → item.completed → turn.completed{usage}
```

The existing `CodexAppServerClient` in `src/gobby/adapters/codex_impl/client.py` handles the app-server protocol. This session wraps it with `ChatSessionProtocol` interface and normalizes events to `ChatEvent`.

**Resume:** Codex `resume <thread_id>` is provisional — verify app-server thread reuse.

**Verification:** Unit tests with mocked `CodexAppServerClient`. Streaming deltas normalize to ChatEvents correctly.

### 3.4 Wire Gemini and Codex into provider routing [category: code] (depends: 3.2, 3.3)

Add Gemini and Codex branches to the provider routing in session creation.

**Target:** `src/gobby/servers/websocket/chat/_session.py`

Update `_create_chat_session_inner()`:

```python
match effective_provider:
    case "claude":
        from gobby.servers.cli_chat_session import CLIChatSession
        session = CLIChatSession(conversation_id=conversation_id, model=model)
    case "gemini":
        from gobby.servers.gemini_cli_chat_session import GeminiCLIChatSession
        session = GeminiCLIChatSession(conversation_id=conversation_id, model=model)
    case "codex":
        from gobby.servers.codex_cli_chat_session import CodexCLIChatSession
        session = CodexCLIChatSession(conversation_id=conversation_id, model=model)
    case _:
        session = ChatSession(conversation_id=conversation_id)
```

**Target:** `web/src/components/chat/ChatPage.tsx` (or provider picker)

Update available providers list to include all three: `["claude", "gemini", "codex"]`. Show provider picker when 2+ providers are available.

**Cleanup candidates (post-verification):**
- `src/gobby/sessions/transcripts/gemini.py` — old Gemini transcript parser, dead code after ACP
- `src/gobby/adapters/codex_impl/client.py` — audit for dead code after fresh CodexCLIChatSession is verified

**Verification:** Select Claude → streaming response. Select Gemini → streaming response. Select Codex → streaming response. All three use the same approval flow via PendingInteractionManager.

## Phase 4: Session Switching + Provider Picker Polish

**Goal**: Switch providers mid-conversation, provider availability detection, local LLM support.

### 4.1 Add provider availability endpoint [category: code] (depends: Phase 2)

REST endpoint that reports which CLI providers are available on this machine.

**Create:** `src/gobby/servers/routes/providers.py`

```python
from fastapi import APIRouter
import shutil

router = APIRouter(prefix="/api/providers", tags=["providers"])

@router.get("")
async def list_providers():
    """Return available CLI providers and their status."""
    providers = []
    for name, binary in [("claude", "claude"), ("gemini", "gemini"), ("codex", "codex")]:
        path = shutil.which(binary)
        providers.append({
            "name": name,
            "available": path is not None,
            "path": path,
        })
    # Check for local LLM config
    # local_llm = get_local_llm_config()
    return {"providers": providers}
```

**Target:** `src/gobby/servers/app_factory.py`

Register the new router:

```python
from gobby.servers.routes.providers import router as providers_router
app.include_router(providers_router)
```

**Verification:** `GET /api/providers` returns list of providers with availability status. Providers without CLI installed show `available: false`.

### 4.2 Implement provider switching in frontend [category: code] (depends: 2.8, 4.1)

Allow users to switch providers mid-conversation. Switching creates a new session — the old one stays alive in background.

**Target:** `web/src/hooks/useChat.ts`

Add `switchProvider` function:

```typescript
const switchProvider = useCallback((newProvider: string) => {
  // 1. Stop streaming on active conversation (don't kill subprocess)
  // 2. Detach from any attached session
  // 3. Create new conversation_id
  // 4. Set selectedProvider to newProvider
  // 5. Old conversation moves to background
}, [/* deps */]);
```

**Target:** `web/src/components/chat/ChatPage.tsx`

Wire provider picker to use `switchProvider`. Fetch available providers from `GET /api/providers` on mount. Only show picker when 2+ providers available.

**Session switching model:**

| State | On switch |
|-------|-----------|
| Active conversation (`conversationId`) | New session created with new provider |
| Viewed session (`viewingSessionId`) | Preserved (independent) |
| Attached session (`attachedSessionId`) | Detached |

**Verification:** Switch from Claude to Gemini → new session created, old session visible in activity. Switch back → another new session. Pending approvals in background session expire via timeout.

### 4.3 Wire local LLM support via ANTHROPIC_BASE_URL [category: code] (depends: 2.3)

When a local LLM endpoint is configured, pass `ANTHROPIC_BASE_URL` to the Claude CLI subprocess so it connects to the local endpoint instead of Anthropic's API.

**Target:** `src/gobby/servers/cli_chat_session.py`

In `CLIChatSession.__init__` or `start()`:

```python
# Check daemon config for local LLM endpoint
local_endpoint = config.get("local_llm_endpoint")
if local_endpoint and self.provider == "claude":
    self._env_overrides["ANTHROPIC_BASE_URL"] = local_endpoint
```

**Target:** Configuration — determine where `local_llm_endpoint` is stored. Could be in `DaemonConfig` (`src/gobby/config/app.py`) or a project-level setting.

**Verification:** With local endpoint configured → Claude CLI subprocess receives `ANTHROPIC_BASE_URL` in env. Without config → no env override (uses Anthropic API). Other providers unaffected.

## Phase 5: Agent Spawning Unification (deferrable)

**Goal**: Consolidate CLI command building across agent spawning and web chat.

### 5.1 Consolidate agent spawn command builders [category: refactor] (depends: Phase 3)

The agent spawning system (`src/gobby/agents/spawners/command_builder.py`) and the web chat system both build CLI commands for Claude, Gemini, and Codex. Consolidate into shared command building logic.

**Target:** `src/gobby/agents/spawners/command_builder.py`

Create a shared `build_cli_command()` function that both agent spawning and web chat session creation can use:

```python
def build_cli_command(
    provider: str,
    mode: str = "interactive",  # "interactive" | "headless" | "agent"
    session_id: str | None = None,
    model: str | None = None,
    output_format: str = "stream-json",
    env_overrides: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build CLI command and env for any provider."""
    ...
```

**Cleanup:**
- Remove preflight functions from `src/gobby/agents/spawn.py` that duplicate path resolution
- Remove `capture_codex_session_id()` from `src/gobby/agents/codex_session.py` if no longer needed
- Audit `src/gobby/adapters/codex_impl/client.py` for dead code

**Verification:** Agent spawning and web chat both use the shared builder. Existing agent tests pass. No duplicate command construction logic.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

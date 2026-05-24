# Unified Web Chat

## Overview

Replace the SDK-backed and Codex-specific web chat with a unified architecture where all three CLIs (Claude, Gemini, Codex) are driven as subprocesses behind a single `ChatSessionProtocol`. Provider-switchable from the frontend. Source identity normalized to bare providers + `session_type` column.

## Relationship to unified-web-chat.md (v1)

This plan (v2) **supersedes** the original `docs/plans/unified-web-chat.md`:

- **Superseded:** Session management (replaced by CLI subprocess approach), approval flow (replaced by `PendingInteractionManager`), `ChatSessionProtocol` design (v2 is the canonical version)
- **Still valid:** Stream JSON parser design (adopted with minor changes in Phase 1.1), provider availability detection (adopted in Phase 4.1)
- **v1 status:** Deprecated. A deprecation notice has been added to the v1 document pointing here.

## Constraints

- `PendingInteractionManager` is the Phase 2 critical path — most other Phase 2 tasks depend on it
- Phases 3 and 4 are parallel after Phase 2
- Phase 5 is deferrable

## Phase 1: ClaudeCLI Launcher + Stream Parser

**Goal**: Build CLI subprocess infrastructure for multi-turn web chat streaming.

### 1.1 Implement stream JSON parser for Claude CLI output [category: code] (no dependencies)

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

    # Security: only these env vars may be overridden
    ALLOWED_ENV_OVERRIDES = {"ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDE_MODEL"}

    def session(
        self,
        session_id: str | None = None,
        model: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> "CLISession":
        """Create a new multi-turn CLI session."""
        if env_overrides:
            disallowed = set(env_overrides) - self.ALLOWED_ENV_OVERRIDES
            if disallowed:
                raise ValueError(f"Disallowed env overrides: {disallowed}")
            # Validate ANTHROPIC_BASE_URL: must be localhost to prevent SSRF
            base_url = env_overrides.get("ANTHROPIC_BASE_URL", "")
            if base_url:
                from urllib.parse import urlparse
                parsed = urlparse(base_url)
                if parsed.hostname not in ("localhost", "127.0.0.1"):
                    raise ValueError(f"ANTHROPIC_BASE_URL must be localhost, got: {parsed.hostname}")
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

### 1.3 Bump hook dispatcher timeout for hold-open pattern [category: config] (no dependencies)

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
    # Enforce single pending interaction per (session_id, kind) at DB level
    conn.execute(
        "CREATE UNIQUE INDEX idx_pending_interactions_active "
        "ON pending_interactions(session_id, kind) "
        "WHERE status = 'pending'"
    )
```

Update `CURRENT_VERSION` to 201.

> **Implementation note (PostgreSQL version):** The v202 migration that drops `pending_plan_path` should detect PostgreSQL version at runtime (`SELECT PostgreSQL_version()`). If >= 3.35.0, use `ALTER TABLE DROP COLUMN`. Otherwise, use the safe table-recreation pattern (create new table, copy data, drop old, rename).

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

**Memory leak prevention:** Every code path must clean up `_waiters`, `_results`, and `_timeouts` entries:
- `create()` registers a timeout `asyncio.Task` that calls `expire()` on expiration
- `resolve()` and `expire()` must cancel+pop the timeout task, set the Event, and populate `_results`
- `wait()` must pop `_results` and `_waiters` after returning the result
- `cleanup()` must cancel/await all remaining timeout tasks and clear all dicts
- `expire_all_pending()` on startup reconciles DB state and purges orphaned in-memory entries

**Race condition in `create()`:** Wrap `supersede()` + INSERT in a retry loop (up to 3 attempts) catching `IntegrityError` from the partial unique index (`idx_pending_interactions_active`), with brief backoff between retries. This handles the race between concurrent `supersede()` and INSERT operations.

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

Add migration v202 — PostgreSQL doesn't support DROP COLUMN before 3.35.0, so recreate the table without `pending_plan_path`:

```python
def _migrate_v202(self, conn: Connection) -> None:
    """Remove pending_plan_path from sessions table."""
    # Use PostgreSQL table recreation pattern:
    # 1. Create new table without pending_plan_path
    # 2. Copy data
    # 3. Drop old table
    # 4. Rename new table
    # 5. Recreate indexes
```

Or if PostgreSQL version >= 3.35.0 (Python 3.13 bundles 3.45+):
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
        # Rate limit: prevent thread exhaustion from too many held-open requests
        MAX_PENDING_PER_SESSION = 3  # configurable
        if await manager.count_pending(session.id) >= MAX_PENDING_PER_SESSION:
            return {"decision": "deny", "reason": "too_many_pending"}
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

    # Verify CLI is installed before routing to CLI session (reuse Phase 4.1 logic)
    if effective_provider and effective_provider != "claude":
        if not await check_provider_available(effective_provider):
            logger.warning(f"Provider {effective_provider} not available, falling back to SDK")
            effective_provider = None

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

**Resume:** Gemini `--resume <id>` is provisional — verify actual behavior. If resume doesn't work, sessions are observable-only (no continue). Resume absence does **not** block provider switching — Phase 4.2 creates new sessions on switch (old sessions remain backgrounded). Phase 2.9 WebSocket rebroadcasting is independent of resume support and only replays already-seen interactions.

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

**Session lifecycle rules:**
- Keep background subprocess alive for configurable timeout (e.g., 5 minutes) to allow fast switch-back
- Auto-deny pending approvals in background sessions with "Provider switched" message
- Terminate background subprocess after timeout, leaving session read-only
- New sessions start with empty context (no history transfer)
- Old session visible in activity panel with "(Provider: {old_provider})" suffix

**Verification:** Switch from Claude to Gemini → new session created, old session visible in activity. Switch back → another new session. Pending approvals in background session expire via timeout.

### 4.3 Wire local LLM support via ANTHROPIC_BASE_URL [category: code] (depends: 2.3)

When a local LLM endpoint is configured, pass `ANTHROPIC_BASE_URL` to the Claude CLI subprocess so it connects to the local endpoint instead of Anthropic's API.

**Target:** `src/gobby/servers/cli_chat_session.py`

In `CLIChatSession.__init__` or `start()`:

```python
# Read from DaemonConfig.local_llm (not ad-hoc config key)
if config.local_llm.enabled and self.provider == "claude":
    endpoint = config.local_llm.endpoint
    # Validate: must be http(s), host must be localhost/127.0.0.1
    self._env_overrides["ANTHROPIC_BASE_URL"] = endpoint
```

**Target:** `src/gobby/config/app.py` — add `local_llm` section to `DaemonConfig`:

```python
class LocalLLMConfig(BaseModel):
    enabled: bool = False
    endpoint: str = ""  # Validated: http(s), localhost/127.0.0.1 only
    providers: list[str] = ["claude"]  # Which providers support local LLM
```

**Validation:** Endpoint must be http(s) with localhost/127.0.0.1 host (SSRF prevention). Test connection with timeout on startup. Migration path: existing configs without `local_llm` get defaults (`enabled=false`).

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

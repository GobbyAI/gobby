# Session Message Tracking

## Overview

Async processing of session JSONL files from Claude Code, Gemini, Codex, and Antigravity. Extensible architecture for adding new CLI parsers.

**Prerequisites:** Must complete before Memory system (Sprint 7.5+)

## Processing Strategy

**Hybrid approach:**

1. Poll every 5-10 seconds during active sessions (incremental byte-offset reads)
2. Final pass on SESSION_END to ensure completeness

**Consumers:**

- Memory system (Sprint 7.5+) - skill learning from trajectories
- WebSocket broadcasting - real-time message streaming
- Search/query API - conversation history search

---

## Database Schema

### Migration 14: Session Messages

```sql
CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    content_type TEXT DEFAULT 'text',
    tool_name TEXT,
    tool_input TEXT,
    tool_result TEXT,
    timestamp TEXT NOT NULL,
    raw_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, message_index)
);

CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_session_messages_role ON session_messages(role);
CREATE INDEX IF NOT EXISTS idx_session_messages_timestamp ON session_messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_session_messages_tool ON session_messages(tool_name);

-- Track processing state per session
CREATE TABLE IF NOT EXISTS session_message_state (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    last_byte_offset INTEGER DEFAULT 0,
    last_message_index INTEGER DEFAULT 0,
    last_processed_at TEXT,
    processing_errors INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**Schema rationale:**

| Column | Purpose |
|--------|---------|
| `message_index` | Unique ordering (line number in JSONL) |
| `role` | "user", "assistant", "system", "tool" |
| `content_type` | "text", "thinking", "tool_use", "tool_result" |
| `session_message_state` | Tracks byte offset for incremental polling |

---

## Architecture

### Parser Protocol Extension

```python
# src/sessions/transcripts/base.py

@dataclass
class ParsedMessage:
    """Normalized message from any CLI transcript."""
    index: int
    role: str
    content: str
    content_type: str
    tool_name: str | None
    tool_input: dict | None
    tool_result: dict | None
    timestamp: datetime
    raw_json: dict

class TranscriptParser(Protocol):
    # Existing methods (backward compatible)
    def extract_last_messages(...) -> list[dict]: ...
    def extract_turns_since_clear(...) -> list[dict]: ...
    def is_session_boundary(...) -> bool: ...

    # New methods for incremental parsing
    def parse_line(self, line: str, index: int) -> ParsedMessage | None: ...
    def parse_lines(self, lines: list[str], start_index: int = 0) -> list[ParsedMessage]: ...
```

### Parser Registry

```python
# src/sessions/transcripts/__init__.py

PARSER_REGISTRY: dict[str, type[TranscriptParser]] = {
    "claude": ClaudeTranscriptParser,
    "gemini": GeminiTranscriptParser,
    "codex": CodexTranscriptParser,
}

def get_parser(source: str) -> TranscriptParser:
    parser_cls = PARSER_REGISTRY.get(source, ClaudeTranscriptParser)
    return parser_cls()
```

### SessionMessageProcessor

```python
# src/sessions/processor.py (NEW)

class SessionMessageProcessor:
    """Async processor for session message tracking."""

    def __init__(
        self,
        database: LocalDatabase,
        websocket_server: WebSocketServer | None = None,
        poll_interval: float = 5.0,
        debounce_delay: float = 1.0,
    ):
        self._trackers: dict[str, SessionTracker] = {}
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start background polling task."""

    async def stop(self) -> None:
        """Stop and flush pending."""

    async def register_session(self, session_id, jsonl_path, source) -> None:
        """Register session for tracking."""

    async def unregister_session(self, session_id) -> None:
        """Unregister and final process."""

@dataclass
class SessionTracker:
    session_id: str
    jsonl_path: Path
    parser: TranscriptParser
    byte_offset: int = 0
    message_index: int = 0
```

### LocalMessageManager

```python
# src/storage/messages.py (NEW)

class LocalMessageManager:
    """SQLite storage for session messages."""

    async def store_messages(session_id, messages: list[ParsedMessage]) -> int
    async def get_messages(session_id, limit, offset, role) -> list[dict]
    async def search_messages(query, session_id, project_id) -> list[dict]
    async def get_state(session_id) -> MessageState | None
    async def update_state(session_id, byte_offset, message_index) -> None
```

---

## Integration Points

### Hook Manager

```python
# src/hooks/hook_manager.py

# In _handle_event_session_start():
if self._message_processor and transcript_path:
    asyncio.create_task(
        self._message_processor.register_session(session_id, transcript_path, source)
    )

# In _handle_event_session_end():
if self._message_processor and session_id:
    asyncio.create_task(
        self._message_processor.unregister_session(session_id)
    )
```

### Runner

```python
# src/runner.py

# In GobbyRunner.__init__():
self.message_processor = SessionMessageProcessor(
    database=self.database,
    websocket_server=self.websocket_server,
)

# In GobbyRunner.run():
await self.message_processor.start()

# In cleanup:
await self.message_processor.stop()
```

### WebSocket Broadcasting

```python
# New event type: session_message
{
    "type": "session_message",
    "session_id": "...",
    "message": {
        "index": 5,
        "role": "assistant",
        "content": "...",
        "content_type": "text",
        "tool_name": null,
        "timestamp": "2025-01-15T..."
    }
}
```

---

## Configuration

```python
# src/config/app.py

@dataclass
class MessageTrackingConfig:
    enabled: bool = True
    poll_interval: float = 5.0
    debounce_delay: float = 1.0
    max_message_length: int = 10000
    broadcast_enabled: bool = True
```

---

## Implementation Phases

### Phase 1: Foundation

- [x] 1.1 Add migration 14 for `session_messages` and `session_message_state` tables
- [x] 1.2 Create `LocalMessageManager` in `src/storage/messages.py`
- [x] 1.3 Add `ParsedMessage` dataclass to `src/sessions/transcripts/base.py`
- [x] 1.4 Extend `ClaudeTranscriptParser` with `parse_line()` and `parse_lines()` methods
- [x] 1.5 Write unit tests for message storage and parsing

### Phase 2: Async Processor

- [x] 2.1 Create `SessionMessageProcessor` in `src/sessions/processor.py`
- [x] 2.2 Create `SessionTracker` dataclass
- [x] 2.3 Implement byte offset tracking for incremental reads
- [x] 2.4 Add debounce logic (reference `TaskSyncManager` pattern)
- [x] 2.5 Write integration tests for polling loop

### Phase 3: Integration

- [x] 3.1 Integrate `SessionMessageProcessor` into `GobbyRunner`
- [x] 3.2 Hook into `HookManager` session start/end events
- [x] 3.3 Add `MessageTrackingConfig` to `DaemonConfig`
- [x] 3.4 Handle graceful shutdown with final flush
- [x] 3.5 End-to-end testing with mock sessions

### Phase 4: WebSocket Broadcasting

- [x] 4.1 Add `session_message` event type to WebSocket
- [x] 4.2 Implement subscription filtering for message events
- [x] 4.3 Add content truncation config
- [-] 4.4 Performance testing with high message volume (Deferred)

### Phase 5: Additional Parsers

- [x] 5.1 Create `GeminiTranscriptParser` in `src/sessions/transcripts/gemini.py`
- [x] 5.2 Create `CodexTranscriptParser` in `src/sessions/transcripts/codex.py`
- [x] 5.3 Add parser registry in `src/sessions/transcripts/__init__.py`
- [x] 5.4 Test with actual Gemini/Codex transcripts
- [x] 5.5 Handle Antigravity (uses Gemini parser)

### Phase 6: Query API

- [x] 6.1 Add HTTP endpoints for message queries (`GET /sessions/{id}/messages`)
- [x] 6.2 Add `gobby-messages` internal tool registry
- [x] 6.3 Implement full-text search across messages
- [x] 6.4 Add message count to session list responses

---

## Critical Files

| File | Changes |
|------|---------|
| `src/storage/migrations.py` | Add migration 14 |
| `src/storage/messages.py` | NEW - LocalMessageManager |
| `src/sessions/transcripts/base.py` | Add ParsedMessage, extend protocol |
| `src/sessions/transcripts/claude.py` | Add parse_line/parse_lines |
| `src/sessions/transcripts/gemini.py` | NEW - GeminiTranscriptParser |
| `src/sessions/transcripts/codex.py` | NEW - CodexTranscriptParser |
| `src/sessions/transcripts/__init__.py` | Add PARSER_REGISTRY |
| `src/sessions/processor.py` | NEW - SessionMessageProcessor |
| `src/runner.py` | Integrate processor lifecycle |
| `src/hooks/hook_manager.py` | Wire session events |
| `src/config/app.py` | Add MessageTrackingConfig |
| `src/servers/http.py` | Add message query endpoints |
| `src/mcp_proxy/tools/messages.py` | NEW - message MCP tools |

---

## Success Criteria

- [ ] Messages from all CLIs stored in SQLite
- [ ] Incremental processing (no full-file re-reads)
- [ ] WebSocket broadcasts new messages in real-time
- [ ] Search API returns messages across sessions
- [ ] New parsers can be added by implementing protocol
- [ ] Memory system can hook into message stream

# Plan: Session Blob Storage & Restore

## Context

Claude Code stores session transcripts as JSONL files at `~/.claude/projects/{project_hash}/{session_uuid}.jsonl`. Gobby currently parses these files incrementally via `SessionMessageProcessor`, normalizes messages through `ClaudeTranscriptParser`, and stores them row-by-row in the `session_messages` table.

Problems with the current approach:
- **Claude purges old sessions.** Once the JSONL file is deleted, the session cannot be resumed — even though Gobby has the normalized messages, they can't be reconstituted into a valid JSONL file for `--resume`.
- **No cross-machine portability.** Session history is tied to the filesystem of the machine that ran it.
- **Normalization is lossy.** The `ParsedMessage` pipeline discards structural details (line ordering, metadata lines, block grouping) that CLIs need for resume.
- **Redundant work.** Parsing every message into individual DB rows just to serve them back as a list is overhead when the raw file is the authoritative format.

## This Plan Covers

1. Compressed blob storage of raw JSONL transcripts in the DB
2. Incremental blob updates during session processing
3. Restore flow: write blob back to filesystem for `--resume`
4. Cross-machine path reconstruction
5. Claude-specific transcript parser removal roadmap

## Relationship to Unified SDK Epic

These are companion epics, scoped to Claude:
- **SDK epic**: Claude autonomous/interactive sessions run in-process via SDK. Messages arrive through callbacks — no transcript file, no parsing.
- **This epic**: Claude terminal sessions store the raw JSONL blob. No parsing needed — just store and restore.

Once both land, `ClaudeTranscriptParser` and Claude-specific paths in `SessionMessageProcessor` become dead code. Gemini/Codex/Cursor/Windsurf/Copilot pipelines are unaffected — both plans serve as templates for those CLIs later.

## Changes

### 1. New table: `session_transcripts`

Separate table to keep `sessions` lightweight. Blobs can be 10-50MB uncompressed for heavy sessions; gzip'd JSONL compresses ~5-10x.

```sql
CREATE TABLE session_transcripts (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    transcript_blob BLOB NOT NULL,
    uncompressed_size INTEGER NOT NULL,
    compressed_size INTEGER NOT NULL,
    last_byte_offset INTEGER DEFAULT 0,
    checksum TEXT NOT NULL,               -- SHA-256 of uncompressed content
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**Why a separate table:** SQLite reads entire rows even when selecting one column. Keeping blobs out of `sessions` means listing/querying sessions stays fast.

### 2. New file: `src/gobby/storage/session_transcripts.py` (~120 lines)

**`LocalSessionTranscriptManager`**:

```python
class LocalSessionTranscriptManager:
    def __init__(self, db: LocalDatabase): ...

    def store_transcript(self, session_id: str, raw_content: bytes) -> dict:
        """Compress and store raw JSONL content. Returns size stats."""
        # gzip compress, SHA-256 checksum, UPSERT into session_transcripts

    def get_transcript(self, session_id: str) -> bytes | None:
        """Retrieve and decompress raw JSONL content."""

    def restore_to_disk(self, session_id: str, path: str | None = None) -> str:
        """Decompress blob and write to filesystem. Returns path written.
        If path is None, uses sessions.jsonl_path."""

    def has_transcript(self, session_id: str) -> bool:
        """Check if blob exists without decompressing."""

    def delete_transcript(self, session_id: str) -> bool:
        """Remove blob (e.g., after confirmed session deletion)."""

    def get_stats(self, session_id: str) -> dict | None:
        """Return size stats without decompressing blob."""
```

### 3. `src/gobby/sessions/processor.py` — Blob capture during polling

Piggyback on existing `SessionMessageProcessor._process_session()`. After reading new bytes and parsing messages, also snapshot the full file:

```python
async def _process_session(self, session_id: str, transcript_path: str) -> None:
    # ... existing JSONL processing (read new bytes, parse, store messages) ...

    # Blob capture (throttled)
    if new_messages_found and self._transcript_manager:
        if self._should_snapshot(session_id):
            raw_content = await asyncio.to_thread(self._read_full_file, transcript_path)
            await asyncio.to_thread(
                self._transcript_manager.store_transcript, session_id, raw_content
            )
```

**Throttle rules** — snapshot when:
- First time (no existing blob)
- 60+ seconds since last snapshot
- Session status transitions to non-active (close, archive)

### 4. New HTTP endpoints: `src/gobby/servers/routes/sessions.py`

```
POST /api/sessions/{session_id}/restore-transcript
  → {"status": "success", "path": "...", "size": 12345}

GET  /api/sessions/{session_id}/transcript
  → raw JSONL content (Content-Type: application/x-ndjson)

GET  /api/sessions/{session_id}/transcript/status
  → {"exists": true, "compressed_size": 1234, "uncompressed_size": 8765, "checksum": "sha256:..."}
```

### 5. New MCP tool: `restore_session_transcript` on `gobby-sessions`

For agent/CLI use:

```python
def restore_session_transcript(session_id: str, target_path: str | None = None) -> dict:
    """Restore a session transcript to disk for CLI resume.
    If target_path is None, restores to the original jsonl_path.
    If the original path's directory doesn't exist (different machine),
    reconstructs from external_id + source + project path.
    """
```

### 6. Cross-machine path reconstruction

When restoring on a different machine, `jsonl_path` won't match. Reconstruct:

```python
def _reconstruct_claude_path(session: Session, project_path: str) -> str:
    """Claude uses: ~/.claude/projects/{path_hash}/{external_id}.jsonl
    where path_hash is the project path with / replaced by -
    """
    path_hash = project_path.replace("/", "-")
    return os.path.expanduser(
        f"~/.claude/projects/{path_hash}/{session.external_id}.jsonl"
    )
```

We already store `external_id` (Claude's session UUID) and `source` on the session. Project path is available from the `projects` table.

### 7. Session lifecycle integration

**On session close/archive:**
- Force a final blob snapshot (bypass throttle)
- Ensures the blob is complete before the JSONL file might be purged

**On daemon startup:**
- For active sessions with `jsonl_path` but no blob: do initial snapshot
- For sessions with blob but missing `jsonl_path` file: log info (file may have been purged — this is expected)

**New CLI command:**
```bash
gobby sessions restore <session-ref>    # Restore transcript to disk
gobby sessions restore --all            # Restore all sessions with blobs
```

### 8. Migration: `src/gobby/storage/migrations.py`

New migration (next version) to create `session_transcripts` table. No data migration needed — blobs populate incrementally as sessions are active.

## Files

| File | Change |
|------|--------|
| `src/gobby/storage/session_transcripts.py` | **NEW** — LocalSessionTranscriptManager |
| `src/gobby/storage/migrations.py` | Add session_transcripts table |
| `src/gobby/sessions/processor.py` | Blob capture during polling (throttled) |
| `src/gobby/servers/routes/sessions.py` | Restore/download/status endpoints |
| `src/gobby/mcp_proxy/tools/sessions.py` | restore_session_transcript tool |
| `src/gobby/cli/sessions.py` | `gobby sessions restore` command |
| `src/gobby/runner.py` | Wire transcript_manager into processor |
| `src/gobby/sessions/lifecycle.py` | Final snapshot on close/archive |

## Key Reuse

| What | From |
|------|------|
| `SessionMessageProcessor` polling loop | `sessions/processor.py` (add blob capture) |
| `LocalSessionManager` | `storage/sessions.py` (session metadata lookup) |
| Session route patterns | `servers/routes/sessions.py` (existing CRUD) |
| MCP tool registration | `mcp_proxy/tools/sessions.py` (existing tools) |

## Deferred (follow-up work)

- **Claude parser removal** — Once SDK epic lands and blob storage handles terminal sessions, `ClaudeTranscriptParser` + Claude-specific processor paths become dead code. Remove in a cleanup pass after both epics are validated.
- **Backfill existing sessions** — One-time job to snapshot blobs for sessions that already have JSONL files on disk. Run as CLI command or maintenance job.
- **Blob sync between machines** — Requires a transport layer (git LFS, S3, direct daemon-to-daemon). Out of scope — this plan ensures blobs are in the DB and restorable locally.
- **Gemini/Codex blob storage** — Same pattern, different path reconstruction. Template from this Claude implementation.

## Verification

1. `uv run pytest tests/storage/test_session_transcripts.py -v` — new storage tests
2. `uv run pytest tests/sessions/test_processor.py -v -k "blob"` — blob capture tests
3. `uv run pytest tests/servers/routes/test_sessions.py -v -k "transcript"` — API tests
4. `uv run ruff check src/` — clean
5. Live: Create session → verify blob stored → delete JSONL file → `gobby sessions restore #N` → verify file restored → `claude --resume` works

"""
Session transcript blob storage.

Stores compressed raw JSONL/JSON transcripts in SQLite for backup and restore.
Enables session resume after CLI purges the original transcript file.
"""

import gzip
import hashlib
import logging
import os
import time
from typing import Any

from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


class LocalSessionTranscriptManager:
    """Manages compressed transcript blob storage in the database."""

    def __init__(self, db: DatabaseProtocol):
        self.db = db

    def store_transcript(self, session_id: str, raw_content: bytes) -> dict[str, Any]:
        """Compress and store raw transcript content.

        Uses gzip compression and SHA-256 checksum. UPSERT semantics —
        safe to call repeatedly for the same session.

        Returns:
            Dict with compressed_size, uncompressed_size, checksum.
        """
        checksum = hashlib.sha256(raw_content).hexdigest()
        compressed = gzip.compress(raw_content, compresslevel=6)
        uncompressed_size = len(raw_content)
        compressed_size = len(compressed)

        with self.db.transaction_immediate() as conn:
            conn.execute(
                """
                INSERT INTO session_transcripts
                    (session_id, transcript_blob, uncompressed_size,
                     compressed_size, checksum)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    transcript_blob = excluded.transcript_blob,
                    uncompressed_size = excluded.uncompressed_size,
                    compressed_size = excluded.compressed_size,
                    checksum = excluded.checksum,
                    updated_at = datetime('now')
                """,
                (session_id, compressed, uncompressed_size, compressed_size, checksum),
            )

        logger.debug(
            f"Stored transcript for {session_id}: "
            f"{uncompressed_size} -> {compressed_size} bytes "
            f"({compressed_size / max(uncompressed_size, 1):.1%} ratio)"
        )

        return {
            "compressed_size": compressed_size,
            "uncompressed_size": uncompressed_size,
            "checksum": f"sha256:{checksum}",
        }

    def get_transcript(self, session_id: str) -> bytes | None:
        """Retrieve and decompress raw transcript content."""
        row = self.db.fetchone(
            "SELECT transcript_blob FROM session_transcripts WHERE session_id = ?",
            (session_id,),
        )
        if not row:
            return None
        return gzip.decompress(row["transcript_blob"])

    def restore_to_disk(self, session_id: str, path: str | None = None) -> str | None:
        """Decompress blob and write to filesystem.

        Args:
            session_id: Session ID to restore.
            path: Target file path. If None, looks up jsonl_path from sessions table.

        Returns:
            Path written, or None if no blob exists.
        """
        raw = self.get_transcript(session_id)
        if raw is None:
            return None

        if path is None:
            row = self.db.fetchone(
                "SELECT jsonl_path FROM sessions WHERE id = ?",
                (session_id,),
            )
            if not row or not row["jsonl_path"]:
                logger.warning(f"No jsonl_path for session {session_id}, cannot restore")
                return None
            path = row["jsonl_path"]

        # Ensure parent directory exists
        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "wb") as f:
            f.write(raw)

        logger.info(f"Restored transcript for {session_id} to {path} ({len(raw)} bytes)")
        return path

    def has_transcript(self, session_id: str) -> bool:
        """Check if blob exists without decompressing."""
        row = self.db.fetchone(
            "SELECT 1 FROM session_transcripts WHERE session_id = ?",
            (session_id,),
        )
        return row is not None

    def delete_transcript(self, session_id: str) -> bool:
        """Remove transcript blob."""
        cursor = self.db.execute(
            "DELETE FROM session_transcripts WHERE session_id = ?",
            (session_id,),
        )
        return bool(cursor.rowcount and cursor.rowcount > 0)

    def get_stats(self, session_id: str) -> dict[str, Any] | None:
        """Return size stats without decompressing blob."""
        row = self.db.fetchone(
            """SELECT compressed_size, uncompressed_size, checksum,
                      created_at, updated_at
               FROM session_transcripts WHERE session_id = ?""",
            (session_id,),
        )
        if not row:
            return None
        return {
            "exists": True,
            "compressed_size": row["compressed_size"],
            "uncompressed_size": row["uncompressed_size"],
            "checksum": f"sha256:{row['checksum']}",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


class TranscriptSnapshotThrottle:
    """Tracks when to snapshot transcripts (throttling logic).

    Snapshots when:
    - First time (no existing blob)
    - 60+ seconds since last snapshot
    - Force flag set (e.g., session close)
    """

    def __init__(self, interval_seconds: float = 60.0):
        self._interval = interval_seconds
        self._last_snapshot: dict[str, float] = {}

    def should_snapshot(self, session_id: str, *, force: bool = False) -> bool:
        if force:
            return True
        last = self._last_snapshot.get(session_id)
        if last is None:
            return True
        return (time.monotonic() - last) >= self._interval

    def record_snapshot(self, session_id: str) -> None:
        self._last_snapshot[session_id] = time.monotonic()

    def remove(self, session_id: str) -> None:
        self._last_snapshot.pop(session_id, None)

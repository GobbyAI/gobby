"""Summary update helpers for session storage."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from gobby.storage.session_models import Session
from gobby.storage.sessions._summary_protocols import SummaryUpdateHost as _SummaryUpdateHost


def _encode_metadata_json(metadata_json: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(metadata_json or {}), sort_keys=True)


def _decode_metadata_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _summary_revision_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "summary_markdown": row["summary_markdown"],
        "generation_mode": row["generation_mode"],
        "source_context_hash": row["source_context_hash"],
        "source_digest_turn_count": row["source_digest_turn_count"],
        "previous_revision_id": row["previous_revision_id"],
        "metadata_json": _decode_metadata_json(row["metadata_json"]),
        "created_at": row["created_at"],
    }


def _wiki_revision_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "wiki_markdown": row["wiki_markdown"],
        "generation_mode": row["generation_mode"],
        "source_context_hash": row["source_context_hash"],
        "digest_turn_count": row["digest_turn_count"],
        "previous_revision_id": row["previous_revision_id"],
        "metadata_json": _decode_metadata_json(row["metadata_json"]),
        "created_at": row["created_at"],
    }


class _SummaryUpdateMixin:
    def persist_summary_state(
        self: _SummaryUpdateHost,
        session_id: str,
        *,
        summary_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = None,
        source_digest_turn_count: int | None = None,
        previous_revision_id: str | None = None,
        metadata_json: Mapping[str, Any] | None = None,
        summary_path: str | None = None,
    ) -> Session | None:
        """Persist summary markdown, source metadata, and a revision row atomically."""
        if source_digest_turn_count is not None and source_digest_turn_count < 0:
            raise ValueError("source_digest_turn_count must be non-negative")

        now = datetime.now(UTC).isoformat()
        revision_id = str(uuid.uuid4())

        with self.db.transaction() as conn:
            current_row = conn.execute(
                "SELECT summary_revision_id FROM sessions WHERE id = %s FOR UPDATE",
                (session_id,),
            ).fetchone()
            if current_row is None:
                return None
            previous_id = previous_revision_id
            if previous_id is None:
                previous_id = current_row["summary_revision_id"]

            conn.execute(
                """
                INSERT INTO session_summary_revisions (
                    id, session_id, summary_markdown, generation_mode,
                    source_context_hash, source_digest_turn_count,
                    previous_revision_id, metadata_json, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    revision_id,
                    session_id,
                    summary_markdown,
                    generation_mode,
                    source_context_hash,
                    source_digest_turn_count,
                    previous_id,
                    _encode_metadata_json(metadata_json),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE sessions
                SET summary_path = COALESCE(%s, summary_path),
                    summary_markdown = %s,
                    summary_revision_id = %s,
                    summary_source_context_hash = %s,
                    summary_digest_turn_count = %s,
                    summary_generation_mode = %s,
                    summary_generated_at = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    summary_path,
                    summary_markdown,
                    revision_id,
                    source_context_hash,
                    source_digest_turn_count,
                    generation_mode,
                    now,
                    now,
                    session_id,
                ),
            )

        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

    def update_summary(
        self: _SummaryUpdateHost,
        session_id: str,
        summary_path: str | None = None,
        summary_markdown: str | None = None,
    ) -> Session | None:
        """Update session summary."""
        if summary_markdown is not None:
            return self.persist_summary_state(
                session_id,
                summary_markdown=summary_markdown,
                generation_mode="agent_authored",
                source_context_hash=None,
                source_digest_turn_count=None,
                metadata_json={"source": "update_summary"},
                summary_path=summary_path,
            )

        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET summary_path = COALESCE(%s, summary_path),
                    summary_markdown = COALESCE(%s, summary_markdown),
                    updated_at = %s
                WHERE id = %s
                """,
                (summary_path, summary_markdown, now, session_id),
            )
        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

    def get_summary_revision(
        self: _SummaryUpdateHost,
        revision_id: str,
    ) -> dict[str, Any] | None:
        """Return one summary revision row for debug/test callers."""
        row = self.db.fetchone(
            "SELECT * FROM session_summary_revisions WHERE id = %s",
            (revision_id,),
        )
        return _summary_revision_from_row(row) if row else None

    def list_summary_revisions(
        self: _SummaryUpdateHost,
        session_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return recent summary revisions for a session, newest first."""
        bounded_limit = max(1, min(int(limit), 100))
        rows = self.db.fetchall(
            """
            SELECT *
            FROM session_summary_revisions
            WHERE session_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (session_id, bounded_limit),
        )
        return [_summary_revision_from_row(row) for row in rows]

    def persist_wiki_state(
        self: _SummaryUpdateHost,
        session_id: str,
        *,
        wiki_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = None,
        digest_turn_count: int | None = None,
        previous_revision_id: str | None = None,
        metadata_json: Mapping[str, Any] | None = None,
        wiki_path: str | None = None,
    ) -> Session | None:
        """Persist wiki markdown, source metadata, and a revision row atomically.

        Mirror of ``persist_summary_state`` for the knowledge-synthesis (wiki)
        artifact: writes a ``session_wiki_revisions`` row and updates the
        ``sessions.wiki_*`` columns in one transaction. The mirror file is
        written by the sessions layer (gobby.sessions.wiki_synthesis) using the
        same already-redacted body, so the DB column and the file cannot diverge
        on secrets.
        """
        if digest_turn_count is not None and digest_turn_count < 0:
            raise ValueError("digest_turn_count must be non-negative")

        now = datetime.now(UTC).isoformat()
        revision_id = str(uuid.uuid4())

        with self.db.transaction() as conn:
            current_row = conn.execute(
                "SELECT wiki_revision_id FROM sessions WHERE id = %s FOR UPDATE",
                (session_id,),
            ).fetchone()
            if current_row is None:
                return None
            previous_id = previous_revision_id
            if previous_id is None:
                previous_id = current_row["wiki_revision_id"]

            conn.execute(
                """
                INSERT INTO session_wiki_revisions (
                    id, session_id, wiki_markdown, generation_mode,
                    source_context_hash, digest_turn_count,
                    previous_revision_id, metadata_json, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    revision_id,
                    session_id,
                    wiki_markdown,
                    generation_mode,
                    source_context_hash,
                    digest_turn_count,
                    previous_id,
                    _encode_metadata_json(metadata_json),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE sessions
                SET wiki_path = COALESCE(%s, wiki_path),
                    wiki_markdown = %s,
                    wiki_revision_id = %s,
                    wiki_source_context_hash = %s,
                    wiki_digest_turn_count = %s,
                    wiki_generation_mode = %s,
                    wiki_generated_at = %s,
                    wiki_synthesis_consecutive_failures = 0,
                    wiki_synthesis_last_failure_reason = NULL,
                    wiki_synthesis_last_error = NULL,
                    wiki_synthesis_last_failed_at = NULL,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    wiki_path,
                    wiki_markdown,
                    revision_id,
                    source_context_hash,
                    digest_turn_count,
                    generation_mode,
                    now,
                    now,
                    session_id,
                ),
            )

        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

    def record_wiki_synthesis_failure(
        self: _SummaryUpdateHost,
        session_id: str,
        reason: str,
        error: str | None = None,
    ) -> Session | None:
        """Increment consecutive wiki synthesis failures for a session."""
        failure_reason = reason.strip() or "unknown"
        now = datetime.now(UTC).isoformat()

        with self.db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE sessions
                SET wiki_synthesis_consecutive_failures =
                        COALESCE(wiki_synthesis_consecutive_failures, 0) + 1,
                    wiki_synthesis_last_failure_reason = %s,
                    wiki_synthesis_last_error = %s,
                    wiki_synthesis_last_failed_at = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING id
                """,
                (failure_reason, error, now, now, session_id),
            ).fetchone()
            if row is None:
                return None

        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

    def get_wiki_revision(
        self: _SummaryUpdateHost,
        revision_id: str,
    ) -> dict[str, Any] | None:
        """Return one wiki revision row for debug/test callers."""
        row = self.db.fetchone(
            "SELECT * FROM session_wiki_revisions WHERE id = %s",
            (revision_id,),
        )
        return _wiki_revision_from_row(row) if row else None

    def list_wiki_revisions(
        self: _SummaryUpdateHost,
        session_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return recent wiki revisions for a session, newest first."""
        bounded_limit = max(1, min(int(limit), 100))
        rows = self.db.fetchall(
            """
            SELECT *
            FROM session_wiki_revisions
            WHERE session_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (session_id, bounded_limit),
        )
        return [_wiki_revision_from_row(row) for row in rows]

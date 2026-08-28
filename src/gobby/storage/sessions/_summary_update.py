"""Summary update helpers for session storage."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from gobby.storage.session_models import Session
from gobby.storage.sessions._summary_protocols import SummaryUpdateHost as _SummaryUpdateHost
from gobby.utils.datetime import utc_now

from ._update_sentinel import UNSET, UnsetType, is_set


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
        previous_revision_id: str | None = None,
        metadata_json: Mapping[str, Any] | None = None,
        summary_path: str | None | UnsetType = UNSET,
    ) -> Session | None:
        """Persist summary markdown, source metadata, and a revision row atomically."""
        now = utc_now()
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
                    source_context_hash, previous_revision_id, metadata_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    revision_id,
                    session_id,
                    summary_markdown,
                    generation_mode,
                    source_context_hash,
                    previous_id,
                    _encode_metadata_json(metadata_json),
                ),
            )
            conn.execute(
                """
                UPDATE sessions
                SET summary_path = CASE WHEN %s THEN %s ELSE summary_path END,
                    summary_markdown = %s,
                    summary_revision_id = %s,
                    summary_source_context_hash = %s,
                    summary_generation_mode = %s,
                    summary_generated_at = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    is_set(summary_path),
                    summary_path if is_set(summary_path) else None,
                    summary_markdown,
                    revision_id,
                    source_context_hash,
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
        summary_path: str | None | UnsetType = UNSET,
        summary_markdown: str | None | UnsetType = UNSET,
    ) -> Session | None:
        """Update summary fields, preserving omissions and clearing explicit None values."""
        if is_set(summary_markdown) and summary_markdown is not None:
            return self.persist_summary_state(
                session_id,
                summary_markdown=summary_markdown,
                generation_mode="agent_authored",
                source_context_hash=None,
                metadata_json={"source": "update_summary"},
                summary_path=summary_path,
            )

        values: dict[str, Any] = {}
        if is_set(summary_path):
            values["summary_path"] = summary_path
        if is_set(summary_markdown):
            values.update(
                summary_markdown=summary_markdown,
                summary_revision_id=None,
                summary_source_context_hash=None,
                summary_generation_mode=None,
                summary_generated_at=None,
            )
        if not values:
            return self.get(session_id)

        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{column} = %s" for column in values)
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE sessions SET {assignments} WHERE id = %s",
                (*values.values(), session_id),
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

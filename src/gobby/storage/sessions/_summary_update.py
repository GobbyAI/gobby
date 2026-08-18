"""Summary update helpers for session storage."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from gobby.storage.session_models import Session
from gobby.storage.sessions._summary_protocols import SummaryUpdateHost as _SummaryUpdateHost
from gobby.utils.datetime import utc_now

from ._title_update import apply_title_mutation
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
        "source_digest_turn_count": row["source_digest_turn_count"],
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
        summary_path: str | None | UnsetType = UNSET,
    ) -> Session | None:
        """Persist summary markdown, source metadata, and a revision row atomically."""
        if source_digest_turn_count is not None and source_digest_turn_count < 0:
            raise ValueError("source_digest_turn_count must be non-negative")

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
                    source_context_hash, source_digest_turn_count,
                    previous_revision_id, metadata_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
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
                ),
            )
            conn.execute(
                """
                UPDATE sessions
                SET summary_path = CASE WHEN %s THEN %s ELSE summary_path END,
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
                    is_set(summary_path),
                    summary_path if is_set(summary_path) else None,
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

    def persist_digest_state(
        self: _SummaryUpdateHost,
        session_id: str,
        *,
        last_turn_markdown: str,
        digest_markdown: str,
        last_digest_input_hash: str,
        last_digested_pair_index: int,
        title: str | None = None,
        title_source: str | None = None,
    ) -> Session | None:
        """Persist digest fields, optionally updating title metadata atomically."""
        current = self.get(session_id)
        if current is None:
            return None
        if title_source is not None and title_source not in self._VALID_TITLE_SOURCES:
            raise ValueError(
                f"Invalid title_source {title_source!r}. Must be one of: "
                f"{', '.join(sorted(self._VALID_TITLE_SOURCES))}"
            )

        now = utc_now()
        assignments = [
            "last_turn_markdown = %s",
            "digest_markdown = %s",
            "last_digest_input_hash = %s",
            "last_digested_pair_index = %s",
            "updated_at = %s",
        ]
        values: list[Any] = [
            last_turn_markdown,
            digest_markdown,
            last_digest_input_hash,
            last_digested_pair_index,
            now,
        ]

        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = %s",  # nosec B608
                (*values, session_id),
            )
            mutation = apply_title_mutation(
                conn,
                session_id,
                title_is_set=title is not None,
                title=title,
                title_source_is_set=title_source is not None,
                title_source=title_source,
                updated_at=now,
            )

        updated = self.get(session_id)
        if updated is None:
            return None
        self._notify_session_change("session_updated", session_id)
        if mutation is not None and mutation.title_changed:
            self._run_title_change_side_effects(updated, updated.title or "")
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
                source_digest_turn_count=None,
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
                summary_digest_turn_count=None,
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

    def update_digest_markdown(
        self: _SummaryUpdateHost, session_id: str, digest_markdown: str
    ) -> Session | None:
        """Update session rolling digest markdown."""
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE sessions
                SET digest_markdown = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (digest_markdown, now, session_id),
            )
        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

    def update_last_turn_markdown(
        self: _SummaryUpdateHost, session_id: str, last_turn_markdown: str
    ) -> Session | None:
        """Update session last turn markdown record."""
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE sessions
                SET last_turn_markdown = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (last_turn_markdown, now, session_id),
            )
        session = self.get(session_id)
        if session is not None:
            self._notify_session_change("session_updated", session_id)
        return session

    def update_last_digest_input_hash(
        self: _SummaryUpdateHost, session_id: str, hash_value: str
    ) -> None:
        """Update the last digest input hash for idempotency."""
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE sessions
                SET last_digest_input_hash = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (hash_value, now, session_id),
            )

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

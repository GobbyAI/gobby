"""Usage accounting mixin for session storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ._constants import get_logger

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase


class _UsageMixin:
    def update_usage(
        self: _ManagerState,
        session_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
        context_window: int | None = None,
        model: str | None = None,
    ) -> bool:
        """Update session usage statistics."""
        if any(
            value < 0
            for value in (
                input_tokens,
                output_tokens,
                cache_creation_tokens,
                cache_read_tokens,
            )
        ):
            get_logger().warning(
                "Rejected absolute usage update for session %s with negative token counts",
                session_id,
            )
            return False

        query = """
        UPDATE sessions
        SET
            usage_input_tokens = ?,
            usage_output_tokens = ?,
            usage_cache_creation_tokens = ?,
            usage_cache_read_tokens = ?,
            context_window = COALESCE(?, context_window),
            model = COALESCE(?, model),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """
        try:
            with self.db.transaction():
                cursor = self.db.execute(
                    query,
                    (
                        input_tokens,
                        output_tokens,
                        cache_creation_tokens,
                        cache_read_tokens,
                        context_window,
                        model,
                        session_id,
                    ),
                )
                return cursor.rowcount > 0
        except Exception as e:
            get_logger().error(f"Failed to update session usage {session_id}: {e}", exc_info=True)
            return False

    def add_usage_delta(
        self: _ManagerState,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        context_window: int | None = None,
        model: str | None = None,
    ) -> bool:
        """Increment session usage statistics atomically."""
        query = """
        UPDATE sessions
        SET
            usage_input_tokens = CASE
                WHEN COALESCE(usage_input_tokens, 0) + ? < 0 THEN 0
                ELSE COALESCE(usage_input_tokens, 0) + ?
            END,
            usage_output_tokens = CASE
                WHEN COALESCE(usage_output_tokens, 0) + ? < 0 THEN 0
                ELSE COALESCE(usage_output_tokens, 0) + ?
            END,
            usage_cache_creation_tokens = CASE
                WHEN COALESCE(usage_cache_creation_tokens, 0) + ? < 0 THEN 0
                ELSE COALESCE(usage_cache_creation_tokens, 0) + ?
            END,
            usage_cache_read_tokens = CASE
                WHEN COALESCE(usage_cache_read_tokens, 0) + ? < 0 THEN 0
                ELSE COALESCE(usage_cache_read_tokens, 0) + ?
            END,
            context_window = COALESCE(?, context_window),
            model = COALESCE(?, model),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """
        try:
            with self.db.transaction():
                cursor = self.db.execute(
                    query,
                    (
                        input_tokens,
                        input_tokens,
                        output_tokens,
                        output_tokens,
                        cache_creation_tokens,
                        cache_creation_tokens,
                        cache_read_tokens,
                        cache_read_tokens,
                        context_window,
                        model,
                        session_id,
                    ),
                )
                return cursor.rowcount > 0
        except Exception as e:
            get_logger().error(
                f"Failed to add usage delta for session {session_id}: {e}",
                exc_info=True,
            )
            return False

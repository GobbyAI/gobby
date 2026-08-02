"""Storage manager for model metadata from OpenRouter registry.

Stores context_length and max_completion_tokens for model lookups.
Pricing data has been removed — tokens are tracked directly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

from gobby.llm.context_window_values import positive_context_window

if TYPE_CHECKING:
    from gobby.llm.model_registry import ModelInfo
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)
MODEL_METADATA_STALE_AFTER = timedelta(hours=48)
_stale_warning_emitted = False


class ModelMetadata(NamedTuple):
    """Model metadata from registry."""

    context_length: int | None = None
    max_completion_tokens: int | None = None


class ModelMetadataStore:
    """Manages model metadata populated from OpenRouter's model registry.

    The registry stores context windows and output-token limits.
    """

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def populate(self, models: list[ModelInfo] | None = None) -> int:
        """Clear and bulk-insert model metadata from OpenRouter registry.

        Args:
            models: Pre-fetched model data. If None, fetches from OpenRouter.

        Returns:
            Number of models inserted.
        """
        global _stale_warning_emitted

        if models is None:
            from gobby.llm.model_registry import fetch_models_sync

            models = fetch_models_sync()

        if not models:
            logger.warning("No models available — keeping existing cached metadata")
            return 0

        from gobby.llm.model_registry import normalize_model_id

        by_model: dict[str, tuple[str, int, int | None, str]] = {}
        for m in models:
            model_key = normalize_model_id(m.id)
            existing = by_model.get(model_key)
            if existing is not None and m.context_length <= existing[1]:
                continue
            by_model[model_key] = (
                model_key,
                m.context_length,
                m.max_completion_tokens,
                "registry",
            )
        rows = list(by_model.values())

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM model_metadata")
            conn.executemany(
                "INSERT INTO model_metadata (model, "
                "context_length, max_completion_tokens, "
                "source) VALUES (%s, %s, %s, %s)",
                rows,
            )

        _stale_warning_emitted = False
        logger.info("Populated model_metadata table with %s models from registry", len(rows))
        return len(rows)

    @staticmethod
    def _warn_if_stale(row: object) -> None:
        global _stale_warning_emitted

        if _stale_warning_emitted:
            return
        updated_at = row.get("metadata_updated_at") if isinstance(row, dict) else None
        if not isinstance(updated_at, datetime):
            return
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if datetime.now(UTC) - updated_at > MODEL_METADATA_STALE_AFTER:
            _stale_warning_emitted = True
            logger.warning("Model metadata cache is older than 48 hours")

    def get_all(self) -> dict[str, ModelMetadata]:
        """Return all model metadata keyed by bare model ID."""
        rows = self.db.fetchall(
            "SELECT model, context_length, max_completion_tokens, "
            "MAX(updated_at) OVER () AS metadata_updated_at FROM model_metadata"
        )
        if rows:
            self._warn_if_stale(rows[0])
        return {
            row["model"]: ModelMetadata(
                context_length=row["context_length"],
                max_completion_tokens=row["max_completion_tokens"],
            )
            for row in rows
        }

    def get_context_window(self, model: str) -> int | None:
        """Look up context_length for a model (exact match, then prefix match)."""
        from gobby.llm.model_registry import normalize_model_id

        model = normalize_model_id(model)

        row = self.db.fetchone(
            "SELECT context_length, updated_at AS metadata_updated_at FROM model_metadata "
            "WHERE model = %s AND context_length > 0",
            (model,),
        )
        self._warn_if_stale(row)
        exact_context_window = positive_context_window(row["context_length"] if row else None)
        if exact_context_window is not None:
            return exact_context_window

        # Prefix match — find longest matching model key via SQL
        row = self.db.fetchone(
            "SELECT context_length, updated_at AS metadata_updated_at FROM model_metadata "
            "WHERE LEFT(%s, LENGTH(model)) = model AND context_length > 0 "
            "ORDER BY LENGTH(model) DESC LIMIT 1",
            (model,),
        )
        self._warn_if_stale(row)
        return positive_context_window(row["context_length"] if row else None)

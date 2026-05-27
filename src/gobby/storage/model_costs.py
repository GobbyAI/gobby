"""Storage manager for model metadata from OpenRouter registry.

Stores context_length and max_completion_tokens for model lookups.
Pricing data has been removed — tokens are tracked directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from gobby.llm.model_registry import ModelInfo
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


class ModelMetadata(NamedTuple):
    """Model metadata from registry."""

    context_length: int | None = None
    max_completion_tokens: int | None = None


class ModelCostStore:
    """Manages the model_costs table populated from OpenRouter's model registry.

    Despite the legacy table name, this now only stores model metadata
    (context_length, max_completion_tokens), not pricing data.
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
        if models is None:
            from gobby.llm.model_registry import fetch_models_sync

            models = fetch_models_sync()

        if not models:
            logger.warning("No models available — keeping existing cached metadata")
            return 0

        from gobby.llm.model_registry import strip_provider_prefix

        rows: list[tuple[str, str, int, int | None, str]] = []
        for m in models:
            model_key = strip_provider_prefix(m.id)
            rows.append(
                (
                    model_key,
                    m.provider,
                    m.context_length,
                    m.max_completion_tokens,
                    "registry",
                )
            )

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM model_costs")
            conn.executemany(
                "INSERT INTO model_costs (model, provider, "
                "context_length, max_completion_tokens, "
                "source) VALUES (%s, %s, %s, %s, %s)",
                rows,
            )

        logger.info(f"Populated model_costs table with {len(rows)} models from registry")
        return len(rows)

    def get_all(self) -> dict[str, ModelMetadata]:
        """Return all model metadata as {model: ModelMetadata}."""
        rows = self.db.fetchall(
            "SELECT model, context_length, max_completion_tokens FROM model_costs"
        )
        return {
            row["model"]: ModelMetadata(
                context_length=row["context_length"],
                max_completion_tokens=row["max_completion_tokens"],
            )
            for row in rows
        }

    def get_context_window(self, model: str) -> int | None:
        """Look up context_length for a model (exact match, then prefix match)."""
        from gobby.llm.model_registry import strip_provider_prefix

        model = strip_provider_prefix(model)

        row = self.db.fetchone("SELECT context_length FROM model_costs WHERE model = %s", (model,))
        if row and row["context_length"]:
            return int(row["context_length"])

        # Prefix match — find longest matching model key via SQL
        row = self.db.fetchone(
            "SELECT context_length FROM model_costs "
            "WHERE %s LIKE model || '%%' AND context_length IS NOT NULL "
            "ORDER BY LENGTH(model) DESC LIMIT 1",
            (model,),
        )
        return int(row["context_length"]) if row else None

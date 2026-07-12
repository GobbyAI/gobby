"""Storage manager for model metadata from OpenRouter registry.

Stores context_length and max_completion_tokens for model lookups.
Pricing data has been removed — tokens are tracked directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from gobby.llm.context_window_values import positive_context_window

if TYPE_CHECKING:
    from gobby.llm.model_registry import ModelInfo
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def _model_lookup_parts(model_id: str) -> tuple[str | None, str]:
    """Return an optional Gobby provider and the provider-local model id."""
    from gobby.llm.model_registry import PROVIDER_MAP, strip_provider_prefix

    if "/" in model_id:
        prefix, suffix = model_id.split("/", 1)
        provider = PROVIDER_MAP.get(f"{prefix}/")
        if provider is not None:
            return provider, suffix
        if prefix in PROVIDER_MAP.values():
            return prefix, suffix
    return None, strip_provider_prefix(model_id)


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
        """Return all model metadata keyed by ``provider/model``."""
        rows = self.db.fetchall(
            "SELECT provider, model, context_length, max_completion_tokens FROM model_costs"
        )
        return {
            f"{row['provider']}/{row['model']}": ModelMetadata(
                context_length=row["context_length"],
                max_completion_tokens=row["max_completion_tokens"],
            )
            for row in rows
        }

    def get_context_window(self, model: str) -> int | None:
        """Look up context_length for a model (exact match, then prefix match)."""
        provider, model = _model_lookup_parts(model)

        if provider is None:
            exact_query = (
                "SELECT context_length FROM model_costs "
                "WHERE model = %s AND context_length > 0 "
                "ORDER BY provider LIMIT 1"
            )
            exact_params: tuple[str, ...] = (model,)
        else:
            exact_query = (
                "SELECT context_length FROM model_costs "
                "WHERE provider = %s AND model = %s AND context_length > 0"
            )
            exact_params = (provider, model)
        row = self.db.fetchone(exact_query, exact_params)
        exact_context_window = positive_context_window(row["context_length"] if row else None)
        if exact_context_window is not None:
            return exact_context_window

        # Prefix match — find longest matching model key via SQL
        prefix_params: tuple[str, ...]
        if provider is None:
            prefix_query = (
                "SELECT context_length FROM model_costs "
                "WHERE LEFT(%s, LENGTH(model)) = model AND context_length > 0 "
                "ORDER BY LENGTH(model) DESC, provider LIMIT 1"
            )
            prefix_params = (model,)
        else:
            prefix_query = (
                "SELECT context_length FROM model_costs "
                "WHERE provider = %s AND LEFT(%s, LENGTH(model)) = model "
                "AND context_length > 0 "
                "ORDER BY LENGTH(model) DESC, provider LIMIT 1"
            )
            prefix_params = (provider, model)
        row = self.db.fetchone(prefix_query, prefix_params)
        return positive_context_window(row["context_length"] if row else None)

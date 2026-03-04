"""Storage manager for cached model costs from LiteLLM."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


class ModelCostStore:
    """Manages the model_costs table populated from LiteLLM's registry."""

    def __init__(self, db: DatabaseProtocol) -> None:
        self.db = db

    def populate_from_litellm(self) -> int:
        """Clear and bulk-insert costs from litellm.model_cost.

        Returns:
            Number of models inserted.
        """
        try:
            import litellm
        except ImportError:
            logger.warning("litellm not installed — skipping model cost population")
            return 0

        rows: list[tuple[str, str | None, float, float, str]] = []
        for model, info in litellm.model_cost.items():
            input_cost = info.get("input_cost_per_token")
            output_cost = info.get("output_cost_per_token")
            if input_cost is None or output_cost is None:
                continue
            # Skip entries with zero costs for both (image/audio-only models)
            if input_cost == 0 and output_cost == 0:
                continue
            provider = info.get("litellm_provider")
            rows.append((model, provider, float(input_cost), float(output_cost), "litellm"))

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM model_costs")
            conn.executemany(
                "INSERT INTO model_costs (model, provider, input_cost_per_token, "
                "output_cost_per_token, source) VALUES (?, ?, ?, ?, ?)",
                rows,
            )

        logger.info(f"Populated model_costs table with {len(rows)} models from LiteLLM")
        return len(rows)

    def get_all(self) -> dict[str, tuple[float, float]]:
        """Return all cached costs as {model: (input_cost, output_cost)}."""
        rows = self.db.fetchall(
            "SELECT model, input_cost_per_token, output_cost_per_token FROM model_costs"
        )
        return {
            row["model"]: (row["input_cost_per_token"], row["output_cost_per_token"])
            for row in rows
        }

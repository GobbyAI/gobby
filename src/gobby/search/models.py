"""Search models and configuration.

This module defines the core data structures for the unified search layer:
- SearchMode: Enum for search modes (keyword, embedding, auto, hybrid)
- SearchConfig: Configuration for search behavior
- FallbackEvent: Event emitted when falling back to keyword search
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SearchMode(str, Enum):
    """Search mode options for UnifiedSearcher.

    Modes:
    - KEYWORD: PostgreSQL keyword search only
    - EMBEDDING: Embedding-based search only (fails if unavailable)
    - AUTO: Try embedding, fallback to keyword if unavailable
    - HYBRID: Combine both with weighted scores
    """

    KEYWORD = "keyword"
    EMBEDDING = "embedding"
    AUTO = "auto"
    HYBRID = "hybrid"


class SearchConfig(BaseModel):
    """Configuration for unified search with fallback.

    This config controls how UnifiedSearcher behaves, including:
    - Which search mode to use (keyword, embedding, auto, hybrid)
    - Weights for hybrid mode
    - Whether to notify on fallback

    Embedding model/endpoint/key are configured once in EmbeddingsConfig
    and passed to search consumers as constructor args.

    Supported modes:
    - keyword: PostgreSQL keyword search only
    - embedding: Embedding-based search only (fails if unavailable)
    - auto: Try embedding, fallback to keyword if unavailable
    - hybrid: Combine both with weighted scores
    """

    model_config = ConfigDict(populate_by_name=True)

    mode: str = Field(
        default="auto",
        description="Search mode: keyword, embedding, auto, hybrid",
    )
    keyword_weight: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description="Weight for keyword scores in hybrid mode",
    )
    embedding_weight: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Weight for embedding scores in hybrid mode",
    )
    notify_on_fallback: bool = Field(
        default=True,
        description="Log warning when falling back to keyword search",
    )

    def get_normalized_weights(self) -> tuple[float, float]:
        """Get normalized weights that sum to 1.0.

        Returns:
            Tuple of (keyword_weight, embedding_weight) normalized to sum to 1.0
        """
        total = self.keyword_weight + self.embedding_weight
        if total == 0:
            # Default to equal weights if both are 0
            return (0.5, 0.5)
        return (self.keyword_weight / total, self.embedding_weight / total)

    def get_mode_enum(self) -> SearchMode:
        """Get the SearchMode enum instance for the configured mode.

        Returns:
            SearchMode enum corresponding to the mode string value

        Raises:
            ValueError: If the configured mode is not a valid SearchMode
        """
        try:
            return SearchMode(self.mode)
        except ValueError as e:
            valid_modes = [m.value for m in SearchMode]
            raise ValueError(
                f"Invalid search mode '{self.mode}'. Valid modes are: {', '.join(valid_modes)}"
            ) from e


@dataclass
class FallbackEvent:
    """Event emitted when UnifiedSearcher falls back to keyword search.

    This event is emitted via the event_callback when:
    - Embedding provider is unavailable (no API key, no connection)
    - Embedding API call fails (rate limit, timeout, error)
    - Any other embedding-related error occurs

    Attributes:
        reason: Human-readable explanation of why fallback occurred
        original_error: The underlying exception, if any
        timestamp: When the fallback occurred
        mode: The original search mode that was attempted
        items_reindexed: Number of items reindexed into keyword backend (if applicable)
        metadata: Additional context about the fallback
    """

    reason: str
    original_error: Exception | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    mode: str = "auto"
    items_reindexed: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "reason": self.reason,
            "original_error": str(self.original_error) if self.original_error else None,
            "timestamp": self.timestamp.isoformat(),
            "mode": self.mode,
            "items_reindexed": self.items_reindexed,
            "metadata": self.metadata,
        }

    def __str__(self) -> str:
        """Human-readable string representation."""
        error_info = f" ({self.original_error})" if self.original_error else ""
        return f"FallbackEvent: {self.reason}{error_info}"

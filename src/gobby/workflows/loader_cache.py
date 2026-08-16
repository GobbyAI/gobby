"""Cache dataclasses and helpers for PipelineLoader.

Revision-aware: entries store get_definitions_revision("pipelines") and are
re-fetched when that counter drifts.
"""

from dataclasses import dataclass
from pathlib import Path

from .pipeline_models import PipelineDefinition


@dataclass
class DiscoveredWorkflow:
    """A discovered pipeline with metadata for ordering."""

    name: str
    definition: PipelineDefinition
    priority: int  # Lower = higher priority (runs first)
    is_project: bool  # True if from project, False if global
    path: Path


@dataclass
class _CachedEntry:
    """Cache entry for a single pipeline definition."""

    definition: PipelineDefinition
    revision: int
    path: Path | None = None
    mtime: float = 0.0


@dataclass
class _CachedDiscovery:
    """Cache entry for pipeline discovery results."""

    results: list[DiscoveredWorkflow]
    revision: int


def clear_cache(
    cache: dict[str, _CachedEntry],
    discovery_cache: dict[str, _CachedDiscovery],
) -> None:
    """Clear the pipeline definition and discovery caches."""
    cache.clear()
    discovery_cache.clear()

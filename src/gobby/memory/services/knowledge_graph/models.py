"""Data models for memory knowledge graph projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


@dataclass
class Entity:
    """An extracted entity from content."""

    name: str
    entity_type: str


@dataclass
class Relationship:
    """An extracted relationship between entities."""

    source: str
    target: str
    relationship: str


class KnowledgeGraphStatus(StrEnum):
    """Status for a knowledge-graph projection attempt."""

    SUCCESS = "success"
    NOOP_NO_ENTITIES = "noop_no_entities"
    PARTIAL_FAILURE = "partial_failure"
    RETRYABLE_FAILURE = "retryable_failure"
    DETERMINISTIC_FAILURE = "deterministic_failure"


@dataclass
class KnowledgeGraphResult:
    """Result of a knowledge-graph projection attempt."""

    status: KnowledgeGraphStatus
    entities_extracted: int = 0
    relationships_extracted: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class _GraphEntity:
    """Normalized entity record used for FalkorDB writes."""

    entity_key: str
    name: str
    entity_type: str
    project_id: str | None
    normalized_name: str

"""Entity and relationship normalization helpers."""

from __future__ import annotations

import logging
import re
import unicodedata

from gobby.memory.falkor_client import normalize_relationship_type
from gobby.memory.identity import entity_key, normalize_entity_name
from gobby.storage.projects import GLOBAL_PROJECT_ID

from .models import Entity, Relationship, _GraphEntity

_DISPLAY_WHITESPACE_RE = re.compile(r"\s+")
logger = logging.getLogger(__name__)


def display_entity_name(name: str) -> str:
    """Normalize an entity name for display while preserving case."""
    normalized = unicodedata.normalize("NFKC", name)
    normalized = normalized.strip()
    return _DISPLAY_WHITESPACE_RE.sub(" ", normalized)


def normalize_entities(
    entities: list[Entity],
    *,
    project_id: str,
    is_global: bool,
) -> list[_GraphEntity]:
    """Normalize and deduplicate extracted entities by stable key."""
    deduped: dict[str, _GraphEntity] = {}
    for entity in entities:
        display_name = display_entity_name(entity.name)
        if not display_name:
            logger.debug("Dropped entity with empty display name: %r", entity.name)
            continue
        normalized_name = normalize_entity_name(display_name)
        if not normalized_name:
            logger.debug("Dropped entity with empty normalized name: %r", entity.name)
            continue
        entity_project_id = GLOBAL_PROJECT_ID if is_global else project_id
        key = entity_key(entity_project_id, display_name, is_global=is_global)
        if key in deduped:
            continue
        deduped[key] = _GraphEntity(
            entity_key=key,
            name=display_name,
            entity_type=entity.entity_type,
            project_id=entity_project_id,
            is_global=is_global,
            normalized_name=normalized_name,
        )
    return list(deduped.values())


def normalize_relationships(
    relationships: list[Relationship],
    *,
    entities: list[_GraphEntity],
    project_id: str,
    is_global: bool,
) -> list[Relationship]:
    """Normalize relationships to stable entity keys and stored Cypher types."""
    entity_map = {entity.entity_key: entity for entity in entities}
    deduped: dict[tuple[str, str, str], Relationship] = {}
    for relationship in relationships:
        entity_project_id = GLOBAL_PROJECT_ID if is_global else project_id
        source_key = entity_key(
            entity_project_id,
            relationship.source,
            is_global=is_global,
        )
        target_key = entity_key(
            entity_project_id,
            relationship.target,
            is_global=is_global,
        )
        relationship_type = normalize_relationship_type(relationship.relationship)
        if source_key not in entity_map or target_key not in entity_map:
            logger.debug(
                "Skipped relationship with missing endpoint: %s -> %s (%s)",
                relationship.source,
                relationship.target,
                relationship.relationship,
            )
            continue
        dedupe_key = (source_key, relationship_type, target_key)
        deduped[dedupe_key] = Relationship(
            source=source_key,
            target=target_key,
            relationship=relationship_type,
        )
    return list(deduped.values())

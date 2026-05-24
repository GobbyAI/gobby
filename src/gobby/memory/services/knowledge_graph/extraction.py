"""LLM-backed knowledge graph extraction prompts."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .models import Entity, Relationship, _GraphEntity

if TYPE_CHECKING:
    from gobby.llm.base import LLMProvider
    from gobby.prompts.loader import PromptLoader

logger = logging.getLogger(__name__)

_NON_ACTIONABLE_JSON_RESPONSE_MARKERS = (
    "i'm ready to help",
    "i’m ready to help",
    "not the actual content",
    "only technical instructions",
)


def _is_non_actionable_json_response_error(error: ValueError) -> bool:
    """Return True when the model replied with chatter instead of extraction JSON."""
    message = str(error).lower()
    if "failed to parse" not in message or "response as json" not in message:
        return False
    return any(marker in message for marker in _NON_ACTIONABLE_JSON_RESPONSE_MARKERS)


class KnowledgeGraphExtractor:
    """Runs LLM prompts for entity, relationship, and cleanup extraction."""

    def __init__(
        self,
        llm_provider: LLMProvider,
        prompt_loader: PromptLoader,
        model: str | None = None,
    ) -> None:
        self._llm = llm_provider
        self._prompt_loader = prompt_loader
        self._model = model

    async def extract_entities(self, content: str) -> list[Entity]:
        """Extract entities from content using LLM."""
        prompt = self._prompt_loader.render(
            "memory/extract_entities",
            {"content": content},
        )
        try:
            response = await self._llm.generate_json(prompt, model=self._model)
        except ValueError as error:
            if not _is_non_actionable_json_response_error(error):
                raise
            logger.info(
                "Entity extraction returned a non-actionable conversational response; "
                "treating it as no entities"
            )
            return []
        raw_entities = response.get("entities", [])
        logger.debug(
            "Entity extraction response keys: %s, raw_entities count: %d",
            list(response.keys()),
            len(raw_entities),
        )
        entities = [
            Entity(name=e["entity"], entity_type=e["entity_type"])
            for e in raw_entities
            if isinstance(e, dict) and "entity" in e and "entity_type" in e
        ]
        dropped = len(raw_entities) - len(entities)
        if dropped:
            logger.warning(
                "Entity extraction dropped %d malformed entries from %d raw entities",
                dropped,
                len(raw_entities),
            )
        return entities

    async def extract_relationships(
        self,
        content: str,
        entities: list[Entity],
    ) -> list[Relationship]:
        """Extract relationships between entities using LLM."""
        entities_json = json.dumps(
            [{"entity": e.name, "entity_type": e.entity_type} for e in entities]
        )
        prompt = self._prompt_loader.render(
            "memory/extract_relations",
            {"content": content, "entities": entities_json},
        )
        response = await self._llm.generate_json(prompt, model=self._model)
        raw_relations = response.get("relations", [])
        if not isinstance(raw_relations, list):
            logger.warning(
                "Relationship extraction dropped relations payload: expected list, got %s",
                type(raw_relations).__name__,
            )
            return []
        relations: list[Relationship] = []
        for index, raw_relation in enumerate(raw_relations):
            if not isinstance(raw_relation, dict):
                logger.warning(
                    "Relationship extraction dropped relation %d: expected object",
                    index,
                )
                continue
            missing = [
                key for key in ("source", "relationship", "destination") if key not in raw_relation
            ]
            if missing:
                logger.warning(
                    "Relationship extraction dropped relation %d: missing %s",
                    index,
                    ", ".join(missing),
                )
                continue
            invalid = [
                key
                for key in ("source", "relationship", "destination")
                if key in raw_relation and not isinstance(raw_relation[key], str)
            ]
            if invalid:
                logger.warning(
                    "Relationship extraction dropped relation %d: invalid %s",
                    index,
                    ", ".join(invalid),
                )
                continue
            relations.append(
                Relationship(
                    source=raw_relation["source"],
                    target=raw_relation["destination"],
                    relationship=raw_relation["relationship"],
                )
            )
        return relations

    async def select_outdated_relations(
        self,
        *,
        entities: list[_GraphEntity],
        new_relations: list[Relationship],
        existing_relations: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Select existing relations that should be deleted."""
        name_by_key = {entity.entity_key: entity.name for entity in entities}
        new_relations_json = json.dumps(
            [
                {
                    "source": name_by_key.get(r.source, r.source),
                    "relationship": r.relationship,
                    "destination": name_by_key.get(r.target, r.target),
                }
                for r in new_relations
            ]
        )
        existing_json = json.dumps(existing_relations)

        prompt = self._prompt_loader.render(
            "memory/delete_relations",
            {"existing_relations": existing_json, "new_relations": new_relations_json},
        )
        response = await self._llm.generate_json(prompt, model=self._model)
        to_delete = response.get("relations_to_delete", [])
        return [rel for rel in to_delete if isinstance(rel, dict)]

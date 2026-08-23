"""JSON schemas for memory feature generation contracts."""

from typing import Any

TURN_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "turn_markdown": {"type": "string"},
        "title_candidate": {"type": "string"},
    },
    "required": ["turn_markdown", "title_candidate"],
    "additionalProperties": False,
}

DREAM_ACTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["keep", "delete", "refresh", "review", "promote"],
                    },
                    "memory_id": {"type": "string"},
                    "content": {"type": ["string", "null"]},
                    "memory_type": {"type": ["string", "null"]},
                    "tags": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                    },
                    "reason": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["action", "memory_id", "reason", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["actions"],
    "additionalProperties": False,
}

ENTITY_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "entity_type": {"type": "string"},
                },
                "required": ["entity", "entity_type"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["entities"],
    "additionalProperties": False,
}

RELATIONSHIP_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "relationship": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["source", "relationship", "destination"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["relations"],
    "additionalProperties": False,
}

RELATIONSHIP_DELETION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relation_ids_to_delete": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["relation_ids_to_delete"],
    "additionalProperties": False,
}

SHADOW_RELEVANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "rationale": {"type": "string"},
                },
                "required": ["key", "relevant", "confidence", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

__all__ = [
    "DREAM_ACTIONS_SCHEMA",
    "ENTITY_EXTRACTION_SCHEMA",
    "RELATIONSHIP_DELETION_SCHEMA",
    "RELATIONSHIP_EXTRACTION_SCHEMA",
    "SHADOW_RELEVANCE_SCHEMA",
    "TURN_RECORD_SCHEMA",
]

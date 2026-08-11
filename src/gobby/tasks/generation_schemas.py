"""JSON schemas for task feature generation contracts."""

from typing import Any

_STRING_ARRAY: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

EXPANSION_COMPILATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "test_intent": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "behaviors": _STRING_ARRAY,
                            "suggested_test_files": _STRING_ARRAY,
                            "entry_criteria": _STRING_ARRAY,
                        },
                        "required": [
                            "summary",
                            "behaviors",
                            "suggested_test_files",
                            "entry_criteria",
                        ],
                        "additionalProperties": False,
                    },
                },
                "required": ["id", "title", "summary", "test_intent"],
                "additionalProperties": False,
            },
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "phase_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "integer"},
                    "task_type": {"type": "string"},
                    "category": {"type": "string"},
                    "validation": {"type": "string"},
                    "affected_files": _STRING_ARRAY,
                    "execution_group": {"type": ["string", "null"]},
                    "implementation_domain": {"type": ["string", "null"]},
                    "assigned_agent": {"type": ["string", "null"]},
                    "additional_skills": _STRING_ARRAY,
                    "labels": _STRING_ARRAY,
                    "depends_on": _STRING_ARRAY,
                },
                "required": [
                    "id",
                    "phase_id",
                    "title",
                    "description",
                    "priority",
                    "task_type",
                    "category",
                    "validation",
                    "affected_files",
                ],
                "additionalProperties": False,
            },
        },
        "dependencies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "depends_on": {"type": "string"},
                },
                "required": ["task_id", "depends_on"],
                "additionalProperties": False,
            },
        },
        "execution_groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "mode": {"type": "string"},
                    "task_ids": _STRING_ARRAY,
                },
                "required": ["id", "mode", "task_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["phases", "tasks", "dependencies", "execution_groups"],
    "additionalProperties": False,
}

TASK_CLOSE_VALIDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["valid", "invalid"]},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "satisfied": {"type": "boolean"},
                    "gap": {"type": ["string", "null"]},
                },
                "required": ["index", "satisfied", "gap"],
                "additionalProperties": False,
            },
        },
        "feedback": {"type": "string"},
    },
    "required": ["status", "criteria", "feedback"],
    "additionalProperties": False,
}

__all__ = ["EXPANSION_COMPILATION_SCHEMA", "TASK_CLOSE_VALIDATION_SCHEMA"]

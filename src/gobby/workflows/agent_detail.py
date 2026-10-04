"""Shared detail projection for stored agent definitions.

The CLI ``agents show`` and the MCP ``get_agent_definition`` tool both render
this projection, so every ``AgentDefinitionBody`` field reaches both surfaces
unless it is listed in ``HIDDEN_DETAIL_FIELDS``.
"""

from __future__ import annotations

from typing import Any

from gobby.storage.definitions.agents import AgentDefinitionRow
from gobby.workflows.definitions import AgentDefinitionBody

# Definition fields no detail view shows. api_token is an endpoint credential.
HIDDEN_DETAIL_FIELDS: frozenset[str] = frozenset({"api_token"})


def agent_definition_detail(row: AgentDefinitionRow) -> dict[str, Any]:
    """Project a stored agent definition with its row metadata."""
    raw = dict(row.definition_json)
    raw.setdefault("name", row.name)
    body = AgentDefinitionBody.model_validate(raw)
    return {
        **body.model_dump(mode="json", exclude=set(HIDDEN_DETAIL_FIELDS)),
        "id": row.id,
        "name": row.name,
        "description": row.description or body.description,
        "mode": raw.get("mode"),
        "enabled": row.enabled,
        "source": row.source,
        "project_id": row.project_id,
        "tags": row.tags,
    }

"""Dispatch agent-definition JSON mapping for heartbeat context."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from gobby.dispatch.context import _agent_definition_view
from gobby.storage.definitions.agents import AgentDefinitionRow

pytestmark = pytest.mark.unit


def _row(definition_json: object, *, name: str = "coder") -> AgentDefinitionRow:
    return cast(
        AgentDefinitionRow,
        SimpleNamespace(
            name=name,
            enabled=True,
            source="installed",
            project_id=None,
            definition_json=definition_json,
        ),
    )


def test_string_json_uses_row_name_fallback() -> None:
    # A spawn surface requires its prompt block (require_surface_prompt_blocks);
    # the subject here is the name fallback, not an incomplete definition.
    view = _agent_definition_view(
        _row('{"surfaces": ["spawn"], "prompts": {"agent": "Do the work."}}')
    )

    assert view.name == "coder"
    assert getattr(view, "parse_error", None) is None
    assert view.definition.name == "coder"
    assert view.spawn_capable is True


def test_null_and_array_bodies_use_parse_error_path() -> None:
    payloads: list[object] = [None, [], "[]", "{not-json"]
    for payload in payloads:
        view = _agent_definition_view(_row(payload))
        assert view.enabled is False
        assert view.parse_error
        assert not hasattr(view, "definition")

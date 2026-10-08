from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.workflows import state_manager
from gobby.workflows.state_manager import (
    SessionVariableManager,
    _clear_variable_defaults_caches,
    _decode_variables_payload,
)

pytestmark = pytest.mark.unit


def test_decode_variables_payload_returns_empty_for_malformed_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    assert _decode_variables_payload("{bad") == {}

    assert "Failed to decode workflow variables payload" in caplog.text


def test_decode_variables_payload_returns_empty_for_non_object_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    assert _decode_variables_payload('["not", "an", "object"]') == {}

    assert "Ignoring non-object workflow variables payload: list" in caplog.text


def test_defaults_load_once_per_hub_across_managers_until_a_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[tuple[object, str | None]] = []

    def load(db: object, project_id: str | None) -> dict[str, Any]:
        loads.append((db, project_id))
        return {"loaded_skills": []}

    monkeypatch.setattr(state_manager, "load_variable_defaults", load)
    hub, other_hub = MagicMock(), MagicMock()
    _clear_variable_defaults_caches()

    first = SessionVariableManager(hub)._get_variable_defaults("project")
    second = SessionVariableManager(hub)._get_variable_defaults("project")
    SessionVariableManager(other_hub)._get_variable_defaults("project")
    _clear_variable_defaults_caches()
    SessionVariableManager(hub)._get_variable_defaults("project")

    assert loads == [(hub, "project"), (other_hub, "project"), (hub, "project")]
    assert first == second == {"loaded_skills": []}
    assert first["loaded_skills"] is not second["loaded_skills"]

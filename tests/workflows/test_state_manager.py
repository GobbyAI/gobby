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


class UnrelatedDefault:
    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        raise AssertionError("A scoped read copied an unrelated default")


def test_scoped_defaults_copy_only_requested_values_on_cache_hit_and_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[str | None] = []

    def load(db: object, project_id: str | None) -> dict[str, Any]:
        loads.append(project_id)
        return {"selected": {"items": []}, "unrelated": UnrelatedDefault()}

    monkeypatch.setattr(state_manager, "load_variable_defaults", load)
    _clear_variable_defaults_caches()
    manager = SessionVariableManager(MagicMock())
    first = manager._get_variable_defaults("project", keys=("selected", "missing"))
    first["selected"]["items"].append("changed")
    second = manager._get_variable_defaults("project", keys=("selected",))

    assert first == {"selected": {"items": ["changed"]}}
    assert second == {"selected": {"items": []}}
    assert loads == ["project"]
    _clear_variable_defaults_caches()


def test_scoped_defaults_preserve_ttl_and_revision_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    revision = [1]
    loads: list[int] = []

    def load(db: object, project_id: str | None) -> dict[str, Any]:
        loads.append(revision[0])
        return {"selected": len(loads), "other": False}

    monkeypatch.setattr(state_manager, "load_variable_defaults", load)
    monkeypatch.setattr("gobby.workflows.state_manager.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(state_manager, "get_definitions_revision", lambda domain: revision[0])
    _clear_variable_defaults_caches()
    manager = SessionVariableManager(MagicMock())
    assert manager._get_variable_defaults(None, keys=("selected",)) == {"selected": 1}
    clock[0] = manager._DEFAULTS_CACHE_TTL - 1
    assert manager._get_variable_defaults(None, keys=("selected",)) == {"selected": 1}
    clock[0] = manager._DEFAULTS_CACHE_TTL
    assert manager._get_variable_defaults(None, keys=("selected",)) == {"selected": 2}
    revision[0] = 2
    assert manager._get_variable_defaults(None) == {"selected": 3, "other": False}
    assert loads == [1, 1, 2]
    _clear_variable_defaults_caches()


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

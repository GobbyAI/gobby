"""A runbook seat's model comes from its model input, its definition, or the policy map (#25641)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.agents.model_defaults import ModelDefaults, PolicyEntry
from gobby.agents.runbook_seats import (
    CatalogueSeat,
    RunbookSeatRefusal,
    _refuse_unmodeled_launches,
)
from gobby.storage.definitions.agents import AgentDefinitionManager

pytestmark = pytest.mark.unit

_PROJECT = "project-1"


class _Definitions:
    def __init__(self, models: dict[str, str | None]) -> None:
        self._models = models

    def get_by_name(self, name: str, *, project_id: str) -> SimpleNamespace | None:
        if name not in self._models:
            return None
        body: dict[str, Any] = {}
        if self._models[name] is not None:
            body["model"] = self._models[name]
        return SimpleNamespace(definition_json=body)


def _definitions(models: dict[str, str | None]) -> AgentDefinitionManager:
    """A definition store holding ``models``; the fake covers the one lookup the check makes."""
    return cast(AgentDefinitionManager, _Definitions(models))


def _seats(*agents: str) -> list[CatalogueSeat]:
    return [CatalogueSeat(name=agent, title=agent, agent=agent) for agent in agents]


def _map_for(agent: str) -> ModelDefaults:
    entry = PolicyEntry(provider="claude", model="claude-opus-5-5", reasoning_effort="medium")
    return ModelDefaults(entries={agent: entry})


def test_seat_with_a_policy_entry_admits_without_a_model_input() -> None:
    _refuse_unmodeled_launches(
        _definitions({"seat-alpha": None}),
        _PROJECT,
        _seats("seat-alpha"),
        ["seat-alpha"],
        {},
        model_defaults=_map_for("seat-alpha"),
    )
    with pytest.raises(RunbookSeatRefusal):
        _refuse_unmodeled_launches(
            _definitions({"seat-alpha": None}),
            _PROJECT,
            _seats("seat-alpha"),
            ["seat-alpha"],
            {},
            model_defaults=ModelDefaults(),
        )


def test_seat_with_no_input_definition_or_entry_is_refused() -> None:
    with pytest.raises(RunbookSeatRefusal) as excinfo:
        _refuse_unmodeled_launches(
            _definitions({"seat-alpha": None}),
            _PROJECT,
            _seats("seat-alpha"),
            ["seat-alpha"],
            {},
            model_defaults=ModelDefaults(),
        )

    assert "seat-alpha" in str(excinfo.value)


def test_policy_entry_for_another_agent_does_not_admit_the_seat() -> None:
    with pytest.raises(RunbookSeatRefusal):
        _refuse_unmodeled_launches(
            _definitions({"seat-alpha": None}),
            _PROJECT,
            _seats("seat-alpha"),
            ["seat-alpha"],
            {},
            model_defaults=_map_for("seat-beta"),
        )


def test_model_input_or_definition_model_still_admits_the_seat() -> None:
    _refuse_unmodeled_launches(
        _definitions({"seat-alpha": None, "seat-beta": "claude-haiku-5-5"}),
        _PROJECT,
        _seats("seat-alpha", "seat-beta"),
        ["seat-alpha", "seat-beta"],
        {"seat-alpha": "claude-sonnet-5-5"},
        model_defaults=ModelDefaults(),
    )
    with pytest.raises(RunbookSeatRefusal):
        _refuse_unmodeled_launches(
            _definitions({"seat-alpha": None}),
            _PROJECT,
            _seats("seat-alpha"),
            ["seat-alpha"],
            {},
            model_defaults=ModelDefaults(),
        )

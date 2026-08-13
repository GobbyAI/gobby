"""Parameterized enabled_pinned reconciliation for typed definition managers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import pytest

from gobby.storage.definitions import (
    AgentDefinitionManager,
    PipelineDefinitionManager,
    RuleDefinitionManager,
    SessionVariableDefaultManager,
)
from gobby.storage.hub.postgres import PostgresHubDatabase


class DefinitionManager(Protocol):
    def get(self, definition_id: str, include_deleted: bool = False) -> Any: ...

    def update(self, definition_id: str, **fields: Any) -> Any: ...

    def toggle_enabled(self, definition_id: str) -> Any: ...

    def update_from_sync(self, definition_id: str, **fields: Any) -> Any: ...


def _rule_factory(db: PostgresHubDatabase) -> tuple[DefinitionManager, str]:
    manager = RuleDefinitionManager(db)
    row = manager.create(name="rule", definition_json={"event": "Stop"}, enabled=True)
    return manager, row.id


def _variable_factory(db: PostgresHubDatabase) -> tuple[DefinitionManager, str]:
    manager = SessionVariableDefaultManager(db)
    row = manager.create(name="var", default_value="a", enabled=True)
    return manager, row.id


def _pipeline_factory(db: PostgresHubDatabase) -> tuple[DefinitionManager, str]:
    manager = PipelineDefinitionManager(db)
    row = manager.create(name="pipe", definition_json={"steps": []}, enabled=True)
    return manager, row.id


def _agent_factory(db: PostgresHubDatabase) -> tuple[DefinitionManager, str]:
    manager = AgentDefinitionManager(db)
    row = manager.create(name="agent", definition_json={"name": "agent"}, enabled=True)
    return manager, row.id


_FACTORIES = [_rule_factory, _variable_factory, _pipeline_factory, _agent_factory]
_FACTORY_IDS = ["rules", "variables", "pipelines", "agents"]


@pytest.mark.parametrize(
    "factory",
    _FACTORIES,
    ids=_FACTORY_IDS,
)
def test_user_update_and_toggle_stamp_enabled_pinned(
    definition_db: PostgresHubDatabase,
    factory: Callable[[PostgresHubDatabase], tuple[DefinitionManager, str]],
) -> None:
    manager, definition_id = factory(definition_db)
    updated = manager.update(definition_id, enabled=False)
    assert updated.enabled is False
    assert updated.enabled_pinned is True

    toggled = manager.toggle_enabled(definition_id)
    assert toggled.enabled is True
    assert toggled.enabled_pinned is True


@pytest.mark.parametrize(
    "factory",
    _FACTORIES,
    ids=_FACTORY_IDS,
)
def test_sync_adopts_template_enabled_while_unpinned(
    definition_db: PostgresHubDatabase,
    factory: Callable[[PostgresHubDatabase], tuple[DefinitionManager, str]],
) -> None:
    manager, definition_id = factory(definition_db)
    synced = manager.update_from_sync(definition_id, enabled=False)
    assert synced.enabled is False
    assert synced.enabled_pinned is False


@pytest.mark.parametrize(
    "factory",
    _FACTORIES,
    ids=_FACTORY_IDS,
)
def test_sync_preserves_pinned_enabled_even_when_equal_to_template(
    definition_db: PostgresHubDatabase,
    factory: Callable[[PostgresHubDatabase], tuple[DefinitionManager, str]],
) -> None:
    manager, definition_id = factory(definition_db)
    manager.update(definition_id, enabled=True)
    pinned = manager.get(definition_id)
    assert pinned.enabled_pinned is True
    synced = manager.update_from_sync(definition_id, enabled=False)
    assert synced.enabled is True
    assert synced.enabled_pinned is True

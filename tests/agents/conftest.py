"""Share isolated definition-schema fixtures with agent sync tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from gobby.agents.spawn_models import SpawnRequest
from tests.fixtures.isolated_checkout import install_isolated_checkout_project
from tests.storage.definitions.conftest import (
    _reset_revision_globals,
    definition_db,
    scoped_postgres_dsn,
)
from tests.terminals.fakes import bind_spawn_runtime

if TYPE_CHECKING:
    from gobby.storage.projects import LocalProjectManager

AGENT_TEST_MACHINE_ID = "21000000-0000-4000-8000-000000000001"

__all__ = [
    "_reset_revision_globals",
    "definition_db",
    "scoped_postgres_dsn",
]


@pytest.fixture(autouse=True)
def bind_terminal_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    original = SpawnRequest.__init__

    def wrapped(self: SpawnRequest, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        if self.terminal_runtime_registry is None:
            bind_spawn_runtime(self)

    monkeypatch.setattr(SpawnRequest, "__init__", wrapped)


@pytest.fixture
def sample_project(
    project_manager: LocalProjectManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """The root ``sample_project`` pinned to the package's local machine id.

    Agent test modules register sessions and runs on
    ``LOCAL_MACHINE_ID = 21000000-0000-4000-8000-000000000001``; the isolated
    checkout has to live on that machine, or ``SessionManager.register`` fails
    closed with ``MachineOwnershipMismatchError``.
    """
    isolated = install_isolated_checkout_project(
        project_manager.db,
        tmp_path / "isolated-checkout",
        machine_id=AGENT_TEST_MACHINE_ID,
        monkeypatch=monkeypatch,
    )
    return isolated.project.to_dict()

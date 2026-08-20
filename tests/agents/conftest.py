"""Share isolated definition-schema fixtures with agent sync tests."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.agents.spawn_models import SpawnRequest
from tests.storage.definitions.conftest import (
    _reset_revision_globals,
    definition_db,
    scoped_postgres_dsn,
)
from tests.terminals.fakes import bind_spawn_runtime

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

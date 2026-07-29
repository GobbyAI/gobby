"""Shared isolation for CLI tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def isolate_cli_runtime_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent root CLI invocations from opening the operator's configured hub."""
    database = MagicMock()
    database.fetchone.return_value = None

    @contextmanager
    def open_database(*_args: object, **_kwargs: object) -> Iterator[MagicMock]:
        yield database

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)
    monkeypatch.setattr("gobby.storage.hub.runtime.runtime_hub_database", open_database)

"""Shared isolation for CLI tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
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


@pytest.fixture(autouse=True)
def isolate_native_bin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the installed-binary-set view (#21507) off the operator's real ~/.gobby/bin.

    ``gobby start``/``restart``/``status`` probe every set member's embedded schema
    identity; against a developer's installed set the verdict depends on what
    happens to be installed, so every CLI test sees an empty managed bin dir
    unless it installs its own stubs there.
    """
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(tmp_path / "native-bin"))

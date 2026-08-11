from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.cli import utils_config
from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FULL_LOADER_CALLERS = (
    "src/gobby/cli/runtime.py",
    "src/gobby/cli/__init__.py",
    "src/gobby/cli/_install_prompts.py",
    "src/gobby/cli/install.py",
)


def test_full_loader_callers_use_cli_runtime() -> None:
    for relative_path in _FULL_LOADER_CALLERS:
        source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "load_full_config_from_db" not in source, relative_path


def test_full_loader_is_not_exported() -> None:
    assert not hasattr(utils_config, "load_full_config_from_db")


def test_cli_runtime_closes_config_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    database = MagicMock()
    repository = MagicMock()
    repository.read.return_value = SimpleNamespace(values={"hooks.provider_timeout": 321})
    repository.runtime_candidate.return_value = DaemonConfig()

    @contextmanager
    def open_database(*args: object, **kwargs: object) -> Iterator[MagicMock]:
        try:
            yield database
        finally:
            database.close()

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)
    runtime = CliRuntime(
        config_file=None,
        config_repository_factory=lambda opened_database: repository,
    )

    assert runtime.require_config() is repository.runtime_candidate.return_value
    assert runtime.require_config() is repository.runtime_candidate.return_value
    runtime.close()

    repository.read.assert_called_once_with(resolve_secrets=True)
    repository.runtime_candidate.assert_called_once_with({"hooks.provider_timeout": 321})
    database.close.assert_called_once_with()

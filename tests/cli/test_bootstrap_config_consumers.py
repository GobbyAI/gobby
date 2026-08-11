from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import gobby.cli._install_daemon as install_daemon
import gobby.cli.utils as cli_utils
import gobby.config as config_package
from gobby.config.bootstrap import BootstrapConfig

qdrant_module = import_module("gobby.cli.qdrant")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRE_DATABASE_CONSUMERS = (
    "src/gobby/cli/utils_config.py",
    "src/gobby/cli/_install_daemon.py",
    "src/gobby/cli/installers/service_common.py",
    "src/gobby/cli/utils_process.py",
    "src/gobby/storage/hub/runtime.py",
)
_POST_DATABASE_CONSUMERS = (
    "src/gobby/cli/postgres.py",
    "src/gobby/cli/qdrant.py",
)


def test_pre_database_operations_use_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_loader = MagicMock(return_value=BootstrapConfig(daemon_port=61977))
    monkeypatch.setattr(install_daemon, "load_bootstrap", bootstrap_loader, raising=False)

    assert install_daemon._daemon_url() == "http://localhost:61977/"
    bootstrap_loader.assert_called_once_with(resolve_database_url=False)
    for relative_path in _PRE_DATABASE_CONSUMERS:
        source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "load_config" not in source, relative_path


def test_post_database_operations_use_runtime_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MagicMock()
    runtime.require_config.return_value = SimpleNamespace(
        databases=SimpleNamespace(qdrant=SimpleNamespace(url="http://runtime-qdrant:6333"))
    )
    runtime_accessor = MagicMock(return_value=runtime)
    monkeypatch.setattr(qdrant_module, "get_cli_runtime", runtime_accessor, raising=False)
    observed_urls: list[str | None] = []

    async def get_qdrant_status(*, qdrant_url: str | None) -> dict[str, object]:
        observed_urls.append(qdrant_url)
        return {"installed": True, "healthy": True, "url": qdrant_url}

    monkeypatch.setattr("gobby.cli.services.get_qdrant_status", get_qdrant_status)

    qdrant_module.qdrant_status.callback()

    runtime_accessor.assert_called_once_with()
    runtime.require_config.assert_called_once_with()
    assert observed_urls == ["http://runtime-qdrant:6333"]
    for relative_path in _POST_DATABASE_CONSUMERS:
        source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "load_config" not in source, relative_path


def test_loader_reexports_are_removed() -> None:
    for module in (config_package, cli_utils):
        assert not hasattr(module, "load_config")
        assert not hasattr(module, "load_full_config_from_db")

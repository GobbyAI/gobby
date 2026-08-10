import os
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

import gobby.runner
import gobby.runner_init.storage
import gobby.runner_maintenance
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


def _get_storage_load_config() -> Callable[..., DaemonConfig]:
    return cast(
        Callable[..., DaemonConfig],
        vars(gobby.runner_init.storage)["load_config"],
    )


def test_import_pathing_trap_is_fixed(protect_production_resources: None) -> None:
    """
    Verify that the protect_production_resources fixture successfully patches
    load_config in modules that have already imported it.
    """
    # Check gobby.runner_init.storage.load_config
    # It should be the 'safe_load_config' function defined in the fixture
    load_config = _get_storage_load_config()
    assert load_config.__name__ == "safe_load_config", (
        "gobby.runner_init.storage.load_config should be patched to safe_load_config"
    )

    # Check its behavior
    config = load_config()
    assert config.database_url is not None
    assert "test-safe-postgres" in config.database_url, (
        "Resulting config should point to safe test database"
    )


@pytest.mark.asyncio
async def test_runner_uses_patched_config(
    protect_production_resources: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration checks that Runner actually initializes with safe config."""
    # Only phase 1 (storage/config) is needed to check database path.
    # Phases 2-4 pull in numpy transitively, which crashes on reimport
    # when other tests have already loaded numpy in this process.
    monkeypatch.setattr("gobby.runner_init.init_runtime_capacity", lambda self: None)
    monkeypatch.setattr("gobby.runner_init.init_services", lambda self: None)
    monkeypatch.setattr(
        "gobby.runner_init.init_orchestration",
        lambda _self, _startup_config: None,
    )
    monkeypatch.setattr("gobby.runner_init.init_servers", lambda self: None)

    def _init_storage(
        runner: gobby.runner.GobbyRunner,
        _config_path: Path | None,
        _verbose: bool,
    ) -> None:
        runner.config = _get_storage_load_config()()
        db = MagicMock()
        db.database_url = runner.config.database_url
        runner.database = db

    monkeypatch.setattr(
        "gobby.runner_init.init_storage_and_config",
        _init_storage,
    )

    runner = gobby.runner.GobbyRunner()

    # Ensure it's using the safe DB
    database_url = cast(MagicMock, runner.database).database_url
    assert "test-safe-postgres" in str(database_url)
    assert database_url == runner.config.database_url


def test_fixture_redirects_gobby_home(protect_production_resources: None) -> None:
    """Fixture should keep daemon-path helpers out of ~/.gobby."""
    safe_home = Path(os.environ["GOBBY_HOME"]).resolve()
    real_home = (Path.home() / ".gobby").resolve()

    assert safe_home != real_home
    assert gobby.runner_maintenance.get_gobby_home().resolve() == safe_home

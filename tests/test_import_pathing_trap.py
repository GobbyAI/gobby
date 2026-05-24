import os
from pathlib import Path

import pytest

import gobby.mcp_proxy.stdio
import gobby.runner
import gobby.runner_maintenance

pytestmark = pytest.mark.unit


def test_import_pathing_trap_is_fixed(protect_production_resources) -> None:
    """
    Verify that the protect_production_resources fixture successfully patches
    load_config in modules that have already imported it.
    """
    # Check gobby.mcp_proxy.stdio.load_config
    # It should be the 'safe_load_config' function defined in the fixture
    assert gobby.mcp_proxy.stdio.load_config.__name__ == "safe_load_config", (
        "gobby.mcp_proxy.stdio.load_config should be patched to safe_load_config"
    )

    # Check its behavior
    config = gobby.mcp_proxy.stdio.load_config()
    assert "test-safe-postgres" in config.database_url, (
        "Resulting config should point to safe test database"
    )


def test_runner_uses_patched_config(protect_production_resources, monkeypatch) -> None:
    """Integration checks that Runner actually initializes with safe config."""
    # Only phase 1 (storage/config) is needed to check database path.
    # Phases 2-4 pull in numpy transitively, which crashes on reimport
    # when other tests have already loaded numpy in this process.
    monkeypatch.setattr("gobby.runner_init.init_services", lambda self: None)
    monkeypatch.setattr("gobby.runner_init.init_orchestration", lambda self: None)
    monkeypatch.setattr("gobby.runner_init.init_servers", lambda self: None)

    runner = gobby.runner.GobbyRunner()

    # Ensure it's using the safe DB
    assert "test-safe-postgres" in str(runner.database.db_path)


def test_fixture_redirects_gobby_home(protect_production_resources) -> None:
    """Fixture should keep daemon-path helpers out of ~/.gobby."""
    safe_home = Path(os.environ["GOBBY_HOME"]).resolve()
    real_home = (Path.home() / ".gobby").resolve()

    assert safe_home != real_home
    assert gobby.runner_maintenance.get_gobby_home().resolve() == safe_home

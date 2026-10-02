"""Live-daemon coverage for local API key adoption at startup."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.storage.api_keys import ApiKeyManager
from gobby.storage.hub.protocol import HubDatabase
from tests.e2e.conftest import DaemonInstance
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.e2e


@pytest.fixture
def e2e_pre_daemon_setup(e2e_config: tuple[Path, int, int]) -> bytes:
    """Capture config.yaml before the daemon's first start."""
    config_path, _http_port, _ws_port = e2e_config
    return config_path.read_bytes()


def _bootstrap_key(home: Path) -> tuple[str, str]:
    bootstrap = yaml.safe_load((home / "bootstrap.yaml").read_text())
    return bootstrap["api_key"], bootstrap["api_key_id"]


def test_startup_adopts_local_key_once(
    daemon_instance: DaemonInstance,
    e2e_pre_daemon_setup: bytes,
    postgres_db: HubDatabase,
) -> None:
    home = daemon_instance.gobby_home
    machine_id = (home / "machine_id").read_text().strip()
    keys = ApiKeyManager(postgres_db)

    plaintext, key_id = _bootstrap_key(home)
    minted = keys.list_for_user(TEST_USER_ID)
    assert [(key.id, key.machine_id) for key in minted] == [(key_id, machine_id)]
    assert keys.is_live_for_machine(key_id, plaintext, machine_id)
    assert daemon_instance.config_path.read_bytes() == e2e_pre_daemon_setup

    daemon_instance.stop()
    daemon_instance.restart()

    assert _bootstrap_key(home) == (plaintext, key_id)
    assert [key.id for key in keys.list_for_user(TEST_USER_ID)] == [key_id]
    assert daemon_instance.config_path.read_bytes() == e2e_pre_daemon_setup

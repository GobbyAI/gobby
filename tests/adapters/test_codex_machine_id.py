"""Machine identity regression tests for the Codex app-server adapter."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.adapters.codex_impl import app_server_adapter
from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.utils import machine_id as machine_id_module

pytestmark = pytest.mark.unit


def test_concurrent_first_boot_hooks_converge_on_persisted_machine_id(tmp_path: Path) -> None:
    """Concurrent hook fallbacks share the durable first-boot machine ID."""
    machine_id_file = tmp_path / ".gobby" / "machine_id"
    worker_count = 8
    ready = threading.Barrier(worker_count)

    def translate_hook(index: int) -> str | None:
        ready.wait(timeout=2)
        event = CodexAdapter().translate_to_hook_event(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "cold-start-thread",
                    "turn": {"id": f"turn-{index}"},
                },
            }
        )
        assert event is not None
        return event.machine_id

    machine_id_module.clear_cache()
    try:
        with (
            patch.object(machine_id_module, "MACHINE_ID_FILE", machine_id_file),
            patch.object(app_server_adapter, "_get_daemon_machine_id", return_value=None),
            patch.object(
                machine_id_module,
                "_generate_machine_id",
                return_value="persisted-machine-id",
            ) as generate_machine_id,
            patch("platform.node", return_value=""),
            ThreadPoolExecutor(max_workers=worker_count) as executor,
        ):
            machine_ids = list(executor.map(translate_hook, range(worker_count)))

        assert set(machine_ids) == {"persisted-machine-id"}
        assert machine_id_file.read_text() == "persisted-machine-id"
        generate_machine_id.assert_called_once()
    finally:
        machine_id_module.clear_cache()

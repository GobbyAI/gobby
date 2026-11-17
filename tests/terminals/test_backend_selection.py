"""Fixed native-first backend selection (#22104, #23212; no configurable backend)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.config.terminals import TerminalConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.terminals.host_client import HostCommandError, HostUnavailableError
from gobby.terminals.native_runtime import HostManagerControl, NativeTerminalRuntime
from gobby.terminals.runtime import PreparedSpawn, TerminalRuntime, TerminalSpawnRequest
from gobby.terminals.web_spawn import spawn_web_terminal
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _manager

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_CONFIG_YAML = _REPO / "src" / "gobby" / "install" / "shared" / "config" / "config.yaml"
_GUIDE = _REPO / "docs" / "guides" / "gterminal-development-guide.md"
_TERMINAL_CREATE_WS = _REPO / "src" / "gobby" / "servers" / "websocket" / "terminal_ws_create.py"
_RUNTIME_CONTRACT = (
    _REPO / "crates" / "gcore" / "assets" / "config" / "runtime_config_contract.json"
)


def test_default_backend_config_is_removed() -> None:
    assert "default_backend" not in TerminalConfig.model_fields
    with pytest.raises(ValidationError, match="default_backend"):
        TerminalConfig.model_validate({"default_backend": "native"})
    with pytest.raises(ValidationError, match="default_backend"):
        DaemonConfig.model_validate({"terminals": {"default_backend": "native"}})
    loaded = yaml.safe_load(_CONFIG_YAML.read_text(encoding="utf-8"))
    assert "default_backend" not in (loaded.get("terminals") or {})
    contract_text = _RUNTIME_CONTRACT.read_text(encoding="utf-8")
    assert '"terminals.spawn_in_doubt_seconds"' in contract_text
    assert "default_backend" not in contract_text
    guide = _GUIDE.read_text(encoding="utf-8")
    assert "default_backend" not in guide
    assert "native gterm is always attempted first" in guide


@pytest.mark.asyncio
async def test_native_spawn_refuses_unavailable_host_and_capacity(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID)
    manager = _manager(temp_db)
    # An explicit native request with no gterm host is refused before fork with the
    # typed host_unreachable code; the pending row fails.
    assert issubclass(HostUnavailableError, HostCommandError)
    no_host = NativeTerminalRuntime(HostManagerControl(SimpleNamespace(_client=None)))
    refused = await spawn_web_terminal(
        manager=manager,
        runtime=no_host,
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert refused.success is False
    assert refused.error == "host_unreachable"
    assert refused.terminal_id is not None
    refused_row = manager.get(refused.terminal_id)
    assert refused_row is not None
    assert refused_row.backend == "native"
    assert refused_row.state == "exited"
    open_rows, _more = manager.list_page(
        [sample_project["id"]], states=("pending", "live"), limit=10
    )
    assert open_rows == []

    prepares: list[str] = []
    entitlements = 0
    max_attachments_total = 8
    ceiling = max_attachments_total - 4

    class SaturatingNativeRuntime:
        backend = "native"

        async def reserve_observer(self, terminal_id: UUID) -> dict[str, str]:
            nonlocal entitlements
            if entitlements >= ceiling:
                raise HostCommandError("capacity")
            entitlements += 1
            return {
                "reservation_id": f"rsv-{terminal_id}",
                "reserve_key": str(terminal_id),
            }

        async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
            prepares.append(str(request.terminal_id))
            prepared = MagicMock()
            prepared.stored_locator = {"host_terminal_id": f"ht-{request.terminal_id}"}
            prepared.locator_key = f"native:epoch:ht-{request.terminal_id}"
            prepared.process = None
            prepared.host_terminal_id = f"ht-{request.terminal_id}"
            prepared.rows = request.rows
            prepared.cols = request.cols
            prepared.acknowledge_persist = MagicMock()
            prepared.acknowledge_observer = MagicMock()
            return prepared

        async def commit_spawn(self, prepared: PreparedSpawn) -> Any:
            del prepared
            return MagicMock(locator=MagicMock(frame_host_epoch="epoch-1"))

        async def terminate(self, terminal: object, grace: float) -> None:
            del terminal, grace
            nonlocal entitlements
            entitlements = max(0, entitlements - 1)

        async def bind_observer(self, prepared: PreparedSpawn, reservation_id: str) -> None:
            del prepared, reservation_id

    runtime = cast(TerminalRuntime, SaturatingNativeRuntime())
    for _ in range(ceiling):
        result = await spawn_web_terminal(
            manager=manager,
            runtime=runtime,
            project_id=sample_project["id"],
            session_id=None,
            rows=24,
            cols=80,
            cwd="/tmp",
            command=["zsh"],
        )
        assert result.success is True
    overflow = await spawn_web_terminal(
        manager=manager,
        runtime=runtime,
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert overflow.success is False
    assert "capacity" in (overflow.error or "")
    assert str(overflow.terminal_id) not in prepares

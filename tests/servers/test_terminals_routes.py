"""Acceptance 2.5.2 / 2.5.8 / 2.5.34: terminal REST inventory surface."""

from __future__ import annotations

import json
import subprocess
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.terminals import create_terminals_router
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import TerminalManager, tmux_locator_key
from gobby.terminals.ws_protocol import (
    TERMINAL_LIST_DEFAULT_PAGE_SIZE,
    TERMINAL_LIST_MAX_ENCODED_BYTES,
    TERMINAL_LIST_MAX_PAGE_SIZE,
)
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _create_pending, _manager

pytestmark = pytest.mark.unit

_SOCKET = "/private/tmp/tmux-501/default"


@pytest.fixture(autouse=True)
def _machine() -> Any:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _server(temp_db: HubDatabase) -> Any:
    from types import SimpleNamespace

    manager = TerminalManager(temp_db)
    return SimpleNamespace(services=SimpleNamespace(terminal_manager=manager, database=temp_db))


def _client(temp_db: HubDatabase) -> TestClient:
    app = FastAPI()
    app.include_router(create_terminals_router(_server(temp_db)))
    return TestClient(app)


def test_terminal_rest_surface(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
    manager = _manager(temp_db)
    live = _create_pending(manager, sample_project["id"])
    promoted = manager.promote_to_live(
        live.id,
        locator={
            "socket_path": _SOCKET,
            "server_pid": 1,
            "server_start_time": 2,
            "pane_id": "%1",
        },
        locator_key=tmux_locator_key(
            socket_path=_SOCKET, server_pid=1, server_start_time=2, pane_id="%1"
        ),
        session_name="sess",
    )
    assert promoted is not None
    exited = _create_pending(manager, sample_project["id"])
    manager.fail_pending(exited.id)

    with _client(temp_db) as client:
        listing = client.get("/api/terminals", params={"project_id": sample_project["id"]})
        assert listing.status_code == 200
        body = listing.json()
        ids = {row["id"] for row in body["items"]}
        assert promoted.id in ids
        assert exited.id not in ids
        assert "next_cursor" in body
        detail = client.get(f"/api/terminals/{promoted.id}")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["id"] == promoted.id
        assert payload["attach"]["backend"] == "tmux"
        other = uuid.uuid4()
        isolated = client.get("/api/terminals", params={"project_id": str(other)})
        assert isolated.json()["items"] == []


def test_terminal_inventory_is_paginated(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = _manager(temp_db)
    live_ids: list[str] = []
    for index in range(TERMINAL_LIST_DEFAULT_PAGE_SIZE + 3):
        row = _create_pending(manager, sample_project["id"], terminal_id=str(uuid.uuid4()))
        locator = {
            "socket_path": _SOCKET,
            "server_pid": index + 10,
            "server_start_time": 2,
            "pane_id": f"%{index}",
        }
        promoted = manager.promote_to_live(
            row.id,
            locator=locator,
            locator_key=tmux_locator_key(
                socket_path=_SOCKET,
                server_pid=index + 10,
                server_start_time=2,
                pane_id=f"%{index}",
            ),
        )
        assert promoted is not None
        live_ids.append(promoted.id)
    extra = _create_pending(manager, sample_project["id"])
    manager.fail_pending(extra.id)

    with _client(temp_db) as client:
        first = client.get(
            "/api/terminals",
            params={"project_id": sample_project["id"], "limit": TERMINAL_LIST_DEFAULT_PAGE_SIZE},
        ).json()
        assert len(first["items"]) == TERMINAL_LIST_DEFAULT_PAGE_SIZE
        assert extra.id not in {row["id"] for row in first["items"]}
        history = client.get(
            "/api/terminals",
            params={"project_id": sample_project["id"], "states": "exited", "limit": 10},
        ).json()
        assert extra.id in {row["id"] for row in history["items"]}
        second = client.get(
            "/api/terminals",
            params={
                "project_id": sample_project["id"],
                "cursor": first["next_cursor"],
                "limit": TERMINAL_LIST_DEFAULT_PAGE_SIZE,
            },
        ).json()
        first_ids = [row["id"] for row in first["items"]]
        second_ids = [row["id"] for row in second["items"]]
        assert not set(first_ids) & set(second_ids)
        too_big = client.get(
            "/api/terminals",
            params={"project_id": sample_project["id"], "limit": TERMINAL_LIST_MAX_PAGE_SIZE + 1},
        )
        assert too_big.status_code == 200
        assert len(too_big.json()["items"]) <= TERMINAL_LIST_MAX_PAGE_SIZE
        encoded = json.dumps(first).encode("utf-8")
        assert len(encoded) <= TERMINAL_LIST_MAX_ENCODED_BYTES


@pytest.mark.asyncio
async def test_dimension_bounds_rejected(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from gobby.servers.websocket.server import WebSocketServer

    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    server = WebSocketServer(config, MagicMock(), AsyncMock(return_value="u"))
    ws = MagicMock()
    ws.send = AsyncMock()
    await server._handle_message(
        ws,
        json.dumps(
            {
                "type": "terminal_create",
                "request_id": "r1",
                "rows": 0,
                "cols": 80,
                "project_id": sample_project["id"],
            }
        ),
    )
    messages = [json.loads(call.args[0]) for call in ws.send.await_args_list]
    assert any(item.get("type") == "terminal_error" or item.get("code") for item in messages)
    assert _manager(temp_db).list_by_project(sample_project["id"]) == []


def _native_live(
    manager: TerminalManager, project_id: str, pgid: int, shell: str | None = None
) -> Any:
    """A live native row carrying the shell pid the host recorded for it."""
    row = _create_pending(manager, project_id, backend="native")
    process: dict[str, object] = {"host_terminal_id": "ht-1", "pgid": pgid, "start_time": 1.0}
    if shell is not None:
        process["shell"] = shell
    recorded = manager.record_process(
        row.id,
        process,
        attempt_generation=row.attempt_generation,
        attempt_started_at=row.attempt_started_at,
    )
    assert recorded is not None
    promoted = manager.promote_to_live(
        row.id,
        locator={"host_terminal_id": "ht-1"},
        locator_key="native:epoch-1:ht-1",
        host_epoch="epoch-1",
    )
    assert promoted is not None
    return promoted


def test_a_native_row_reports_the_command_in_its_terminal_foreground(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = _manager(temp_db)
    native = _native_live(manager, sample_project["id"], pgid=4242, shell="zsh")
    tmux_row = _create_pending(manager, sample_project["id"])
    promoted = manager.promote_to_live(
        tmux_row.id,
        locator={
            "socket_path": _SOCKET,
            "server_pid": 1,
            "server_start_time": 2,
            "pane_id": "%1",
        },
        locator_key=tmux_locator_key(
            socket_path=_SOCKET, server_pid=1, server_start_time=2, pane_id="%1"
        ),
    )
    assert promoted is not None
    table = "4242 5150 -zsh\n5150 5150 /usr/local/bin/nvim\n"
    run = MagicMock(
        return_value=subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=table, stderr="")
    )

    with patch("gobby.terminals.foreground.spawn.run", run), _client(temp_db) as client:
        listing = client.get("/api/terminals", params={"project_id": sample_project["id"]})
        detail = client.get(f"/api/terminals/{native.id}")

    rows = {row["id"]: row for row in listing.json()["items"]}
    assert rows[native.id]["command"] == "nvim"
    # A tmux row records no shell pid, so the key is present and empty rather
    # than missing: the label ladder reads one field for every backend.
    assert rows[promoted.id]["command"] is None
    assert detail.json()["command"] == "nvim"


def test_a_native_row_falls_back_to_its_spawn_shell(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = _manager(temp_db)
    native = _native_live(manager, sample_project["id"], pgid=4242, shell="zsh")
    # The process table no longer lists the shell, so no live foreground resolves.
    table = "1 0 /sbin/launchd\n"
    run = MagicMock(
        return_value=subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=table, stderr="")
    )

    with patch("gobby.terminals.foreground.subprocess.run", run), _client(temp_db) as client:
        listing = client.get("/api/terminals", params={"project_id": sample_project["id"]})
        detail = client.get(f"/api/terminals/{native.id}")

    rows = {row["id"]: row for row in listing.json()["items"]}
    assert rows[native.id]["command"] == "zsh"
    assert detail.json()["command"] == "zsh"

"""``terminal_list`` is machine-wide, project-narrowed, paged, and pane-enriched (#21190)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.tmux.session_manager import TmuxPaneInfo
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.terminals import TerminalManager, tmux_locator_key
from tests.servers.test_tmux_mixin import MockWebSocket
from tests.storage.test_terminals import LOCAL_MACHINE_ID

pytestmark = pytest.mark.unit

SOCKET = "/private/tmp/tmux-501/default"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def manager(temp_db: HubDatabase) -> TerminalManager:
    return TerminalManager(temp_db)


@pytest.fixture
def server(manager: TerminalManager) -> WebSocketServer:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    ws_server = WebSocketServer(config, MagicMock(), AsyncMock(return_value="test-user"))
    ws_server.terminal_manager = manager
    return ws_server


def seed_pane(manager: TerminalManager, project_id: str, pane_id: str, title: str) -> Any:
    locator = {
        "socket_path": SOCKET,
        "server_pid": 6051,
        "server_start_time": 1787385464,
        "pane_id": pane_id,
    }
    return manager.upsert_external(
        project_id=project_id,
        backend="tmux",
        locator=locator,
        locator_key=tmux_locator_key(
            socket_path=SOCKET, server_pid=6051, server_start_time=1787385464, pane_id=pane_id
        ),
        session_name=title,
        title=title,
    )


def pane_for(row: Any, **overrides: Any) -> TmuxPaneInfo:
    values: dict[str, Any] = {
        "socket_path": SOCKET,
        "server_pid": 6051,
        "server_start_time": 1787385464,
        "session_name": row.session_name,
        "window_id": "@1",
        "window_name": "zsh",
        "pane_id": row.locator["pane_id"],
        "pane_pid": 42,
        "pane_title": "MBP.local",
        "pane_dead": False,
        "pane_command": "vim",
        "pane_path": "/Users/dev/projects/gobby",
    }
    values.update(overrides)
    return TmuxPaneInfo(**values)


async def listed(server: WebSocketServer, request: dict[str, Any]) -> dict[str, Any]:
    ws = MockWebSocket()
    await server._handle_terminal_list(ws, {"type": "terminal_list", **request})
    return ws.last_message()


@pytest.mark.asyncio
async def test_list_without_a_project_is_machine_wide_and_carries_pane_metadata(
    server: WebSocketServer,
    manager: TerminalManager,
    sample_project: dict[str, Any],
    project_manager: LocalProjectManager,
) -> None:
    other = project_manager.create(name="other-project")
    here = seed_pane(manager, sample_project["id"], "%1", "75")
    elsewhere = seed_pane(manager, other.id, "%2", "100")
    sweep = AsyncMock(return_value={here.locator_key: pane_for(here)})

    with patch("gobby.servers.websocket.terminal_ws.sweep_tmux_terminals", sweep):
        page = await listed(server, {"request_id": "init"})

    assert page["type"] == "terminal_list"
    assert page["request_id"] == "init"
    assert page["next_cursor"] is None
    by_id = {item["terminal_id"]: item for item in page["items"]}
    assert set(by_id) == {here.id, elsewhere.id}
    enriched = by_id[here.id]
    assert enriched["name"] == "75"
    assert enriched["socket"] == "default"
    assert (enriched["pane_command"], enriched["pane_path"]) == ("vim", "/Users/dev/projects/gobby")
    assert (enriched["window_name"], enriched["pane_pid"], enriched["pane_title"]) == (
        "zsh",
        42,
        "MBP.local",
    )
    assert "name" not in by_id[elsewhere.id]
    sweep.assert_awaited_once()
    assert sweep.await_args is not None
    assert sweep.await_args.kwargs["machine_id"] == LOCAL_MACHINE_ID


@pytest.mark.asyncio
async def test_list_with_a_project_keeps_that_project_and_unscoped_terminals(
    server: WebSocketServer,
    manager: TerminalManager,
    sample_project: dict[str, Any],
    project_manager: LocalProjectManager,
) -> None:
    other = project_manager.create(name="other-project")
    unscoped = project_manager.create(name="unscoped")
    mine = seed_pane(manager, sample_project["id"], "%1", "mine")
    seed_pane(manager, other.id, "%2", "theirs")
    shared = seed_pane(manager, unscoped.id, "%3", "shared")

    with (
        patch(
            "gobby.servers.websocket.terminal_ws.sweep_tmux_terminals", AsyncMock(return_value={})
        ),
        patch("gobby.servers.websocket.terminal_ws.GLOBAL_PROJECT_ID", unscoped.id),
    ):
        page = await listed(server, {"request_id": "init", "project_id": sample_project["id"]})

    assert {item["terminal_id"] for item in page["items"]} == {mine.id, shared.id}


@pytest.mark.asyncio
async def test_list_pages_through_the_cursor_it_hands_out(
    server: WebSocketServer, manager: TerminalManager, sample_project: dict[str, Any]
) -> None:
    rows = [seed_pane(manager, sample_project["id"], f"%{n}", str(n)) for n in range(3)]

    with patch(
        "gobby.servers.websocket.terminal_ws.sweep_tmux_terminals", AsyncMock(return_value={})
    ):
        first = await listed(server, {"request_id": "init", "limit": 2})
        assert [item["terminal_id"] for item in first["items"]] == [rows[0].id, rows[1].id]
        assert first["next_cursor"]
        second = await listed(
            server, {"request_id": "page", "limit": 2, "cursor": first["next_cursor"]}
        )
        assert [item["terminal_id"] for item in second["items"]] == [rows[2].id]
        assert second["next_cursor"] is None
        broken = await listed(server, {"request_id": "bad", "cursor": "not-a-cursor"})

    assert broken == {"type": "terminal_error", "code": "invalid_cursor", "request_id": "bad"}


@pytest.mark.asyncio
async def test_list_still_answers_when_discovery_fails(
    server: WebSocketServer, manager: TerminalManager, sample_project: dict[str, Any]
) -> None:
    row = seed_pane(manager, sample_project["id"], "%1", "1")

    with patch(
        "gobby.servers.websocket.terminal_ws.sweep_tmux_terminals",
        AsyncMock(side_effect=RuntimeError("tmux exploded")),
    ):
        page = await listed(server, {"request_id": "init"})

    assert [item["terminal_id"] for item in page["items"]] == [row.id]


@pytest.mark.asyncio
async def test_list_without_a_terminal_manager_is_empty(server: WebSocketServer) -> None:
    server.terminal_manager = None
    page = await listed(server, {"request_id": uuid.uuid4().hex})
    assert (page["items"], page["next_cursor"]) == ([], None)

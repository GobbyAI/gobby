"""Plan 4.3.5: tmux web attach shares the host observer; TmuxPTYBridge is gone."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import tmux_locator_key
from tests.servers.test_native_web_proxy import _attach, _harness, _send, _take, _until
from tests.servers.test_tmux_mixin import MockWebSocket
from tests.storage.test_terminals import LOCAL_MACHINE_ID

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_SOCKET = "/private/tmp/tmux-501/default"


def _tmux_pty_bridge_refs() -> list[str]:
    hits: list[str] = []
    for path in list((_REPO / "src").rglob("*.py")) + list((_REPO / "tests").rglob("*.py")):
        if path.name == "test_web_tmux_through_host.py":
            continue
        text = path.read_text()
        if "TmuxPTYBridge" in text:
            hits.append(str(path.relative_to(_REPO)))
    return hits


def test_tmux_pty_bridge_has_zero_references() -> None:
    assert _tmux_pty_bridge_refs() == []
    assert not (_REPO / "src/gobby/agents/tmux/pty_bridge.py").exists()


@pytest.mark.asyncio
async def test_browser_and_gclient_share_one_observer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        harness = _harness(temp_db, sample_project)
        browser = MockWebSocket()
        gclient = MockWebSocket()
        ext = harness.external_row
        b_att = await _attach(harness, browser, ext, request_id="browser")
        g_att = await _attach(harness, gclient, ext, request_id="gclient")
        frames = [frame for frame in harness.frame_list if frame.reservation_ids]
        assert frames
        for frame in frames:
            assert frame.reservation_ids == [None] * len(frame.reservation_ids)
        before_commands = list(harness.tmux_rt.tmux_commands)
        await _take(harness, browser, ext, b_att)
        await _send(
            harness.server,
            gclient,
            {
                "type": "terminal_take_control",
                "terminal_id": ext.id,
                "attachment_id": g_att,
                "takeover": True,
            },
        )
        await _until(lambda: browser.messages_of_type("terminal_lease_lost"))
        await _send(
            harness.server,
            browser,
            {
                "type": "terminal_take_control",
                "terminal_id": ext.id,
                "attachment_id": b_att,
                "takeover": True,
            },
        )
        await _until(lambda: gclient.messages_of_type("terminal_lease_lost"))
        await _send(
            harness.server,
            browser,
            {
                "type": "terminal_resize",
                "terminal_id": ext.id,
                "attachment_id": b_att,
                "rows": 50,
                "cols": 132,
            },
        )
        assert harness.tmux_rt.tmux_commands == before_commands
        assert harness.tmux_rt.resize_calls == []
        assert ext.locator is not None
        assert tmux_locator_key(
            socket_path=_SOCKET,
            server_pid=11,
            server_start_time=11,
            pane_id="%11",
        ) == tmux_locator_key(
            socket_path=str(ext.locator["socket_path"]),
            server_pid=int(ext.locator["server_pid"]),
            server_start_time=int(ext.locator["server_start_time"]),
            pane_id=str(ext.locator["pane_id"]),
        )

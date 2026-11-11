"""Shared send_keys target scene for the events and sessions tests (#25830).

A live wake that reaches a terminal row passes the send_keys target gate, which reads a
foreground process, a pane and the composer ledger. Events tests have no real terminal, so
the autouse ``agent_target`` fixture installs one scene for every test in a suite that re-exports it: an
idle Claude composer in the foreground. A test that exercises another target re-installs
the scene through the same fixture, for example ``agent_target(foreground="zsh", snapshot=...)``.
The composer read is the ledger's, so the probe a test scripts remains the composer authority.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.events.wake import WakeDispatcher
from gobby.mcp_proxy.tools.sessions import _terminal_send_keys_gate as gate
from tests.agents.detection_test_support import BundledDetectionRegistry

CLAUDE_IDLE = "────────────────────\n❯ \n────────────────────"
SHELL_PID = 4321


class ScriptedPane:
    """A live pane whose snapshot is the scripted screen text."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def snapshot(self, lines: int = 80, *, mode: str = "text") -> str | None:
        return self._text


def install_agent_clis(monkeypatch: pytest.MonkeyPatch, root: Path) -> Callable[[str], str]:
    """Install every agent CLI as a build under ``root`` that the gate's PATH lookup finds.

    Returns the foreground executable for a name: an installed CLI's build path, or the
    name itself for anything else (a shell, an editor), which the gate never resolves.
    """
    bin_dir = root / "installed-clis"
    bin_dir.mkdir(exist_ok=True)
    for cli in gate._AGENT_CLIS:
        (bin_dir / cli).write_bytes(b"")
    monkeypatch.setattr(gate, "_installed_cli", lambda cli: bin_dir / cli)
    return lambda name: str(bin_dir / name) if name in gate._AGENT_CLIS else name


@pytest.fixture(autouse=True)
def agent_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., None]:
    """Script the gate's process, pane, ledger and manifest reads for one terminal."""
    executable = install_agent_clis(monkeypatch, tmp_path)

    def install(*, foreground: str, snapshot: str) -> None:
        pane = ScriptedPane(snapshot)
        monkeypatch.setattr(gate, "shell_pid", lambda _row: SHELL_PID)
        monkeypatch.setattr(
            gate,
            "foreground_executables",
            lambda pids: dict.fromkeys(pids, executable(foreground)),
        )
        monkeypatch.setattr(gate, "read_composer", lambda _id: SimpleNamespace(state="empty"))
        monkeypatch.setattr(gate, "live_runtime_pane", lambda *_args: pane)
        monkeypatch.setattr(
            gate, "_detection_registry", lambda _manager: BundledDetectionRegistry()
        )

    # The runner wires the runtime registry at startup. A dispatcher without one refuses a
    # terminal wake as unconfirmed, so every dispatcher a test builds gets one here.
    init = WakeDispatcher.__init__

    def wired_init(self: WakeDispatcher, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        self.set_terminal_runtime_registry(object())

    monkeypatch.setattr(WakeDispatcher, "__init__", wired_init)
    install(foreground="claude", snapshot=CLAUDE_IDLE)
    return install

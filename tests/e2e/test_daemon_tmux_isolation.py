"""Isolated test daemons own a private tmux server (#21175).

The runtime config that ``_seed_e2e_runtime_state`` writes used to leave
``tmux.socket_path`` unset, so every agent an isolated daemon spawned landed on
the user's production ``tmux -L gobby`` server and outlived the SIGKILLed
daemon.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.constants import ALL_TERMINAL_ENV_VARS
from gobby.storage.config_mutations import ConfigMutations
from tests.e2e.conftest import _seed_e2e_runtime_state, prepare_daemon_env, reserve_tmux_socket

pytestmark = pytest.mark.e2e


def test_nested_daemon_discards_parent_agent_identity(tmp_path: Path) -> None:
    inherited_names = [
        *ALL_TERMINAL_ENV_VARS,
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
        "GOBBY_MACHINE_ID",
        "GOBBY_DAEMON_PORT",
    ]
    inherited = dict.fromkeys(inherited_names, "parent-only")
    inherited.update({"TMPDIR": str(tmp_path), "GOBBY_TEST_PROTECT": "0"})
    prepared = prepare_daemon_env(inherited, home_dir=tmp_path)

    assert not set(inherited_names).intersection(prepared)
    assert prepared["HOME"] == str(tmp_path)
    assert prepared["TMPDIR"] == str(tmp_path)
    assert prepared["GOBBY_TEST_PROTECT"] == "1"
    assert inherited["GOBBY_AGENT_API_TOKEN"] == "parent-only"


def test_seeded_runtime_state_pins_a_private_tmux_socket(postgres_db: Any, tmp_path: Path) -> None:
    first = _seed_e2e_runtime_state(postgres_db, tmp_path)
    second = _seed_e2e_runtime_state(postgres_db, tmp_path)

    assert isinstance(first, Path) and isinstance(second, Path)
    assert first != second
    permitted_root = Path(os.environ.get("CLAUDE_CODE_TMPDIR") or tempfile.gettempdir()).resolve()
    assert first.parent == second.parent == permitted_root
    assert len(str(second).encode()) < 100, "unix socket paths must stay short"
    snapshot = ConfigMutations(postgres_db).repository.read(resolve_secrets=False)
    assert snapshot.values["tmux.socket_path"] == str(second)


def test_kill_tmux_server_stops_the_private_server() -> None:
    from tests.e2e.conftest import kill_tmux_server

    sock = reserve_tmux_socket()
    try:
        subprocess.run(
            [
                "tmux",
                "-S",
                str(sock),
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "probe",
                "sleep 60",
            ],
            check=True,
        )
        assert (
            subprocess.run(["tmux", "-S", str(sock), "has-session", "-t", "probe"]).returncode == 0
        )
        kill_tmux_server(sock)
        probe = subprocess.run(
            ["tmux", "-S", str(sock), "has-session", "-t", "probe"], capture_output=True
        )
        assert probe.returncode != 0
        assert not sock.exists()
    finally:
        subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)

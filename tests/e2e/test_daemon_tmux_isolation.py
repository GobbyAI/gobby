"""Isolated test daemons own a private tmux server (#21175).

The runtime config that ``_seed_e2e_runtime_state`` writes used to leave
``tmux.socket_path`` unset, so every agent an isolated daemon spawned landed on
the user's production ``tmux -L gobby`` server and outlived the SIGKILLed
daemon.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.config_mutations import ConfigMutations
from tests.e2e.conftest import _seed_e2e_runtime_state

pytestmark = pytest.mark.e2e


def test_seeded_runtime_state_pins_a_private_tmux_socket(postgres_db: Any, tmp_path: Path) -> None:
    first = _seed_e2e_runtime_state(postgres_db, tmp_path)
    second = _seed_e2e_runtime_state(postgres_db, tmp_path)

    assert isinstance(first, Path) and isinstance(second, Path)
    assert first != second
    assert len(str(second).encode()) < 100, "unix socket paths must stay short"
    snapshot = ConfigMutations(postgres_db).repository.read(resolve_secrets=False)
    assert snapshot.values["tmux.socket_path"] == str(second)


def test_kill_tmux_server_stops_the_private_server() -> None:
    from tests.e2e.conftest import kill_tmux_server

    sock = Path(f"/tmp/gobby-tmux-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")
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
    try:
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

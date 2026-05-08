"""Tests for the installed ghook shutdown guard."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


SCRIPT = Path("src/gobby/install/shared/hooks/ghook_guard.py")


def test_ghook_guard_allows_stop_when_fresh_shutdown_marker_exists(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown_intent_active.json"
    marker.write_text(
        json.dumps(
            {
                "source": "cli_restart",
                "intent": "restart",
                "timestamp": time.time(),
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--",
            sys.executable,
            "-c",
            "import sys; sys.exit(17)",
            "--type=Stop",
        ],
        input=b"{}",
        capture_output=True,
        check=False,
        env={
            "GOBBY_HOME": str(tmp_path),
            "GOBBY_DAEMON_URL": "http://127.0.0.1:9",
        },
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"continue": True}


def test_ghook_guard_runs_child_for_stale_shutdown_marker(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown_intent_active.json"
    marker.write_text(
        json.dumps(
            {
                "source": "cli_restart",
                "intent": "restart",
                "timestamp": 1,
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--",
            sys.executable,
            "-c",
            "import sys; sys.exit(17)",
            "--type=Stop",
        ],
        input=b"{}",
        capture_output=True,
        check=False,
        env={
            "GOBBY_HOME": str(tmp_path),
            "GOBBY_DAEMON_URL": "http://127.0.0.1:9",
        },
    )

    assert result.returncode == 17

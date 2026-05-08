"""Tests for the installed ghook shutdown guard."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


SCRIPT = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/hooks/ghook_guard.py"
_SPEC = importlib.util.spec_from_file_location("ghook_guard", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
ghook_guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ghook_guard)


def test_yaml_scalar_uses_safe_load_for_bootstrap_values() -> None:
    data = """
    bind_host: "127.0.0.1"
    daemon_port: 60887
    ignored:
      daemon_port: 12345
    """

    assert ghook_guard._yaml_scalar(data, "bind_host") == "127.0.0.1"
    assert ghook_guard._yaml_scalar(data, "daemon_port") == "60887"
    assert ghook_guard._yaml_scalar(data, "missing") is None


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
            **os.environ,
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
            **os.environ,
            "GOBBY_HOME": str(tmp_path),
            "GOBBY_DAEMON_URL": "http://127.0.0.1:9",
        },
    )

    assert result.returncode == 17


def test_ghook_guard_treats_file_daemon_url_as_unreachable(tmp_path: Path) -> None:
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
    file_target = tmp_path / "not-a-daemon"
    file_target.write_text("not http")

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
            **os.environ,
            "GOBBY_HOME": str(tmp_path),
            "GOBBY_DAEMON_URL": file_target.as_uri(),
        },
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"continue": True}


def test_ghook_guard_rejects_missing_child_before_reading_stdin(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--",
            str(tmp_path / "missing-ghook"),
            "--type=Stop",
        ],
        input=b"{}",
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "GOBBY_HOME": str(tmp_path),
            "GOBBY_DAEMON_URL": "http://127.0.0.1:9",
        },
    )

    assert result.returncode == 127
    assert "command not found or not executable" in result.stderr.decode()

"""Tests for cli/utils.py — targeting uncovered lines."""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import psutil
import pytest

pytestmark = pytest.mark.unit


# --- get_gobby_home ---


def test_get_gobby_home_default() -> None:
    from gobby.cli.utils import get_gobby_home

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GOBBY_HOME", None)
        result = get_gobby_home()
        assert result == Path.home() / ".gobby"


def test_get_gobby_home_custom() -> None:
    from gobby.cli.utils import get_gobby_home

    with patch.dict(os.environ, {"GOBBY_HOME": "/custom/path"}):
        result = get_gobby_home()
        assert result == Path("/custom/path")


# --- get_resources_dir ---


def test_get_resources_dir_global(tmp_path: Path) -> None:
    from gobby.cli.utils import get_resources_dir

    with patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path):
        result = get_resources_dir()
        assert result == tmp_path / "resources"
        assert result.exists()


def test_get_resources_dir_project(tmp_path: Path) -> None:
    from gobby.cli.utils import get_resources_dir

    result = get_resources_dir(str(tmp_path))
    assert result == tmp_path / ".gobby" / "resources"
    assert result.exists()


# --- format_uptime ---


def test_format_uptime_seconds() -> None:
    from gobby.cli.utils import format_uptime

    assert format_uptime(45) == "45s"
    assert format_uptime(0) == "0s"


def test_format_uptime_minutes() -> None:
    from gobby.cli.utils import format_uptime

    assert format_uptime(125) == "2m 5s"
    assert format_uptime(60) == "1m"


def test_format_uptime_hours() -> None:
    from gobby.cli.utils import format_uptime

    assert format_uptime(3661) == "1h 1m 1s"
    assert format_uptime(7200) == "2h"


# --- find_web_dir ---


def test_find_web_dir_from_config(tmp_path: Path) -> None:
    from gobby.cli.utils import find_web_dir

    web_dir = tmp_path / "my-web"
    web_dir.mkdir()
    (web_dir / "package.json").write_text("{}")

    config = MagicMock()
    config.ui.web_dir = str(web_dir)

    result = find_web_dir(config)
    assert result == web_dir


def test_find_web_dir_config_missing(tmp_path: Path) -> None:
    from gobby.cli.utils import find_web_dir

    config = MagicMock()
    config.ui.web_dir = str(tmp_path / "nonexistent")

    # Also patch cwd to avoid matching real cwd
    with patch("gobby.cli.utils.Path.cwd", return_value=tmp_path):
        result = find_web_dir(config)
    assert result is None


def test_find_web_dir_from_cwd(tmp_path: Path) -> None:
    from gobby.cli.utils import find_web_dir

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package.json").write_text("{}")

    with patch("gobby.cli.utils.Path.cwd", return_value=tmp_path):
        result = find_web_dir(None)
    assert result == web_dir


def test_find_web_dir_none(tmp_path: Path) -> None:
    from gobby.cli.utils import find_web_dir

    with patch("gobby.cli.utils.Path.cwd", return_value=tmp_path):
        result = find_web_dir(None)
    assert result is None


# --- is_port_available ---


def test_is_port_available() -> None:
    from gobby.cli.utils import is_port_available

    # Port 0 is a special case that always returns True
    result = is_port_available(0, "127.0.0.1")
    assert result is True


# --- stop_watchdog ---


def test_stop_watchdog_no_pid_file(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_watchdog

    with patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path):
        result = stop_watchdog(quiet=True)
    assert result is True


def test_stop_watchdog_stale_pid(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_watchdog

    pid_file = tmp_path / "watchdog.pid"
    pid_file.write_text("999999")

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils._is_process_alive", return_value=False),
    ):
        result = stop_watchdog(quiet=False)
    assert result is True
    assert not pid_file.exists()


def test_stop_watchdog_bad_pid_file(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_watchdog

    pid_file = tmp_path / "watchdog.pid"
    pid_file.write_text("not-a-number")

    with patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path):
        result = stop_watchdog(quiet=False)
    assert result is True


# --- stop_ui_server ---


def test_stop_ui_server_no_pid_file(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_ui_server

    with patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path):
        result = stop_ui_server(quiet=True)
    assert result is True


def test_stop_ui_server_stale_pid(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_ui_server

    pid_file = tmp_path / "ui.pid"
    pid_file.write_text("999999")

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils._is_process_alive", return_value=False),
    ):
        result = stop_ui_server(quiet=False)
    assert result is True


# --- stop_daemon ---


def test_stop_daemon_no_pid_file(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_daemon

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server") as mock_ui,
        patch("gobby.cli.utils.stop_watchdog") as mock_wd,
    ):
        result = stop_daemon(quiet=True)
    assert result is True
    mock_ui.assert_called_once_with(quiet=True)
    mock_wd.assert_called_once_with(quiet=True)


def test_stop_daemon_stale_pid(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("999999")

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", return_value=False),
        patch("gobby.cli.utils.kill_all_gobby_daemons", return_value=0),
    ):
        result = stop_daemon(quiet=False)
    assert result is True


def test_stop_daemon_bad_pid_file(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("not-a-pid")

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
    ):
        result = stop_daemon(quiet=False)
    assert result is False


def test_stop_daemon_not_gobby_process(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("12345")

    mock_proc = MagicMock()
    mock_proc.cmdline.return_value = ["node", "server.js"]

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_proc),
        patch("gobby.cli.utils.kill_all_gobby_daemons", return_value=0),
    ):
        result = stop_daemon(quiet=False)
    assert result is True


def test_stop_daemon_process_lookup_error(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("12345")

    mock_proc = MagicMock()
    mock_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_proc),
        patch("gobby.cli.utils.os.kill", side_effect=ProcessLookupError),
    ):
        result = stop_daemon(quiet=False)
    assert result is True


def test_stop_daemon_permission_error(tmp_path: Path) -> None:
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("12345")

    mock_proc = MagicMock()
    mock_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_proc),
        patch("gobby.cli.utils.os.kill", side_effect=PermissionError),
    ):
        result = stop_daemon(quiet=False)
    assert result is False


# --- load_full_config_from_db ---


def test_load_full_config_from_db_no_db(tmp_path: Path) -> None:
    from gobby.cli.utils import load_full_config_from_db

    mock_config = MagicMock()
    mock_config.database_path = str(tmp_path / "nonexistent.db")

    with patch("gobby.cli.utils.load_config", return_value=mock_config) as mock_load:
        result = load_full_config_from_db()
    assert result is mock_config
    mock_load.assert_called_once()


def test_load_full_config_from_db_with_db(tmp_path: Path) -> None:
    from gobby.cli.utils import load_full_config_from_db

    db_path = tmp_path / "test.db"
    db_path.write_bytes(b"")  # Create empty file

    mock_config = MagicMock()
    mock_config.database_path = str(db_path)

    final_config = MagicMock()

    with (
        patch("gobby.cli.utils.load_config") as mock_load,
        patch("gobby.cli.utils.LocalDatabase"),
        patch("gobby.storage.config_store.ConfigStore"),
        patch("gobby.storage.secrets.SecretStore"),
    ):
        mock_load.side_effect = [mock_config, final_config]
        result = load_full_config_from_db()
    assert result is final_config
    assert mock_load.call_count == 2


# ---------------------------------------------------------------------------
# resolve_project_ref (lines 109-130)
# ---------------------------------------------------------------------------


def test_resolve_project_ref_none_with_context() -> None:
    """When no ref provided, returns project id from context."""
    from gobby.cli.utils import resolve_project_ref

    with patch("gobby.cli.utils.get_project_context", return_value={"id": "proj-123"}):
        result = resolve_project_ref(None)
    assert result == "proj-123"


def test_resolve_project_ref_none_no_context() -> None:
    """When no ref and no context, returns None."""
    from gobby.cli.utils import resolve_project_ref

    with patch("gobby.cli.utils.get_project_context", return_value=None):
        result = resolve_project_ref(None)
    assert result is None


def test_resolve_project_ref_by_uuid() -> None:
    """Resolves direct UUID match."""
    from gobby.cli.utils import resolve_project_ref

    mock_project = MagicMock()
    mock_project.id = "uuid-abc"

    mock_db = MagicMock()
    mock_manager = MagicMock()
    mock_manager.get.return_value = mock_project

    with (
        patch("gobby.cli.utils.LocalDatabase", return_value=mock_db),
        patch("gobby.cli.utils.LocalProjectManager", return_value=mock_manager),
    ):
        result = resolve_project_ref("uuid-abc")
    assert result == "uuid-abc"
    mock_db.close.assert_called_once()


def test_resolve_project_ref_by_name() -> None:
    """Resolves project by name when UUID lookup fails."""
    from gobby.cli.utils import resolve_project_ref

    mock_project = MagicMock()
    mock_project.id = "uuid-from-name"

    mock_db = MagicMock()
    mock_manager = MagicMock()
    mock_manager.get.return_value = None
    mock_manager.get_by_name.return_value = mock_project

    with (
        patch("gobby.cli.utils.LocalDatabase", return_value=mock_db),
        patch("gobby.cli.utils.LocalProjectManager", return_value=mock_manager),
    ):
        result = resolve_project_ref("my-project")
    assert result == "uuid-from-name"


def test_resolve_project_ref_not_found() -> None:
    """Returns None when project not found by uuid or name."""
    from gobby.cli.utils import resolve_project_ref

    mock_db = MagicMock()
    mock_manager = MagicMock()
    mock_manager.get.return_value = None
    mock_manager.get_by_name.return_value = None

    with (
        patch("gobby.cli.utils.LocalDatabase", return_value=mock_db),
        patch("gobby.cli.utils.LocalProjectManager", return_value=mock_manager),
    ):
        result = resolve_project_ref("nonexistent")
    assert result is None
    mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# get_active_session_id (lines 135-151)
# ---------------------------------------------------------------------------


def test_get_active_session_id_with_result() -> None:
    """Returns session id when an active session exists."""
    from gobby.cli.utils import get_active_session_id

    mock_db = MagicMock()
    mock_db.fetchone.return_value = {"id": "sess-123"}

    result = get_active_session_id(mock_db)
    assert result == "sess-123"


def test_get_active_session_id_no_result() -> None:
    """Returns None when no active session."""
    from gobby.cli.utils import get_active_session_id

    mock_db = MagicMock()
    mock_db.fetchone.return_value = None

    result = get_active_session_id(mock_db)
    assert result is None


def test_get_active_session_id_creates_db() -> None:
    """Creates and closes LocalDatabase when none provided."""
    from gobby.cli.utils import get_active_session_id

    mock_db = MagicMock()
    mock_db.fetchone.return_value = {"id": "sess-auto"}

    with patch("gobby.cli.utils.LocalDatabase", return_value=mock_db):
        result = get_active_session_id(None)
    assert result == "sess-auto"
    mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# resolve_session_id (lines 171-192)
# ---------------------------------------------------------------------------


def test_resolve_session_id_no_ref_active() -> None:
    """Returns active session when no ref provided."""
    from gobby.cli.utils import resolve_session_id

    mock_db = MagicMock()

    with (
        patch("gobby.cli.utils.LocalDatabase", return_value=mock_db),
        patch("gobby.cli.utils.get_active_session_id", return_value="sess-active"),
    ):
        result = resolve_session_id(None)
    assert result == "sess-active"
    mock_db.close.assert_called_once()


def test_resolve_session_id_no_ref_no_active() -> None:
    """Raises ClickException when no ref and no active session."""
    from gobby.cli.utils import resolve_session_id

    mock_db = MagicMock()

    with (
        patch("gobby.cli.utils.LocalDatabase", return_value=mock_db),
        patch("gobby.cli.utils.get_active_session_id", return_value=None),
        pytest.raises(click.ClickException, match="No active session"),
    ):
        resolve_session_id(None)


def test_resolve_session_id_with_ref() -> None:
    """Resolves a session reference string."""
    from gobby.cli.utils import resolve_session_id

    mock_db = MagicMock()
    mock_manager = MagicMock()
    mock_manager.resolve_session_reference.return_value = "uuid-resolved"

    with (
        patch("gobby.cli.utils.LocalDatabase", return_value=mock_db),
        patch("gobby.cli.utils.get_project_context", return_value={"id": "proj-1"}),
        patch("gobby.cli.utils.LocalSessionManager", return_value=mock_manager),
    ):
        result = resolve_session_id("#5")
    assert result == "uuid-resolved"


def test_resolve_session_id_value_error() -> None:
    """Raises ClickException on ValueError from session manager."""
    from gobby.cli.utils import resolve_session_id

    mock_db = MagicMock()
    mock_manager = MagicMock()
    mock_manager.resolve_session_reference.side_effect = ValueError("ambiguous")

    with (
        patch("gobby.cli.utils.LocalDatabase", return_value=mock_db),
        patch("gobby.cli.utils.get_project_context", return_value=None),
        patch("gobby.cli.utils.LocalSessionManager", return_value=mock_manager),
        pytest.raises(click.ClickException, match="ambiguous"),
    ):
        resolve_session_id("abc")


# ---------------------------------------------------------------------------
# list_project_names (lines 197-202)
# ---------------------------------------------------------------------------


def test_list_project_names() -> None:
    from gobby.cli.utils import list_project_names

    mock_db = MagicMock()
    p1 = MagicMock()
    p1.name = "alpha"
    p2 = MagicMock()
    p2.name = "beta"
    mock_manager = MagicMock()
    mock_manager.list.return_value = [p1, p2]

    with (
        patch("gobby.cli.utils.LocalDatabase", return_value=mock_db),
        patch("gobby.cli.utils.LocalProjectManager", return_value=mock_manager),
    ):
        result = list_project_names()
    assert result == ["alpha", "beta"]
    mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# setup_logging (lines 212-221)
# ---------------------------------------------------------------------------


def test_setup_logging_default() -> None:
    from gobby.cli.utils import setup_logging

    with patch("gobby.cli.utils.logging.basicConfig") as mock_basic:
        setup_logging(verbose=False)
    mock_basic.assert_called_once()
    call_kwargs = mock_basic.call_args[1]
    assert call_kwargs["level"] == logging.INFO


def test_setup_logging_verbose() -> None:
    from gobby.cli.utils import setup_logging

    with patch("gobby.cli.utils.logging.basicConfig") as mock_basic:
        setup_logging(verbose=True)
    mock_basic.assert_called_once()
    call_kwargs = mock_basic.call_args[1]
    assert call_kwargs["level"] == logging.DEBUG


# ---------------------------------------------------------------------------
# is_port_available — OSError path (lines 270-272)
# ---------------------------------------------------------------------------


def test_is_port_available_oserror() -> None:
    from gobby.cli.utils import is_port_available

    mock_sock = MagicMock()
    mock_sock.bind.side_effect = OSError("Address already in use")

    with patch("socket.socket", return_value=mock_sock):
        result = is_port_available(8080)
    assert result is False
    mock_sock.close.assert_called()


# ---------------------------------------------------------------------------
# wait_for_port_available (lines 287-294)
# ---------------------------------------------------------------------------


def test_wait_for_port_available_immediate() -> None:
    from gobby.cli.utils import wait_for_port_available

    with patch("gobby.cli.utils.is_port_available", return_value=True):
        result = wait_for_port_available(8080, timeout=1.0)
    assert result is True


def test_wait_for_port_available_timeout() -> None:
    from gobby.cli.utils import wait_for_port_available

    with (
        patch("gobby.cli.utils.is_port_available", return_value=False),
        patch("gobby.cli.utils.time.sleep"),
        patch("gobby.cli.utils.time.time", side_effect=[0.0, 0.1, 0.2, 0.3, 1.1]),
    ):
        result = wait_for_port_available(8080, timeout=1.0)
    assert result is False


# ---------------------------------------------------------------------------
# _is_process_alive (lines 453-457)
# ---------------------------------------------------------------------------


def test_is_process_alive_running() -> None:
    from gobby.cli.utils import _is_process_alive

    mock_proc = MagicMock()
    mock_proc.status.return_value = psutil.STATUS_RUNNING

    with patch("gobby.cli.utils.psutil.Process", return_value=mock_proc):
        assert _is_process_alive(1234) is True


def test_is_process_alive_zombie() -> None:
    from gobby.cli.utils import _is_process_alive

    mock_proc = MagicMock()
    mock_proc.status.return_value = psutil.STATUS_ZOMBIE

    with patch("gobby.cli.utils.psutil.Process", return_value=mock_proc):
        assert _is_process_alive(1234) is False


def test_is_process_alive_no_such_process() -> None:
    from gobby.cli.utils import _is_process_alive

    with patch("gobby.cli.utils.psutil.Process", side_effect=psutil.NoSuchProcess(9999)):
        assert _is_process_alive(9999) is False


# ---------------------------------------------------------------------------
# init_local_storage (lines 411-423)
# ---------------------------------------------------------------------------


def test_init_local_storage(tmp_path: Path) -> None:
    from gobby.cli.utils import init_local_storage

    db_path = tmp_path / "sub" / "test.db"

    mock_config = MagicMock()
    mock_config.database_path = str(db_path)

    mock_db = MagicMock()

    with (
        patch("gobby.cli.utils.load_config", return_value=mock_config),
        patch("gobby.storage.database.LocalDatabase", return_value=mock_db) as mock_db_cls,
        patch("gobby.storage.migrations.run_migrations") as mock_migrate,
    ):
        result = init_local_storage()

    assert result is mock_db
    mock_db_cls.assert_called_once_with(db_path)
    mock_migrate.assert_called_once_with(mock_db)
    # Directory should have been created
    assert db_path.parent.exists()


# ---------------------------------------------------------------------------
# get_install_dir (lines 436-438)
# ---------------------------------------------------------------------------


def test_get_install_dir() -> None:
    from gobby.cli.utils import get_install_dir

    sentinel = Path("/fake/install")
    with patch("gobby.paths.get_install_dir", return_value=sentinel):
        result = get_install_dir()
    assert result == sentinel


# ---------------------------------------------------------------------------
# kill_all_gobby_daemons (lines 312-402) — the biggest uncovered block
# ---------------------------------------------------------------------------


def test_kill_all_gobby_daemons_no_processes() -> None:
    """No matching processes — returns 0."""
    from gobby.cli.utils import kill_all_gobby_daemons

    mock_config = MagicMock()
    mock_config.daemon_port = 60887
    mock_config.websocket.port = 60888

    with (
        patch("gobby.cli.utils.load_config", return_value=mock_config),
        patch("gobby.cli.utils.psutil.process_iter", return_value=[]),
        patch("gobby.cli.utils.psutil.Process") as mock_proc_cls,
    ):
        # Mock parent process traversal
        parent_proc = MagicMock()
        parent_proc.parent.return_value = None
        mock_proc_cls.return_value = parent_proc

        result = kill_all_gobby_daemons()
    assert result == 0


def test_kill_all_gobby_daemons_config_fallback() -> None:
    """Falls back to default ports when config fails to load."""
    from gobby.cli.utils import kill_all_gobby_daemons

    with (
        patch("gobby.cli.utils.load_config", side_effect=Exception("no config")),
        patch("gobby.cli.utils.psutil.process_iter", return_value=[]),
        patch("gobby.cli.utils.psutil.Process") as mock_proc_cls,
    ):
        parent_proc = MagicMock()
        parent_proc.parent.return_value = None
        mock_proc_cls.return_value = parent_proc

        result = kill_all_gobby_daemons()
    assert result == 0


def test_kill_all_gobby_daemons_kills_runner_process() -> None:
    """Finds and gracefully stops a gobby.runner process."""
    from gobby.cli.utils import kill_all_gobby_daemons

    mock_config = MagicMock()
    mock_config.daemon_port = 60887
    mock_config.websocket.port = 60888

    # Create a fake process that matches gobby.runner
    fake_proc = MagicMock()
    fake_proc.pid = 99999
    fake_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]
    fake_proc.send_signal = MagicMock()
    fake_proc.wait = MagicMock()  # Graceful shutdown succeeds

    # Parent process traversal
    parent_proc = MagicMock()
    parent_proc.parent.return_value = None
    parent_proc.pid = 1

    with (
        patch("gobby.cli.utils.load_config", return_value=mock_config),
        patch("gobby.cli.utils.os.getpid", return_value=10000),
        patch("gobby.cli.utils.os.getppid", return_value=10001),
        patch("gobby.cli.utils.psutil.Process", return_value=parent_proc),
        patch("gobby.cli.utils.psutil.process_iter", return_value=[fake_proc]),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = kill_all_gobby_daemons()
    assert result == 1
    fake_proc.send_signal.assert_called_once_with(signal.SIGTERM)


def test_kill_all_gobby_daemons_force_kill_on_timeout() -> None:
    """Force kills when graceful shutdown times out."""
    from gobby.cli.utils import kill_all_gobby_daemons

    mock_config = MagicMock()
    mock_config.daemon_port = 60887
    mock_config.websocket.port = 60888

    fake_proc = MagicMock()
    fake_proc.pid = 99999
    fake_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]
    fake_proc.send_signal = MagicMock()
    # First wait (after SIGTERM) times out, second wait (after kill) succeeds
    fake_proc.wait = MagicMock(side_effect=[psutil.TimeoutExpired(5), None])
    fake_proc.kill = MagicMock()

    parent_proc = MagicMock()
    parent_proc.parent.return_value = None
    parent_proc.pid = 1

    with (
        patch("gobby.cli.utils.load_config", return_value=mock_config),
        patch("gobby.cli.utils.os.getpid", return_value=10000),
        patch("gobby.cli.utils.os.getppid", return_value=10001),
        patch("gobby.cli.utils.psutil.Process", return_value=parent_proc),
        patch("gobby.cli.utils.psutil.process_iter", return_value=[fake_proc]),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = kill_all_gobby_daemons()
    assert result == 1
    fake_proc.kill.assert_called_once()


def test_kill_all_gobby_daemons_port_match() -> None:
    """Detects daemon by port listening when cmdline doesn't match runner pattern."""
    from gobby.cli.utils import kill_all_gobby_daemons

    mock_config = MagicMock()
    mock_config.daemon_port = 60887
    mock_config.websocket.port = 60888

    # A python process on the daemon port but not matching gobby.runner
    conn = MagicMock()
    conn.laddr = MagicMock()
    conn.laddr.port = 60887

    fake_proc = MagicMock()
    fake_proc.pid = 88888
    fake_proc.cmdline.return_value = ["python", "some_script.py"]
    fake_proc.net_connections.return_value = [conn]
    fake_proc.send_signal = MagicMock()
    fake_proc.wait = MagicMock()

    parent_proc = MagicMock()
    parent_proc.parent.return_value = None
    parent_proc.pid = 1

    with (
        patch("gobby.cli.utils.load_config", return_value=mock_config),
        patch("gobby.cli.utils.os.getpid", return_value=10000),
        patch("gobby.cli.utils.os.getppid", return_value=10001),
        patch("gobby.cli.utils.psutil.Process", return_value=parent_proc),
        patch("gobby.cli.utils.psutil.process_iter", return_value=[fake_proc]),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = kill_all_gobby_daemons()
    assert result == 1


def test_kill_all_gobby_daemons_skips_self() -> None:
    """Skips own pid and parent pids."""
    from gobby.cli.utils import kill_all_gobby_daemons

    mock_config = MagicMock()
    mock_config.daemon_port = 60887
    mock_config.websocket.port = 60888

    fake_proc = MagicMock()
    fake_proc.pid = 10000  # Same as our pid
    fake_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]

    parent_proc = MagicMock()
    parent_proc.parent.return_value = None
    parent_proc.pid = 1

    with (
        patch("gobby.cli.utils.load_config", return_value=mock_config),
        patch("gobby.cli.utils.os.getpid", return_value=10000),
        patch("gobby.cli.utils.os.getppid", return_value=10001),
        patch("gobby.cli.utils.psutil.Process", return_value=parent_proc),
        patch("gobby.cli.utils.psutil.process_iter", return_value=[fake_proc]),
    ):
        result = kill_all_gobby_daemons()
    assert result == 0


def test_kill_all_gobby_daemons_handles_process_error() -> None:
    """Handles unexpected exceptions from process inspection."""
    from gobby.cli.utils import kill_all_gobby_daemons

    mock_config = MagicMock()
    mock_config.daemon_port = 60887
    mock_config.websocket.port = 60888

    fake_proc = MagicMock()
    fake_proc.pid = 77777
    fake_proc.cmdline.side_effect = RuntimeError("unexpected")

    parent_proc = MagicMock()
    parent_proc.parent.return_value = None
    parent_proc.pid = 1

    with (
        patch("gobby.cli.utils.load_config", return_value=mock_config),
        patch("gobby.cli.utils.os.getpid", return_value=10000),
        patch("gobby.cli.utils.os.getppid", return_value=10001),
        patch("gobby.cli.utils.psutil.Process", return_value=parent_proc),
        patch("gobby.cli.utils.psutil.process_iter", return_value=[fake_proc]),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = kill_all_gobby_daemons()
    assert result == 0


# ---------------------------------------------------------------------------
# stop_watchdog — SIGTERM/SIGKILL paths (lines 492-524)
# ---------------------------------------------------------------------------


def test_stop_watchdog_running_graceful(tmp_path: Path) -> None:
    """Sends SIGTERM and process exits gracefully."""
    from gobby.cli.utils import stop_watchdog

    pid_file = tmp_path / "watchdog.pid"
    pid_file.write_text("12345")

    # _is_process_alive returns True initially, then False after SIGTERM
    alive_calls = iter([True, False])

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils._is_process_alive", side_effect=lambda pid: next(alive_calls)),
        patch("gobby.cli.utils.os.kill") as mock_kill,
        patch("gobby.cli.utils.time.sleep"),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = stop_watchdog(quiet=False)
    assert result is True
    mock_kill.assert_called_once_with(12345, signal.SIGTERM)
    assert not pid_file.exists()


def test_stop_watchdog_running_force_kill(tmp_path: Path) -> None:
    """Sends SIGTERM, process doesn't stop, then force-kills."""
    from gobby.cli.utils import stop_watchdog

    pid_file = tmp_path / "watchdog.pid"
    pid_file.write_text("12345")

    # _is_process_alive always returns True (never stops gracefully),
    # except we need 1 initial call + 50 loop iterations
    alive_results = [True] * 52  # Initial + 50 loop checks + final
    alive_iter = iter(alive_results)

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils._is_process_alive", side_effect=lambda pid: next(alive_iter, False)),
        patch("gobby.cli.utils.os.kill") as mock_kill,
        patch("gobby.cli.utils.time.sleep"),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = stop_watchdog(quiet=False)
    assert result is True
    # Should have called SIGTERM then SIGKILL
    assert mock_kill.call_count == 2
    mock_kill.assert_any_call(12345, signal.SIGTERM)
    mock_kill.assert_any_call(12345, signal.SIGKILL)


def test_stop_watchdog_process_lookup_error(tmp_path: Path) -> None:
    """ProcessLookupError during os.kill — returns True."""
    from gobby.cli.utils import stop_watchdog

    pid_file = tmp_path / "watchdog.pid"
    pid_file.write_text("12345")

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.os.kill", side_effect=ProcessLookupError),
    ):
        result = stop_watchdog(quiet=True)
    assert result is True


def test_stop_watchdog_generic_exception(tmp_path: Path) -> None:
    """Generic exception during os.kill — returns False."""
    from gobby.cli.utils import stop_watchdog

    pid_file = tmp_path / "watchdog.pid"
    pid_file.write_text("12345")

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.os.kill", side_effect=PermissionError("denied")),
    ):
        result = stop_watchdog(quiet=False)
    assert result is False


# ---------------------------------------------------------------------------
# _kill_port_holder (lines 573-595)
# ---------------------------------------------------------------------------


def test_kill_port_holder_finds_process() -> None:
    from gobby.cli.utils import _kill_port_holder

    conn = MagicMock()
    conn.laddr = MagicMock()
    conn.laddr.port = 5173
    conn.status = psutil.CONN_LISTEN

    child = MagicMock()
    parent_proc = MagicMock()
    parent_proc.children.return_value = [child]

    fake_proc = MagicMock()
    fake_proc.pid = 55555
    fake_proc.name.return_value = "node"
    fake_proc.net_connections.return_value = [conn]

    with (
        patch("gobby.cli.utils.psutil.process_iter", return_value=[fake_proc]),
        patch("gobby.cli.utils.psutil.Process", return_value=parent_proc),
        patch("gobby.cli.utils.psutil.wait_procs", return_value=([], [])),
    ):
        _kill_port_holder(5173)

    parent_proc.terminate.assert_called_once()
    child.terminate.assert_called_once()


def test_kill_port_holder_no_match() -> None:
    from gobby.cli.utils import _kill_port_holder

    conn = MagicMock()
    conn.laddr = MagicMock()
    conn.laddr.port = 9999  # Different port
    conn.status = psutil.CONN_LISTEN

    fake_proc = MagicMock()
    fake_proc.pid = 55555
    fake_proc.net_connections.return_value = [conn]

    with patch("gobby.cli.utils.psutil.process_iter", return_value=[fake_proc]):
        _kill_port_holder(5173)  # No error, just no-op


def test_kill_port_holder_access_denied() -> None:
    from gobby.cli.utils import _kill_port_holder

    fake_proc = MagicMock()
    fake_proc.pid = 55555
    fake_proc.net_connections.side_effect = psutil.AccessDenied(55555)

    with patch("gobby.cli.utils.psutil.process_iter", return_value=[fake_proc]):
        _kill_port_holder(5173)  # Should not raise


def test_kill_port_holder_kills_alive_procs() -> None:
    """When wait_procs returns alive processes, they get killed."""
    from gobby.cli.utils import _kill_port_holder

    conn = MagicMock()
    conn.laddr = MagicMock()
    conn.laddr.port = 5173
    conn.status = psutil.CONN_LISTEN

    child = MagicMock()
    parent_proc = MagicMock()
    parent_proc.children.return_value = [child]

    alive_proc = MagicMock()

    fake_proc = MagicMock()
    fake_proc.pid = 55555
    fake_proc.name.return_value = "node"
    fake_proc.net_connections.return_value = [conn]

    with (
        patch("gobby.cli.utils.psutil.process_iter", return_value=[fake_proc]),
        patch("gobby.cli.utils.psutil.Process", return_value=parent_proc),
        patch("gobby.cli.utils.psutil.wait_procs", return_value=([], [alive_proc])),
    ):
        _kill_port_holder(5173)
    alive_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# stop_ui_server — kill path (lines 717, 723-727, 736-778)
# ---------------------------------------------------------------------------


def test_stop_ui_server_not_quiet_no_pid() -> None:
    """Non-quiet mode logs when no PID file exists."""
    from gobby.cli.utils import stop_ui_server

    with patch("gobby.cli.utils.get_gobby_home", return_value=Path("/tmp/nonexistent-gobby")):
        result = stop_ui_server(quiet=False)
    assert result is True


def test_stop_ui_server_bad_pid_file(tmp_path: Path) -> None:
    """Bad PID file content — cleans up and returns True."""
    from gobby.cli.utils import stop_ui_server

    pid_file = tmp_path / "ui.pid"
    pid_file.write_text("not-a-number")

    with patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path):
        result = stop_ui_server(quiet=False)
    assert result is True
    assert not pid_file.exists()


def test_stop_ui_server_running_graceful(tmp_path: Path) -> None:
    """Stops running UI server gracefully."""
    from gobby.cli.utils import stop_ui_server

    pid_file = tmp_path / "ui.pid"
    pid_file.write_text("12345")

    # Process alive check: True initially, False in loop (breaks), False post-loop
    alive_calls = iter([True, False, False])
    mock_parent = MagicMock()
    mock_parent.children.return_value = []

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils._is_process_alive", side_effect=lambda pid: next(alive_calls, False)),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_parent),
        patch("gobby.cli.utils.os.kill") as mock_kill,
        patch("gobby.cli.utils.time.sleep"),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = stop_ui_server(quiet=False)
    assert result is True
    mock_kill.assert_called_once_with(12345, signal.SIGTERM)


def test_stop_ui_server_force_kill(tmp_path: Path) -> None:
    """Force kills UI server when graceful doesn't work."""
    from gobby.cli.utils import stop_ui_server

    pid_file = tmp_path / "ui.pid"
    pid_file.write_text("12345")

    # Process never dies gracefully — need enough True responses for
    # initial check + 50 loop iterations + final alive check
    alive_results = [True] * 55
    alive_iter = iter(alive_results)

    mock_child = MagicMock()
    mock_child.is_running.return_value = True
    mock_parent = MagicMock()
    mock_parent.children.return_value = [mock_child]

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils._is_process_alive", side_effect=lambda pid: next(alive_iter, False)),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_parent),
        patch("gobby.cli.utils.os.kill") as mock_kill,
        patch("gobby.cli.utils.time.sleep"),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = stop_ui_server(quiet=False)
    assert result is True
    # SIGTERM + SIGKILL
    assert mock_kill.call_count == 2
    mock_kill.assert_any_call(12345, signal.SIGTERM)
    mock_kill.assert_any_call(12345, signal.SIGKILL)
    mock_child.kill.assert_called_once()


def test_stop_ui_server_process_lookup_error(tmp_path: Path) -> None:
    """ProcessLookupError during kill — returns True."""
    from gobby.cli.utils import stop_ui_server

    pid_file = tmp_path / "ui.pid"
    pid_file.write_text("12345")

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.psutil.Process", side_effect=psutil.NoSuchProcess(12345)),
    ):
        result = stop_ui_server(quiet=False)
    assert result is True


def test_stop_ui_server_generic_exception(tmp_path: Path) -> None:
    """Generic exception during kill — returns False."""
    from gobby.cli.utils import stop_ui_server

    pid_file = tmp_path / "ui.pid"
    pid_file.write_text("12345")

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.psutil.Process", side_effect=RuntimeError("unexpected")),
    ):
        result = stop_ui_server(quiet=False)
    assert result is False


# ---------------------------------------------------------------------------
# stop_daemon — graceful/force kill paths (lines 803, 824, 841, 843-844, 849-882, 896-899)
# ---------------------------------------------------------------------------


def test_stop_daemon_no_pid_file_not_quiet(tmp_path: Path) -> None:
    """Non-quiet mode echoes when no PID file."""
    from gobby.cli.utils import stop_daemon

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils.click.echo") as mock_echo,
    ):
        result = stop_daemon(quiet=False)
    assert result is True
    mock_echo.assert_called_once()
    assert "not running" in mock_echo.call_args[0][0]


def test_stop_daemon_stale_pid_with_orphans(tmp_path: Path) -> None:
    """Stale PID file + orphaned daemons found by kill_all."""
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("99999")

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", return_value=False),
        patch("gobby.cli.utils.kill_all_gobby_daemons", return_value=2),
        patch("gobby.cli.utils.click.echo") as mock_echo,
    ):
        result = stop_daemon(quiet=False)
    assert result is True
    # Check that orphan cleanup message was printed
    echo_msgs = [call[0][0] for call in mock_echo.call_args_list]
    assert any("orphaned" in m for m in echo_msgs)


def test_stop_daemon_not_gobby_with_orphans(tmp_path: Path) -> None:
    """PID is alive but not gobby, orphans found."""
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("12345")

    mock_proc = MagicMock()
    mock_proc.cmdline.return_value = ["node", "server.js"]

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_proc),
        patch("gobby.cli.utils.kill_all_gobby_daemons", return_value=1),
        patch("gobby.cli.utils.click.echo") as mock_echo,
    ):
        result = stop_daemon(quiet=False)
    assert result is True
    echo_msgs = [call[0][0] for call in mock_echo.call_args_list]
    assert any("orphaned" in m for m in echo_msgs)


def test_stop_daemon_psutil_nosuchprocess_on_verify(tmp_path: Path) -> None:
    """psutil.NoSuchProcess during PID verification — continues with kill attempt."""
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("12345")

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.psutil.Process", side_effect=psutil.NoSuchProcess(12345)),
        patch("gobby.cli.utils.os.kill", side_effect=ProcessLookupError),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = stop_daemon(quiet=False)
    assert result is True


def test_stop_daemon_graceful_shutdown(tmp_path: Path) -> None:
    """Sends SIGTERM and daemon exits gracefully."""
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("12345")

    mock_proc = MagicMock()
    mock_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]

    # First alive=True (initial check), second alive=True (verify cmdline),
    # then alive=False (after SIGTERM)
    alive_calls = iter([True, True, False])

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", side_effect=lambda pid: next(alive_calls)),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_proc),
        patch("gobby.cli.utils.os.kill") as mock_kill,
        patch("gobby.cli.utils.time.sleep"),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = stop_daemon(quiet=False)
    assert result is True
    mock_kill.assert_called_once_with(12345, signal.SIGTERM)


def test_stop_daemon_force_kill_after_timeout(tmp_path: Path) -> None:
    """Process doesn't stop gracefully, falls back to SIGKILL."""
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("12345")

    mock_proc = MagicMock()
    mock_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]

    # Process stays alive through initial check + 200 loop iterations (201 Trues),
    # then dies after SIGKILL (default False at line 874)
    alive_results = [True] * 201  # 1 initial + 200 loop iterations
    alive_iter = iter(alive_results)

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", side_effect=lambda pid: next(alive_iter, False)),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_proc),
        patch("gobby.cli.utils.os.kill") as mock_kill,
        patch("gobby.cli.utils.time.sleep"),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = stop_daemon(quiet=False)
    assert result is True
    # Should have sent both SIGTERM and SIGKILL
    assert mock_kill.call_count == 2
    mock_kill.assert_any_call(12345, signal.SIGTERM)
    mock_kill.assert_any_call(12345, signal.SIGKILL)


def test_stop_daemon_force_kill_fails(tmp_path: Path) -> None:
    """Process refuses to die even after SIGKILL — returns False."""
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("12345")

    mock_proc = MagicMock()
    mock_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]

    # Process is always alive — never dies
    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_proc),
        patch("gobby.cli.utils.os.kill"),
        patch("gobby.cli.utils.time.sleep"),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = stop_daemon(quiet=False)
    assert result is False


def test_stop_daemon_generic_exception(tmp_path: Path) -> None:
    """Generic exception during stop — returns False."""
    from gobby.cli.utils import stop_daemon

    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("12345")

    mock_proc = MagicMock()
    mock_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]

    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils.stop_watchdog"),
        patch("gobby.cli.utils._is_process_alive", return_value=True),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_proc),
        patch("gobby.cli.utils.os.kill", side_effect=RuntimeError("unexpected")),
        patch("gobby.cli.utils.click.echo"),
    ):
        result = stop_daemon(quiet=False)
    assert result is False


# ---------------------------------------------------------------------------
# find_web_dir — package path fallback (lines 558-562)
# ---------------------------------------------------------------------------


def test_find_web_dir_import_error(tmp_path: Path) -> None:
    """ImportError when importing gobby — returns None."""
    from gobby.cli.utils import find_web_dir

    with (
        patch("gobby.cli.utils.Path.cwd", return_value=tmp_path),
        patch.dict("sys.modules", {"gobby": None}),
    ):
        result = find_web_dir(None)
    assert result is None


def test_find_web_dir_package_path(tmp_path: Path) -> None:
    """Falls back to gobby package web dir."""
    from gobby.cli.utils import find_web_dir

    web_dir = tmp_path / "ui" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "package.json").write_text("{}")

    mock_gobby = MagicMock()
    mock_gobby.__file__ = str(tmp_path / "__init__.py")

    with (
        patch("gobby.cli.utils.Path.cwd", return_value=Path("/nonexistent")),
        patch.dict("sys.modules", {"gobby": mock_gobby}),
    ):
        result = find_web_dir(None)
    assert result == web_dir


# ---------------------------------------------------------------------------
# stop_watchdog — not-quiet messages (line 473)
# ---------------------------------------------------------------------------


def test_stop_watchdog_not_quiet_no_pid() -> None:
    """Non-quiet mode when no PID file — logs debug message."""
    from gobby.cli.utils import stop_watchdog

    with patch("gobby.cli.utils.get_gobby_home", return_value=Path("/tmp/nonexistent-gobby")):
        result = stop_watchdog(quiet=False)
    assert result is True

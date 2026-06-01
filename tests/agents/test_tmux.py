"""Unit tests for the gobby.agents.tmux module.

Tests session manager, output reader, config, errors, and singletons.
All tmux subprocess calls are mocked — no real tmux binary required.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.tmux.errors import TmuxNotFoundError, TmuxSessionError
from gobby.agents.tmux.output_reader import TmuxOutputReader, _safe_fifo_component
from gobby.agents.tmux.pty_bridge import TmuxPTYBridge
from gobby.agents.tmux.session_manager import TmuxSessionInfo, TmuxSessionManager
from gobby.agents.tmux.spawner import TmuxSpawner
from gobby.agents.tmux.text_injection import (
    TmuxPaneModeUnavailableError,
    TmuxTargetUnavailableError,
    TmuxTextInjectionTimeout,
    classify_tmux_text_injection_error,
    send_literal_text_to_tmux_target,
    submit_literal_text_to_tmux_target,
)
from gobby.config.tmux import TmuxConfig
from gobby.config.tmux import TmuxConfig as TmuxConfigCanonical

pytestmark = pytest.mark.unit


# =============================================================================
# TmuxConfig
# =============================================================================


class TestTmuxConfig:
    """Tests for TmuxConfig pydantic model."""

    def test_defaults(self) -> None:
        config = TmuxConfig()
        assert config.enabled is True
        assert config.command == "tmux"
        assert config.socket_name == "gobby"
        assert config.config_file is None
        assert config.session_prefix == "gobby"
        assert config.history_limit == 10000
        assert config.idle_reprompt_delay_seconds == 300
        assert config.init_activity_grace_seconds == 5.0

    def test_custom_values(self) -> None:
        config = TmuxConfig(
            enabled=False,
            command="/usr/local/bin/tmux",
            socket_name="test",
            socket_path="/tmp/tmux-1000/test",
            config_file="/tmp/tmux.conf",
            session_prefix="myprefix",
            history_limit=5000,
            idle_reprompt_delay_seconds=420,
            init_activity_grace_seconds=7.5,
        )
        assert config.enabled is False
        assert config.command == "/usr/local/bin/tmux"
        assert config.socket_name == "test"
        assert config.socket_path == "/tmp/tmux-1000/test"
        assert config.config_file == "/tmp/tmux.conf"
        assert config.session_prefix == "myprefix"
        assert config.history_limit == 5000
        assert config.idle_reprompt_delay_seconds == 420
        assert config.init_activity_grace_seconds == 7.5

    def test_wsl_distribution_default(self) -> None:
        config = TmuxConfig()
        assert config.wsl_distribution is None

    def test_wsl_distribution_custom(self) -> None:
        config = TmuxConfig(wsl_distribution="Ubuntu")
        assert config.wsl_distribution == "Ubuntu"

    def test_history_limit_minimum(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal to 100"):
            TmuxConfig(history_limit=50)

    def test_init_activity_grace_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="greater than 0"):
            TmuxConfig(init_activity_grace_seconds=0)

    def test_re_export_matches_canonical(self) -> None:
        """agents/tmux/config.py re-exports the same class from config/tmux.py."""
        assert TmuxConfig is TmuxConfigCanonical


# =============================================================================
# Errors
# =============================================================================


class TestTmuxErrors:
    """Tests for TmuxNotFoundError and TmuxSessionError."""

    def test_not_found_error_default(self) -> None:
        err = TmuxNotFoundError()
        assert "tmux" in str(err)
        assert "not found" in str(err)
        assert err.command == "tmux"

    def test_not_found_error_has_install_hint(self) -> None:
        err = TmuxNotFoundError()
        msg = str(err)
        # Should contain platform-specific install instructions
        assert "Install" in msg or "install" in msg

    def test_not_found_error_custom_command(self) -> None:
        err = TmuxNotFoundError("/opt/tmux")
        assert "/opt/tmux" in str(err)
        assert err.command == "/opt/tmux"

    def test_session_error_with_name(self) -> None:
        err = TmuxSessionError("already exists", session_name="test")
        assert "test" in str(err)
        assert "already exists" in str(err)
        assert err.session_name == "test"

    def test_session_error_without_name(self) -> None:
        err = TmuxSessionError("generic failure")
        assert "tmux:" in str(err)
        assert err.session_name is None


class TestTmuxTextInjection:
    """Tests for literal tmux text injection."""

    @pytest.mark.asyncio
    async def test_uses_buffer_paste_with_configured_tmux_args(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commands: list[list[str]] = []

        async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
            commands.append(list(args))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.asyncio.create_subprocess_exec",
            fake_exec,
        )

        tmux_cmd = ["/opt/tmux", "-S", "/tmp/tmux-501/gobby", "-f", "/tmp/tmux.conf"]
        await send_literal_text_to_tmux_target(
            "%12",
            "-X message\n",
            tmux_cmd=tmux_cmd,
            enter_delay_seconds=0,
        )

        assert len(commands) == 4
        buffer_name = commands[0][7]
        assert commands[0][:5] == tmux_cmd
        assert commands[0][5:] == ["set-buffer", "-b", buffer_name, "--", "-X message"]
        assert commands[1] == [
            *tmux_cmd,
            "paste-buffer",
            "-d",
            "-b",
            buffer_name,
            "-t",
            "%12",
        ]
        assert commands[2] == [*tmux_cmd, "delete-buffer", "-b", buffer_name]
        assert commands[3] == [*tmux_cmd, "send-keys", "-t", "%12", "Enter"]
        assert not any("send-keys" in command and "-l" in command for command in commands)

    @pytest.mark.asyncio
    async def test_submit_literal_text_sends_separate_enter_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commands: list[list[str]] = []

        async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
            commands.append(list(args))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.asyncio.create_subprocess_exec",
            fake_exec,
        )

        await submit_literal_text_to_tmux_target(
            "%12",
            "Message from Gobby daemon: New activity available.",
            enter_delay_seconds=0,
        )

        buffer_name = commands[0][3]
        assert commands == [
            [
                "tmux",
                "set-buffer",
                "-b",
                buffer_name,
                "--",
                "Message from Gobby daemon: New activity available.",
            ],
            [
                "tmux",
                "paste-buffer",
                "-d",
                "-b",
                buffer_name,
                "-t",
                "%12",
            ],
            ["tmux", "delete-buffer", "-b", buffer_name],
            ["tmux", "send-keys", "-t", "%12", "Enter"],
        ]
        assert not any("\n" in arg for command in commands for arg in command)

    @pytest.mark.asyncio
    async def test_submit_literal_text_can_escape_before_paste(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commands: list[list[str]] = []

        async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
            commands.append(list(args))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.asyncio.create_subprocess_exec",
            fake_exec,
        )

        await submit_literal_text_to_tmux_target(
            "%12",
            "Message from Gobby daemon: New activity available.",
            enter_delay_seconds=0,
            escape_before_submit=True,
        )

        buffer_name = commands[1][3]
        assert commands == [
            ["tmux", "send-keys", "-t", "%12", "Escape"],
            [
                "tmux",
                "set-buffer",
                "-b",
                buffer_name,
                "--",
                "Message from Gobby daemon: New activity available.",
            ],
            [
                "tmux",
                "paste-buffer",
                "-d",
                "-b",
                buffer_name,
                "-t",
                "%12",
            ],
            ["tmux", "delete-buffer", "-b", buffer_name],
            ["tmux", "send-keys", "-t", "%12", "Enter"],
        ]

    @pytest.mark.asyncio
    async def test_paste_failure_still_deletes_tmux_buffer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commands: list[list[str]] = []

        async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
            commands.append(list(args))
            proc = MagicMock()
            proc.returncode = 1 if "paste-buffer" in args else 0
            proc.communicate = AsyncMock(return_value=(b"", b"can't find pane: %12"))
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.asyncio.create_subprocess_exec",
            fake_exec,
        )

        with pytest.raises(TmuxTargetUnavailableError):
            await send_literal_text_to_tmux_target(
                "%12",
                "hello",
                enter_delay_seconds=0,
            )

        buffer_name = commands[0][3]
        assert commands[1][:4] == ["tmux", "paste-buffer", "-d", "-b"]
        assert commands[1][4] == buffer_name
        assert commands[2] == ["tmux", "delete-buffer", "-b", buffer_name]

    @pytest.mark.asyncio
    async def test_timeout_is_expected_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=[TimeoutError, (b"", b"")])
        proc.kill = MagicMock()

        async def fake_exec(*_args: str, **_kwargs: object) -> MagicMock:
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.asyncio.create_subprocess_exec",
            fake_exec,
        )

        with pytest.raises(TmuxTextInjectionTimeout) as exc_info:
            await send_literal_text_to_tmux_target("%12", "hello", timeout=0.01)

        assert exc_info.value.expected is True
        assert exc_info.value.error_code == "tmux_command_timeout"
        proc.kill.assert_called_once()

    @pytest.mark.parametrize(
        ("stderr", "expected_type", "error_code"),
        [
            ("can't find pane: %12", TmuxTargetUnavailableError, "tmux_target_unavailable"),
            ("pane is dead", TmuxTargetUnavailableError, "tmux_target_unavailable"),
            ("not in a mode", TmuxPaneModeUnavailableError, "tmux_pane_mode_unavailable"),
        ],
    )
    def test_classifies_expected_tmux_failures(
        self,
        stderr: str,
        expected_type: type[Exception],
        error_code: str,
    ) -> None:
        error = classify_tmux_text_injection_error(("tmux", "paste-buffer"), 1, stderr)

        assert isinstance(error, expected_type)
        assert error.expected is True
        assert error.error_code == error_code


# =============================================================================
# TmuxSessionManager
# =============================================================================


class TestTmuxSessionManager:
    """Tests for TmuxSessionManager."""

    def test_base_args_default(self) -> None:
        mgr = TmuxSessionManager()
        args = mgr._base_args()
        assert args == ["tmux", "-L", "gobby", "-f", "/dev/null"]

    def test_base_args_with_config_file(self) -> None:
        config = TmuxConfig(config_file="/tmp/my.conf", socket_name="test")
        mgr = TmuxSessionManager(config)
        args = mgr._base_args()
        assert args == ["tmux", "-L", "test", "-f", "/tmp/my.conf"]

    def test_base_args_empty_socket_name(self) -> None:
        """Empty socket_name skips -L flag (uses default tmux server)."""
        config = TmuxConfig(socket_name="")
        mgr = TmuxSessionManager(config)
        args = mgr._base_args()
        assert args == ["tmux", "-f", "/dev/null"]

    def test_base_args_empty_socket_with_config(self) -> None:
        config = TmuxConfig(socket_name="", config_file="/tmp/my.conf")
        mgr = TmuxSessionManager(config)
        args = mgr._base_args()
        assert args == ["tmux", "-f", "/tmp/my.conf"]

    def test_base_args_socket_path_takes_precedence(self) -> None:
        config = TmuxConfig(socket_name="ignored", socket_path="/tmp/tmux-1000/gobby")
        mgr = TmuxSessionManager(config)
        args = mgr._base_args()
        assert args == ["tmux", "-S", "/tmp/tmux-1000/gobby", "-f", "/dev/null"]

    @patch("shutil.which", return_value="/usr/bin/tmux")
    def test_is_available_true(self, mock_which: MagicMock) -> None:
        mgr = TmuxSessionManager()
        assert mgr.is_available() is True

    @patch("shutil.which", return_value=None)
    def test_is_available_false(self, mock_which: MagicMock) -> None:
        mgr = TmuxSessionManager()
        assert mgr.is_available() is False

    @patch("shutil.which", return_value=None)
    def test_require_available_raises(self, mock_which: MagicMock) -> None:
        mgr = TmuxSessionManager()
        with pytest.raises(TmuxNotFoundError):
            mgr.require_available()

    @pytest.mark.asyncio
    async def test_create_session_success(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            # create_session calls _run three times: has_session, new-session, display-message (get_pane_pid)
            mock_run.side_effect = [
                (1, "", ""),  # has_session → not found (rc=1)
                (0, "", ""),  # new-session
                (0, "12345\n", ""),  # display-message for pane_pid
            ]
            with patch.object(mgr, "is_available", return_value=True):
                info = await mgr.create_session(
                    name="test.session:1",
                    command="echo hello",
                    cwd="/tmp",
                )

            assert info.name == "test-session-1"  # sanitised
            assert info.pane_pid == 12345

    @pytest.mark.asyncio
    async def test_create_session_failure(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "duplicate session: test")
            with (
                patch.object(mgr, "is_available", return_value=True),
                pytest.raises(TmuxSessionError, match="duplicate session"),
            ):
                await mgr.create_session(name="test")

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no server running")
            result = await mgr.list_sessions()
            assert result == []

    @pytest.mark.asyncio
    async def test_run_timeout_handles_already_exited_process(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mgr = TmuxSessionManager()
        proc = MagicMock()
        proc.pid = 12345
        proc.communicate = AsyncMock()
        proc.kill.side_effect = ProcessLookupError()
        proc.wait = AsyncMock()

        with (
            patch(
                "gobby.agents.tmux.session_manager.asyncio.create_subprocess_exec",
                return_value=proc,
            ),
            patch(
                "gobby.agents.tmux.session_manager.asyncio.wait_for",
                side_effect=TimeoutError,
            ),
        ):
            with (
                caplog.at_level(logging.DEBUG, logger="gobby.agents.tmux.session_manager"),
                pytest.raises(TimeoutError),
            ):
                await mgr._run("list-sessions", timeout=0.01)

        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()
        assert "pid=12345" in caplog.text
        assert "list-sessions" in caplog.text
        assert "timeout=0.01s" in caplog.text

    @pytest.mark.asyncio
    async def test_list_sessions_with_entries(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            # Format: session_name\tpane_pid\tpane_id\twindow_name\tpane_title\tpane_dead
            mock_run.return_value = (
                0,
                "session1\t100\t%1\tzsh\t\t0\nsession2\t200\t%2\tzsh\t\t0\n",
                "",
            )
            result = await mgr.list_sessions()
            assert len(result) == 2
            assert result[0].name == "session1"
            assert result[0].pane_pid == 100
            assert result[0].pane_id == "%1"
            assert result[1].name == "session2"
            assert result[1].pane_id == "%2"

    @pytest.mark.asyncio
    async def test_get_session_returns_target_pane_metadata(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "session1\t100\t%1\tzsh\tTitle\t0\n", "")
            result = await mgr.get_session("session1")

        assert result is not None
        assert result.name == "session1"
        assert result.pane_pid == 100
        assert result.pane_id == "%1"
        assert result.window_name == "zsh"
        assert result.pane_title == "Title"
        mock_run.assert_awaited_once_with(
            "list-panes",
            "-t",
            "session1",
            "-F",
            "#{session_name}\t#{pane_pid}\t#{pane_id}\t#{window_name}\t#{pane_title}\t#{pane_dead}",
            timeout=2.0,
        )

    @pytest.mark.asyncio
    async def test_get_session_returns_none_when_missing(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "can't find session")
            result = await mgr.get_session("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_has_session(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            assert await mgr.has_session("test") is True

            mock_run.return_value = (1, "", "")
            assert await mgr.has_session("missing") is False

    @pytest.mark.asyncio
    async def test_kill_session(self, caplog: pytest.LogCaptureFixture) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            assert await mgr.kill_session("test") is True

            mock_run.return_value = (1, "", "no such session")
            assert await mgr.kill_session("missing") is False
            assert await mgr.kill_session("missing", missing_ok=True) is True

            mock_run.return_value = (1, "", "no server running on /tmp/tmux-123/gobby")
            assert await mgr.kill_session("missing-server") is False
            assert await mgr.kill_session("missing-server", missing_ok=True) is True

        assert not [record for record in caplog.records if record.levelname == "WARNING"]

    @pytest.mark.asyncio
    async def test_rename_window_success(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            assert await mgr.rename_window("%42", "My Title") is True
            mock_run.assert_called_once_with(
                "set-option",
                "-t",
                "%42",
                "set-titles",
                "on",
                ";",
                "set-option",
                "-t",
                "%42",
                "set-titles-string",
                "#W",
                ";",
                "rename-window",
                "-t",
                "%42",
                "My Title",
                ";",
                "select-pane",
                "-t",
                "%42",
                "-T",
                "My Title",
                ";",
                "set-option",
                "-w",
                "-t",
                "%42",
                "automatic-rename",
                "off",
                ";",
                "set-option",
                "-w",
                "-t",
                "%42",
                "allow-rename",
                "off",
            )
            assert "-g" not in mock_run.call_args.args

    @pytest.mark.asyncio
    async def test_rename_window_missing_pane_logs_debug(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "can't find pane: %53")

            with caplog.at_level("DEBUG", logger="gobby.agents.tmux.session_manager"):
                assert await mgr.rename_window("%53", "Title") is False

        assert "Skipping tmux window rename for missing target '%53'" in caplog.text
        assert not [record for record in caplog.records if record.levelname == "WARNING"]

    @pytest.mark.asyncio
    async def test_rename_window_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no such window")

            with caplog.at_level("WARNING", logger="gobby.agents.tmux.session_manager"):
                assert await mgr.rename_window("%99", "Title") is False

        assert "Failed to rename tmux window for '%99': no such window" in caplog.text

    @pytest.mark.asyncio
    async def test_send_keys(self) -> None:
        mgr = TmuxSessionManager()
        with patch(
            "gobby.agents.tmux.session_manager.send_literal_text_to_tmux_target",
            new_callable=AsyncMock,
        ) as mock_send:
            assert await mgr.send_keys("test", "hello") is True
        mock_send.assert_awaited_once_with(
            "test",
            "hello",
            tmux_cmd=["tmux", "-L", "gobby", "-f", "/dev/null"],
        )

    @pytest.mark.asyncio
    async def test_get_pane_pid(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "42\n", "")
            assert await mgr.get_pane_pid("test") == 42

            mock_run.return_value = (1, "", "")
            assert await mgr.get_pane_pid("missing") is None


# =============================================================================
# TmuxOutputReader
# =============================================================================


class TestTmuxOutputReader:
    """Tests for TmuxOutputReader."""

    def test_set_output_callback(self) -> None:
        reader = TmuxOutputReader()
        assert reader._output_callback is None

        async def cb(run_id: str, data: str) -> None:
            pass

        reader.set_output_callback(cb)
        assert reader._output_callback is cb

    def test_base_args(self) -> None:
        config = TmuxConfig(socket_name="test-sock")
        reader = TmuxOutputReader(config)
        args = reader._base_args()
        assert args == ["tmux", "-L", "test-sock"]

    def test_base_args_empty_socket(self) -> None:
        """Empty socket_name skips -L flag."""
        config = TmuxConfig(socket_name="")
        reader = TmuxOutputReader(config)
        args = reader._base_args()
        assert args == ["tmux"]

    def test_base_args_socket_path(self) -> None:
        config = TmuxConfig(socket_name="", socket_path="/tmp/tmux-1000/gobby")
        reader = TmuxOutputReader(config)
        args = reader._base_args()
        assert args == ["tmux", "-S", "/tmp/tmux-1000/gobby"]

    def test_safe_fifo_component_removes_path_separators(self) -> None:
        assert _safe_fifo_component("../session/name") == "session-name"

    @pytest.mark.asyncio
    async def test_stop_reader_not_running(self) -> None:
        reader = TmuxOutputReader()
        assert await reader.stop_reader("nonexistent") is False

    @pytest.mark.asyncio
    async def test_stop_all_empty(self) -> None:
        reader = TmuxOutputReader()
        await reader.stop_all()

        assert reader._reader_tasks == {}


# =============================================================================
# TmuxSessionInfo
# =============================================================================


class TestTmuxSessionInfo:
    def test_defaults(self) -> None:
        info = TmuxSessionInfo(name="test")
        assert info.name == "test"
        assert info.pane_pid is None
        assert info.pane_id is None
        assert info.window_name is None
        assert info.created_at > 0


# =============================================================================
# Singletons
# =============================================================================


class TestSingletons:
    """Tests for module-level singleton getters."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self) -> Iterator[None]:
        """Reset module-level singletons before and after each test."""
        import gobby.agents.tmux as mod

        mod._session_manager = None
        mod._output_reader = None
        yield
        mod._session_manager = None
        mod._output_reader = None

    def test_get_tmux_session_manager_returns_same(self) -> None:
        import gobby.agents.tmux as mod

        mgr1 = mod.get_tmux_session_manager()
        mgr2 = mod.get_tmux_session_manager()
        assert mgr1 is mgr2

    def test_get_tmux_output_reader_returns_same(self) -> None:
        import gobby.agents.tmux as mod

        r1 = mod.get_tmux_output_reader()
        r2 = mod.get_tmux_output_reader()
        assert r1 is r2


# =============================================================================
# DaemonConfig integration
# =============================================================================


class TestDaemonConfigTmux:
    """TmuxConfig is properly wired into DaemonConfig."""

    def test_default_tmux_config(self) -> None:
        from gobby.config.app import DaemonConfig

        config = DaemonConfig()
        assert config.tmux.enabled is True
        assert config.tmux.socket_name == "gobby"

    def test_custom_tmux_config(self) -> None:
        from gobby.config.app import DaemonConfig

        config = DaemonConfig(tmux={"enabled": False, "socket_name": "custom"})
        assert config.tmux.enabled is False
        assert config.tmux.socket_name == "custom"


# =============================================================================
# TmuxPTYBridge
# =============================================================================


class TestTmuxPTYBridge:
    """Tests for TmuxPTYBridge."""

    @pytest.mark.asyncio
    async def test_init(self) -> None:
        bridge = TmuxPTYBridge()
        assert bridge._bridges == {}
        assert await bridge.list_bridges() == {}

    @pytest.mark.asyncio
    async def test_get_master_fd_missing(self) -> None:
        bridge = TmuxPTYBridge()
        assert await bridge.get_master_fd("nonexistent") is None

    @pytest.mark.asyncio
    async def test_get_bridge_missing(self) -> None:
        bridge = TmuxPTYBridge()
        assert await bridge.get_bridge("nonexistent") is None

    def test_build_attach_cmd_gobby(self) -> None:
        bridge = TmuxPTYBridge()
        config = TmuxConfig(socket_name="gobby")
        cmd = bridge._build_attach_cmd("my-session", config)
        assert cmd == ["tmux", "-L", "gobby", "attach-session", "-t", "my-session"]

    def test_build_attach_cmd_default_server(self) -> None:
        bridge = TmuxPTYBridge()
        config = TmuxConfig(socket_name="")
        cmd = bridge._build_attach_cmd("my-session", config)
        assert cmd == ["tmux", "attach-session", "-t", "my-session"]

    def test_build_attach_cmd_socket_path(self) -> None:
        bridge = TmuxPTYBridge()
        config = TmuxConfig(socket_name="", socket_path="/tmp/tmux-1000/gobby")
        cmd = bridge._build_attach_cmd("my-session", config)
        assert cmd == ["tmux", "-S", "/tmp/tmux-1000/gobby", "attach-session", "-t", "my-session"]

    @pytest.mark.asyncio
    async def test_detach_missing_is_noop(self) -> None:
        bridge = TmuxPTYBridge()
        await bridge.detach("nonexistent")

        assert await bridge.list_bridges() == {}

    @pytest.mark.asyncio
    async def test_detach_all_empty(self) -> None:
        bridge = TmuxPTYBridge()
        await bridge.detach_all()

        assert await bridge.list_bridges() == {}

    @pytest.mark.asyncio
    async def test_attach_duplicate_raises(self) -> None:
        bridge = TmuxPTYBridge()
        # Manually insert a bridge entry
        from unittest.mock import MagicMock

        from gobby.agents.tmux.pty_bridge import BridgeInfo

        mock_proc = MagicMock()
        bridge._bridges["test-id"] = BridgeInfo(
            master_fd=999, proc=mock_proc, session_name="sess", socket_name="gobby"
        )

        with pytest.raises(RuntimeError, match="already exists"):
            await bridge.attach("sess", "test-id")

    @pytest.mark.asyncio
    async def test_resize_missing_is_noop(self) -> None:
        bridge = TmuxPTYBridge()
        assert await bridge.resize("nonexistent", 50, 200) is None


# =============================================================================
# TmuxSpawner
# =============================================================================


class TestTmuxSpawner:
    """Tests for TmuxSpawner._async_spawn environment handling."""

    @pytest.mark.asyncio
    async def test_virtual_env_cleared_in_extra_env(self) -> None:
        """VIRTUAL_ENV and VIRTUAL_ENV_PROMPT are set to empty via -e flags."""
        spawner = TmuxSpawner()
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(
                command=["echo", "hello"],
                cwd="/tmp",
                env={"GOBBY_SESSION_ID": "sess-1"},
            )

            mock_create.assert_called_once()
            env_arg = (
                mock_create.call_args[1].get("env") or mock_create.call_args[0][3]
                if len(mock_create.call_args[0]) > 3
                else mock_create.call_args[1].get("env")
            )
            assert env_arg["VIRTUAL_ENV"] == ""
            assert env_arg["VIRTUAL_ENV_PROMPT"] == ""
            # Gobby-specific vars should still be passed
            assert env_arg["GOBBY_SESSION_ID"] == "sess-1"

    @pytest.mark.asyncio
    async def test_uv_cache_dir_defaults_to_session_temp_path(self) -> None:
        """Spawned agents get a writable per-session uv cache by default."""
        spawner = TmuxSpawner()
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
            patch("gobby.agents.constants.tempfile.gettempdir", return_value="/tmp/test-tmp"),
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(
                command=["echo", "hello"],
                cwd="/tmp",
                env={"GOBBY_SESSION_ID": "sess/1"},
            )

            env_arg = mock_create.call_args[1].get("env")
            uv_cache = Path(env_arg["UV_CACHE_DIR"])
            assert uv_cache.parts[-3:-1] == ("gobby", "uv-cache")
            assert uv_cache.parts[-1].startswith("sess-1-")

    @pytest.mark.asyncio
    async def test_managed_tool_bin_path_is_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """tmux receives PATH explicitly so ~/.gobby/bin is visible in the child shell."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("PATH", "/usr/bin")
        spawner = TmuxSpawner()
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(
                command=["echo", "hello"],
                cwd="/tmp",
                env={"GOBBY_SESSION_ID": "sess-1"},
            )

            env_arg = mock_create.call_args[1].get("env")
            assert env_arg["PATH"].split(os.pathsep) == [
                str(tmp_path / ".gobby" / "bin"),
                "/usr/bin",
            ]

    @pytest.mark.asyncio
    async def test_uv_cache_dir_explicit_value_preserved(self) -> None:
        """Explicit UV_CACHE_DIR values are passed through unchanged."""
        spawner = TmuxSpawner()
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(
                command=["echo", "hello"],
                cwd="/tmp",
                env={
                    "GOBBY_SESSION_ID": "sess-1",
                    "UV_CACHE_DIR": "/custom/uv-cache",
                },
            )

            env_arg = mock_create.call_args[1].get("env")
            assert env_arg["UV_CACHE_DIR"] == "/custom/uv-cache"

    @pytest.mark.asyncio
    async def test_uv_cache_dir_empty_value_gets_default(self) -> None:
        """Empty UV_CACHE_DIR values are treated as missing."""
        spawner = TmuxSpawner()
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
            patch("gobby.agents.constants.tempfile.gettempdir", return_value="/tmp/test-tmp"),
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(
                command=["echo", "hello"],
                cwd="/tmp",
                env={
                    "GOBBY_SESSION_ID": "sess-1",
                    "UV_CACHE_DIR": "",
                },
            )

            env_arg = mock_create.call_args[1].get("env")
            uv_cache = Path(env_arg["UV_CACHE_DIR"])
            assert uv_cache.parts[-3:-1] == ("gobby", "uv-cache")
            assert uv_cache.parts[-1].startswith("sess-1-")

    @pytest.mark.asyncio
    async def test_unset_in_shell_command(self) -> None:
        """Shell command is prefixed with unset VIRTUAL_ENV."""
        spawner = TmuxSpawner()
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(
                command=["claude", "--session-id=xxx"],
                cwd="/tmp",
            )

            mock_create.assert_called_once()
            cmd_arg = (
                mock_create.call_args[1].get("command") or mock_create.call_args[0][1]
                if len(mock_create.call_args[0]) > 1
                else mock_create.call_args[1].get("command")
            )
            assert cmd_arg.startswith("unset VIRTUAL_ENV VIRTUAL_ENV_PROMPT;")

    @pytest.mark.asyncio
    async def test_spawn_returns_success(self) -> None:
        """Successful spawn returns SpawnResult with success=True."""
        spawner = TmuxSpawner()
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=456)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=456)
            result = await spawner._async_spawn(
                command=["echo", "test"],
                cwd="/tmp",
            )

            assert result.success is True
            assert result.pid == 456
            assert result.tmux_session_name == "test-session"
            assert result.tmux_socket_name == "gobby"
            assert result.tmux_socket_path is None

    @pytest.mark.asyncio
    async def test_spawn_fails_when_verified_pane_is_dead(self) -> None:
        """A dead tmux pane is not a usable spawn."""
        spawner = TmuxSpawner()
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=456)
            mock_get.return_value = TmuxSessionInfo(
                name="test-session",
                pane_pid=456,
                pane_dead=True,
            )
            result = await spawner._async_spawn(command=["echo", "test"], cwd="/tmp")

        assert result.success is False
        assert result.error == "tmux session 'test-session' pane is dead"

    @pytest.mark.asyncio
    async def test_spawn_fails_when_verified_pane_pid_is_missing(self) -> None:
        """A tmux session without pane_pid fails live-pane verification."""
        spawner = TmuxSpawner()
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
            patch("gobby.agents.tmux.spawner.time.monotonic", side_effect=[0.0, 2.1]),
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=None)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=None)
            result = await spawner._async_spawn(command=["echo", "test"], cwd="/tmp")

        assert result.success is False
        assert result.error == "tmux session 'test-session' has no pane PID"

    @pytest.mark.asyncio
    async def test_spawn_failure_returns_error(self) -> None:
        """Failed spawn returns SpawnResult with success=False."""
        spawner = TmuxSpawner()
        with patch.object(
            spawner._session_manager, "create_session", new_callable=AsyncMock
        ) as mock_create:
            mock_create.side_effect = RuntimeError("tmux not found")
            result = await spawner._async_spawn(
                command=["echo", "test"],
                cwd="/tmp",
            )

            assert result.success is False
            assert result.error is not None
            assert "tmux not found" in result.error

    @pytest.mark.asyncio
    async def test_spawn_forwards_auth_env_from_daemon_environment(self) -> None:
        """tmux receives allowlisted auth env from the live daemon process."""
        spawner = TmuxSpawner()
        daemon_env = {
            "HOME": "/home/daemon",
            "ANTHROPIC_API_KEY": "sk-daemon",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
            "UNRELATED_SECRET": "nope",
        }
        with (
            patch.dict(os.environ, daemon_env, clear=False),
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(
                command=["claude", "--dangerously-skip-permissions"],
                cwd="/tmp",
                env={"GOBBY_SESSION_ID": "sess-1"},
            )

        env_arg = mock_create.call_args[1]["env"]
        assert env_arg["GOBBY_SESSION_ID"] == "sess-1"
        assert env_arg["HOME"] == "/home/daemon"
        assert env_arg["ANTHROPIC_API_KEY"] == "sk-daemon"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_arg
        assert "UNRELATED_SECRET" not in env_arg

    @pytest.mark.asyncio
    async def test_spawn_keeps_explicit_env_over_passthrough(self) -> None:
        """Command/sandbox env wins over daemon passthrough values."""
        spawner = TmuxSpawner()
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-daemon"}, clear=False),
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(
                command=["claude"],
                cwd="/tmp",
                env={"ANTHROPIC_API_KEY": "sk-override"},
            )

        assert mock_create.call_args[1]["env"]["ANTHROPIC_API_KEY"] == "sk-override"

    @pytest.mark.asyncio
    async def test_spawn_never_forwards_claude_oauth_token(self) -> None:
        """Claude OAuth env tokens are not forwarded into spawned tmux panes."""
        spawner = TmuxSpawner()
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token"}, clear=False),
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(command=["claude"], cwd="/tmp")

        assert "CLAUDE_CODE_OAUTH_TOKEN" not in mock_create.call_args[1]["env"]


# =============================================================================
# TmuxSessionManager additional coverage
# =============================================================================


class TestTmuxSessionManagerExtended:
    """Additional tests for TmuxSessionManager uncovered methods."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(self) -> None:
        """health_check returns True when tmux socket is responsive."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (0, "session1: 1 windows\n", "")
            result = await mgr.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_no_server_is_ok(self) -> None:
        """health_check returns True when no server running (rc=1 with message)."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (1, "", "no server running on /tmp/tmux-123/gobby")
            result = await mgr.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_unavailable(self) -> None:
        """health_check returns False when tmux is not available."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "is_available", return_value=False):
            result = await mgr.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_timeout_kills_server(self) -> None:
        """health_check kills stale server on timeout and returns True."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            # First call times out, second call (kill-server) succeeds
            mock_run.side_effect = [
                TimeoutError("socket stuck"),
                (0, "", ""),
            ]
            result = await mgr.health_check()
        assert result is True
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_health_check_timeout_kill_fails(self) -> None:
        """health_check returns False when kill-server also fails."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [
                TimeoutError("socket stuck"),
                RuntimeError("kill failed too"),
            ]
            result = await mgr.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_generic_error_kills_server(self) -> None:
        """health_check attempts kill-server on generic error."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [
                RuntimeError("unexpected"),
                (0, "", ""),
            ]
            result = await mgr.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_list_pane_ids(self) -> None:
        """list_pane_ids returns only panes that tmux reports as alive."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "%0\t0\n%5\t1\n%12\t0\n", "")
            result = await mgr.list_pane_ids()
        assert result == {"%0", "%12"}
        mock_run.assert_awaited_once_with(
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{pane_dead}",
        )

    @pytest.mark.asyncio
    async def test_list_pane_ids_failure(self) -> None:
        """list_pane_ids returns empty set on failure."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no server")
            result = await mgr.list_pane_ids()
        assert result == set()

    @pytest.mark.asyncio
    async def test_create_session_already_exists_raises(self) -> None:
        """create_session raises TmuxSessionError if session already exists."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "has_session", new_callable=AsyncMock, return_value=True),
            pytest.raises(TmuxSessionError, match="already exists"),
        ):
            await mgr.create_session(name="existing")

    @pytest.mark.asyncio
    async def test_create_session_with_list_command(self) -> None:
        """create_session accepts command as list."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [
                (1, "", ""),  # has_session
                (0, "", ""),  # new-session
                (0, "999\n", ""),  # display-message for pane_pid
            ]
            info = await mgr.create_session(
                name="test",
                command=["claude", "--session-id=abc"],
                cwd="/tmp",
                env={"MY_VAR": "value"},
            )
        assert info.name == "test"
        assert info.pane_pid == 999

    @pytest.mark.asyncio
    async def test_capture_pane_success(self) -> None:
        """capture_pane returns captured output."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "line 1\nline 2\n", "")
            result = await mgr.capture_pane("my-session", lines=2)
        assert result == "line 1\nline 2\n"

    @pytest.mark.asyncio
    async def test_capture_pane_failure(self) -> None:
        """capture_pane returns None on failure."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no such session")
            result = await mgr.capture_pane("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_send_keys_with_newline(self) -> None:
        """send_keys delegates literal text, including trailing newline, to the helper."""
        config = TmuxConfig(socket_name="ignored", socket_path="/tmp/tmux-501/gobby")
        mgr = TmuxSessionManager(config)
        with patch(
            "gobby.agents.tmux.session_manager.send_literal_text_to_tmux_target",
            new_callable=AsyncMock,
        ) as mock_send:
            result = await mgr.send_keys("test-sess", "hello\n")
        assert result is True
        mock_send.assert_awaited_once_with(
            "test-sess",
            "hello\n",
            tmux_cmd=["tmux", "-S", "/tmp/tmux-501/gobby", "-f", "/dev/null"],
        )

    @pytest.mark.asyncio
    async def test_send_keys_without_newline(self) -> None:
        """send_keys without trailing newline still uses paste-buffer helper."""
        mgr = TmuxSessionManager()
        with patch(
            "gobby.agents.tmux.session_manager.send_literal_text_to_tmux_target",
            new_callable=AsyncMock,
        ) as mock_send:
            result = await mgr.send_keys("test-sess", "hello")
        assert result is True
        mock_send.assert_awaited_once_with(
            "test-sess",
            "hello",
            tmux_cmd=["tmux", "-L", "gobby", "-f", "/dev/null"],
        )

    @pytest.mark.asyncio
    async def test_send_keys_text_failure(self) -> None:
        """send_keys returns False when literal text injection fails."""
        mgr = TmuxSessionManager()
        with patch(
            "gobby.agents.tmux.session_manager.send_literal_text_to_tmux_target",
            new_callable=AsyncMock,
        ) as mock_send:
            mock_send.side_effect = TmuxTargetUnavailableError(
                "tmux target is unavailable: no such session",
                command=("tmux", "paste-buffer"),
                stderr="no such session",
                returncode=1,
            )
            result = await mgr.send_keys("missing", "text")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_keys_raw_key_mode_uses_send_keys(self) -> None:
        """Raw key mode still sends tmux key names through send-keys."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            result = await mgr.send_keys("test", "C-c", literal=False)
        assert result is True
        mock_run.assert_awaited_once_with("send-keys", "-t", "test", "C-c")

    @pytest.mark.asyncio
    async def test_get_pane_pid_invalid_output(self) -> None:
        """get_pane_pid returns None when output is not a valid integer."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "not-a-number\n", "")
            result = await mgr.get_pane_pid("test")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_sessions_skips_blank_lines(self) -> None:
        """list_sessions skips blank lines in output."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (
                0,
                "session1\t100\t%1\tzsh\t\t0\n\n\nsession2\t200\n",
                "",
            )
            result = await mgr.list_sessions()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_sessions_pane_dead_flag(self) -> None:
        """list_sessions correctly parses pane_dead flag."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (
                0,
                "dead-session\t100\t%1\tzsh\tTitle\t1\n",
                "",
            )
            result = await mgr.list_sessions()
        assert len(result) == 1
        assert result[0].pane_dead is True
        assert result[0].pane_title == "Title"

    @pytest.mark.asyncio
    async def test_kill_session_with_pids(self) -> None:
        """kill_session sends SIGTERM and SIGKILL to pane PIDs."""

        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
            patch("os.killpg") as mock_killpg,
            patch("os.getpgid", return_value=12345),
        ):
            mock_run.side_effect = [
                (0, "12345\n", ""),  # list-panes for PIDs
                (0, "", ""),  # kill-session
            ]
            result = await mgr.kill_session("test")
        assert result is True
        # Should have called killpg with SIGTERM then SIGKILL
        assert mock_killpg.call_count >= 2


class TestGetWindowAutomaticRename:
    """Tests for TmuxSessionManager.get_window_automatic_rename."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1", True),
            ("on", True),
            ("0", False),
            ("off", False),
            ("", None),
            ("weird", None),
        ],
    )
    async def test_parses_flag(self, raw: str, expected: bool | None) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, raw + "\n", "")
            result = await mgr.get_window_automatic_rename("%1")
        assert result is expected

    @pytest.mark.asyncio
    async def test_returns_none_on_error_rc(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no server running")
            result = await mgr.get_window_automatic_rename("%1")
        assert result is None


class TestGetWindowName:
    """Tests for TmuxSessionManager.get_window_name."""

    @pytest.mark.asyncio
    async def test_returns_window_name(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "#99: gobby\n", "")
            result = await mgr.get_window_name("%1")

        assert result == "#99: gobby"
        mock_run.assert_called_once_with("display-message", "-t", "%1", "-p", "#{window_name}")

    @pytest.mark.asyncio
    async def test_returns_none_on_error_or_empty(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no server running")
            assert await mgr.get_window_name("%1") is None

            mock_run.return_value = (0, "\n", "")
            assert await mgr.get_window_name("%1") is None

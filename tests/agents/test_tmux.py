"""Unit tests for the gobby.agents.tmux module.

Tests session manager, text injection, config, and pane probes.
All tmux subprocess calls are mocked — no real tmux binary required.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.tmux.session_manager import (
    TmuxProbeState,
    TmuxReleaseOutcome,
    TmuxSessionManager,
)
from gobby.agents.tmux.text_injection import (
    TMUX_BUFFER_CHUNK_BYTES,
    TmuxPaneModeUnavailableError,
    TmuxTargetUnavailableError,
    TmuxTextInjectionTimeout,
    _split_for_tmux_buffer,
    classify_tmux_text_injection_error,
    paste_literal_text_to_tmux_target,
    send_literal_text_to_tmux_target,
)
from gobby.config.tmux import TmuxConfig

pytestmark = pytest.mark.unit

_SOCKET = "/tmp/tmux-501/gobby"


# =============================================================================
# TmuxConfig
# =============================================================================


class TestTmuxConfig:
    """Tests for TmuxConfig pydantic model."""

    def test_defaults(self) -> None:
        config = TmuxConfig()
        assert config.attach_history_lines == 500
        assert config.idle_reprompt_delay_seconds == 300

    def test_custom_values(self) -> None:
        config = TmuxConfig(attach_history_lines=700, idle_reprompt_delay_seconds=420)
        assert config.attach_history_lines == 700
        assert config.idle_reprompt_delay_seconds == 420


class TestTmuxTextInjection:
    """Tests for literal tmux text injection."""

    @pytest.mark.asyncio
    async def test_uses_buffer_paste_with_configured_tmux_args(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commands: list[list[str]] = []
        sleep = AsyncMock()

        async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
            commands.append(list(args))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.spawn.create_subprocess_exec",
            fake_exec,
        )
        monkeypatch.setattr("gobby.agents.tmux.text_injection.asyncio.sleep", sleep)

        tmux_cmd = ["tmux", "-S", _SOCKET]
        await send_literal_text_to_tmux_target(
            "%12",
            "-X message\n",
            tmux_cmd=tmux_cmd,
        )

        assert len(commands) == 4
        buffer_name = commands[0][5]
        assert commands[0][:3] == tmux_cmd
        assert commands[0][3:] == ["set-buffer", "-b", buffer_name, "--", "-X message"]
        assert commands[1] == [
            *tmux_cmd,
            "paste-buffer",
            "-d",
            "-p",
            "-b",
            buffer_name,
            "-t",
            "%12",
        ]
        assert commands[2] == [*tmux_cmd, "delete-buffer", "-b", buffer_name]
        assert commands[3] == [*tmux_cmd, "send-keys", "-t", "%12", "Enter"]
        assert not any("send-keys" in command and "-l" in command for command in commands)
        sleep.assert_awaited_once_with(1.0)

    @pytest.mark.asyncio
    async def test_multiple_trailing_newlines_send_one_enter_after_one_delay(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commands: list[list[str]] = []
        sleep = AsyncMock()

        async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
            commands.append(list(args))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.spawn.create_subprocess_exec",
            fake_exec,
        )
        monkeypatch.setattr("gobby.agents.tmux.text_injection.asyncio.sleep", sleep)

        await send_literal_text_to_tmux_target("%12", "hello\n\n")

        assert [command[1] for command in commands] == [
            "set-buffer",
            "paste-buffer",
            "delete-buffer",
            "send-keys",
        ]
        assert commands[0][-1] == "hello"
        assert commands[-1] == ["tmux", "send-keys", "-t", "%12", "Enter"]
        sleep.assert_awaited_once_with(1.0)

    @pytest.mark.asyncio
    async def test_without_trailing_newline_preserves_internal_newline_without_enter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commands: list[list[str]] = []
        sleep = AsyncMock()

        async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
            commands.append(list(args))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.spawn.create_subprocess_exec",
            fake_exec,
        )
        monkeypatch.setattr("gobby.agents.tmux.text_injection.asyncio.sleep", sleep)

        await send_literal_text_to_tmux_target("%12", "alpha\nbeta")

        assert [command[1] for command in commands] == [
            "set-buffer",
            "paste-buffer",
            "delete-buffer",
        ]
        assert commands[0][-1] == "alpha\nbeta"
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_newline_only_sends_one_enter_without_paste_delay(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commands: list[list[str]] = []
        sleep = AsyncMock()

        async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
            commands.append(list(args))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.spawn.create_subprocess_exec",
            fake_exec,
        )
        monkeypatch.setattr("gobby.agents.tmux.text_injection.asyncio.sleep", sleep)

        await send_literal_text_to_tmux_target("%12", "\n")

        assert commands == [["tmux", "send-keys", "-t", "%12", "Enter"]]
        assert not any(command[1] in {"set-buffer", "paste-buffer"} for command in commands)
        sleep.assert_not_awaited()

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
            "gobby.agents.tmux.text_injection.spawn.create_subprocess_exec",
            fake_exec,
        )

        with pytest.raises(TmuxTargetUnavailableError):
            await send_literal_text_to_tmux_target(
                "%12",
                "hello",
                enter_delay_seconds=0,
            )

        buffer_name = commands[0][3]
        assert commands[1][:5] == ["tmux", "paste-buffer", "-d", "-p", "-b"]
        assert commands[1][5] == buffer_name
        assert commands[2] == ["tmux", "delete-buffer", "-b", buffer_name]

    @pytest.mark.asyncio
    async def test_timeout_is_expected_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=[TimeoutError, (b"", b"")])
        proc.kill = MagicMock()

        async def fake_exec(*_args: str, **_kwargs: object) -> MagicMock:
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.spawn.create_subprocess_exec",
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

    def test_short_text_is_one_chunk(self) -> None:
        assert _split_for_tmux_buffer("hello") == ["hello"]
        assert _split_for_tmux_buffer("") == [""]

    def test_oversized_text_splits_under_the_limit(self) -> None:
        text = "A" * (TMUX_BUFFER_CHUNK_BYTES * 2 + 17)

        chunks = _split_for_tmux_buffer(text)

        assert len(chunks) == 3
        assert all(len(chunk.encode()) <= TMUX_BUFFER_CHUNK_BYTES for chunk in chunks)
        assert "".join(chunks) == text

    def test_split_never_breaks_a_multibyte_code_point(self) -> None:
        # Land a 4-byte code point across the boundary: 8190 ASCII bytes leaves
        # only two bytes of room before the cut at 8192.
        text = "A" * (TMUX_BUFFER_CHUNK_BYTES - 2) + "😀" + "B" * 32

        chunks = _split_for_tmux_buffer(text)

        assert "".join(chunks) == text
        assert all(len(chunk.encode()) <= TMUX_BUFFER_CHUNK_BYTES for chunk in chunks)
        # The emoji moved wholly into the second chunk rather than being torn.
        assert chunks[0] == "A" * (TMUX_BUFFER_CHUNK_BYTES - 2)
        assert chunks[1].startswith("😀")

    @pytest.mark.asyncio
    async def test_large_payload_is_appended_in_chunks(
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
            "gobby.agents.tmux.text_injection.spawn.create_subprocess_exec",
            fake_exec,
        )

        text = "A" * (TMUX_BUFFER_CHUNK_BYTES * 2 + 5)
        await paste_literal_text_to_tmux_target("%12", text)

        writes = [command for command in commands if command[1] == "set-buffer"]
        assert len(writes) == 3
        buffer_name = writes[0][3]
        assert writes[0] == ["tmux", "set-buffer", "-b", buffer_name, "--", writes[0][5]]
        for append in writes[1:]:
            assert append[:5] == ["tmux", "set-buffer", "-a", "-b", buffer_name]
            assert append[5] == "--"
        assert "".join(write[-1] for write in writes) == text
        assert all(len(write[-1].encode()) <= TMUX_BUFFER_CHUNK_BYTES for write in writes)
        # The buffer is still pasted once and cleaned up once.
        assert [command[1] for command in commands[3:]] == ["paste-buffer", "delete-buffer"]

    @pytest.mark.asyncio
    async def test_failed_append_still_deletes_the_buffer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commands: list[list[str]] = []

        async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
            commands.append(list(args))
            proc = MagicMock()
            # Fail the first append (the second set-buffer call).
            failed = args[1] == "set-buffer" and "-a" in args
            proc.returncode = 1 if failed else 0
            proc.communicate = AsyncMock(
                return_value=(b"", b"no server running" if failed else b"")
            )
            return proc

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.spawn.create_subprocess_exec",
            fake_exec,
        )

        with pytest.raises(TmuxTargetUnavailableError):
            await paste_literal_text_to_tmux_target("%12", "A" * (TMUX_BUFFER_CHUNK_BYTES * 2))

        assert [command[1] for command in commands] == [
            "set-buffer",
            "set-buffer",
            "delete-buffer",
        ]


# =============================================================================
# TmuxSessionManager
# =============================================================================


class TestTmuxSessionManager:
    """Tests for TmuxSessionManager."""

    def test_base_args_address_the_recorded_socket(self) -> None:
        mgr = TmuxSessionManager("/tmp/tmux-1000/gobby")
        assert mgr._base_args() == ["tmux", "-S", "/tmp/tmux-1000/gobby"]

    @pytest.mark.asyncio
    async def test_run_timeout_raises_timeout_error_and_logs_the_command(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A timed-out tmux command must surface TimeoutError and name itself.

        subprocess.run kills and reaps the child itself on timeout, so there is
        no hand-rolled kill to assert; what callers depend on is the exception
        type, and what diagnosis depends on is the command and timeout in the
        log.
        """
        mgr = TmuxSessionManager(_SOCKET)

        with patch(
            "gobby.agents.tmux.session_activation.spawn.run",
            side_effect=subprocess.TimeoutExpired(cmd=["tmux"], timeout=0.01),
        ):
            with (
                caplog.at_level(logging.DEBUG, logger="gobby.agents.tmux.session_activation"),
                pytest.raises(TimeoutError),
            ):
                await mgr._run("list-sessions", timeout=0.01)

        assert "list-sessions" in caplog.text
        assert "timeout=0.01s" in caplog.text

    @pytest.mark.asyncio
    async def test_has_session(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            assert await mgr.has_session("test") is True

            mock_run.return_value = (1, "", "")
            assert await mgr.has_session("missing") is False

    @pytest.mark.asyncio
    async def test_kill_session(self, caplog: pytest.LogCaptureFixture) -> None:
        mgr = TmuxSessionManager(_SOCKET)
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
        mgr = TmuxSessionManager(_SOCKET)
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
    async def test_rename_window_escapes_tmux_format_markers(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            assert await mgr.rename_window("%42", "#99 Fix #title") is True

        args = mock_run.call_args.args
        assert args[args.index("rename-window") + 3] == "##99 Fix ##title"
        assert args[args.index("select-pane") + 4] == "##99 Fix ##title"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        [
            "can't find pane: %53",
            "can't find window: %53",
            "no such window: %53",
        ],
    )
    async def test_rename_window_missing_target_logs_debug(
        self,
        caplog: pytest.LogCaptureFixture,
        message: str,
    ) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", message)

            with caplog.at_level("DEBUG", logger="gobby.agents.tmux.session_manager"):
                assert await mgr.rename_window("%53", "Title") is False

        assert "Skipping tmux window rename for missing target '%53'" in caplog.text
        assert not [record for record in caplog.records if record.levelname == "WARNING"]

    @pytest.mark.asyncio
    async def test_rename_window_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "ambiguous target")

            with caplog.at_level("WARNING", logger="gobby.agents.tmux.session_manager"):
                assert await mgr.rename_window("%99", "Title") is False

        assert "Failed to rename tmux window for '%99': ambiguous target" in caplog.text

    @pytest.mark.asyncio
    async def test_send_keys(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch(
            "gobby.agents.tmux.session_manager.send_literal_text_to_tmux_target",
            new_callable=AsyncMock,
        ) as mock_send:
            assert await mgr.send_keys("test", "hello") is True
            mock_send.assert_awaited_once_with(
                "=test:",
                "hello",
                tmux_cmd=["tmux", "-S", _SOCKET],
            )

    @pytest.mark.asyncio
    async def test_send_keys_preserves_pane_target_for_literal_text(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch(
            "gobby.agents.tmux.session_manager.send_literal_text_to_tmux_target",
            new_callable=AsyncMock,
        ) as mock_send:
            assert await mgr.send_keys("%12", "hello") is True
            mock_send.assert_awaited_once_with(
                "%12",
                "hello",
                tmux_cmd=["tmux", "-S", _SOCKET],
            )


# =============================================================================
# DaemonConfig integration
# =============================================================================


class TestDaemonConfigTmux:
    """TmuxConfig is properly wired into DaemonConfig."""

    def test_default_tmux_config(self) -> None:
        from gobby.config.app import DaemonConfig

        config = DaemonConfig()
        assert config.tmux.attach_history_lines == 500

    def test_custom_tmux_config(self) -> None:
        from gobby.config.app import DaemonConfig

        config = DaemonConfig(tmux={"attach_history_lines": 700})
        assert config.tmux.attach_history_lines == 700


# =============================================================================
# TmuxSessionManager additional coverage
# =============================================================================


class TestTmuxSessionManagerExtended:
    """Additional tests for TmuxSessionManager uncovered methods."""

    @pytest.mark.asyncio
    async def test_capture_pane_success(self) -> None:
        """capture_pane returns captured output."""
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "line 1\nline 2\n", "")
            result = await mgr.capture_pane("my-session", lines=2)
        assert result == "line 1\nline 2\n"
        mock_run.assert_awaited_once_with(
            "capture-pane",
            "-t",
            "=my-session:",
            "-p",
            "-J",
            "-S-2",
        )

    @pytest.mark.asyncio
    async def test_capture_pane_ansi_mode_keeps_escape_sequences(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "\x1b[2mfaint\x1b[0m\n", "")
            result = await mgr.capture_pane("my-session", lines=1, mode="ansi")
        assert result == "\x1b[2mfaint\x1b[0m\n"
        mock_run.assert_awaited_once_with(
            "capture-pane",
            "-t",
            "=my-session:",
            "-p",
            "-e",
            "-J",
            "-S-1",
        )

    @pytest.mark.asyncio
    async def test_capture_full_pane_uses_complete_history(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "full history\n", "")

            result = await mgr.capture_full_pane("my-session")

        assert result == "full history\n"
        mock_run.assert_awaited_once_with(
            "capture-pane",
            "-t",
            "=my-session:",
            "-p",
            "-S",
            "-",
        )

    @pytest.mark.asyncio
    async def test_capture_pane_preserves_pane_target(self) -> None:
        """capture_pane targets raw tmux pane IDs directly."""
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "line 1\nline 2\n", "")
            result = await mgr.capture_pane("%12", lines=2)
        assert result == "line 1\nline 2\n"
        mock_run.assert_awaited_once_with(
            "capture-pane",
            "-t",
            "%12",
            "-p",
            "-J",
            "-S-2",
        )

    @pytest.mark.asyncio
    async def test_capture_pane_limits_output_to_requested_lines(self) -> None:
        """capture_pane trims tmux history plus visible-screen output to the requested tail."""
        mgr = TmuxSessionManager(_SOCKET)
        pane_output = "".join(f"line {idx}\n" for idx in range(1, 67))
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, pane_output, "")
            result = await mgr.capture_pane("my-session", lines=15)
        assert result == "".join(f"line {idx}\n" for idx in range(52, 67))
        assert len(result.splitlines()) == 15

    @pytest.mark.asyncio
    async def test_capture_pane_failure(self) -> None:
        """capture_pane returns None on failure."""
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no such session")
            result = await mgr.capture_pane("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_send_keys_with_newline(self) -> None:
        """send_keys delegates literal text, including trailing newline, to the helper."""
        mgr = TmuxSessionManager(_SOCKET)
        with patch(
            "gobby.agents.tmux.session_manager.send_literal_text_to_tmux_target",
            new_callable=AsyncMock,
        ) as mock_send:
            result = await mgr.send_keys("test-sess", "hello\n")
        assert result is True
        mock_send.assert_awaited_once_with(
            "=test-sess:",
            "hello\n",
            tmux_cmd=["tmux", "-S", _SOCKET],
        )

    @pytest.mark.asyncio
    async def test_send_keys_without_newline(self) -> None:
        """send_keys without trailing newline still uses paste-buffer helper."""
        mgr = TmuxSessionManager(_SOCKET)
        with patch(
            "gobby.agents.tmux.session_manager.send_literal_text_to_tmux_target",
            new_callable=AsyncMock,
        ) as mock_send:
            result = await mgr.send_keys("test-sess", "hello")
        assert result is True
        mock_send.assert_awaited_once_with(
            "=test-sess:",
            "hello",
            tmux_cmd=["tmux", "-S", _SOCKET],
        )

    @pytest.mark.asyncio
    async def test_send_keys_text_failure(self) -> None:
        """send_keys returns False when literal text injection fails."""
        mgr = TmuxSessionManager(_SOCKET)
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
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            result = await mgr.send_keys("test", "Enter", literal=False)
        assert result is True
        mock_run.assert_awaited_once_with("send-keys", "-t", "=test:", "Enter")

    @pytest.mark.asyncio
    async def test_send_keys_raw_key_mode_preserves_pane_target(self) -> None:
        """Raw key mode targets tmux panes directly when given a pane id."""
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            result = await mgr.send_keys("%12", "C-c", literal=False)
        assert result is True
        mock_run.assert_awaited_once_with("send-keys", "-t", "%12", "C-c")

    @pytest.mark.asyncio
    async def test_kill_session_with_pids(self) -> None:
        """kill_session sends SIGTERM and SIGKILL to pane PIDs."""

        mgr = TmuxSessionManager(_SOCKET)
        with (
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
            patch("os.killpg") as mock_killpg,
            patch("os.getpgid", return_value=12345),
        ):
            mock_run.side_effect = [
                (0, "12345\n", ""),  # list-panes for PIDs
                (0, "", ""),  # kill-session
            ]
            result = await mgr.kill_session("test", timeout=0)
        assert result is True
        mock_killpg.assert_any_call(12345, signal.SIGTERM)
        mock_killpg.assert_any_call(12345, 0)
        mock_killpg.assert_any_call(12345, signal.SIGKILL)

    @pytest.mark.asyncio
    async def test_kill_session_passes_timeout_to_process_group_wait(self) -> None:
        """kill_session waits for process-group exit using the caller's timeout."""

        mgr = TmuxSessionManager(_SOCKET)
        with (
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
            patch.object(
                mgr, "_wait_for_process_groups_exit", new_callable=AsyncMock, return_value={12345}
            ) as mock_wait,
            patch("os.killpg") as mock_killpg,
            patch("os.getpgid", return_value=12345),
        ):
            mock_run.side_effect = [
                (0, "12345\n", ""),  # list-panes for PIDs
                (0, "", ""),  # kill-session
            ]
            result = await mgr.kill_session("test", timeout=1.75)

        assert result is True
        mock_wait.assert_awaited_once_with({12345}, 1.75)
        assert len(mock_run.await_args_list) == 2
        assert mock_run.await_args_list[0].args == (
            "list-panes",
            "-t",
            "=test:",
            "-F",
            "#{pane_pid}",
        )
        assert mock_run.await_args_list[1].args == ("kill-session", "-t", "=test:")
        mock_killpg.assert_any_call(12345, signal.SIGTERM)
        mock_killpg.assert_any_call(12345, signal.SIGKILL)


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
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, raw + "\n", "")
            result = await mgr.get_window_automatic_rename("%1")
        assert result is expected

    @pytest.mark.asyncio
    async def test_returns_none_on_error_rc(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no server running")
            result = await mgr.get_window_automatic_rename("%1")
        assert result is None


class TestGetWindowName:
    """Tests for TmuxSessionManager.get_window_name."""

    @pytest.mark.asyncio
    async def test_returns_window_name(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "#99: gobby\n", "")
            result = await mgr.get_window_name("%1")

        assert result == "#99: gobby"
        mock_run.assert_called_once_with("display-message", "-t", "%1", "-p", "#{window_name}")

    @pytest.mark.asyncio
    async def test_returns_none_on_error_or_empty(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no server running")
            assert await mgr.get_window_name("%1") is None

            mock_run.return_value = (0, "\n", "")
            assert await mgr.get_window_name("%1") is None


class TestReleaseWindowTitleOwnership:
    @pytest.mark.asyncio
    async def test_unsets_window_overrides_and_clears_pane_title(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")

            result = await mgr.release_window_title_ownership("%1")

        assert result is TmuxReleaseOutcome.RELEASED
        mock_run.assert_awaited_once_with(
            "set-option",
            "-w",
            "-u",
            "-t",
            "%1",
            "automatic-rename",
            ";",
            "set-option",
            "-w",
            "-u",
            "-t",
            "%1",
            "allow-rename",
            ";",
            "select-pane",
            "-t",
            "%1",
            "-T",
            "",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stderr",
        [
            "can't find pane: %1",
            "error connecting to /private/tmp/tmux-501/gobby (No such file or directory)",
        ],
    )
    async def test_missing_target_is_already_released(self, stderr: str) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", stderr)

            result = await mgr.release_window_title_ownership("%1")

        assert result is TmuxReleaseOutcome.ALREADY_RELEASED

    @pytest.mark.asyncio
    async def test_unexpected_release_failure_is_indeterminate(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "permission denied by policy")

            result = await mgr.release_window_title_ownership("%1")

        assert result is TmuxReleaseOutcome.INDETERMINATE


@pytest.mark.asyncio
class TestTmuxTargetProbe:
    @pytest.mark.parametrize(
        ("result", "expected_state", "expected_pane"),
        [
            ((0, "%1\n", ""), TmuxProbeState.LIVE, True),
            (
                (
                    1,
                    "",
                    "error connecting to /private/tmp/tmux-501/gobby (No such file or directory)",
                ),
                TmuxProbeState.SERVER_MISSING,
                None,
            ),
            ((1, "", "can't find pane: %1"), TmuxProbeState.LIVE, False),
            ((1, "", "permission denied"), TmuxProbeState.INDETERMINATE, None),
        ],
    )
    async def test_classifies_probe_result(
        self,
        result: tuple[int, str, str],
        expected_state: TmuxProbeState,
        expected_pane: bool | None,
    ) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = result

            probe = await mgr.probe_target("%1")

        assert probe.state is expected_state
        assert probe.pane_exists is expected_pane

    async def test_timeout_is_indeterminate(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = TimeoutError("tmux timed out")

            probe = await mgr.probe_target("%1")

        assert probe.state is TmuxProbeState.INDETERMINATE
        assert probe.pane_exists is None

    async def test_unexpected_probe_failure_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with (
            caplog.at_level(logging.WARNING),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (1, "", "unexpected tmux failure")

            probe = await mgr.probe_target("%1")

        assert probe.state is TmuxProbeState.INDETERMINATE
        assert "Tmux target probe failed unexpectedly" in caplog.text

    async def test_permission_probe_failure_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        with (
            caplog.at_level(logging.WARNING),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (1, "", "permission denied")

            probe = await mgr.probe_target("%1")

        assert probe.state is TmuxProbeState.INDETERMINATE
        assert not caplog.records


@pytest.mark.asyncio
async def test_tmux_commands_do_not_fork_on_the_event_loop() -> None:
    """Spawning a tmux subprocess must not fork from the loop thread.

    ``asyncio.create_subprocess_exec`` runs ``Popen.__init__`` inline, so the
    fork/exec happens on the event loop. In a daemon holding around a gigabyte
    resident that is expensive, and an in-process stack sampler caught this
    exact chain repeatedly during multi-second stalls: the tmux pane monitor's
    poll loop and the window-name repair loop both reaching
    ``Popen._execute_child`` on the loop thread (#20841).

    Patching ``Popen.__init__`` pins the real fork site rather than either
    spawn API, so this stays honest whichever one ``_run`` uses. Every tmux
    call funnels through ``_run``, so covering it covers list_panes,
    send_keys and the rest.
    """
    manager = TmuxSessionManager(_SOCKET)
    fork_threads: list[int] = []
    real_init = subprocess.Popen.__init__

    def recording_init(self: Any, args: Any, *rest: Any, **kwargs: Any) -> None:
        fork_threads.append(threading.get_ident())
        # Run a trivial command instead of real tmux, keeping the spawn honest
        # without needing a tmux server in the test environment.
        real_init(self, ["true"], *rest, **kwargs)

    loop_thread = threading.get_ident()
    with patch.object(subprocess.Popen, "__init__", recording_init):
        await manager._run("list-sessions")

    assert fork_threads, "the tmux command must actually have spawned a process"
    assert loop_thread not in fork_threads, (
        f"tmux forked on the event loop thread {loop_thread}; the spawn must be offloaded"
    )


async def test_session_manager_injection_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing injection byte sequences stay intact through TmuxTerminalRuntime."""
    from gobby.storage.terminals import Terminal
    from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
    from tests.terminals.fakes import make_memory_terminal

    commands: list[list[str]] = []
    sleep = AsyncMock()

    async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
        commands.append(list(args))
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    monkeypatch.setattr(
        "gobby.agents.tmux.text_injection.spawn.create_subprocess_exec",
        fake_exec,
    )
    monkeypatch.setattr("gobby.agents.tmux.text_injection.asyncio.sleep", sleep)
    monkeypatch.setattr("gobby.terminals.tmux_runtime.asyncio.sleep", sleep)

    terminal: Terminal = make_memory_terminal()
    locator = terminal.locator or {}
    generation = f"{locator['server_pid']}\t{locator['server_start_time']}\n"

    async def same_server(
        _manager: TmuxSessionManager, *_args: str, timeout: float = 10.0
    ) -> tuple[int, str, str]:
        return 0, generation, ""

    monkeypatch.setattr(TmuxSessionManager, "_run", same_server)
    runtime = TmuxTerminalRuntime()
    await runtime.write_text(terminal, "-X message\n", submit=True)

    tmux_cmd = ["tmux", "-S", locator["socket_path"]]
    assert commands
    assert commands[0][:3] == tmux_cmd
    assert "set-buffer" in commands[0]
    assert any(command[-1] == "Enter" for command in commands if "send-keys" in command)
    assert not any("send-keys" in command and "-l" in command for command in commands)


class TestListPanes:
    """``list_panes`` enumerates every pane with the server generation that keys it."""

    @pytest.mark.asyncio
    async def test_parses_every_pane_and_asks_for_the_server_generation(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)
        run_calls: list[tuple[str, ...]] = []
        rows = [
            "/private/tmp/tmux-501/default\t6051\t1787385464\t75\t@76\t(gobby-S#11155): Task"
            "\t%76\t99781\tpane title\t0\t2.1.247\t/Users/josh/Projects/gobby\t1\t",
            "/private/tmp/tmux-501/default\t6051\t1787385464\t0\t@0\t\t%0\t\t\t1\t\t\t0"
            "\t1789700000",
            "/private/tmp/tmux-501/default\t6051\t1787385464\t9\t@9\t\t%9\t\t\t0\t\t",
            "garbage line",
            "",
        ]

        async def fake_run(*tmux_args: str, timeout: float = 0) -> tuple[int, str, str]:
            run_calls.append(tmux_args)
            return (0, "\n".join(rows), "")

        with patch.object(mgr, "_run", side_effect=fake_run):
            panes = await mgr.list_panes()

        assert run_calls[0][:3] == ("list-panes", "-a", "-F")
        assert "#{pid}" in run_calls[0][3] and "#{start_time}" in run_calls[0][3]
        assert "#{session_attached}" in run_calls[0][3]
        assert "#{pane_dead_time}" in run_calls[0][3]
        assert panes is not None
        assert [pane.pane_id for pane in panes] == ["%76", "%0"]
        first, second = panes
        assert (first.socket_path, first.server_pid, first.server_start_time) == (
            "/private/tmp/tmux-501/default",
            6051,
            1787385464,
        )
        assert (first.session_name, first.window_id, first.window_name) == (
            "75",
            "@76",
            "(gobby-S#11155): Task",
        )
        assert (first.pane_pid, first.pane_title, first.pane_dead) == (99781, "pane title", False)
        assert (first.pane_command, first.pane_path) == ("2.1.247", "/Users/josh/Projects/gobby")
        assert second.pane_dead is True
        assert (second.window_name, second.pane_pid, second.pane_title) == (None, None, None)
        assert (second.pane_command, second.pane_path) == (None, None)
        assert (first.session_attached, second.session_attached) == (1, 0)
        # A live pane has no death to date; a dead one carries the timestamp the
        # reaper measures its retention window against.
        assert (first.pane_dead_time, second.pane_dead_time) == (None, 1789700000)

    @pytest.mark.asyncio
    async def test_no_server_is_an_empty_list_and_other_failures_are_none(self) -> None:
        mgr = TmuxSessionManager(_SOCKET)

        async def no_server(*tmux_args: str, timeout: float = 0) -> tuple[int, str, str]:
            return (1, "", "no server running on /private/tmp/tmux-501/gobby")

        async def broken(*tmux_args: str, timeout: float = 0) -> tuple[int, str, str]:
            return (1, "", "protocol version mismatch (client 8, server 7)")

        with patch.object(mgr, "_run", side_effect=no_server):
            assert await mgr.list_panes() == []
        with patch.object(mgr, "_run", side_effect=broken):
            assert await mgr.list_panes() is None

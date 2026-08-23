"""Unit tests for the gobby.agents.tmux module.

Tests session manager, output reader, config, errors, and singletons.
All tmux subprocess calls are mocked — no real tmux binary required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import gobby.agents.tmux.output_reader as output_reader_mod
from gobby.agents.tmux.errors import TmuxNotFoundError, TmuxSessionError
from gobby.agents.tmux.output_reader import TmuxOutputReader, _safe_fifo_component
from gobby.agents.tmux.pty_bridge import TmuxPTYBridge
from gobby.agents.tmux.session_manager import (
    TmuxProbeState,
    TmuxReleaseOutcome,
    TmuxSessionInfo,
    TmuxSessionManager,
)
from gobby.agents.tmux.spawner import TmuxSpawner
from gobby.agents.tmux.text_injection import (
    TMUX_BUFFER_CHUNK_BYTES,
    TmuxPaneModeUnavailableError,
    TmuxTargetUnavailableError,
    TmuxTextInjectionTimeout,
    _split_for_tmux_buffer,
    classify_tmux_text_injection_error,
    paste_literal_text_to_tmux_target,
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
        sleep = AsyncMock()

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
        monkeypatch.setattr("gobby.agents.tmux.text_injection.asyncio.sleep", sleep)

        tmux_cmd = ["/opt/tmux", "-S", "/tmp/tmux-501/gobby", "-f", "/tmp/tmux.conf"]
        await send_literal_text_to_tmux_target(
            "%12",
            "-X message\n",
            tmux_cmd=tmux_cmd,
        )

        assert len(commands) == 4
        buffer_name = commands[0][7]
        assert commands[0][:5] == tmux_cmd
        assert commands[0][5:] == ["set-buffer", "-b", buffer_name, "--", "-X message"]
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
            "gobby.agents.tmux.text_injection.asyncio.create_subprocess_exec",
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
            "gobby.agents.tmux.text_injection.asyncio.create_subprocess_exec",
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
            "gobby.agents.tmux.text_injection.asyncio.create_subprocess_exec",
            fake_exec,
        )
        monkeypatch.setattr("gobby.agents.tmux.text_injection.asyncio.sleep", sleep)

        await send_literal_text_to_tmux_target("%12", "\n")

        assert commands == [["tmux", "send-keys", "-t", "%12", "Enter"]]
        assert not any(command[1] in {"set-buffer", "paste-buffer"} for command in commands)
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_submit_literal_text_uses_buffer_enter_without_extra_submit(
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
                "-p",
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
    async def test_submit_empty_literal_text_sends_raw_enter(
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

        await submit_literal_text_to_tmux_target("%12", "\n", enter_delay_seconds=0)

        assert commands == [["tmux", "send-keys", "-t", "%12", "Enter"]]

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
                "-p",
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
            "gobby.agents.tmux.text_injection.asyncio.create_subprocess_exec",
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
            "gobby.agents.tmux.text_injection.asyncio.create_subprocess_exec",
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method_name", "method_args", "tmux_args"),
        [
            (
                "set_option",
                ("demo", "status", "off"),
                ("set-option", "-t", "demo", "status", "off"),
            ),
        ],
    )
    async def test_client_commands_use_base_args(
        self,
        method_name: str,
        method_args: tuple[str, ...],
        tmux_args: tuple[str, ...],
    ) -> None:
        mgr = TmuxSessionManager()
        assert mgr._base_args()[-2:] == ["-f", "/dev/null"]
        proc = MagicMock(returncode=0)
        proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch(
            "gobby.agents.tmux.session_manager.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ) as mock_exec:
            await getattr(mgr, method_name)(*method_args)

        mock_exec.assert_awaited_once_with(
            "tmux",
            "-L",
            "gobby",
            "-f",
            "/dev/null",
            *tmux_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @pytest.mark.asyncio
    async def test_refresh_client_refreshes_each_listed_client_tty(self) -> None:
        mgr = TmuxSessionManager()
        run_calls: list[tuple[str, ...]] = []

        async def fake_run(*tmux_args: str, timeout: float = 0) -> tuple[int, str, str]:
            run_calls.append(tmux_args)
            if tmux_args[0] == "list-clients":
                return (0, "/dev/ttys001\n/dev/ttys002\n\n", "")
            return (0, "", "")

        with patch.object(mgr, "_run", side_effect=fake_run):
            await mgr.refresh_client("demo")

        assert run_calls[0] == ("list-clients", "-t", "demo", "-F", "#{client_tty}")
        assert sorted(run_calls[1:]) == [
            ("refresh-client", "-t", "/dev/ttys001"),
            ("refresh-client", "-t", "/dev/ttys002"),
        ]

    @pytest.mark.asyncio
    async def test_refresh_client_fans_the_per_tty_redraws_out_concurrently(self) -> None:
        # Serial redraws made the worst case scale with the number of attached
        # clients, and that cost is charged to whoever is waiting -- on the
        # attach path, to a user whose next request is queued behind it. Every
        # redraw parks until all three have started, so a serial implementation
        # deadlocks here rather than passing more slowly.
        mgr = TmuxSessionManager()
        all_started = asyncio.Event()
        started: list[str] = []

        async def fake_run(*tmux_args: str, timeout: float = 0) -> tuple[int, str, str]:
            if tmux_args[0] == "list-clients":
                return (0, "/dev/ttys001\n/dev/ttys002\n/dev/ttys003\n", "")
            started.append(tmux_args[2])
            if len(started) == 3:
                all_started.set()
            await all_started.wait()
            return (0, "", "")

        with patch.object(mgr, "_run", side_effect=fake_run):
            await asyncio.wait_for(mgr.refresh_client("demo"), timeout=5)

        assert sorted(started) == ["/dev/ttys001", "/dev/ttys002", "/dev/ttys003"]

    @pytest.mark.asyncio
    async def test_refresh_client_shares_one_deadline_across_the_whole_sweep(self) -> None:
        mgr = TmuxSessionManager()
        timeouts: list[float] = []

        async def fake_run(*tmux_args: str, timeout: float = 0) -> tuple[int, str, str]:
            timeouts.append(timeout)
            if tmux_args[0] == "list-clients":
                return (0, "/dev/ttys001\n/dev/ttys002\n", "")
            return (0, "", "")

        with patch.object(mgr, "_run", side_effect=fake_run):
            await mgr.refresh_client("demo", timeout=0.5)

        assert timeouts[0] == 0.5
        # The redraws get what the lookup left over, once, rather than a fresh
        # 0.5s each -- which is what stopped the sweep scaling with client count.
        assert timeouts[1] == timeouts[2]
        assert 0 < timeouts[1] < 0.5

    @pytest.mark.asyncio
    async def test_refresh_client_reports_one_failing_tty_without_dropping_the_rest(
        self,
    ) -> None:
        mgr = TmuxSessionManager()
        refreshed: list[str] = []

        async def fake_run(*tmux_args: str, timeout: float = 0) -> tuple[int, str, str]:
            if tmux_args[0] == "list-clients":
                return (0, "/dev/ttys001\n/dev/ttys002\n", "")
            tty = tmux_args[2]
            if tty == "/dev/ttys001":
                raise TimeoutError
            refreshed.append(tty)
            return (0, "", "")

        with patch.object(mgr, "_run", side_effect=fake_run):
            await mgr.refresh_client("demo")

        assert refreshed == ["/dev/ttys002"]

    @pytest.mark.asyncio
    async def test_refresh_client_raises_when_list_clients_fails(self) -> None:
        mgr = TmuxSessionManager()

        with (
            patch.object(
                mgr,
                "_run",
                new_callable=AsyncMock,
                return_value=(1, "", "no such session"),
            ),
            pytest.raises(RuntimeError, match="list-clients failed"),
        ):
            await mgr.refresh_client("demo")

    @pytest.mark.asyncio
    async def test_refresh_client_no_attached_clients_is_noop(self) -> None:
        mgr = TmuxSessionManager()

        with patch.object(
            mgr,
            "_run",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ) as mock_run:
            await mgr.refresh_client("demo")

        assert mock_run.await_count == 1
        assert mock_run.await_args == call(
            "list-clients", "-t", "demo", "-F", "#{client_tty}", timeout=5.0
        )

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
            # Format: session_name\tpane_pid\tpane_id\twindow_name\tpane_title
            # \tpane_dead\tpane_current_command\tpane_current_path
            mock_run.return_value = (
                0,
                "session1\t100\t%1\tzsh\t\t0\tclaude\t/Users/dev/proj\n"
                "session2\t200\t%2\tzsh\t\t0\t\t\n",
                "",
            )
            result = await mgr.list_sessions()
            assert len(result) == 2
            assert result[0].name == "session1"
            assert result[0].pane_pid == 100
            assert result[0].pane_id == "%1"
            assert result[0].pane_command == "claude"
            assert result[0].pane_path == "/Users/dev/proj"
            assert result[1].name == "session2"
            assert result[1].pane_id == "%2"
            assert result[1].pane_command is None
            assert result[1].pane_path is None

    @pytest.mark.asyncio
    async def test_get_session_returns_target_pane_metadata(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "session1\t100\t%1\tzsh\tTitle\t0\tvim\t/tmp/work\n", "")
            result = await mgr.get_session("session1")

        assert result is not None
        assert result.name == "session1"
        assert result.pane_pid == 100
        assert result.pane_id == "%1"
        assert result.window_name == "zsh"
        assert result.pane_title == "Title"
        assert result.pane_command == "vim"
        assert result.pane_path == "/tmp/work"
        mock_run.assert_awaited_once_with(
            "list-panes",
            "-t",
            "=session1:",
            "-F",
            "#{session_name}\t#{pane_pid}\t#{pane_id}\t#{window_name}\t#{pane_title}\t#{pane_dead}\t#{pane_current_command}\t#{pane_current_path}",
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
    async def test_rename_window_escapes_tmux_format_markers(self) -> None:
        mgr = TmuxSessionManager()
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
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", message)

            with caplog.at_level("DEBUG", logger="gobby.agents.tmux.session_manager"):
                assert await mgr.rename_window("%53", "Title") is False

        assert "Skipping tmux window rename for missing target '%53'" in caplog.text
        assert not [record for record in caplog.records if record.levelname == "WARNING"]

    @pytest.mark.asyncio
    async def test_rename_window_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "ambiguous target")

            with caplog.at_level("WARNING", logger="gobby.agents.tmux.session_manager"):
                assert await mgr.rename_window("%99", "Title") is False

        assert "Failed to rename tmux window for '%99': ambiguous target" in caplog.text

    @pytest.mark.asyncio
    async def test_send_keys(self) -> None:
        mgr = TmuxSessionManager()
        with patch(
            "gobby.agents.tmux.session_manager.send_literal_text_to_tmux_target",
            new_callable=AsyncMock,
        ) as mock_send:
            assert await mgr.send_keys("test", "hello") is True
            mock_send.assert_awaited_once_with(
                "=test:",
                "hello",
                tmux_cmd=["tmux", "-L", "gobby", "-f", "/dev/null"],
            )

    @pytest.mark.asyncio
    async def test_send_keys_preserves_pane_target_for_literal_text(self) -> None:
        mgr = TmuxSessionManager()
        with patch(
            "gobby.agents.tmux.session_manager.send_literal_text_to_tmux_target",
            new_callable=AsyncMock,
        ) as mock_send:
            assert await mgr.send_keys("%12", "hello") is True
            mock_send.assert_awaited_once_with(
                "%12",
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
    async def test_run_logs_vanished_target_at_debug(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reader = TmuxOutputReader()
        proc = MagicMock(returncode=1)
        proc.communicate = AsyncMock(
            return_value=(b"pane contents must stay private", b"can't find pane: dead-pane")
        )
        caplog.set_level(logging.DEBUG, logger=output_reader_mod.__name__)

        with patch.object(
            asyncio,
            "create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            result = await reader._run("pipe-pane", "-t", "dead-pane")

        assert result.returncode == 1
        assert result.stderr == "can't find pane: dead-pane"
        assert result.timed_out is False
        assert "target vanished" in caplog.text
        assert "target=dead-pane" in caplog.text
        assert "pane contents must stay private" not in caplog.text
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_run_logs_timeout_at_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reader = TmuxOutputReader()
        proc = MagicMock(returncode=-9)
        proc.communicate = AsyncMock(
            side_effect=[
                TimeoutError(),
                (b"pane contents must stay private", b"partial tmux diagnostic"),
            ]
        )
        caplog.set_level(logging.DEBUG, logger=output_reader_mod.__name__)

        with patch.object(
            asyncio,
            "create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            result = await reader._run("pipe-pane", "-t", "slow-pane", timeout=0.01)

        assert result.returncode == -9
        assert result.stderr == "partial tmux diagnostic"
        assert result.timed_out is True
        assert "pipe-pane timed out" in caplog.text
        assert "status=-9" in caplog.text
        assert "pane contents must stay private" not in caplog.text
        proc.kill.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_run_distinguishes_unexpected_stderr_and_bounds_diagnostics(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reader = TmuxOutputReader()
        long_stderr = f"unexpected warning {'x' * 700}"
        proc = MagicMock(returncode=0)
        proc.communicate = AsyncMock(
            return_value=(b"pane contents must stay private", long_stderr.encode())
        )
        caplog.set_level(logging.DEBUG, logger=output_reader_mod.__name__)

        with patch.object(
            asyncio,
            "create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            result = await reader._run("pipe-pane", "-t", "live-pane")

        assert result.returncode == 0
        assert result.stderr == long_stderr
        assert result.timed_out is False
        assert "returned unexpected stderr" in caplog.text
        assert "pane contents must stay private" not in caplog.text
        assert "x" * 600 not in caplog.text
        assert "…" in caplog.text

    @pytest.mark.asyncio
    async def test_run_logs_actual_tmux_subcommand(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reader = TmuxOutputReader()
        proc = MagicMock(returncode=1)
        proc.communicate = AsyncMock(return_value=(b"", b"unexpected failure"))
        caplog.set_level(logging.WARNING, logger=output_reader_mod.__name__)

        with patch.object(
            asyncio,
            "create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            await reader._run("capture-pane", "-t", "pane")

        assert "tmux capture-pane failed" in caplog.text
        assert "tmux pipe-pane failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_run_bounds_post_kill_process_drain(self) -> None:
        reader = TmuxOutputReader()
        proc = MagicMock(returncode=None)
        proc.communicate = AsyncMock(side_effect=[TimeoutError, TimeoutError])

        with patch.object(
            asyncio,
            "create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            result = await reader._run("capture-pane", "-t", "pane", timeout=0.01)

        assert result.timed_out is True
        assert result.stderr == ""
        proc.kill.assert_called_once_with()
        assert proc.communicate.await_count == 2

    @pytest.mark.asyncio
    async def test_stop_reader_not_running(self) -> None:
        reader = TmuxOutputReader()
        assert await reader.stop_reader("nonexistent") is False

    @pytest.mark.asyncio
    async def test_stop_all_empty(self) -> None:
        reader = TmuxOutputReader()
        await reader.stop_all()

        assert reader._reader_tasks == {}

    @pytest.mark.asyncio
    async def test_read_loop_decodes_split_multibyte_utf8(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = TmuxOutputReader()
        stop_event = asyncio.Event()
        chunks: list[str] = []
        encoded = "\u2500".encode()
        reads = [encoded[:2], encoded[2:]]

        def fake_read(fd: int, size: int) -> bytes:
            assert fd == 123
            assert size == 4096
            if reads:
                return reads.pop(0)
            stop_event.set()
            return b""

        monkeypatch.setattr(output_reader_mod.os, "open", lambda path, flags: 123)
        monkeypatch.setattr(output_reader_mod.os, "read", fake_read)
        monkeypatch.setattr(output_reader_mod.os, "close", lambda fd: None)
        monkeypatch.setattr(
            output_reader_mod.select, "select", lambda r, w, e, timeout: (r, [], [])
        )

        async def callback(run_id: str, text: str) -> None:
            assert run_id == "run-1"
            chunks.append(text)
            stop_event.set()

        reader.set_output_callback(callback)

        await asyncio.wait_for(
            reader._read_loop("run-1", "ignored.pipe", stop_event),
            timeout=1.0,
        )

        assert chunks == ["\u2500"]
        assert reads == []


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

        original_config = mod._configured_tmux_config
        original_session_manager = mod._session_manager
        original_output_reader = mod._output_reader
        mod._configured_tmux_config = None
        mod._session_manager = None
        mod._output_reader = None
        try:
            yield
        finally:
            mod._configured_tmux_config = original_config
            mod._session_manager = original_session_manager
            mod._output_reader = original_output_reader

    def test_get_tmux_session_manager_returns_same(self) -> None:
        import gobby.agents.tmux as mod

        mod.configure_tmux(TmuxConfig())
        mgr1 = mod.get_tmux_session_manager()
        mgr2 = mod.get_tmux_session_manager()
        assert mgr1 is mgr2

    def test_get_tmux_output_reader_returns_same(self) -> None:
        import gobby.agents.tmux as mod

        mod.configure_tmux(TmuxConfig())
        r1 = mod.get_tmux_output_reader()
        r2 = mod.get_tmux_output_reader()
        assert r1 is r2

    def test_reset_output_callback_does_not_construct_reader(self) -> None:
        import gobby.agents.tmux as mod

        mod.reset_tmux_output_callback()

        assert mod._output_reader is None

    def test_reset_output_callback_clears_existing_reader(self) -> None:
        import gobby.agents.tmux as mod

        async def callback(_run_id: str, _text: str) -> None:
            return None

        mod.configure_tmux(TmuxConfig())
        reader = mod.get_tmux_output_reader()
        reader.set_output_callback(callback)

        mod.reset_tmux_output_callback()

        assert reader._output_callback is None


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
        assert cmd == [
            "tmux",
            "-L",
            "gobby",
            "-T",
            "256,RGB",
            "attach-session",
            "-t",
            "my-session",
        ]

    def test_build_attach_cmd_default_server(self) -> None:
        bridge = TmuxPTYBridge()
        config = TmuxConfig(socket_name="")
        cmd = bridge._build_attach_cmd("my-session", config)
        assert cmd == ["tmux", "-T", "256,RGB", "attach-session", "-t", "my-session"]

    def test_build_attach_cmd_socket_path(self) -> None:
        bridge = TmuxPTYBridge()
        config = TmuxConfig(socket_name="", socket_path="/tmp/tmux-1000/gobby")
        cmd = bridge._build_attach_cmd("my-session", config)
        assert cmd == [
            "tmux",
            "-S",
            "/tmp/tmux-1000/gobby",
            "-T",
            "256,RGB",
            "attach-session",
            "-t",
            "my-session",
        ]

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
        bridge._bridges["21000000-0000-4000-8000-00000000001c"] = BridgeInfo(
            master_fd=999, proc=mock_proc, session_name="sess", socket_name="gobby"
        )

        with pytest.raises(RuntimeError, match="already exists"):
            await bridge.attach("sess", "21000000-0000-4000-8000-00000000001c")

    @pytest.mark.asyncio
    async def test_attach_duplicate_pending_raises(self) -> None:
        bridge = TmuxPTYBridge()
        bridge._pending_bridges.add("21000000-0000-4000-8000-00000000001c")

        with pytest.raises(RuntimeError, match="already exists"):
            await bridge.attach("sess", "21000000-0000-4000-8000-00000000001c")

    @pytest.mark.asyncio
    async def test_resize_missing_is_noop(self) -> None:
        bridge = TmuxPTYBridge()
        assert await bridge.resize("nonexistent", 50, 200) is None

    @pytest.mark.asyncio
    async def test_resize_signals_sigwinch_to_attach_process(self) -> None:
        """TIOCSWINSZ alone never reaches the tmux client: the attach process
        has no controlling-terminal tie to the PTY, so resize must deliver
        SIGWINCH explicitly or the client keeps its old size forever."""
        from gobby.agents.tmux.pty_bridge import BridgeInfo

        bridge = TmuxPTYBridge()
        master_fd, slave_fd = os.openpty()
        mock_proc = MagicMock()
        bridge._bridges["winch-id"] = BridgeInfo(
            master_fd=master_fd, proc=mock_proc, session_name="sess", socket_name=""
        )

        try:
            result = await bridge.resize("winch-id", 21, 111)
        finally:
            os.close(master_fd)
            os.close(slave_fd)

        assert result is not None
        mock_proc.send_signal.assert_called_once_with(signal.SIGWINCH)

    @pytest.mark.asyncio
    async def test_resize_to_the_size_it_already_has_is_not_a_resize(self) -> None:
        """The web client resizes again right after activation.

        Repainting for a resize that changed nothing lands after the attach's
        history capture, and the history boundary is only correct for the
        screen the capture's own repaint painted -- so the redundant redraw
        costs the seam whatever scrolled in between.
        """
        from gobby.agents.tmux.pty_bridge import BridgeInfo

        bridge = TmuxPTYBridge()
        master_fd, slave_fd = os.openpty()
        mock_proc = MagicMock()
        bridge._bridges["same-id"] = BridgeInfo(
            master_fd=master_fd,
            proc=mock_proc,
            session_name="sess",
            socket_name="",
            rows=39,
            cols=80,
        )

        try:
            assert await bridge.resize("same-id", 39, 80) is None
            # A genuine change still resizes, and is then itself remembered.
            assert await bridge.resize("same-id", 20, 80) is not None
            assert await bridge.resize("same-id", 20, 80) is None
        finally:
            os.close(master_fd)
            os.close(slave_fd)

        assert mock_proc.send_signal.call_count == 1
        assert (bridge._bridges["same-id"].rows, bridge._bridges["same-id"].cols) == (20, 80)

    @pytest.mark.asyncio
    async def test_attach_forces_xterm_term_in_subprocess_env(self) -> None:
        """attach must override TERM: the daemon env has none (or an unusable
        one), and tmux attach-session exits immediately without a usable TERM,
        leaving a dead PTY that only surfaces as EIO on later input writes."""
        bridge = TmuxPTYBridge()
        mock_proc = MagicMock()
        with (
            patch(
                "gobby.agents.tmux.pty_bridge.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=mock_proc,
            ) as mock_exec,
            patch.dict(os.environ, {"TERM": "dumb"}),
        ):
            master_fd = await bridge.attach("sess", "term-env-id", TmuxConfig(socket_name="gobby"))

        assert mock_exec.await_args is not None
        env = mock_exec.await_args.kwargs["env"]
        assert env["TERM"] == "xterm-256color"
        assert await bridge.get_master_fd("term-env-id") == master_fd
        os.close(master_fd)


# =============================================================================
# TmuxSpawner
# =============================================================================


class TestTmuxSpawner:
    """Tests for TmuxSpawner._async_spawn environment handling."""

    @pytest.mark.asyncio
    async def test_virtual_env_cleared_in_extra_env(self) -> None:
        """VIRTUAL_ENV and VIRTUAL_ENV_PROMPT are set to empty via -e flags."""
        spawner = TmuxSpawner(TmuxConfig())
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
        spawner = TmuxSpawner(TmuxConfig())
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
        spawner = TmuxSpawner(TmuxConfig())
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
        spawner = TmuxSpawner(TmuxConfig())
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
        spawner = TmuxSpawner(TmuxConfig())
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
        spawner = TmuxSpawner(TmuxConfig())
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
        spawner = TmuxSpawner(TmuxConfig())
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
        spawner = TmuxSpawner(TmuxConfig())
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
            patch.object(
                spawner._session_manager, "kill_session", new_callable=AsyncMock
            ) as mock_kill,
            patch.object(
                spawner._session_manager, "capture_pane", new_callable=AsyncMock
            ) as mock_capture,
        ):
            events: list[str] = []

            def capture_failure(*_args: object, **_kwargs: object) -> str:
                events.append("capture")
                return "/bin/bash: claude: command not found\n"

            def record_cleanup(*_args: object, **_kwargs: object) -> None:
                events.append("cleanup")

            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=456)
            mock_get.return_value = TmuxSessionInfo(
                name="test-session",
                pane_pid=456,
                pane_dead=True,
            )
            mock_capture.side_effect = capture_failure
            mock_kill.side_effect = record_cleanup
            result = await spawner._async_spawn(command=["echo", "test"], cwd="/tmp")

        assert result.success is False
        assert result.error is not None
        assert result.error.startswith("tmux session 'test-session' pane is dead")
        assert "/bin/bash: claude: command not found" in result.error
        assert events == ["capture", "cleanup"]
        mock_capture.assert_awaited_once_with("test-session", lines=50)
        mock_kill.assert_awaited_once_with("test-session", missing_ok=True)

    @pytest.mark.asyncio
    async def test_spawn_fails_when_verified_pane_pid_is_missing(self) -> None:
        """A tmux session without pane_pid fails live-pane verification."""
        spawner = TmuxSpawner(TmuxConfig())
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
            patch.object(
                spawner._session_manager, "kill_session", new_callable=AsyncMock
            ) as mock_kill,
            patch.object(
                spawner._session_manager, "capture_pane", new_callable=AsyncMock
            ) as mock_capture,
            patch("gobby.agents.tmux.spawner.time.monotonic", side_effect=[0.0, 2.1]),
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=None)
            mock_get.return_value = TmuxSessionInfo(name="test-session", pane_pid=None)
            mock_capture.return_value = "/bin/bash: claude: command not found\n"
            result = await spawner._async_spawn(command=["echo", "test"], cwd="/tmp")

        assert result.success is False
        assert result.error is not None
        assert result.error.startswith("tmux session 'test-session' has no pane PID")
        assert "/bin/bash: claude: command not found" in result.error
        mock_capture.assert_awaited_once_with("test-session", lines=50)
        mock_kill.assert_awaited_once_with("test-session", missing_ok=True)

    @pytest.mark.asyncio
    async def test_spawn_keeps_generic_error_when_dead_pane_capture_fails(self) -> None:
        spawner = TmuxSpawner(TmuxConfig())
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
            patch.object(
                spawner._session_manager, "capture_pane", new_callable=AsyncMock
            ) as mock_capture,
            patch.object(spawner._session_manager, "kill_session", new_callable=AsyncMock),
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=456)
            mock_get.return_value = TmuxSessionInfo(
                name="test-session",
                pane_pid=456,
                pane_dead=True,
            )
            mock_capture.side_effect = TmuxSessionError("capture failed")
            result = await spawner._async_spawn(command=["echo", "test"], cwd="/tmp")

        assert result.success is False
        assert result.error == "tmux session 'test-session' pane is dead"
        assert result.message == "tmux session 'test-session' failed live-pane verification"
        assert "Pane output:" not in result.error

    @pytest.mark.asyncio
    async def test_spawn_kills_session_when_live_pane_verification_raises(self) -> None:
        """Verification exceptions clean up the tmux session that was just created."""
        spawner = TmuxSpawner(TmuxConfig())
        with (
            patch.object(
                spawner._session_manager, "create_session", new_callable=AsyncMock
            ) as mock_create,
            patch.object(
                spawner._session_manager, "get_session", new_callable=AsyncMock
            ) as mock_get,
            patch.object(
                spawner._session_manager, "kill_session", new_callable=AsyncMock
            ) as mock_kill,
        ):
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=456)
            mock_get.side_effect = RuntimeError("tmux exploded")
            result = await spawner._async_spawn(command=["echo", "test"], cwd="/tmp")

        assert result.success is False
        assert result.error == "tmux session verification failed: tmux exploded"
        mock_kill.assert_awaited_once_with("test-session", missing_ok=True)

    @pytest.mark.asyncio
    async def test_spawn_failure_returns_error(self) -> None:
        """Failed spawn returns SpawnResult with success=False."""
        spawner = TmuxSpawner(TmuxConfig())
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
        spawner = TmuxSpawner(TmuxConfig())
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
                command=[
                    "/managed/node",
                    "/managed/runner.mjs",
                    "--",
                    "/opt/claude/versions/2.1.220",
                    "--dangerously-skip-permissions",
                ],
                cwd="/tmp",
                env={"GOBBY_SESSION_ID": "sess-1"},
                auth_cli="claude",
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
        spawner = TmuxSpawner(TmuxConfig())
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
        spawner = TmuxSpawner(TmuxConfig())
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
    async def test_health_check_missing_socket_is_ok(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """health_check returns True for tmux's missing socket wording."""
        mgr = TmuxSessionManager()
        stderr = "error connecting to /private/tmp/tmux-501/gobby (No such file or directory)"
        with (
            caplog.at_level(logging.WARNING, logger="gobby.agents.tmux.session_manager"),
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (1, "", stderr)
            result = await mgr.health_check()
        assert result is True
        assert "tmux health check returned" not in caplog.text

    @pytest.mark.asyncio
    async def test_health_check_connection_error_with_other_reason_warns(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """health_check still warns for non-missing-socket connection errors."""
        mgr = TmuxSessionManager()
        stderr = "error connecting to /private/tmp/tmux-501/gobby (Permission denied)"
        with (
            caplog.at_level(logging.WARNING, logger="gobby.agents.tmux.session_manager"),
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (1, "", stderr)
            result = await mgr.health_check()
        assert result is False
        assert "tmux health check returned rc=1" in caplog.text
        assert stderr in caplog.text
        mock_run.assert_awaited_once_with("list-sessions", timeout=5.0)

    @pytest.mark.asyncio
    async def test_health_check_unavailable(self) -> None:
        """health_check returns False when tmux is not available."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "is_available", return_value=False):
            result = await mgr.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_single_timeout_does_not_kill_server(self) -> None:
        """health_check defers kill-server for an isolated timeout."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [TimeoutError("socket stuck")]
            result = await mgr.health_check()
        assert result is False
        mock_run.assert_awaited_once_with("list-sessions", timeout=5.0)

    @pytest.mark.asyncio
    async def test_health_check_kills_server_after_consecutive_timeouts(self) -> None:
        """health_check kills stale server after repeated timeout evidence."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [
                TimeoutError("socket stuck"),
                TimeoutError("socket stuck"),
                TimeoutError("socket stuck"),
                (0, "", ""),
            ]
            results = [await mgr.health_check() for _ in range(3)]
        assert results == [False, False, True]
        assert mock_run.await_args_list[0].args == ("list-sessions",)
        assert mock_run.await_args_list[0].kwargs == {"timeout": 5.0}
        assert mock_run.await_args_list[1].args == ("list-sessions",)
        assert mock_run.await_args_list[1].kwargs == {"timeout": 5.0}
        assert mock_run.await_args_list[2].args == ("list-sessions",)
        assert mock_run.await_args_list[2].kwargs == {"timeout": 5.0}
        assert mock_run.await_args_list[3].args == ("kill-server",)
        assert mock_run.await_args_list[3].kwargs == {"timeout": 5.0}

    @pytest.mark.asyncio
    async def test_health_check_generic_error_resets_timeout_count(self) -> None:
        """health_check requires timeouts to be consecutive before kill-server."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [
                TimeoutError("socket stuck"),
                RuntimeError("transient"),
                TimeoutError("socket stuck"),
                TimeoutError("socket stuck"),
            ]
            results = [await mgr.health_check() for _ in range(4)]
        assert results == [False, False, False, False]
        assert mock_run.await_args_list[-1].args == ("list-sessions",)

    @pytest.mark.asyncio
    async def test_health_check_timeout_kill_fails(self) -> None:
        """health_check returns False when kill-server also fails after repeated timeouts."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [
                TimeoutError("socket stuck"),
                TimeoutError("socket stuck"),
                TimeoutError("socket stuck"),
                RuntimeError("kill failed too"),
            ]
            results = [await mgr.health_check() for _ in range(3)]
        assert results == [False, False, False]
        assert mock_run.await_args_list[-1].args == ("kill-server",)

    @pytest.mark.asyncio
    async def test_health_check_generic_error_does_not_kill_server(self) -> None:
        """health_check does not use arbitrary exceptions as stale-server evidence."""
        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [RuntimeError("unexpected")]
            result = await mgr.health_check()
        assert result is False
        mock_run.assert_awaited_once_with("list-sessions", timeout=5.0)

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
    async def test_create_session_routes_credentials_through_private_env_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Credential env vars are kept out of tmux new-session argv."""
        env_file = tmp_path / "agent-env.sh"

        def fake_mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
            assert prefix == "gobby-agent-env-"
            assert suffix == ".sh"
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            return fd, str(env_file)

        monkeypatch.setattr(
            "gobby.agents.tmux.session_manager.tempfile.mkstemp",
            fake_mkstemp,
        )

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
            await mgr.create_session(
                name="test",
                command=["claude", "--session-id=abc"],
                cwd="/tmp",
                env={
                    "GOBBY_SESSION_ID": "session-123",
                    "GOBBY_AGENT_API_TOKEN": "scoped-agent-token",
                    "ANTHROPIC_AUTH_TOKEN": "anthropic secret",
                    "QWEN_API_KEY": "qwen-secret",
                    "XAI_API_KEY": "xai-secret",
                    "FACTORY_API_KEY": "factory-secret",
                    "GOBBY_CODEX_ENDPOINT_API_KEY": "codex-endpoint-secret",
                },
            )

        new_session_args = mock_run.await_args_list[1].args
        argv_text = "\0".join(str(arg) for arg in new_session_args)
        assert "GOBBY_SESSION_ID=session-123" in argv_text
        assert "GOBBY_AGENT_API_TOKEN" not in argv_text
        assert "scoped-agent-token" not in argv_text
        assert "ANTHROPIC_AUTH_TOKEN" not in argv_text
        assert "anthropic secret" not in argv_text
        assert "QWEN_API_KEY" not in argv_text
        assert "qwen-secret" not in argv_text
        assert "XAI_API_KEY" not in argv_text
        assert "xai-secret" not in argv_text
        assert "FACTORY_API_KEY" not in argv_text
        assert "factory-secret" not in argv_text
        assert "GOBBY_CODEX_ENDPOINT_API_KEY" not in argv_text
        assert "codex-endpoint-secret" not in argv_text

        command_arg = next(arg for arg in new_session_args if "__gobby_env_file=" in str(arg))
        assert str(env_file) in command_arg
        assert '. "$__gobby_env_file"' in command_arg
        assert 'rm -f "$__gobby_env_file"' in command_arg

        assert env_file.stat().st_mode & 0o777 == 0o600
        env_file_text = env_file.read_text(encoding="utf-8")
        assert "GOBBY_AGENT_API_TOKEN=scoped-agent-token\n" in env_file_text
        assert "ANTHROPIC_AUTH_TOKEN='anthropic secret'\n" in env_file_text
        assert "QWEN_API_KEY=qwen-secret\n" in env_file_text
        assert "XAI_API_KEY=xai-secret\n" in env_file_text
        assert "FACTORY_API_KEY=factory-secret\n" in env_file_text
        assert "GOBBY_CODEX_ENDPOINT_API_KEY=codex-endpoint-secret\n" in env_file_text

    @pytest.mark.parametrize("prompt", ["finish this;", "continue with this\\"])
    @pytest.mark.asyncio
    async def test_create_session_routes_tmux_unsafe_prompt_through_private_env_file(
        self,
        prompt: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GOBBY_PROMPT values unsafe for tmux -e are kept out of new-session argv."""
        env_file = tmp_path / "agent-env.sh"

        def fake_mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
            assert prefix == "gobby-agent-env-"
            assert suffix == ".sh"
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            return fd, str(env_file)

        monkeypatch.setattr(
            "gobby.agents.tmux.session_manager.tempfile.mkstemp",
            fake_mkstemp,
        )

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
            await mgr.create_session(
                name="test",
                command=["claude", "--session-id=abc"],
                cwd="/tmp",
                env={
                    "GOBBY_PROMPT": prompt,
                    "GOBBY_SESSION_ID": "session-123",
                },
            )

        new_session_args = mock_run.await_args_list[1].args
        argv_text = "\0".join(str(arg) for arg in new_session_args)
        assert f"GOBBY_PROMPT={prompt}" not in argv_text
        assert "GOBBY_SESSION_ID=session-123" in argv_text

        command_arg = next(arg for arg in new_session_args if "__gobby_env_file=" in str(arg))
        assert str(env_file) in command_arg

        env_file_text = env_file.read_text(encoding="utf-8")
        assert f"GOBBY_PROMPT={shlex.quote(prompt)}\n" in env_file_text

    @pytest.mark.asyncio
    async def test_create_session_removes_private_env_file_when_tmux_create_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Parent process removes the credential file if tmux rejects launch."""
        env_file = tmp_path / "agent-env.sh"

        def fake_mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            return fd, str(env_file)

        monkeypatch.setattr(
            "gobby.agents.tmux.session_manager.tempfile.mkstemp",
            fake_mkstemp,
        )

        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "is_available", return_value=True),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
            pytest.raises(TmuxSessionError, match="launch failed"),
        ):
            mock_run.side_effect = [
                (1, "", ""),  # has_session
                (1, "", "launch failed"),  # new-session
            ]
            await mgr.create_session(
                name="test",
                command="claude",
                cwd="/tmp",
                env={"ANTHROPIC_AUTH_TOKEN": "anthropic secret"},
            )

        assert not env_file.exists()

    @pytest.mark.asyncio
    async def test_capture_pane_success(self) -> None:
        """capture_pane returns captured output."""
        mgr = TmuxSessionManager()
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
    async def test_capture_full_pane_uses_complete_history(self) -> None:
        mgr = TmuxSessionManager()
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
        mgr = TmuxSessionManager()
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
        mgr = TmuxSessionManager()
        pane_output = "".join(f"line {idx}\n" for idx in range(1, 67))
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, pane_output, "")
            result = await mgr.capture_pane("my-session", lines=15)
        assert result == "".join(f"line {idx}\n" for idx in range(52, 67))
        assert len(result.splitlines()) == 15

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
            "=test-sess:",
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
            "=test-sess:",
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
            result = await mgr.send_keys("test", "Enter", literal=False)
        assert result is True
        mock_run.assert_awaited_once_with("send-keys", "-t", "=test:", "Enter")

    @pytest.mark.asyncio
    async def test_send_keys_raw_key_mode_preserves_pane_target(self) -> None:
        """Raw key mode targets tmux panes directly when given a pane id."""
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            result = await mgr.send_keys("%12", "C-c", literal=False)
        assert result is True
        mock_run.assert_awaited_once_with("send-keys", "-t", "%12", "C-c")

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
            result = await mgr.kill_session("test", timeout=0)
        assert result is True
        mock_killpg.assert_any_call(12345, signal.SIGTERM)
        mock_killpg.assert_any_call(12345, 0)
        mock_killpg.assert_any_call(12345, signal.SIGKILL)

    @pytest.mark.asyncio
    async def test_kill_session_passes_timeout_to_process_group_wait(self) -> None:
        """kill_session waits for process-group exit using the caller's timeout."""

        mgr = TmuxSessionManager()
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

    async def test_kill_session_skips_process_groups_for_wsl(self) -> None:
        """kill_session avoids host process-group signals when tmux is running via WSL."""

        mgr = TmuxSessionManager()
        with (
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
            patch("gobby.agents.tmux.session_manager.needs_wsl", return_value=True),
            patch("os.killpg") as mock_killpg,
            patch("os.getpgid") as mock_getpgid,
        ):
            mock_run.side_effect = [
                (0, "12345\n", ""),  # list-panes for PIDs
                (0, "", ""),  # kill-session
            ]
            result = await mgr.kill_session("test")
        assert result is True
        assert len(mock_run.await_args_list) == 2
        assert mock_run.await_args_list[0].args == (
            "list-panes",
            "-t",
            "=test:",
            "-F",
            "#{pane_pid}",
        )
        assert mock_run.await_args_list[1].args == ("kill-session", "-t", "=test:")
        mock_getpgid.assert_not_called()
        mock_killpg.assert_not_called()


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


class TestReleaseWindowTitleOwnership:
    @pytest.mark.asyncio
    async def test_unsets_window_overrides_and_clears_pane_title(self) -> None:
        mgr = TmuxSessionManager()
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
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", stderr)

            result = await mgr.release_window_title_ownership("%1")

        assert result is TmuxReleaseOutcome.ALREADY_RELEASED

    @pytest.mark.asyncio
    async def test_unexpected_release_failure_is_indeterminate(self) -> None:
        mgr = TmuxSessionManager()
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
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = result

            probe = await mgr.probe_target("%1")

        assert probe.state is expected_state
        assert probe.pane_exists is expected_pane

    async def test_timeout_is_indeterminate(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = TimeoutError("tmux timed out")

            probe = await mgr.probe_target("%1")

        assert probe.state is TmuxProbeState.INDETERMINATE
        assert probe.pane_exists is None

    async def test_unexpected_probe_failure_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        mgr = TmuxSessionManager()
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
        mgr = TmuxSessionManager()
        with (
            caplog.at_level(logging.WARNING),
            patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (1, "", "permission denied")

            probe = await mgr.probe_target("%1")

        assert probe.state is TmuxProbeState.INDETERMINATE
        assert not caplog.records

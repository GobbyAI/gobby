"""Tests for bounded tmux scrollback capture."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.tmux.history import (
    HistoryCaptureError,
    bound_history,
    build_capture_args,
    capture_history,
)
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig

pytestmark = pytest.mark.unit

_RESET = "\x1b[0m"


@pytest.fixture
def manager() -> TmuxSessionManager:
    return TmuxSessionManager(TmuxConfig(socket_name="gobby"))


class FakeProcess:
    """Minimal asyncio subprocess stand-in for capture-pane."""

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        *,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        # A hanging child has not exited, so kill_and_reap must act on it.
        self.returncode: int | None = None if hang else returncode
        self._hang = hang
        self.pid = 4242
        self.killed = False
        self.waited = False
        # Set once the fake is actually parked, so tests can cancel at a known
        # point instead of sleeping and hoping.
        self.entered = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.entered.set()
        if self._hang:
            await asyncio.Event().wait()
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode or 0


class TestBuildCaptureArgs:
    def test_requests_the_probe_line_without_joining_wraps(
        self, manager: TmuxSessionManager
    ) -> None:
        args = build_capture_args(manager, "demo", max_lines=2000)

        assert args[:2] == ["tmux", "-L"]
        assert "capture-pane" in args
        # -J joins soft-wrapped lines, the opposite of preserving them.
        assert "-J" not in args
        # -e preserves SGR; -E -1 ends one line above the visible pane.
        assert "-e" in args
        assert args[args.index("-E") + 1] == "-1"
        # One line more than the bound is what makes truncation observable.
        assert "-S-2001" in args
        assert args[args.index("-t") + 1] == "=demo:"

    def test_probe_tracks_the_requested_bound(self, manager: TmuxSessionManager) -> None:
        assert "-S-6" in build_capture_args(manager, "demo", max_lines=5)

    def test_no_repaint_is_appended_without_a_tty(self, manager: TmuxSessionManager) -> None:
        args = build_capture_args(manager, "demo", max_lines=5)

        assert ";" not in args
        assert "refresh-client" not in args

    def test_the_repaint_rides_in_the_same_command_list(self, manager: TmuxSessionManager) -> None:
        """The boundary and the screen have to be decided at the same instant.

        A redraw issued as its own tmux call lands a round trip after the
        capture, and every line that scrolls out of the pane in between is in
        neither the history nor the painted screen.
        """
        args = build_capture_args(manager, "demo", max_lines=5, refresh_tty="/dev/ttys009")

        separator = args.index(";")
        # Everything the capture needs precedes the separator, and the repaint
        # is the whole of what follows it.
        assert "capture-pane" in args[:separator]
        assert args[separator + 1 :] == ["refresh-client", "-t", "/dev/ttys009"]

    @pytest.mark.asyncio
    async def test_capture_history_passes_the_tty_through(
        self, manager: TmuxSessionManager
    ) -> None:
        proc = SimpleNamespace(
            returncode=0,
            communicate=AsyncMock(return_value=(b"one\ntwo\n", b"")),
            kill=MagicMock(),
            wait=AsyncMock(return_value=0),
        )
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as create_proc:
            await capture_history(manager, "demo", refresh_tty="/dev/ttys009")

        assert create_proc.await_args is not None
        argv = list(create_proc.await_args.args)
        assert argv[argv.index(";") + 1 :] == ["refresh-client", "-t", "/dev/ttys009"]

    @pytest.mark.asyncio
    async def test_a_failed_repaint_keeps_the_window_it_captured(
        self, manager: TmuxSessionManager
    ) -> None:
        """A command list reports one status for both of its commands.

        The capture wrote its window to stdout, so the nonzero status belongs
        to the repaint -- discarding a history that did arrive would cost the
        user their scrollback over a screen the stream repaints anyway.
        """
        proc = SimpleNamespace(
            returncode=1,
            communicate=AsyncMock(return_value=(b"one\ntwo\n", b"can't find client: /dev/x")),
            kill=MagicMock(),
            wait=AsyncMock(return_value=1),
        )
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            capture = await capture_history(manager, "demo", refresh_tty="/dev/x")

        assert capture.text == "one\r\ntwo\x1b[0m"
        assert capture.repainted is False

    @pytest.mark.asyncio
    async def test_a_failed_capture_still_raises_when_nothing_was_written(
        self, manager: TmuxSessionManager
    ) -> None:
        # capture-pane writes nothing when it is the command that failed, which
        # is what separates it from a repaint failure.
        proc = SimpleNamespace(
            returncode=1,
            communicate=AsyncMock(return_value=(b"", b"can't find pane")),
            kill=MagicMock(),
            wait=AsyncMock(return_value=1),
        )
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(HistoryCaptureError) as raised:
                await capture_history(manager, "demo", refresh_tty="/dev/x")

        message = str(raised.value)
        assert "can't find pane" in message
        assert "demo" in message
        assert "rc=1" in message
        # The child exited on its own, so nothing was killed or reaped.
        assert proc.kill.call_count == 0
        assert proc.wait.await_count == 0

    @pytest.mark.asyncio
    async def test_a_clean_capture_reports_its_repaint(self, manager: TmuxSessionManager) -> None:
        proc = SimpleNamespace(
            returncode=0,
            communicate=AsyncMock(return_value=(b"one\n", b"")),
            kill=MagicMock(),
            wait=AsyncMock(return_value=0),
        )
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            assert (await capture_history(manager, "demo", refresh_tty="/dev/x")).repainted is True
            # Without a tty there was no repaint to report.
            assert (await capture_history(manager, "demo")).repainted is False


class TestBoundHistoryLines:
    def test_empty_capture_yields_empty_window(self) -> None:
        capture = bound_history("", max_lines=10, max_bytes=1024)

        assert capture.text == ""
        assert capture.truncated is False
        assert capture.dropped_bytes == 0
        assert capture.total_bytes == 0

    def test_trailing_newline_does_not_add_a_blank_line(self) -> None:
        capture = bound_history("one\ntwo\n", max_lines=10, max_bytes=1024)

        assert capture.text == f"one\r\ntwo{_RESET}"
        assert capture.truncated is False

    def test_keeps_the_newest_lines_when_the_probe_fires(self) -> None:
        raw = "\n".join(f"line-{index}" for index in range(6))

        capture = bound_history(raw, max_lines=3, max_bytes=1024)

        assert capture.text == f"line-3\r\nline-4\r\nline-5{_RESET}"
        assert capture.truncated is True
        # The probe proves older history existed; no bytes of the delivered
        # window were dropped.
        assert capture.dropped_bytes == 0
        assert capture.total_bytes == len(b"line-3\nline-4\nline-5")

    def test_counters_ignore_crlf_expansion_and_the_reset(self) -> None:
        raw = "alpha\nbeta\ngamma"

        capture = bound_history(raw, max_lines=10, max_bytes=1024)

        assert capture.total_bytes == len(raw.encode())
        assert capture.dropped_bytes == 0
        assert len(capture.text.encode()) > capture.total_bytes

    def test_normalizes_crlf_and_bare_cr_input(self) -> None:
        capture = bound_history("one\r\ntwo\rthree", max_lines=10, max_bytes=1024)

        assert capture.text == f"one\r\ntwo\r\nthree{_RESET}"
        assert capture.total_bytes == len(b"one\ntwo\nthree")


class TestBoundHistoryBytes:
    def test_byte_cut_on_a_line_boundary_keeps_its_first_line(self) -> None:
        raw = "aaaa\nbbbb\ncccc"
        # Exactly "bbbb\ncccc" -- the byte before the cut is the newline, so
        # the first retained line is complete and must survive.
        capture = bound_history(raw, max_lines=10, max_bytes=9)

        assert capture.text == f"bbbb\r\ncccc{_RESET}"
        assert capture.truncated is True
        assert capture.total_bytes == len(raw.encode())
        assert capture.dropped_bytes == len(raw.encode()) - 9

    def test_byte_cut_mid_line_drops_the_partial_line(self) -> None:
        raw = "aaaa\nbbbb\ncccc"
        # Lands inside "bbbb", so that partial line must go.
        capture = bound_history(raw, max_lines=10, max_bytes=6)

        assert capture.text == f"cccc{_RESET}"
        assert capture.truncated is True
        assert capture.dropped_bytes == len(raw.encode()) - len(b"cccc")

    def test_boundary_cut_keeps_a_multibyte_first_line_intact(self) -> None:
        raw = "aaaa\n───"
        # The cut lands exactly on the newline preceding the glyph run.
        capture = bound_history(raw, max_lines=10, max_bytes=len("───".encode()))

        assert capture.text == f"───{_RESET}"
        assert capture.dropped_bytes == len(b"aaaa\n")

    def test_split_multibyte_codepoint_never_surfaces_a_replacement_char(self) -> None:
        raw = "───\nbbbb"
        encoded = len(raw.encode())
        # Cut one byte into the second box-drawing glyph.
        capture = bound_history(raw, max_lines=10, max_bytes=encoded - 4)

        assert "�" not in capture.text
        assert capture.text == f"bbbb{_RESET}"
        assert capture.dropped_bytes == encoded - len(b"bbbb")

    def test_a_single_over_long_line_collapses_rather_than_leaking_a_partial(self) -> None:
        raw = "x" * 100

        capture = bound_history(raw, max_lines=10, max_bytes=10)

        assert capture.text == ""
        assert capture.truncated is True
        assert capture.dropped_bytes == 100

    def test_zero_byte_budget_delivers_nothing(self) -> None:
        capture = bound_history("aaaa\nbbbb", max_lines=10, max_bytes=0)

        assert capture.text == ""
        assert capture.truncated is True
        assert capture.dropped_bytes == capture.total_bytes

    def test_both_bounds_can_fire_together(self) -> None:
        raw = "\n".join(f"line-{index}" for index in range(6))

        capture = bound_history(raw, max_lines=3, max_bytes=13)

        assert capture.truncated is True
        assert capture.dropped_bytes > 0
        assert capture.text.endswith(_RESET)


class TestCaptureHistory:
    @pytest.mark.asyncio
    async def test_decodes_and_bounds_capture_output(self, manager: TmuxSessionManager) -> None:
        proc = FakeProcess(stdout=b"one\ntwo\n")

        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as create_proc:
            capture = await capture_history(manager, "demo", max_lines=5, max_bytes=1024)

        assert capture.text == f"one\r\ntwo{_RESET}"
        assert create_proc.await_args is not None
        assert "-S-6" in create_proc.await_args.args

    @pytest.mark.asyncio
    async def test_replaces_undecodable_bytes_instead_of_raising(
        self, manager: TmuxSessionManager
    ) -> None:
        proc = FakeProcess(stdout=b"ok\n\xff\xfe\n")

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            capture = await capture_history(manager, "demo", max_lines=5, max_bytes=1024)

        assert capture.text.startswith("ok\r\n")
        assert "�" in capture.text

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises_capture_error(self, manager: TmuxSessionManager) -> None:
        proc = FakeProcess(stderr=b"no such session", returncode=1)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(HistoryCaptureError, match="no such session"):
                await capture_history(manager, "demo")

    @pytest.mark.asyncio
    async def test_spawn_failure_becomes_a_capture_error(self, manager: TmuxSessionManager) -> None:
        # The caller runs after the attach is acknowledged, so an OSError
        # escaping this function would strand the attachment behind a generic
        # error frame the client no longer matches.
        spawn = AsyncMock(side_effect=FileNotFoundError(2, "No such file or directory"))

        with patch("asyncio.create_subprocess_exec", new=spawn):
            with pytest.raises(HistoryCaptureError, match="could not spawn"):
                await capture_history(manager, "demo")

    @pytest.mark.asyncio
    async def test_timeout_kills_and_reaps_the_child(self, manager: TmuxSessionManager) -> None:
        proc = FakeProcess(hang=True)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(HistoryCaptureError, match="timed out"):
                await capture_history(manager, "demo", timeout=0.01)

        assert proc.killed is True
        assert proc.waited is True

    @pytest.mark.asyncio
    async def test_cancellation_kills_and_reaps_the_child(
        self, manager: TmuxSessionManager
    ) -> None:
        proc = FakeProcess(hang=True)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            task: asyncio.Task[Any] = asyncio.create_task(capture_history(manager, "demo"))
            await asyncio.wait_for(proc.entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert proc.killed is True
        # Reaping is the half that leaves no orphan behind, so it is asserted
        # separately from the kill.
        assert proc.waited is True

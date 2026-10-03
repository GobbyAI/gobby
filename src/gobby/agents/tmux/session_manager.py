"""Tmux session queries and control for hand-started panes.

Lists, probes, captures, writes to, and kills tmux sessions on the socket a
pane recorded. Gobby creates no tmux sessions (#22856).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from gobby.agents.tmux.session_activation import (
    TMUX_COMMAND_TIMEOUT_SECONDS,
    exact_session_target,
    run_tmux_command,
)
from gobby.agents.tmux.text_injection import (
    TmuxTextInjectionError,
    send_literal_text_to_tmux_target,
)

if TYPE_CHECKING:
    from gobby.terminals.runtime import SnapshotMode

logger = logging.getLogger(__name__)


_MISSING_SESSION_ERRORS = ("can't find session", "no such session", "no server running")
_MISSING_TARGET_ERRORS = (
    *_MISSING_SESSION_ERRORS,
    "can't find pane",
    "no such pane",
    "can't find window",
    "no such window",
)


class TmuxProbeState(StrEnum):
    """Observed state of the tmux server used for a target probe."""

    LIVE = "live"
    SERVER_MISSING = "server_missing"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class TmuxProbeResult:
    """Server liveness plus target presence from one tmux command."""

    state: TmuxProbeState
    pane_exists: bool | None
    detail: str = ""


class TmuxReleaseOutcome(StrEnum):
    """Outcome of releasing Gobby-owned tmux title state."""

    RELEASED = "released"
    ALREADY_RELEASED = "already_released"
    INDETERMINATE = "indeterminate"


def _is_missing_tmux_target_error(stderr: str) -> bool:
    """Return True for tmux errors that mean the target disappeared."""
    message = stderr.lower()
    return any(fragment in message for fragment in _MISSING_TARGET_ERRORS)


def _escape_tmux_format(value: str) -> str:
    """Escape tmux format markers in user-visible strings."""
    return value.replace("#", "##")


def _is_missing_tmux_server_error(stderr: str) -> bool:
    """Return True when tmux reports that the isolated server is not running."""
    message = stderr.strip().lower()
    return "no server running" in message or (
        message.startswith("error connecting to ") and "(no such file or directory)" in message
    )


def _is_tmux_permission_error(stderr: str) -> bool:
    message = stderr.strip().lower()
    return "permission denied" in message or "operation not permitted" in message


def _send_keys_target(target: str) -> str:
    """Return the tmux target form used for keystroke delivery."""
    if target.startswith("%"):
        return target
    return exact_session_target(target)


@dataclass
class TmuxPaneInfo:
    """One pane on a tmux server, keyed by the server generation that owns it."""

    socket_path: str
    server_pid: int
    server_start_time: int
    session_name: str
    window_id: str
    window_name: str | None
    pane_id: str
    pane_pid: int | None
    pane_title: str | None
    pane_dead: bool
    pane_command: str | None
    pane_path: str | None
    session_attached: int = 0
    # Unix timestamp tmux recorded when the pane's process exited. None while the
    # pane is live, and on any dead pane tmux declined to date.
    pane_dead_time: int | None = None


_PANE_LIST_FORMAT = "\t".join(
    (
        "#{socket_path}",
        "#{pid}",
        "#{start_time}",
        "#{session_name}",
        "#{window_id}",
        "#{window_name}",
        "#{pane_id}",
        "#{pane_pid}",
        "#{pane_title}",
        "#{pane_dead}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{session_attached}",
        "#{pane_dead_time}",
    )
)


class TmuxSessionManager:
    """Queries and controls one tmux server, addressed by its socket path.

    Gobby starts no tmux server; the path is the one a hand-started pane
    recorded from ``$TMUX``.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_args(self) -> list[str]:
        """Return the tmux prefix that addresses this manager's server."""
        return ["tmux", "-S", self._socket_path]

    def base_args(self) -> list[str]:
        """Return the public tmux command prefix for this manager."""
        return self._base_args()

    async def _run(
        self,
        *tmux_args: str,
        timeout: float = TMUX_COMMAND_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        """Run a tmux subcommand off the event loop; see ``run_tmux_command``."""
        return await run_tmux_command(
            [*self._base_args(), *tmux_args],
            timeout=timeout,
        )

    async def probe_target(self, target: str) -> TmuxProbeResult:
        """Probe one pane while distinguishing server loss from uncertainty."""
        try:
            rc, _stdout, stderr = await self._run(
                "display-message", "-p", "-t", target, "#{pane_id}"
            )
        except (TimeoutError, PermissionError) as exc:
            logger.debug("Tmux target probe was indeterminate for '%s': %s", target, exc)
            return TmuxProbeResult(TmuxProbeState.INDETERMINATE, None, str(exc))
        except OSError as exc:
            logger.warning("Tmux target probe failed unexpectedly for '%s': %s", target, exc)
            return TmuxProbeResult(TmuxProbeState.INDETERMINATE, None, str(exc))

        detail = stderr.strip()
        if rc == 0:
            return TmuxProbeResult(TmuxProbeState.LIVE, True)
        if _is_missing_tmux_server_error(detail):
            return TmuxProbeResult(TmuxProbeState.SERVER_MISSING, None, detail)
        if _is_missing_tmux_target_error(detail):
            return TmuxProbeResult(TmuxProbeState.LIVE, False, detail)
        if _is_tmux_permission_error(detail):
            logger.debug("Tmux target probe was indeterminate for '%s': %s", target, detail)
            return TmuxProbeResult(TmuxProbeState.INDETERMINATE, None, detail)
        logger.warning("Tmux target probe failed unexpectedly for '%s': %s", target, detail)
        return TmuxProbeResult(TmuxProbeState.INDETERMINATE, None, detail)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_panes(
        self, *, timeout: float = TMUX_COMMAND_TIMEOUT_SECONDS
    ) -> list[TmuxPaneInfo] | None:
        """Every pane on this server.

        ``[]`` when no server is running on the socket; ``None`` when tmux
        failed for any other reason, so callers keep their last view instead
        of treating the panes as gone.
        """
        rc, stdout, stderr = await self._run(
            "list-panes", "-a", "-F", _PANE_LIST_FORMAT, timeout=timeout
        )
        if rc != 0:
            return [] if _is_missing_tmux_server_error(stderr) else None
        panes: list[TmuxPaneInfo] = []
        for line in stdout.splitlines():
            pane = self._parse_pane_line(line)
            if pane is not None:
                panes.append(pane)
        return panes

    @staticmethod
    def _parse_pane_line(line: str) -> TmuxPaneInfo | None:
        """Parse one ``_PANE_LIST_FORMAT`` row; rows with a tab in a name are dropped."""
        parts = line.split("\t")
        if len(parts) != 14:
            return None
        (
            socket_path,
            server_pid,
            start_time,
            session_name,
            window_id,
            window_name,
            pane_id,
            pane_pid,
            pane_title,
            pane_dead,
            pane_command,
            pane_path,
            session_attached,
            pane_dead_time,
        ) = parts
        if not (
            socket_path
            and server_pid.isdigit()
            and start_time.isdigit()
            and session_name
            and window_id.startswith("@")
            and pane_id.startswith("%")
        ):
            return None
        return TmuxPaneInfo(
            socket_path=socket_path,
            server_pid=int(server_pid),
            server_start_time=int(start_time),
            session_name=session_name,
            window_id=window_id,
            window_name=window_name or None,
            pane_id=pane_id,
            pane_pid=int(pane_pid) if pane_pid.isdigit() else None,
            pane_title=pane_title or None,
            pane_dead=pane_dead == "1",
            pane_command=pane_command or None,
            pane_path=pane_path or None,
            session_attached=int(session_attached) if session_attached.isdigit() else 0,
            pane_dead_time=int(pane_dead_time) if pane_dead_time.isdigit() else None,
        )

    async def has_session(self, name: str) -> bool:
        """Check whether a session with *name* exists."""
        rc, _stdout, _stderr = await self._run("has-session", "-t", exact_session_target(name))
        return rc == 0

    @staticmethod
    def _live_process_groups(pgids: set[int]) -> set[int]:
        live_pgids: set[int] = set()
        for pgid in pgids:
            try:
                os.killpg(pgid, 0)
                live_pgids.add(pgid)
            except ProcessLookupError:
                continue
            except PermissionError:
                live_pgids.add(pgid)
            except OSError:
                continue
        return live_pgids

    @classmethod
    async def _wait_for_process_groups_exit(cls, pgids: set[int], timeout: float) -> set[int]:
        if not pgids or timeout <= 0:
            return cls._live_process_groups(pgids)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        live_pgids = cls._live_process_groups(pgids)
        while live_pgids and loop.time() < deadline:
            await asyncio.sleep(min(0.1, max(0.0, deadline - loop.time())))
            live_pgids = cls._live_process_groups(live_pgids)
        return live_pgids

    async def kill_session(
        self, name: str, *, missing_ok: bool = False, timeout: float = 5.0
    ) -> bool:
        """Kill a tmux session and all processes in it.

        Collects pane PIDs before destroying the session, then sends SIGTERM
        to the process groups so that child processes (the actual agent CLI)
        are also killed. Stragglers get SIGKILL after a brief grace period.
        """
        # Collect pane PIDs before killing the session
        pids = await self._get_session_pids(name)

        # Kill the tmux session
        target = exact_session_target(name)
        rc, _stdout, stderr = await self._run("kill-session", "-t", target)
        if rc != 0:
            message = stderr.strip()
            if any(error in message.lower() for error in _MISSING_SESSION_ERRORS):
                logger.debug(
                    "Tmux session '%s' was already missing during kill (missing_ok=%s): %s",
                    name,
                    missing_ok,
                    message,
                )
                return missing_ok
            logger.warning("Failed to kill tmux session '%s': %s", name, message)
            return False

        # Kill process groups rooted at each pane shell
        pgids: set[int] = set()
        for pid in pids:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                pgids.add(pgid)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        # Honor the caller's grace period, then SIGKILL straggling process groups.
        live_pgids = await self._wait_for_process_groups_exit(pgids, timeout)
        for pgid in live_pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        logger.info("Killed tmux session '%s' (pids: %s)", name, pids)
        return True

    async def _get_session_pids(self, name: str) -> list[int]:
        """Get all pane PIDs in a tmux session."""
        rc, stdout, _ = await self._run(
            "list-panes",
            "-t",
            exact_session_target(name),
            "-F",
            "#{pane_pid}",
        )
        if rc != 0:
            return []
        pids: list[int] = []
        for line in stdout.strip().splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                pass
        return pids

    async def get_window_automatic_rename(self, target: str) -> bool | None:
        """Return whether ``automatic-rename`` is on for *target*'s window.

        A window Gobby has named via :meth:`rename_window` has
        ``automatic-rename`` disabled, so this is a cheap "has Gobby named this
        window yet?" probe for the repair sweep.

        Returns True/False, or None when the option cannot be read (e.g. the
        target window no longer exists).
        """
        rc, stdout, _stderr = await self._run(
            "display-message", "-t", target, "-p", "#{automatic-rename}"
        )
        if rc != 0:
            return None
        value = stdout.strip()
        if value in ("1", "on"):
            return True
        if value in ("0", "off"):
            return False
        return None

    async def get_window_name(self, target: str) -> str | None:
        """Return *target*'s current tmux window name, or None when unreadable."""
        rc, stdout, _stderr = await self._run(
            "display-message", "-t", target, "-p", "#{window_name}"
        )
        if rc != 0:
            return None
        value = stdout.strip()
        return value or None

    async def rename_window(self, target: str, title: str) -> bool:
        """Rename the tmux window containing *target*.

        Also enables ``set-titles`` so the name propagates to the outer
        terminal emulator, disables ``automatic-rename`` to prevent tmux
        from overwriting it, and disables ``allow-rename`` so a program
        running inside the pane (e.g. Claude Code's version/status OSC
        title escapes) cannot overwrite the window name either.

        Args:
            target: A tmux target (session name, pane ID like ``%42``, etc.).
            title: New window title.

        Returns:
            True on success.
        """
        tmux_title = _escape_tmux_format(title)
        rc, _stdout, stderr = await self._run(
            "set-option",
            "-t",
            target,
            "set-titles",
            "on",
            ";",
            "set-option",
            "-t",
            target,
            "set-titles-string",
            "#W",
            ";",
            "rename-window",
            "-t",
            target,
            tmux_title,
            ";",
            "select-pane",
            "-t",
            target,
            "-T",
            tmux_title,
            ";",
            "set-option",
            "-w",
            "-t",
            target,
            "automatic-rename",
            "off",
            ";",
            "set-option",
            "-w",
            "-t",
            target,
            "allow-rename",
            "off",
        )
        if rc != 0:
            message = stderr.strip()
            if _is_missing_tmux_target_error(message):
                logger.debug(
                    "Skipping tmux window rename for missing target '%s': %s", target, message
                )
            else:
                logger.warning("Failed to rename tmux window for '%s': %s", target, message)
            return False
        return True

    async def release_window_title_ownership(self, target: str) -> TmuxReleaseOutcome:
        """Release Gobby's window and pane title overrides for *target*."""
        try:
            rc, _stdout, stderr = await self._run(
                "set-option",
                "-w",
                "-u",
                "-t",
                target,
                "automatic-rename",
                ";",
                "set-option",
                "-w",
                "-u",
                "-t",
                target,
                "allow-rename",
                ";",
                "select-pane",
                "-t",
                target,
                "-T",
                "",
            )
        except (TimeoutError, PermissionError) as exc:
            logger.debug("Tmux title release was indeterminate for '%s': %s", target, exc)
            return TmuxReleaseOutcome.INDETERMINATE
        except OSError as exc:
            logger.warning("Tmux title release failed unexpectedly for '%s': %s", target, exc)
            return TmuxReleaseOutcome.INDETERMINATE
        if rc != 0:
            message = stderr.strip()
            if _is_missing_tmux_server_error(message) or _is_missing_tmux_target_error(message):
                logger.debug(
                    "Tmux title for missing target '%s' is already released: %s", target, message
                )
                return TmuxReleaseOutcome.ALREADY_RELEASED
            if _is_tmux_permission_error(message):
                logger.debug("Tmux title release was indeterminate for '%s': %s", target, message)
                return TmuxReleaseOutcome.INDETERMINATE
            logger.warning("Failed to release tmux title for '%s': %s", target, message)
            return TmuxReleaseOutcome.INDETERMINATE
        return TmuxReleaseOutcome.RELEASED

    async def capture_pane(
        self, session_name: str, lines: int = 5, *, mode: SnapshotMode = "text"
    ) -> str | None:
        """Capture the last N lines from a tmux session's pane.

        Args:
            session_name: Target session name.
            lines: Number of lines to capture from the bottom.
            mode: ``ansi`` keeps the SGR styling escapes (``-e``).

        Returns:
            Captured text, or None on failure.
        """
        rc, stdout, _stderr = await self._run(
            "capture-pane",
            "-t",
            _send_keys_target(session_name),
            "-p",  # print to stdout
            *(("-e",) if mode == "ansi" else ()),
            "-J",  # join wrapped lines
            f"-S-{max(lines, 0)}",  # tmux returns history plus the visible pane
        )
        if rc != 0:
            return None
        if lines <= 0:
            return ""
        return "".join(stdout.splitlines(keepends=True)[-lines:])

    async def capture_full_pane(self, session_name: str) -> str | None:
        """Capture the complete configured tmux history and visible pane."""
        rc, stdout, _stderr = await self._run(
            "capture-pane",
            "-t",
            _send_keys_target(session_name),
            "-p",
            "-S",
            "-",
        )
        if rc != 0:
            return None
        return stdout

    async def send_keys(self, session_name: str, keys: str, *, literal: bool = True) -> bool:
        """Send keys to a tmux session.

        Args:
            session_name: Target session name.
            keys: Key string to send.  When *literal* is True a trailing
                  ``\\n`` triggers an ``Enter`` keypress after the literal
                  text.  When *literal* is False, *keys* are passed directly
                  to ``tmux send-keys`` (accepts tmux key names such as
                  ``C-c``, ``Escape``, ``Enter``, ``C-d``).
            literal: If True (default), send text in literal mode (``-l``)
                     so special characters are not interpreted.  If False,
                     pass keys directly to tmux without ``-l``, allowing
                     tmux key names.

        Returns:
            True on success.
        """
        if not literal:
            # Raw mode: pass keys directly, tmux interprets key names.
            rc, _stdout, stderr = await self._run(
                "send-keys",
                "-t",
                _send_keys_target(session_name),
                keys,
            )
            if rc != 0:
                logger.warning(
                    "Failed to send raw keys to tmux session '%s': %s", session_name, stderr.strip()
                )
                return False
            return True

        try:
            await send_literal_text_to_tmux_target(
                _send_keys_target(session_name),
                keys,
                tmux_cmd=self._base_args(),
            )
        except TmuxTextInjectionError as exc:
            logger.warning(
                "Failed to send keys to tmux session '%s': %s",
                session_name,
                exc,
            )
            return False

        return True

    async def dispatch_keys(self, session_name: str, keys: str, *, literal: bool = True) -> bool:
        """Backend-neutral alias used by plan-keystroke playback."""
        return await self.send_keys(session_name, keys, literal=literal)

    async def snapshot_lines(
        self, session_name: str, lines: int = 5, *, mode: SnapshotMode = "text"
    ) -> str | None:
        """Backend-neutral alias for capturing the last N pane lines."""
        return await self.capture_pane(session_name, lines=lines, mode=mode)

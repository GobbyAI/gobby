"""PTY relay bridge for interactive tmux session access.

Creates a PTY pair, spawns ``tmux attach-session`` in it, and provides
master_fd for read/write. The existing :class:`PTYReaderManager` handles
output streaming; input goes via ``os.write(master_fd, data)``.

This gives full terminal fidelity (Ctrl+C, arrows, Tab, etc.) unlike
``send-keys -l`` which can only handle literal characters.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import struct
import termios
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from gobby.config.tmux import TmuxConfig

logger = logging.getLogger(__name__)


@dataclass
class BridgeInfo:
    """Tracks a single PTY bridge to a tmux session."""

    master_fd: int
    proc: asyncio.subprocess.Process
    session_name: str
    socket_name: str
    # The terminals row this client views. Required: output frames carry it as
    # terminal_id, and the web client drops frames it cannot key — a bridge
    # registered without its row would stream into the void.
    terminal_id: str
    # The geometry tmux is currently running this client at. A resize to the
    # size it already has is not a resize, and the repaint it would trigger is
    # the one thing an attach must not do: activation's history boundary is
    # only correct for the screen its own repaint painted.
    rows: int = 50
    cols: int = 200
    # The config that reaches this client's tmux server (socket path or name),
    # so a repaint after resize and the reap of a stale viewer can find their
    # way back without a lookup.
    config: TmuxConfig | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class TmuxPTYBridge:
    """Bridge tmux sessions to PTYs for full-fidelity web terminal access.

    Creates a PTY pair, spawns ``tmux [-L socket] attach-session -t <name>``,
    and provides master_fd for read/write. The PTYReaderManager handles
    output streaming; input goes via ``os.write(master_fd, data)``.
    """

    def __init__(self) -> None:
        self._bridges: dict[str, BridgeInfo] = {}  # streaming_id -> BridgeInfo
        self._pending_bridges: set[str] = set()
        self._lock = asyncio.Lock()

    async def attach(
        self,
        session_name: str,
        streaming_id: str,
        config: TmuxConfig | None = None,
        rows: int = 50,
        cols: int = 200,
        *,
        terminal_id: str,
    ) -> int:
        """Attach to a tmux session via PTY. Returns master_fd.

        Args:
            session_name: Tmux session to attach to.
            streaming_id: Unique ID for this bridge (used as run_id for output).
            config: TmuxConfig specifying socket_name and command.
            rows: Initial terminal rows.
            cols: Initial terminal cols.
            terminal_id: The terminals row this client views. Must be nonempty:
                output frames key on it and the web client drops unkeyed frames.

        Returns:
            The master file descriptor for reading/writing.

        Raises:
            RuntimeError: If attach fails.
            ValueError: If terminal_id is empty.
        """
        if not terminal_id:
            raise ValueError(
                "terminal_id is required: a bridge without its terminals row "
                "emits frames the client drops"
            )
        async with self._lock:
            if streaming_id in self._bridges or streaming_id in self._pending_bridges:
                raise RuntimeError(f"Bridge {streaming_id} already exists")
            self._pending_bridges.add(streaming_id)

        try:
            if config is None:
                from gobby.agents.tmux import get_configured_tmux_config

                config = get_configured_tmux_config()
            cfg = config

            master_fd: int | None = None
            slave_fd: int | None = None
            proc: asyncio.subprocess.Process | None = None
            try:
                master_fd, slave_fd = os.openpty()

                # Set initial terminal size
                fcntl.ioctl(
                    slave_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )

                cmd = self._build_attach_cmd(session_name, cfg)
                # The daemon usually runs without a usable $TERM (launchd, gobby
                # start). tmux attach-session exits immediately with "missing or
                # unsuitable terminal" in that case, leaving a dead PTY that
                # surfaces only as EIO on later writes. The bridge output is
                # rendered by an xterm-compatible web terminal, so force a
                # matching terminfo regardless of the inherited environment.
                env = {**os.environ, "TERM": "xterm-256color"}
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    env=env,
                )
            except Exception:
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                if slave_fd is not None:
                    try:
                        os.close(slave_fd)
                    except OSError:
                        pass
                if proc is not None and proc.returncode is None:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except (TimeoutError, ProcessLookupError):
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                raise

            try:
                os.close(slave_fd)
            except OSError:
                pass
            slave_fd = None

            assert master_fd is not None
            assert proc is not None

            try:
                bridge = BridgeInfo(
                    master_fd=master_fd,
                    proc=proc,
                    session_name=session_name,
                    socket_name=cfg.socket_name,
                    rows=rows,
                    cols=cols,
                    terminal_id=terminal_id,
                    config=cfg,
                )

                async with self._lock:
                    self._bridges[streaming_id] = bridge
                    self._pending_bridges.discard(streaming_id)
            except Exception:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (TimeoutError, ProcessLookupError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                raise
        except Exception:
            async with self._lock:
                self._pending_bridges.discard(streaming_id)
            raise

        logger.info(
            "PTY bridge attached: %s -> tmux session '%s' (socket=%s)",
            streaming_id,
            session_name,
            cfg.socket_name or "default",
        )
        return master_fd

    async def detach(self, streaming_id: str) -> None:
        """Detach from a tmux session, close PTY."""
        async with self._lock:
            bridge = self._bridges.pop(streaming_id, None)

        if not bridge:
            return

        try:
            os.close(bridge.master_fd)
        except OSError:
            pass

        try:
            bridge.proc.terminate()
            await asyncio.wait_for(bridge.proc.wait(), timeout=2.0)
        except (TimeoutError, ProcessLookupError):
            try:
                bridge.proc.kill()
            except ProcessLookupError:
                pass

        logger.info("PTY bridge detached: %s", streaming_id)

    async def detach_all(self) -> None:
        """Detach all active bridges."""
        async with self._lock:
            ids = list(self._bridges.keys())
        for sid in ids:
            await self.detach(sid)

    async def resize(self, streaming_id: str, rows: int, cols: int) -> BridgeInfo | None:
        """Resize the PTY (propagates to tmux client).

        Returns:
            The BridgeInfo when the geometry actually changed, so the caller
            knows the client needs a repaint (it can use session_name and
            socket_name to issue ``tmux refresh-client``). ``None`` when there
            is no such bridge, when the resize failed, or when the client is
            already that size -- a repaint it does not need would land after
            the attach's history capture and cost the seam a line.
        """
        async with self._lock:
            bridge = self._bridges.get(streaming_id)
            if bridge and (bridge.rows, bridge.cols) == (rows, cols):
                return None

        if bridge:
            try:
                fcntl.ioctl(
                    bridge.master_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
                # The attach process is not a session leader with this PTY as
                # its controlling terminal, so the kernel does not deliver the
                # SIGWINCH that TIOCSWINSZ normally implies. Without it the
                # tmux client never re-reads the winsize and the resize is a
                # silent no-op.
                try:
                    bridge.proc.send_signal(signal.SIGWINCH)
                except ProcessLookupError:
                    logger.debug("Bridge %s process exited before SIGWINCH", streaming_id)
                async with self._lock:
                    # Only record what tmux was actually told, so a failed
                    # ioctl leaves the next resize to try again.
                    if self._bridges.get(streaming_id) is bridge:
                        self._bridges[streaming_id] = replace(bridge, rows=rows, cols=cols)
                return bridge
            except OSError as e:
                logger.warning("Resize failed for %s: %s", streaming_id, e)
        return None

    async def get_master_fd(self, streaming_id: str) -> int | None:
        """Get master_fd for writing input."""
        async with self._lock:
            bridge = self._bridges.get(streaming_id)
        return bridge.master_fd if bridge else None

    async def get_bridge(self, streaming_id: str) -> BridgeInfo | None:
        """Get bridge info."""
        async with self._lock:
            return self._bridges.get(streaming_id)

    async def list_bridges(self) -> dict[str, BridgeInfo]:
        """List all active bridges."""
        async with self._lock:
            return dict(self._bridges)

    def _build_attach_cmd(self, session_name: str, config: TmuxConfig) -> list[str]:
        args = [config.command]
        if config.socket_path:
            args.extend(["-S", config.socket_path])
        elif config.socket_name:
            args.extend(["-L", config.socket_name])
        # The forced TERM=xterm-256color terminfo carries no RGB capability, so
        # tmux would downgrade 24-bit SGRs to the 256-color palette for this
        # client. The web terminal renders truecolor, so declare the client's
        # real feature set explicitly (-T requires tmux >= 3.2, our floor).
        args.extend(["-T", "256,RGB", "attach-session", "-t", session_name])
        return args

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
import struct
import termios
from dataclasses import dataclass, field
from datetime import UTC, datetime

from gobby.agents.tmux.config import TmuxConfig

logger = logging.getLogger(__name__)


@dataclass
class BridgeInfo:
    """Tracks a single PTY bridge to a tmux session."""

    master_fd: int
    proc: asyncio.subprocess.Process
    session_name: str
    socket_name: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class TmuxPTYBridge:
    """Bridge tmux sessions to PTYs for full-fidelity web terminal access.

    Creates a PTY pair, spawns ``tmux [-L socket] attach-session -t <name>``,
    and provides master_fd for read/write. The PTYReaderManager handles
    output streaming; input goes via ``os.write(master_fd, data)``.
    """

    def __init__(self) -> None:
        self._bridges: dict[str, BridgeInfo] = {}  # streaming_id -> BridgeInfo
        self._lock = asyncio.Lock()

    async def attach(
        self,
        session_name: str,
        streaming_id: str,
        config: TmuxConfig | None = None,
        rows: int = 50,
        cols: int = 200,
    ) -> int:
        """Attach to a tmux session via PTY. Returns master_fd.

        Args:
            session_name: Tmux session to attach to.
            streaming_id: Unique ID for this bridge (used as run_id for output).
            config: TmuxConfig specifying socket_name and command.
            rows: Initial terminal rows.
            cols: Initial terminal cols.

        Returns:
            The master file descriptor for reading/writing.

        Raises:
            RuntimeError: If attach fails.
        """
        cfg = config or TmuxConfig()

        async with self._lock:
            if streaming_id in self._bridges:
                raise RuntimeError(f"Bridge {streaming_id} already exists")

        master_fd, slave_fd = os.openpty()

        # Set initial terminal size
        fcntl.ioctl(
            slave_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )

        cmd = self._build_attach_cmd(session_name, cfg)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise

        os.close(slave_fd)

        bridge = BridgeInfo(
            master_fd=master_fd,
            proc=proc,
            session_name=session_name,
            socket_name=cfg.socket_name,
        )

        async with self._lock:
            self._bridges[streaming_id] = bridge

        logger.info(
            f"PTY bridge attached: {streaming_id} -> "
            f"tmux session '{session_name}' (socket={cfg.socket_name or 'default'})"
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

        logger.info(f"PTY bridge detached: {streaming_id}")

    async def detach_all(self) -> None:
        """Detach all active bridges."""
        async with self._lock:
            ids = list(self._bridges.keys())
        for sid in ids:
            await self.detach(sid)

    async def resize(self, streaming_id: str, rows: int, cols: int) -> BridgeInfo | None:
        """Resize the PTY (propagates to tmux client).

        Returns:
            The BridgeInfo if resize succeeded (caller can use session_name/socket_name
            to issue ``tmux refresh-client``), or None if no bridge found.
        """
        async with self._lock:
            bridge = self._bridges.get(streaming_id)

        if bridge:
            try:
                fcntl.ioctl(
                    bridge.master_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
                return bridge
            except OSError as e:
                logger.warning(f"Resize failed for {streaming_id}: {e}")
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
        if config.socket_name:
            args.extend(["-L", config.socket_name])
        args.extend(["attach-session", "-t", session_name])
        return args

"""Stream tmux pane output to the web UI via pipe-pane and a FIFO.

Mirrors the :class:`PTYReaderManager` interface so the runner can wire
both readers identically.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import select
import shlex
import stat
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from gobby.config.tmux import TmuxConfig

logger = logging.getLogger(__name__)

# Same signature as pty_reader.OutputCallback
OutputCallback = Callable[[str, str], Awaitable[None]]

_TMUX_STDERR_LOG_LIMIT = 512
_VANISHED_TMUX_ERROR_MARKERS = (
    "can't find pane",
    "can't find session",
    "error connecting to",
    "failed to connect to server",
    "no server running on",
)


@dataclass(frozen=True, slots=True)
class TmuxCommandResult:
    """Structured result from a tmux subprocess."""

    returncode: int
    stderr: str
    timed_out: bool


def _safe_fifo_component(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    cleaned = cleaned.strip("-_")
    return cleaned[:80] or "default"


class TmuxOutputReader:
    """Streams output from tmux panes via ``pipe-pane`` to a named FIFO.

    Lifecycle per agent:

    1. ``start_reader(run_id, session_name)``
       - Creates ``<tempdir>/gobby-tmux-<socket>-<session>-<run>.pipe`` FIFO.
       - Runs ``tmux pipe-pane -t <session> "cat >> <fifo>"``.
       - Starts an async read loop on the FIFO fd.

    2. ``stop_reader(run_id)``
       - Runs ``tmux pipe-pane -t <session>`` (no arg → disables).
       - Cancels the read task and unlinks the FIFO.
    """

    def __init__(self, config: TmuxConfig | None = None) -> None:
        self._config = config or TmuxConfig()
        self._output_callback: OutputCallback | None = None
        self._reader_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._fifo_paths: dict[str, str] = {}  # run_id → fifo path
        self._session_names: dict[str, str] = {}  # run_id → tmux session
        self._lock = asyncio.Lock()

    def set_output_callback(self, callback: OutputCallback | None) -> None:
        """Set the async callback invoked with ``(run_id, text)``."""
        self._output_callback = callback

    # ------------------------------------------------------------------
    # tmux helpers
    # ------------------------------------------------------------------

    def _base_args(self) -> list[str]:
        args = [self._config.command]
        if self._config.socket_path:
            args.extend(["-S", self._config.socket_path])
        elif self._config.socket_name:
            args.extend(["-L", self._config.socket_name])
        if self._config.config_file:
            args.extend(["-f", self._config.config_file])
        return args

    @staticmethod
    def _target_from_args(tmux_args: tuple[str, ...]) -> str:
        try:
            return tmux_args[tmux_args.index("-t") + 1]
        except (ValueError, IndexError):
            return "<unspecified>"

    @staticmethod
    def _bounded_stderr(stderr: str) -> str:
        compact = " ".join(stderr.split())
        if len(compact) <= _TMUX_STDERR_LOG_LIMIT:
            return compact
        return f"{compact[:_TMUX_STDERR_LOG_LIMIT]}…"

    def _log_command_result(
        self,
        *,
        target: str,
        result: TmuxCommandResult,
    ) -> None:
        bounded_stderr = self._bounded_stderr(result.stderr) or "<empty>"
        if result.timed_out:
            logger.warning(
                "tmux pipe-pane timed out: target=%s status=%s stderr=%s",
                target,
                result.returncode,
                bounded_stderr,
            )
            return
        if result.returncode == 0:
            if result.stderr:
                logger.warning(
                    "tmux pipe-pane returned unexpected stderr: target=%s status=%s stderr=%s",
                    target,
                    result.returncode,
                    bounded_stderr,
                )
            return
        if any(marker in result.stderr.casefold() for marker in _VANISHED_TMUX_ERROR_MARKERS):
            logger.debug(
                "tmux pipe-pane target vanished: target=%s status=%s stderr=%s",
                target,
                result.returncode,
                bounded_stderr,
            )
            return
        logger.warning(
            "tmux pipe-pane failed: target=%s status=%s stderr=%s",
            target,
            result.returncode,
            bounded_stderr,
        )

    async def _run(
        self,
        *tmux_args: str,
        timeout: float = 5.0,
    ) -> TmuxCommandResult:
        """Run a tmux command and classify its structured result."""
        cmd = [*self._base_args(), *tmux_args]
        target = self._target_from_args(tmux_args)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            _, stderr = await proc.communicate()
        returncode = proc.returncode
        if returncode is None:
            returncode = 1 if timed_out else 0
        result = TmuxCommandResult(
            returncode=returncode,
            stderr=stderr.decode(errors="replace").strip(),
            timed_out=timed_out,
        )
        self._log_command_result(target=target, result=result)
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_reader(self, run_id: str, session_name: str) -> bool:
        """Start streaming output from a tmux session.

        Returns True if the reader was started, False if already running.
        """
        async with self._lock:
            if run_id in self._reader_tasks:
                return False

            # Create FIFO
            fifo_dir = tempfile.gettempdir()
            socket_prefix = _safe_fifo_component(self._config.socket_name or "default")
            session_component = _safe_fifo_component(session_name)
            run_component = _safe_fifo_component(run_id)
            fifo_path = os.path.join(
                fifo_dir,
                f"gobby-tmux-{socket_prefix}-{session_component}-{run_component}.pipe",
            )

            # Clean up stale FIFO from previous run
            try:
                if os.path.exists(fifo_path):
                    os.unlink(fifo_path)
            except OSError:
                pass

            try:
                os.mkfifo(fifo_path, mode=stat.S_IRUSR | stat.S_IWUSR)
            except OSError as e:
                logger.error("Failed to create FIFO %s: %s", fifo_path, e)
                return False

            # Quote path to prevent shell injection in tmux invocation
            safe_fifo_path = shlex.quote(fifo_path)

            # Tell tmux to pipe pane output into the FIFO
            # Note: Holding lock to ensure race-free start vs stop
            result = await self._run(
                "pipe-pane",
                "-t",
                session_name,
                f"cat >> {safe_fifo_path}",
            )
            if result.returncode != 0:
                try:
                    os.unlink(fifo_path)
                except OSError:
                    pass
                return False

            self._fifo_paths[run_id] = fifo_path
            self._session_names[run_id] = session_name

            stop_event = asyncio.Event()
            self._stop_events[run_id] = stop_event

            task = asyncio.create_task(
                self._read_loop(run_id, fifo_path, stop_event),
                name=f"tmux_reader_{run_id}",
            )
            self._reader_tasks[run_id] = task

        logger.debug("Started tmux output reader for %s (%s)", run_id, session_name)
        return True

    async def stop_reader(self, run_id: str) -> bool:
        """Stop streaming for a given run_id. Returns True if stopped."""
        async with self._lock:
            stop_event = self._stop_events.pop(run_id, None)
            task = self._reader_tasks.pop(run_id, None)
            fifo_path = self._fifo_paths.pop(run_id, None)
            session_name = self._session_names.pop(run_id, None)

            # Disable pipe-pane inside lock to prevent race with start_reader
            # If we don't hold lock, a new start_reader could enable pipe,
            # and then we disable it here, breaking the new reader.
            if session_name:
                # Check if anyone else is using this session (not possible with current logic,
                # but good for safety if we allow shared sessions later)
                if session_name not in self._session_names.values():
                    await self._run("pipe-pane", "-t", session_name)

        if stop_event:
            stop_event.set()

        if task:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Unlink FIFO
        if fifo_path:
            try:
                os.unlink(fifo_path)
            except OSError:
                pass

        if task:
            logger.debug("Stopped tmux output reader for %s", run_id)
            return True
        return False

    async def stop_all(self) -> None:
        """Stop all active readers."""
        async with self._lock:
            run_ids = list(self._reader_tasks.keys())
        for run_id in run_ids:
            await self.stop_reader(run_id)

    # ------------------------------------------------------------------
    # Read loop
    # ------------------------------------------------------------------

    async def _read_loop(
        self,
        run_id: str,
        fifo_path: str,
        stop_event: asyncio.Event,
    ) -> None:
        """Read from the FIFO and invoke the output callback.

        Opens the FIFO in non-blocking mode and uses ``select`` to poll
        for data, same pattern as :class:`PTYReaderManager._read_loop`.
        """
        loop = asyncio.get_running_loop()
        fd: int | None = None
        # Incremental decoder buffers incomplete multi-byte UTF-8 sequences
        # across read boundaries, preventing corruption when a character
        # straddles two 4 KB chunks.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        try:
            # Open FIFO for reading (O_RDONLY | O_NONBLOCK so we don't
            # block if the writer hasn't connected yet).
            fd = await loop.run_in_executor(
                None,
                lambda: os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK),
            )

            while not stop_event.is_set():
                try:
                    ready, _, _ = await loop.run_in_executor(
                        None,
                        lambda: select.select([fd], [], [], 0.1),
                    )
                except (ValueError, OSError):
                    break

                if not ready:
                    continue

                try:
                    data = await loop.run_in_executor(
                        None,
                        lambda: os.read(fd, 4096),
                    )
                except OSError as e:
                    logger.debug("FIFO read error for %s: %s", run_id, e)
                    break

                if not data:
                    # EOF — writer closed; wait briefly and retry in case
                    # tmux reconnects pipe-pane after a brief gap.
                    await asyncio.sleep(0.2)
                    continue

                text = decoder.decode(data)

                if text and self._output_callback:
                    try:
                        await self._output_callback(run_id, text)
                    except Exception as e:
                        logger.warning("Output callback error for %s: %s", run_id, e)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Tmux reader error for %s: %s", run_id, e)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            logger.debug("Tmux reader finished for %s", run_id)

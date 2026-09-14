"""A stdlib PTY and visible-screen driver for the installed gclient."""

from __future__ import annotations

import codecs
import errno
import os
import pty
import select
import signal
import termios
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from tests.native_binary_selection import select_native_binary


class Screen:
    """Track ratatui's cursor-addressed cells, including fragmented escapes."""

    def __init__(self, cols: int, rows: int) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending = ""
        self.resize(cols, rows)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.cells = [[" "] * cols for _ in range(rows)]
        self.x = self.y = 0

    @property
    def lines(self) -> list[str]:
        return ["".join(row) for row in self.cells]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def feed(self, data: bytes) -> None:
        self.pending += self.decoder.decode(data)
        while self.pending:
            char = self.pending[0]
            if char == "\x1b":
                if len(self.pending) < 2:
                    return
                kind = self.pending[1]
                if kind == "[":
                    end = next(
                        (i for i in range(2, len(self.pending)) if "@" <= self.pending[i] <= "~"),
                        None,
                    )
                    if end is None:
                        return
                    self._csi(self.pending[2:end], self.pending[end])
                    self.pending = self.pending[end + 1 :]
                elif kind in "]P_":
                    ends = [
                        (i, size)
                        for marker, size in (("\a", 1), ("\x1b\\", 2))
                        if (i := self.pending.find(marker, 2)) >= 0
                    ]
                    if not ends:
                        return
                    end, size = min(ends)
                    self.pending = self.pending[end + size :]
                else:
                    self.pending = self.pending[2:]
                continue
            self.pending = self.pending[1:]
            if char == "\r":
                self.x = 0
            elif char == "\n":
                self.y += 1
                if self.y >= self.rows:
                    self.cells.pop(0)
                    self.cells.append([" "] * self.cols)
                    self.y = self.rows - 1
            elif char == "\b":
                self.x = max(0, self.x - 1)
            elif char == "\t":
                self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
            elif ord(char) >= 32:
                if unicodedata.combining(char):
                    if self.x:
                        self.cells[self.y][min(self.x, self.cols) - 1] += char
                    continue
                width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
                if self.x < self.cols:
                    self.cells[self.y][self.x] = char
                    if width == 2 and self.x + 1 < self.cols:
                        self.cells[self.y][self.x + 1] = ""
                self.x = min(self.cols, self.x + width)

    def _csi(self, params: str, command: str) -> None:
        if params.startswith(("?", ">", "<", "=")):
            return  # modes and device attributes don't draw cells
        values = [int(part) if part.isdigit() else 0 for part in params.split(";")]
        n = values[0] or 1
        if command in {"H", "f"}:
            self.y = min(self.rows - 1, n - 1)
            self.x = min(self.cols - 1, (values[1] or 1) - 1 if len(values) > 1 else 0)
        elif command == "G":
            self.x = min(self.cols - 1, n - 1)
        elif command == "d":
            self.y = min(self.rows - 1, n - 1)
        elif command in "ABCD":
            dx, dy = {"A": (0, -n), "B": (0, n), "C": (n, 0), "D": (-n, 0)}[command]
            self.x = max(0, min(self.cols - 1, self.x + dx))
            self.y = max(0, min(self.rows - 1, self.y + dy))
        elif command == "J":
            mode = values[0]
            for y in range(self.rows):
                for x in range(self.cols):
                    if mode in {2, 3} or (mode == 0 and (y, x) >= (self.y, self.x)):
                        self.cells[y][x] = " "
                    elif mode == 1 and (y, x) <= (self.y, self.x):
                        self.cells[y][x] = " "
        elif command == "K":
            mode = values[0]
            for x in range(self.cols):
                if mode == 2 or (mode == 0 and x >= self.x) or (mode == 1 and x <= self.x):
                    self.cells[self.y][x] = " "
        elif command == "X":
            for x in range(self.x, min(self.cols, self.x + n)):
                self.cells[self.y][x] = " "


PREFIX_CUE = "prefix "


def in_prefix_mode(screen: Screen) -> bool:
    """Whether the status line is showing prefix mode.

    `render_status_line` (crates/gclient/src/ui/status.rs) always names the
    prefix -- "the prefix is the way into every chord, quit included" -- so the
    bare word is never absent and cannot report the mode. Prefix mode adds the
    mode segment from `mode_name`, ahead of that permanent cue, giving a second
    occurrence. Two is the mode; one is the cue alone.
    """
    return screen.lines[-1].count(PREFIX_CUE) > 1


class GclientDriver:
    """Run a real client with a controlling 120×40 PTY and bounded teardown."""

    def __init__(self, args: list[str], *, env: dict[str, str], cwd: Path) -> None:
        selected = select_native_binary("gclient", required=True)
        assert selected is not None
        binary = selected.path
        self.screen = Screen(120, 40)
        self.output = bytearray()
        self.exit_code: int | None = None
        self.eof = False
        child_env = dict(env, TERM="xterm-256color", COLORTERM="truecolor")
        for name in ("TMUX", "TMUX_PANE", "COLUMNS", "LINES"):
            child_env.pop(name, None)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                termios.tcsetwinsize(0, (40, 120))
                os.chdir(cwd)
                os.execve(binary, [str(binary), *args], child_env)
            finally:
                os._exit(127)
        os.set_blocking(self.fd, False)

    def __enter__(self) -> GclientDriver:
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def poll(self) -> int | None:
        if self.exit_code is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.exit_code = os.waitstatus_to_exitcode(status)
        return self.exit_code

    def read(self, timeout: float = 0.05) -> None:
        if self.eof or not select.select([self.fd], [], [], max(0.0, timeout))[0]:
            return
        try:
            data = os.read(self.fd, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b""
        if not data:
            self.eof = True
            return
        self.output.extend(data)
        self.screen.feed(data)

    def wait_for(
        self, predicate: Callable[[Screen], bool], *, description: str, timeout: float = 15.0
    ) -> Screen:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read(min(0.05, deadline - time.monotonic()))
            if predicate(self.screen):
                return self.screen
            if self.poll() is not None:
                break
        raise AssertionError(
            f"gclient did not render {description}; exit={self.poll()}\n{self.screen.text}\n"
            f"PTY tail: {bytes(self.output[-2000:])!r}"
        )

    def expect(self, text: str, *, timeout: float = 15.0) -> Screen:
        return self.wait_for(
            lambda screen: text in screen.text, description=repr(text), timeout=timeout
        )

    def send(self, keys: str | bytes) -> None:
        data = keys.encode() if isinstance(keys, str) else keys
        deadline = time.monotonic() + 5.0
        while data:
            if time.monotonic() >= deadline or self.poll() is not None:
                raise AssertionError(f"Cannot send keys to gclient; exit={self.poll()}")
            if select.select([], [self.fd], [], 0.05)[1]:
                data = data[os.write(self.fd, data) :]

    def chord(self, key: str) -> None:
        self.send("\x02")
        self.wait_for(in_prefix_mode, description="prefix mode", timeout=3.0)
        self.send(key)
        self.wait_for(
            lambda screen: not in_prefix_mode(screen),
            description="prefix action completed",
            timeout=3.0,
        )

    def resize(self, cols: int, rows: int) -> None:
        self.screen.resize(cols, rows)
        termios.tcsetwinsize(self.fd, (rows, cols))
        os.kill(self.pid, signal.SIGWINCH)

    def wait_exit(self, timeout: float = 10.0) -> int:
        deadline = time.monotonic() + timeout
        while self.poll() is None and time.monotonic() < deadline:
            self.read(0.05)
        if self.poll() is None:
            raise AssertionError(f"gclient did not exit\n{self.screen.text}")
        while not self.eof and time.monotonic() < deadline:
            self.read(0.01)
        assert self.exit_code is not None
        return self.exit_code

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            if self.poll() is None:
                os.kill(self.pid, signal.SIGTERM)
                try:
                    self.wait_exit(5.0)
                except AssertionError:
                    os.kill(self.pid, signal.SIGKILL)
                    _, status = os.waitpid(self.pid, 0)
                    self.exit_code = os.waitstatus_to_exitcode(status)
        finally:
            os.close(self.fd)
            self.fd = -1

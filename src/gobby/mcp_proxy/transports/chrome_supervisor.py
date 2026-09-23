"""Bounded cleanup for the Chrome DevTools MCP subprocess and its browser."""

import os
import signal
import subprocess
import sys
import threading
import time

import psutil


def main() -> int:
    if len(sys.argv) < 2:
        return 2

    parent_pid = os.getppid()
    stop_requested = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    child = subprocess.Popen(
        sys.argv[1:], stdin=subprocess.PIPE, stdout=sys.stdout, stderr=sys.stderr, bufsize=0
    )

    def forward_stdin() -> None:
        assert child.stdin is not None
        try:
            while chunk := os.read(sys.stdin.fileno(), 65536):
                child.stdin.write(chunk)
        except (BrokenPipeError, OSError):
            pass
        finally:
            child.stdin.close()
            stop_requested.set()

    threading.Thread(target=forward_stdin, daemon=True).start()
    process = psutil.Process(child.pid)
    owned: dict[int, psutil.Process] = {}
    try:
        while child.poll() is None and not stop_requested.is_set() and os.getppid() == parent_pid:
            try:
                owned.update(
                    {descendant.pid: descendant for descendant in process.children(recursive=True)}
                )
            except psutil.NoSuchProcess:
                break
            time.sleep(1)
    finally:
        try:
            owned.update(
                {descendant.pid: descendant for descendant in process.children(recursive=True)}
            )
        except psutil.NoSuchProcess:
            pass
        targets = [*owned.values(), process]
        for target in targets:
            try:
                target.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(targets, timeout=2)
        for target in alive:
            try:
                target.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(alive, timeout=2)
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=1)
    return 0 if stop_requested.is_set() or os.getppid() != parent_pid else child.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())

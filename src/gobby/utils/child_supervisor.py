"""Bounded cleanup for a daemon's stdio child and its process tree.

The child inherits this process's stdin, stdout and stderr, so no bytes pass
through here. The tree is reaped when the child exits, when this process is
asked to stop, or when its parent dies, so a SIGKILLed runner leaves no orphan.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil


def supervised_argv(argv: list[str], *, stop_file: Path | None = None) -> list[str]:
    """Run ``argv`` under this supervisor; ``-P`` keeps the child's cwd off ``sys.path``."""
    control = [f"--stop-file={stop_file}"] if stop_file is not None else []
    return [sys.executable, "-P", "-m", "gobby.utils.child_supervisor", *control, *argv]


def main() -> int:
    if len(sys.argv) < 2:
        return 2

    argv = sys.argv[1:]
    stop_file = None
    if argv[0].startswith("--stop-file="):
        stop_file = Path(argv.pop(0).split("=", 1)[1])
    if not argv:
        return 2
    parent_pid = os.getppid()
    stop_requested = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    owns_process_group = stop_file is not None and os.name == "posix"
    child = subprocess.Popen(argv, start_new_session=owns_process_group)
    process = psutil.Process(child.pid)
    owned: dict[int, psutil.Process] = {}
    try:
        while child.poll() is None and not stop_requested.is_set() and os.getppid() == parent_pid:
            if stop_file is not None and not stop_file.exists():
                stop_requested.set()
                break
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
        if owns_process_group:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for target in targets:
            try:
                target.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(targets, timeout=2)
        if owns_process_group:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
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

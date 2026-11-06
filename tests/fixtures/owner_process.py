"""Real owner process trees for the graceful owner-end tests.

An owner stands in for a CLI. Its shell, a child that leads its own process group and session
as the CLI's Bash shell does, starts background jobs that inherit that group. A holder stands
in for a process outside the owner's jobs that keeps a background call's output file open.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

_SLEEP = "import time; time.sleep(600)"

SHELL_SCRIPT = (
    "import subprocess, sys, time\n"
    f"subprocess.Popen([sys.executable, '-c', {_SLEEP!r}])\n"
    "time.sleep(600)\n"
)
OWNER_SCRIPT = (
    "import subprocess, sys, time\n"
    f"subprocess.Popen([sys.executable, '-c', {SHELL_SCRIPT!r}], start_new_session=True)\n"
    "time.sleep(600)\n"
)


def wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10.0
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert predicate(), "timed out waiting for a test process to reach its state"


def live_processes(processes: list[psutil.Process]) -> list[psutil.Process]:
    """Processes still running; a zombie has exited and counts as gone."""
    alive: list[psutil.Process] = []
    for process in processes:
        try:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                alive.append(process)
        except psutil.NoSuchProcess:
            continue
    return alive


def _context(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    owner = psutil.Process(process.pid)
    return {
        "parent_pid": process.pid,
        "parent_create_time": owner.create_time(),
        "parent_name": owner.name(),
    }


def start_owner() -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    """An owner whose shell leads a group that holds one job and the job's child."""
    process = subprocess.Popen([sys.executable, "-c", OWNER_SCRIPT])
    owner = psutil.Process(process.pid)
    wait_for(lambda: len(owner.children(recursive=True)) >= 2)
    return process, _context(process)


def start_owner_with_orphan(pid_file: Path) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    """An owner whose job starts a child and exits, so the child is reparented.

    The child writes its pid to ``pid_file``. Once the job has exited, the child is no longer a
    descendant of the owner, though it still belongs to the shell's group.
    """
    job = (
        "import subprocess, sys\n"
        f"grandchild = subprocess.Popen([sys.executable, '-c', {_SLEEP!r}])\n"
        f"open({str(pid_file)!r}, 'w').write(str(grandchild.pid))\n"
    )
    shell = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {job!r}]).wait()\n"
        "time.sleep(600)\n"
    )
    owner_script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {shell!r}], start_new_session=True)\n"
        "time.sleep(600)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", owner_script])
    wait_for(lambda: pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() != "")
    return process, _context(process)


def start_owner_without_a_job_group() -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    """An owner whose job shares the owner's own group, which the owner does not lead."""
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {_SLEEP!r}])\n"
        "time.sleep(600)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    owner = psutil.Process(process.pid)
    wait_for(lambda: bool(owner.children(recursive=True)))
    return process, _context(process)


def stop_owner(process: subprocess.Popen[bytes]) -> None:
    """Kill the owner and whatever of its tree is still running."""
    if process.poll() is None:
        descendants = psutil.Process(process.pid).children(recursive=True)
        process.kill()
        for child in descendants:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                continue
    process.wait(timeout=10)


def start_holder(output: Path) -> subprocess.Popen[bytes]:
    """Start a process outside any owner tree that holds ``output`` open for appending."""
    output.touch()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import time\nhandle = open({str(output)!r}, 'a')\ntime.sleep(600)\n",
        ]
    )

    def holds_output() -> bool:
        try:
            return any(
                item.path == str(output) for item in psutil.Process(process.pid).open_files()
            )
        except psutil.Error:
            return False

    wait_for(holds_output)
    return process


def stop_holder(process: subprocess.Popen[bytes]) -> None:
    process.kill()
    process.wait(timeout=10)


def start_unrelated_process(*, own_session: bool) -> subprocess.Popen[bytes]:
    """A same-user process that belongs to no owner's group; ``own_session`` leads its own."""
    return subprocess.Popen([sys.executable, "-c", _SLEEP], start_new_session=own_session)


def start_owner_with_orphaned_group(
    go_file: Path, pid_file: Path
) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    """An owner whose job shell exits once ``go_file`` exists, leaving its grandchild running.

    The shell leads a group and a session. Its grandchild writes its pid to ``pid_file``. When
    the shell exits, the grandchild is reparented, but it still belongs to the shell's group.
    """
    grandchild = (
        f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(600)\n"
    )
    shell = (
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        f"while not os.path.exists({str(go_file)!r}):\n"
        "    time.sleep(0.05)\n"
    )
    owner = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {shell!r}], start_new_session=True)\n"
        "time.sleep(600)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", owner])
    wait_for(lambda: pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() != "")
    return process, _context(process)

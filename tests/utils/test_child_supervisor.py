"""The child supervisor reaps its child's tree across restarts."""

import select
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from gobby.utils.child_supervisor import supervised_argv

pytestmark = pytest.mark.unit


def test_supervisor_reaps_child_tree_before_restart(tmp_path: Path) -> None:
    child_script = tmp_path / "spawn_browser.py"
    child_script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    for _attempt in range(2):
        supervisor = subprocess.Popen(
            supervised_argv([sys.executable, str(child_script)]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert supervisor.stdout is not None
            ready, _, _ = select.select([supervisor.stdout], [], [], 5)
            assert ready, "supervisor did not launch the browser child"
            browser_pid = int(supervisor.stdout.readline())
            assert psutil.pid_exists(browser_pid)
            browser = psutil.Process(browser_pid)

            # The MCP SDK's teardown stops the supervisor with SIGTERM.
            supervisor.terminate()
            assert supervisor.wait(timeout=5) == 0
            _, alive = psutil.wait_procs([browser], timeout=5)
            assert not alive
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=5)

"""Existing detached restart handoff for Windows' Python-owned daemon."""

import os
import subprocess
import sys
import time


def spawn_restart_helper(pid: int, port: int) -> None:
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from gobby.runner_windows_restart import restart_after_exit; "
            "import sys; restart_after_exit(int(sys.argv[1]), int(sys.argv[2]))",
            str(pid),
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )


def restart_after_exit(pid: int, port: int) -> None:
    from gobby.cli.installers.service import get_service_status, service_restart
    from gobby.cli.utils import _is_process_alive, get_gobby_home
    from gobby.runner import _healthy_daemon_running

    deadline = time.monotonic() + 30.0
    while _is_process_alive(pid):
        if time.monotonic() >= deadline:
            raise RuntimeError("Windows daemon did not exit for restart")
        time.sleep(0.1)
    service = get_service_status()
    if service.get("installed") and service.get("enabled"):
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if _healthy_daemon_running(port):
                return
            time.sleep(0.25)
        result = service_restart(shutdown_source="http_restart")
        if not result.get("success"):
            raise RuntimeError(str(result.get("error")))
        return
    time.sleep(2.0)
    log_dir = get_gobby_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "admin-restart.log").open("a") as log:
        subprocess.Popen(
            [sys.executable, "-m", "gobby.runner"],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )

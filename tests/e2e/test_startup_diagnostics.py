"""Prove startup diagnostics through the real isolated daemon front door."""

import os
import select
import subprocess
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO

import pytest

from tests.e2e import conftest
from tests.e2e.startup_delay_bootstrap import ENTERED_FILE, RELEASE_FILE, RELEASED_FILE

pytestmark = pytest.mark.e2e


def test_delayed_skill_search_names_readiness_blocker(
    e2e_project_dir: Path,
    e2e_config: tuple[Path, int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = e2e_config[0].parent
    real_wait_for_health = conftest.wait_for_daemon_health
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    initializer_entered = False
    initializer_released = False

    with ExitStack() as channels:

        def sync_channel(name: str) -> BinaryIO:
            path = home / name
            os.mkfifo(path, mode=0o600)
            # Keeping both ends open prevents EOF before the child connects.
            return channels.enter_context(
                os.fdopen(os.open(path, os.O_RDWR | os.O_NONBLOCK), "r+b", buffering=0)
            )

        entered = sync_channel(ENTERED_FILE)
        release = sync_channel(RELEASE_FILE)
        released = sync_channel(RELEASED_FILE)

        def wait_for_delayed_health(
            port: int,
            *,
            log_file: Path | None = None,
            process: subprocess.Popen[bytes] | None = None,
        ) -> None:
            nonlocal initializer_entered, initializer_released
            ready, _, _ = select.select([entered], [], [], 45.0)
            initializer_entered = bool(ready) and entered.read(1) == b"1"
            try:
                real_wait_for_health(
                    port, log_file=log_file, process=process, timeout=2.0, min_attempts=2
                )
            finally:
                # Release before spawn_daemon_instance tears down on the error.
                release.write(b"1")
                ready, _, _ = select.select([released], [], [], 2.0)
                initializer_released = bool(ready) and released.read(1) == b"1"

        monkeypatch.setattr(conftest, "wait_for_daemon_health", wait_for_delayed_health)
        daemon = conftest.spawn_daemon_instance(
            e2e_project_dir,
            e2e_config,
            runner_module="tests.e2e.startup_delay_bootstrap",
        )
        try:
            with pytest.raises(conftest.DaemonHealthTimeoutError) as exc_info:
                next(daemon)
            error = exc_info.value
            assert initializer_entered, "Genuine registry init never reached the injected delay"
            assert error.last_status_code == 503
            assert error.process_status == "running"
            assert error.backend_state in ("down", "starting")
            assert "delayed_skill_search" in error.thread_stack_tail
            assert "run_gobby" in error.task_stack_tail
            assert '"stage": "HTTP MCP setup"' in error.startup_timing_tail
            assert "Startup step skills search started" in error.mcp_log_tail
            assert "Startup step skills search completed" not in error.mcp_log_tail
            assert "Startup step skills search started" in str(error)
            assert initializer_released, "Initializer was not released before daemon teardown"
            step_line = next(
                line
                for line in error.mcp_log_tail.splitlines()
                if "Startup step skills search started" in line
            )
            print(
                f"Readiness trace: HTTP {error.last_status_code}, "
                f"blocked for {error.elapsed_seconds:.3f}s; {step_line}; "
                "no skills search completion; initializer released before teardown"
            )
        finally:
            release.write(b"1")
            daemon.close()

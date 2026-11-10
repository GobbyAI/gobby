"""Interpreter exit after a shutdown deadline overrun (#25838).

A transcript-evidence pool worker and a non-agent child are alive when the async
shutdown cleanup runs past its overall deadline. The daemon must still reap both so
the interpreter can finish exiting. Before the fix, interpreter finalization joined
the live pool worker without bound.

The check runs in a child interpreter with an isolated ``GOBBY_HOME`` so it never
touches the daemon or the user's real state.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

EXIT_BOUND_SECONDS = 90.0
# Allowance for the script to reach shutdown: starting a pool worker can take long on a
# loaded host. The exit clock starts only once shutdown has begun.
STARTUP_ALLOWANCE_SECONDS = 240.0

_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import os
    import subprocess
    import sys
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import psutil

    import gobby.agents.sandbox_policy as sandbox_policy
    import gobby.runner_lifecycle_processes as runner_lifecycle_processes
    import gobby.runner_lifecycle_shutdown as runner_lifecycle_shutdown
    import gobby.telemetry.rule_allow_audit as rule_allow_audit
    from gobby.config.bootstrap import BootstrapConfig
    from gobby.shutdown_intent import ShutdownIntent
    from gobby.tasks.transcript_evidence_pool import run_in_transcript_evidence_pool

    async def stalled_step() -> None:
        # A cleanup step that never settles, so the overall deadline cuts the cleanup.
        await asyncio.Event().wait()

    async def no_op(*_args, **_kwargs) -> None:
        return None

    async def main() -> None:
        # One derivation starts a live pool worker; prewarm's barrier is not needed here.
        worker_pid = await run_in_transcript_evidence_pool(os.getpid)
        if worker_pid == os.getpid():
            raise SystemExit("derivation did not run in a pool worker")
        print(f"pool-worker={worker_pid}", flush=True)
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"sleeper={sleeper.pid}", flush=True)
        runner_lifecycle_shutdown._OVERALL_SHUTDOWN_DEADLINE_SECONDS = 2.0
        runner_lifecycle_shutdown._run_graceful_shutdown_sequence = no_op
        rule_allow_audit.shutdown_rule_allow_audit = stalled_step
        sandbox_policy.shutdown_pre_commit_store_spare = lambda timeout=None: None
        runner = SimpleNamespace(
            bootstrap_config=BootstrapConfig(),
            _shutdown_intent=ShutdownIntent.STOP,
            agent_runner=None,
            http_server=SimpleNamespace(
                services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
                _terminate_streamable_http_sessions=AsyncMock(),
            ),
            wake_dispatcher=SimpleNamespace(
                reconcile_restart_active_sessions=AsyncMock(return_value=[])
            ),
            lifecycle_manager=SimpleNamespace(stop=AsyncMock()),
            agent_lifecycle_monitor=None,
            cron_scheduler=None,
            message_processor=None,
            communications_manager=None,
            config=SimpleNamespace(ui=SimpleNamespace(enabled=False, mode="production")),
            startup_config=SimpleNamespace(ui=SimpleNamespace(enabled=False, mode="production")),
            memory_manager=None,
            vector_store=None,
            mcp_proxy=SimpleNamespace(disconnect_all=AsyncMock()),
            database=SimpleNamespace(close=MagicMock()),
        )
        server = SimpleNamespace(should_exit=False)

        async def completed_server() -> None:
            return None

        print("shutdown-started", flush=True)
        # Inject a cold-import delay here, after all startup work and before the clock.
        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            server,
            asyncio.create_task(completed_server()),
            1,
            await_critical_stop_hook_grace_window=no_op,
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=runner_lifecycle_processes._reap_remaining_child_processes,
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )
        if psutil.pid_exists(sleeper.pid):
            raise SystemExit(f"non-agent child {sleeper.pid} survived the deadline reap")
        print("reaped", flush=True)

    asyncio.run(main())
    """
)


def _kill_sleeper(output: str) -> None:
    """Kill the child the script reports, so a failing run leaves nothing behind."""
    for line in output.splitlines():
        if line.startswith("sleeper="):
            try:
                os.kill(int(line.removeprefix("sleeper=")), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


def _wait_for_shutdown_start(
    proc: subprocess.Popen[bytes], stdout_path: Path, stderr_path: Path
) -> None:
    """Block until the script reaches shutdown. Prewarm is setup, not the bound under test."""
    deadline = time.monotonic() + STARTUP_ALLOWANCE_SECONDS
    while "shutdown-started" not in stdout_path.read_text(encoding="utf-8"):
        if proc.poll() is not None:
            stderr = stderr_path.read_text(encoding="utf-8")
            pytest.fail(f"script exited before shutdown: {stderr[-2000:]}")
        if time.monotonic() > deadline:
            pytest.fail("script did not reach shutdown within the startup allowance")
        time.sleep(0.1)


@pytest.mark.parametrize("slow_cold_import", [False, True], ids=["normal", "slow-cold-import"])
def test_interpreter_exits_after_deadline_overrun_with_live_pool_worker(
    tmp_path: Path, slow_cold_import: bool
) -> None:
    env = {**os.environ, "GOBBY_HOME": str(tmp_path / "gobby-home")}
    script = _SCRIPT
    if slow_cold_import:
        # A loaded host can spend the entire fallback budget importing the initialization
        # facade. Teardown must not initialize serving leases that never existed.
        injection = textwrap.indent(
            textwrap.dedent(
                """
                import builtins
                import time
                from gobby.shutdown_budget import DEADLINE_CHILD_REAP_BUDGET_SECONDS

                original_import = builtins.__import__

                def delayed_import(name, *args, **kwargs):
                    if name == "gobby.runner_init.embedding_lease" and name not in sys.modules:
                        started = time.monotonic()
                        time.sleep(DEADLINE_CHILD_REAP_BUDGET_SECONDS)
                        print(f"cold-import-delay={time.monotonic() - started:.3f}s", flush=True)
                    return original_import(name, *args, **kwargs)

                builtins.__import__ = delayed_import
                print("cold-import-guard-installed", flush=True)
                """
            ),
            "    ",
        )
        script = script.replace(
            "    # Inject a cold-import delay here, after all startup work and before the clock.",
            injection,
        )
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    with stdout_path.open("w", encoding="utf-8") as out:
        with stderr_path.open("w", encoding="utf-8") as err:
            proc = subprocess.Popen([sys.executable, "-c", script], stdout=out, stderr=err, env=env)
            try:
                _wait_for_shutdown_start(proc, stdout_path, stderr_path)
                started = time.monotonic()
                try:
                    proc.wait(timeout=EXIT_BOUND_SECONDS)
                except subprocess.TimeoutExpired:
                    pytest.fail(
                        "interpreter did not exit within "
                        f"{EXIT_BOUND_SECONDS:.0f}s after shutdown started"
                    )
                elapsed = time.monotonic() - started
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                _kill_sleeper(stdout_path.read_text(encoding="utf-8"))
    output = stdout_path.read_text(encoding="utf-8")
    assert proc.returncode == 0, stderr_path.read_text(encoding="utf-8")[-2000:]
    assert "reaped" in output
    assert elapsed < EXIT_BOUND_SECONDS
    if slow_cold_import:
        assert "cold-import-guard-installed" in output
        assert "cold-import-delay=" not in output


def test_shutdown_disposes_loaded_leases_before_stopping_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.runner_background_services import shutdown_background_services
    from gobby.runner_init import embedding_lease
    from gobby.tasks import transcript_evidence_pool

    stopped: list[str] = []

    def dispose_leases() -> None:
        stopped.append("leases")

    def shutdown_pool() -> None:
        stopped.append("pool")

    monkeypatch.setattr(embedding_lease, "dispose_active_embedding_leases", dispose_leases)
    monkeypatch.setattr(
        transcript_evidence_pool, "shutdown_transcript_evidence_pool", shutdown_pool
    )

    shutdown_background_services()

    assert stopped == ["leases", "pool"]

"""Fixture-only stack signals and startup timing around the genuine runner."""

from __future__ import annotations

import asyncio
import faulthandler
import json
import os
import runpy
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from functools import wraps
from pathlib import Path
from types import FrameType
from typing import Any, TextIO
from unittest.mock import patch

import psutil


@contextmanager
def watch_pytest_parent() -> Iterator[None]:
    """Stop this detached runner when its identity-bound fixture owner dies."""
    owner_pid = int(os.environ["GOBBY_E2E_OWNER_PID"])
    owner_created = float(os.environ["GOBBY_E2E_OWNER_CREATE_TIME"])
    stopped = threading.Event()

    def owner_alive() -> bool:
        try:
            owner = psutil.Process(owner_pid)
            return bool(
                owner.create_time() == owner_created
                and owner.is_running()
                and owner.status() != psutil.STATUS_ZOMBIE
            )
        except psutil.NoSuchProcess:
            return False

    def watch() -> None:
        while owner_alive():
            if stopped.wait(0.1):
                return
        if stopped.is_set():
            return
        os.kill(os.getpid(), signal.SIGTERM)
        # Allow the runner's bounded shutdown before enforcing parent death.
        if not stopped.wait(20):
            os._exit(1)

    watcher = threading.Thread(target=watch, name="pytest-parent-watch", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stopped.set()
        watcher.join(timeout=1)


@contextmanager
def stage_timings(stream: TextIO) -> Iterator[Callable[[str], AbstractContextManager[None]]]:
    started_at = time.monotonic()
    lock = threading.Lock()

    def record(stage: str, state: str, duration: float | None = None) -> None:
        # Function names, elapsed time and PID only; never arguments or errors.
        payload = {
            "pid": os.getpid(),
            "stage": stage,
            "state": state,
            "at_seconds": round(time.monotonic() - started_at, 6),
            "duration_seconds": duration,
        }
        with lock:
            stream.write(json.dumps(payload) + "\n")
            stream.flush()

    @contextmanager
    def stage(name: str) -> Iterator[None]:
        start = time.monotonic()
        record(name, "started")
        try:
            yield
        except BaseException:
            record(name, "failed", time.monotonic() - start)
            raise
        else:
            record(name, "completed", time.monotonic() - start)

    yield stage


@contextmanager
def instrument_startup(
    stage: Callable[[str], AbstractContextManager[None]],
) -> Iterator[None]:
    with stage("diagnostic imports"):
        import uvicorn

        from gobby import runner_init, runner_lifecycle, runner_service_readiness
        from gobby.events.coordination_waits import CoordinationWaitService
        from gobby.runner_gate import acquire_runner_gate
        from gobby.servers import app_factory
        from gobby.servers.http import HTTPServer
        from gobby.tasks import transcript_evidence_pool

    def sync(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def timed(*args: Any, **kwargs: Any) -> Any:
            with stage(name):
                return function(*args, **kwargs)

        return timed

    def async_(name: str, function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(function)
        async def timed(*args: Any, **kwargs: Any) -> Any:
            with stage(name):
                return await function(*args, **kwargs)

        return timed

    with ExitStack() as patches:
        patches.enter_context(
            patch.object(HTTPServer, "__init__", sync("HTTP construction", HTTPServer.__init__))
        )
        patches.enter_context(
            patch.object(
                HTTPServer,
                "_init_mcp_subsystems",
                sync("HTTP MCP setup", HTTPServer._init_mcp_subsystems),
            )
        )
        patches.enter_context(
            patch.object(
                app_factory, "create_app", sync("HTTP app construction", app_factory.create_app)
            )
        )
        patches.enter_context(
            patch.object(
                runner_init,
                "init_servers",
                sync("runtime server construction", runner_init.init_servers),
            )
        )
        patches.enter_context(
            patch.object(
                runner_lifecycle,
                "acquire_runner_gate",
                async_("predecessor gate", acquire_runner_gate),
            )
        )
        patches.enter_context(
            patch.object(
                runner_service_readiness,
                "require_managed_services_ready",
                async_(
                    "managed readiness", runner_service_readiness.require_managed_services_ready
                ),
            )
        )
        patches.enter_context(
            patch.object(
                transcript_evidence_pool,
                "prewarm_transcript_evidence_pool",
                async_(
                    "transcript pool prewarm",
                    transcript_evidence_pool.prewarm_transcript_evidence_pool,
                ),
            )
        )
        patches.enter_context(
            patch.object(
                CoordinationWaitService,
                "start",
                async_("coordination listener", CoordinationWaitService.start),
            )
        )
        patches.enter_context(
            patch.object(
                uvicorn.Server, "startup", async_("HTTP bind and lifespan", uvicorn.Server.startup)
            )
        )
        yield


def main() -> None:
    # Executed only by spawn_daemon_instance, with a temporary fixture home.
    if os.environ.get("GOBBY_TEST_PROTECT") != "1":
        raise RuntimeError("Readiness diagnostics require an isolated test process")
    log_dir = Path(os.environ["GOBBY_HOME"]) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    module = sys.argv.pop(1)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    with (
        watch_pytest_parent(),
        (log_dir / "startup-threads.log").open("w") as threads,
        (log_dir / "startup-tasks.log").open("w") as tasks,
        (log_dir / "startup-timings.jsonl").open("w") as timings,
        stage_timings(timings) as stage,
    ):

        def dump_tasks(_signal: int, _frame: FrameType | None) -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                tasks.write("<no running asyncio loop in signal thread>\n")
            else:
                for number, task in enumerate(asyncio.all_tasks(loop)):
                    tasks.write(f"Task {number}: done={task.done()}\n")
                    for frame in task.get_stack(limit=20):
                        tasks.write(
                            f"  {frame.f_code.co_filename}:{frame.f_lineno} "
                            f"{frame.f_code.co_name}\n"
                        )
            tasks.flush()
            (log_dir / "readiness-tasks.ready").write_text(str(os.getpid()))

        previous = signal.signal(signal.SIGUSR1, dump_tasks)
        faulthandler.register(signal.SIGUSR2, file=threads, all_threads=True)
        marker = log_dir / "readiness-diagnostics.pid"
        (log_dir / "readiness-tasks.ready").unlink(missing_ok=True)
        marker.write_text(str(os.getpid()))
        try:
            with instrument_startup(stage), stage("runner execution"):
                runpy.run_module(module, run_name="__main__")
        finally:
            marker.unlink(missing_ok=True)
            faulthandler.unregister(signal.SIGUSR2)
            signal.signal(signal.SIGUSR1, previous)


if __name__ == "__main__":
    main()

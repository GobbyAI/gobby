"""Fixture-only stack signals and startup timing around the genuine runner."""

from __future__ import annotations

import asyncio
import builtins
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
from importlib.util import resolve_name
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
        except psutil.Error:
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
    """Wrap boundaries after their modules load; diagnostics never preload them."""
    boundaries: dict[str, tuple[tuple[str, str, bool], ...]] = {
        "gobby.servers.http": (
            ("HTTPServer.__init__", "HTTP construction", False),
            ("HTTPServer._init_mcp_subsystems", "HTTP MCP setup", False),
        ),
        "gobby.servers.app_factory": (("create_app", "HTTP app construction", False),),
        "gobby.runner_init.servers": (("init_servers", "runtime server construction", False),),
        "gobby.runner_gate": (("acquire_runner_gate", "predecessor gate", True),),
        "gobby.runner_service_readiness": (
            ("require_managed_services_ready", "managed readiness", True),
        ),
        "gobby.tasks.transcript_evidence_pool": (
            ("prewarm_transcript_evidence_pool", "transcript pool prewarm", True),
        ),
        "gobby.events.coordination_waits": (
            ("CoordinationWaitService.start", "coordination listener", True),
        ),
        "uvicorn.server": (("Server.startup", "HTTP bind and lifespan", True),),
    }

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
        wrapped: set[int] = set()

        def attach(module_name: str) -> None:
            for qualified_name, label, is_async in boundaries.get(module_name, ()):
                target: Any = sys.modules.get(module_name)
                members = qualified_name.split(".")
                for member in members[:-1]:
                    if target is None:
                        break
                    target = vars(target).get(member)
                if target is None:
                    continue
                function = vars(target).get(members[-1])
                if not callable(function) or id(function) in wrapped:
                    continue
                timed = async_(label, function) if is_async else sync(label, function)
                patches.enter_context(patch.object(target, members[-1], timed))
                wrapped.add(id(timed))

        for name in boundaries:
            attach(name)
        original_import = builtins.__import__

        def imported(
            name: str,
            globals: dict[str, Any] | None = None,
            locals: dict[str, Any] | None = None,
            fromlist: tuple[str, ...] | list[str] | None = (),
            level: int = 0,
        ) -> Any:
            module = original_import(name, globals, locals, fromlist, level)
            package = (globals or {}).get("__package__")
            resolved = resolve_name("." * level + name, package) if level and package else name
            if resolved in boundaries:
                attach(resolved)
            for member in fromlist or ():
                child = f"{resolved}.{member}"
                if child in boundaries:
                    attach(child)
            return module

        # Import returns precede copying `from` exports into the caller. Observe
        # those boundaries only, leaving ordinary runtime calls uninstrumented.
        patches.enter_context(patch.object(builtins, "__import__", imported))
        yield


def main() -> None:
    # Executed only by spawn_daemon_instance, with a temporary fixture home.
    if os.environ.get("GOBBY_TEST_PROTECT") != "1":
        raise RuntimeError("Readiness diagnostics require an isolated test process")
    log_dir = Path(os.environ["GOBBY_HOME"]) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    module = sys.argv.pop(1)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.fixtures.external_write_audit import daemon_write_audit

    with (
        daemon_write_audit(),
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
            # Interpreter thread joins happen after this scope. Preserve the
            # threads still alive when runner execution ends, before closing
            # the files and removing the opt-in signal marker.
            threads.write("<runner execution ended; interpreter finalization pending>\n")
            threads.flush()
            faulthandler.dump_traceback(file=threads, all_threads=True)
            # A process-owned descriptor lets the C watchdog report a stuck
            # interpreter join after these context-managed files are closed.
            # Normal exits close it with the process before the watchdog fires.
            faulthandler.dump_traceback_later(10, file=os.dup(threads.fileno()))
            marker.unlink(missing_ok=True)
            faulthandler.unregister(signal.SIGUSR2)
            signal.signal(signal.SIGUSR1, previous)


if __name__ == "__main__":
    main()

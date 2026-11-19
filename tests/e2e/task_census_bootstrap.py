"""Census bootstrap: the genuine runner under the task-census recorder (WP-1; design-v2 5.16, 5.17).

The e2e route reaches this module through ``readiness_bootstrap.py``'s ``runpy``. The runner is
entered through ``gobby.runner._standalone_main`` directly, so the patches apply to the one
``GobbyRunner`` class the daemon uses. The module refuses to run outside the isolated test hub.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import inspect
import json
import os
import sys
import threading
import weakref
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any, TextIO
from unittest.mock import patch
from urllib.parse import urlsplit

if sys.platform != "win32":
    import pwd

TEST_HUB_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
TEST_HUB_USER = "gobby_test"
TEST_HUB_PASSWORD = "gobby_test"
TEST_HUB_PORT = 60892
TEST_HUB_DATABASE = "gobby_test"
ISOLATED_DATABASE_PREFIX = "gobby_test_"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
BOOTSTRAP_FILE = "bootstrap.yaml"
DATABASE_URL_KEY = b"database_url:"
REPO = Path(__file__).resolve().parents[2]
CENSUS_LOG = Path("logs") / "task-census.jsonl"
# Switches read by the test process that records the census; they are not part of a boot.


def database_url_is_test_hub(url: str | None) -> bool:
    """Whether a DSN names the isolated test hub (R57).

    The parts are compared, not the string: the user, password, loopback host (127.0.0.1 or
    localhost), port 60892, and a database that is ``gobby_test`` or an isolated
    ``gobby_test_*`` database. The query string is ignored, because the e2e bootstrap adds a
    search-path option to it.
    """
    if not url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    database = parts.path.removeprefix("/")
    return (
        parts.scheme == "postgresql"
        and parts.username == TEST_HUB_USER
        and parts.password == TEST_HUB_PASSWORD
        and parts.hostname in LOOPBACK_HOSTS
        and port == TEST_HUB_PORT
        and (database == TEST_HUB_DATABASE or database.startswith(ISOLATED_DATABASE_PREFIX))
    )


def bootstrap_database_url(home: Path) -> str | None:
    """The value of the top-level ``database_url:`` line in ``home``'s bootstrap.yaml.

    Only that line is decoded. Every other line is skipped as raw bytes, so no other field of
    the file is read or parsed, and an undecodable line elsewhere cannot stop the check.
    """
    path = home / BOOTSTRAP_FILE
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.startswith(DATABASE_URL_KEY):
                continue
            try:
                value = raw[len(DATABASE_URL_KEY) :].decode("utf-8").strip()
            except UnicodeDecodeError:
                return None
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            return value or None
    return None


def unsafe_reason(env: Mapping[str, str], *, operator_home: Path) -> str | None:
    """Why the census must refuse this environment, or None when the boot is safe.

    Checked in order: the test guard, the DATABASE_URL, GOBBY_HOME (set, and not the operator
    home or its ``.gobby`` directory), then the home's bootstrap.yaml, which is opened only
    after the home check passes. Reasons never echo a URL.
    """
    if env.get("GOBBY_TEST_PROTECT") != "1":
        return "task census refused: GOBBY_TEST_PROTECT=1 is required"
    if not database_url_is_test_hub(env.get("DATABASE_URL")):
        return "task census refused: DATABASE_URL must be the isolated test hub URL"
    home_value = env.get("GOBBY_HOME")
    if not home_value:
        return "task census refused: GOBBY_HOME must name an isolated home"
    home = Path(home_value).resolve()
    operator = operator_home.resolve()
    live = operator / ".gobby"
    if home == operator or home == live or home.is_relative_to(live):
        return "task census refused: GOBBY_HOME is the operator home"
    url = bootstrap_database_url(home)
    if url is None:
        return "task census refused: GOBBY_HOME/bootstrap.yaml names no database_url"
    if not database_url_is_test_hub(url):
        return "task census refused: GOBBY_HOME/bootstrap.yaml database_url is not the test hub"
    return None


def real_home() -> Path:
    """The operator's home from the account database, never from HOME.

    The e2e harness rewrites HOME to the isolated gobby home (so ~/.gobby resolves inside the
    temp dir), so HOME cannot name the operator. The passwd entry is not affected by HOME.
    """
    if sys.platform == "win32":
        return Path.home()
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def refuse_unsafe_environment() -> None:
    """Refuse without the test guard or outside the isolated test hub (critique F4)."""
    reason = unsafe_reason(os.environ, operator_home=real_home())
    if reason is not None:
        sys.exit(reason)


def _caller_frame() -> FrameType | None:
    """The caller of the wrapper that calls this helper: the frame that created the object."""
    here = inspect.currentframe()
    wrapper = here.f_back if here is not None else None
    return wrapper.f_back if wrapper is not None else None


def _stack(start: FrameType | None) -> list[tuple[str, str]]:
    stack: list[tuple[str, str]] = []
    frame = start
    while frame is not None:
        stack.append((str(frame.f_globals.get("__name__", "")), frame.f_code.co_qualname))
        frame = frame.f_back
    return stack


def _nearest_gobby_line(start: FrameType | None, recorder: ModuleType) -> int:
    frame = start
    while frame is not None:
        if recorder.is_gobby(str(frame.f_globals.get("__name__", ""))):
            return frame.f_lineno
        frame = frame.f_back
    return 0


def _app_lifecycle_cite(start: FrameType | None, recorder: ModuleType) -> str | None:
    """The innermost frame in the lifespan module, so its start site is the cite (R18)."""
    frame = start
    while frame is not None:
        if frame.f_globals.get("__name__") == recorder.APP_LIFECYCLE_MODULE:
            path = recorder.APP_LIFECYCLE_MODULE.replace(".", "/")
            return f"src/{path}.py:{frame.f_lineno}"
        frame = frame.f_back
    return None


def _coroutine_target(coro: Any) -> tuple[str, str]:
    code = getattr(coro, "cr_code", None)
    if code is None:
        return (type(coro).__module__, type(coro).__qualname__)
    frame = getattr(coro, "cr_frame", None)
    module = frame.f_globals.get("__name__", "") if frame is not None else ""
    return (str(module), str(code.co_qualname))


def _thread_target(thread: threading.Thread) -> tuple[str, str]:
    function = getattr(thread, "_target", None)
    if function is None:
        return (type(thread).__module__, f"{type(thread).__qualname__}.run")
    inner = getattr(function, "func", function)  # unwrap functools.partial
    module = getattr(inner, "__module__", "") or ""
    return (str(module), str(getattr(inner, "__qualname__", type(inner).__qualname__)))


class Census:
    """Writes one boot's census records and tags them with the current stage."""

    def __init__(self, recorder: ModuleType, handle: TextIO) -> None:
        self._rec = recorder
        self._handle = handle
        self._lock = threading.Lock()
        self._stage = "preflight"
        self._executors: weakref.WeakKeyDictionary[concurrent.futures.ThreadPoolExecutor, str] = (
            weakref.WeakKeyDictionary()
        )

    def write(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._handle.write(json.dumps(record, sort_keys=True) + "\n")
            self._handle.flush()

    def set_stage(self, name: str) -> None:
        with self._lock:
            self._stage = name
        self.write({"kind": "stage", "stage": name})

    def _current_stage(self) -> str:
        with self._lock:
            return self._stage

    def _record_creation(
        self,
        kind: str,
        caller: FrameType | None,
        target: tuple[str, str],
        name: str,
        daemon: bool | None = None,
    ) -> None:
        record = self._rec.creation_record(
            kind,
            _stack(caller),
            stage=self._current_stage(),
            target=target,
            name=name,
            line=_nearest_gobby_line(caller, self._rec),
            cite=_app_lifecycle_cite(caller, self._rec),
        )
        if record is None:
            return
        if daemon is not None:
            record["daemon"] = daemon
        self.write(record)

    def install_runtime(self, patches: ExitStack) -> None:
        """Patch the creation points every task, thread and pool passes through."""
        loop = asyncio.BaseEventLoop
        create_task = loop.create_task

        def task_created(self_loop: asyncio.BaseEventLoop, coro: Any, **kwargs: Any) -> Any:
            caller = _caller_frame()
            target = _coroutine_target(coro)
            task = create_task(self_loop, coro, **kwargs)
            self._record_creation("task", caller, target, task.get_name())
            return task

        patches.enter_context(patch.object(loop, "create_task", task_created))

        start = threading.Thread.start

        def thread_started(thread: threading.Thread) -> None:
            self._record_creation(
                "thread",
                _caller_frame(),
                _thread_target(thread),
                thread.name,
                daemon=thread.daemon,
            )
            start(thread)

        patches.enter_context(patch.object(threading.Thread, "start", thread_started))

        pool_init = concurrent.futures.ThreadPoolExecutor.__init__

        def pool_initialized(
            pool: concurrent.futures.ThreadPoolExecutor, *args: Any, **kwargs: Any
        ) -> None:
            pool_init(pool, *args, **kwargs)
            caller = _caller_frame()
            frames = _stack(caller)
            # The loop's default executor is built by run_in_executor itself, so its immediate
            # caller is asyncio. Its gobby frames belong to whichever coroutine first used it.
            loop_default = bool(frames) and frames[0][0] == "asyncio.base_events"
            prefix = kwargs.get("thread_name_prefix", args[1] if len(args) > 1 else "")
            # The argument as passed: a defaulted pool's size depends on the machine's CPU count.
            passed = args[0] if args else kwargs.get("max_workers")
            record = self._rec.executor_record(
                frames,
                prefix=str(prefix),
                max_workers=passed,
                default_loop=loop_default,
                stage=self._current_stage(),
                line=_nearest_gobby_line(caller, self._rec),
            )
            if record is None:
                return
            self._executors[pool] = str(record["creator"])
            self.write(record)

        patches.enter_context(
            patch.object(concurrent.futures.ThreadPoolExecutor, "__init__", pool_initialized)
        )

        submit = concurrent.futures.ThreadPoolExecutor.submit

        def pool_submitted(
            pool: concurrent.futures.ThreadPoolExecutor, fn: Any, /, *args: Any, **kwargs: Any
        ) -> Any:
            creator = self._executors.get(pool)
            if creator is not None:
                record = self._rec.submit_record(creator, _stack(_caller_frame()))
                if record is not None:
                    self.write(record)
            return submit(pool, fn, *args, **kwargs)

        patches.enter_context(
            patch.object(concurrent.futures.ThreadPoolExecutor, "submit", pool_submitted)
        )

    def install_runner(self, patches: ExitStack, runner_class: Any) -> None:
        """Mark the stage: construct while ``create`` runs, serve once it has returned."""
        create: Callable[..., Any] = runner_class.create

        async def staged_create(*args: Any, **kwargs: Any) -> Any:
            self.set_stage("construct")
            result = await create(*args, **kwargs)
            self.set_stage("serve")
            return result

        patches.enter_context(patch.object(runner_class, "create", staticmethod(staged_create)))


def pin_host_inputs(home: Path, patches: ExitStack, recorder: ModuleType) -> None:
    """Pin the host inputs the boot would otherwise read from this machine.

    The defaulted executor sizes come from the CPU count (finding 1), so the count is fixed
    before any gobby module reads it. The version probe spawns whatever agy the host has
    (finding 2), so a stub agy with a fixed version line stands in for it on PATH. The child
    watcher is chosen when the loop is built, from whether the kernel offers pidfd, so the
    choice is pinned as well. All three are recorded in the census profile so the fixture says
    what the boot ran under.
    """
    patches.enter_context(patch("os.process_cpu_count", return_value=recorder.CENSUS_CPU_COUNT))
    patches.enter_context(
        patch(
            "asyncio.unix_events.can_use_pidfd",
            return_value=recorder.CENSUS_CHILD_WATCHER == "pidfd",
        )
    )
    stub = home / recorder.CENSUS_AGY_STUB
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(f"#!/bin/sh\necho '{recorder.CENSUS_AGY_OUTPUT}'\n", encoding="utf-8")
    stub.chmod(0o755)
    os.environ["PATH"] = os.pathsep.join([str(stub.parent), os.environ.get("PATH", "")])


def main(argv: Sequence[str] | None = None) -> None:
    refuse_unsafe_environment()
    parser = argparse.ArgumentParser(description="Run Gobby daemon under the census recorder")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--config", type=Path, help="Path to config file")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(REPO))
    from tests.e2e import task_census_recorder

    home = Path(os.environ["GOBBY_HOME"])
    (home / "logs").mkdir(parents=True, exist_ok=True)
    with (home / CENSUS_LOG).open("w", encoding="utf-8") as handle, ExitStack() as patches:
        pin_host_inputs(home, patches, task_census_recorder)
        census = Census(task_census_recorder, handle)
        census.write(
            {
                "kind": "environment",
                "gobby_env_keys": sorted(
                    key for key in os.environ if key in task_census_recorder.CENSUS_ENV_ALLOWLIST
                ),
            }
        )
        census.set_stage("preflight")
        census.install_runtime(patches)
        from gobby.runner import GobbyRunner, _standalone_main

        census.install_runner(patches, GobbyRunner)
        _standalone_main(config_path=args.config, verbose=args.verbose)


if __name__ == "__main__":
    main()

"""Keep isolated-runner evidence after its diagnostic signal handlers are removed."""

import asyncio
import builtins
import faulthandler
import os
import runpy
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest

from tests.e2e import readiness_bootstrap


def test_startup_instrumentation_does_not_eagerly_import_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__
    runtime_imports: list[str] = []

    def forbid_runtime_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "uvicorn" or name.startswith("gobby"):
            runtime_imports.append(name)
            raise AssertionError(f"Eager diagnostic import: {name}")
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as import_guard:
        import_guard.setattr(builtins, "__import__", forbid_runtime_import)
        with readiness_bootstrap.instrument_startup(lambda _name: nullcontext()):
            pass
    assert runtime_imports == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "function_name", "definition", "stage_name"),
    [
        (
            "gobby.servers.app_factory",
            "create_app",
            "def create_app(): return 'ready'",
            "HTTP app construction",
        ),
        (
            "gobby.runner_service_readiness",
            "require_managed_services_ready",
            "async def require_managed_services_ready():\n"
            "    await asyncio.sleep(0)\n"
            "    return 'ready'",
            "managed readiness",
        ),
    ],
)
async def test_later_module_execution_retains_startup_timing(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    function_name: str,
    definition: str,
    stage_name: str,
) -> None:
    events: list[tuple[str, str]] = []

    @contextmanager
    def record_stage(name: str) -> Iterator[None]:
        events.append((name, "started"))
        yield
        events.append((name, "completed"))

    imported = ModuleType(module_name)
    imported.__dict__["asyncio"] = asyncio
    with readiness_bootstrap.instrument_startup(record_stage):
        monkeypatch.setitem(sys.modules, module_name, imported)
        exec(compile(definition, "isolated_startup_module.py", "exec"), imported.__dict__)
        function = imported.__dict__[function_name]
        result = function()
        if asyncio.iscoroutine(result):
            result = await result
        assert result == "ready"
        assert events[-2:] == [(stage_name, "started"), (stage_name, "completed")]
    assert sys.getprofile() is None
    assert imported.__dict__[function_name] is not function


@pytest.mark.parametrize("runner_error", [None, RuntimeError("runner failed")])
def test_runner_exit_retains_thread_stacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner_error: RuntimeError | None
) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["bootstrap", "inert_runner"])
    monkeypatch.setattr(readiness_bootstrap, "watch_pytest_parent", nullcontext)
    monkeypatch.setattr(readiness_bootstrap, "instrument_startup", Mock(return_value=nullcontext()))

    # This test runs inside pytest, rather than the isolated bootstrap process.
    # Keep its diagnostic watchdog from outliving the test's files.
    def disable_watchdog(_timeout: float, *, file: int) -> None:
        os.close(file)

    monkeypatch.setattr(faulthandler, "dump_traceback_later", disable_watchdog)
    monkeypatch.setattr(runpy, "run_module", Mock(side_effect=runner_error, return_value={}))

    if runner_error is None:
        readiness_bootstrap.main()
    else:
        with pytest.raises(RuntimeError, match="runner failed"):
            readiness_bootstrap.main()

    log_dir = tmp_path / "logs"
    assert not (log_dir / "readiness-diagnostics.pid").exists()
    thread_stacks = (log_dir / "startup-threads.log").read_text()
    assert "runner execution ended" in thread_stacks
    assert "test_runner_exit_retains_thread_stacks" in thread_stacks

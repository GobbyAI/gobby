"""Tests for nightly code-index repair automation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.code_index.gcode_gateway import GcodeCommandResult
from gobby.code_index.models import IndexedProject
from gobby.code_index.nightly_repair import (
    CODE_INDEX_NIGHTLY_REPAIR_HANDLER,
    CODE_INDEX_NIGHTLY_REPAIR_JOB_NAME,
    CodeIndexNightlyRepairer,
    register_code_index_nightly_repair_cron,
)
from gobby.config.code_index import CodeIndexConfig
from gobby.runtime_grants.launch import ManagedLaunch
from gobby.utils.datetime import resolve_local_timezone

pytestmark = pytest.mark.unit


def _gcode_result(
    command: tuple[str, ...],
    *,
    returncode: int | None = 0,
    stderr: str = "",
    timed_out: bool = False,
    timeout_seconds: float | None = 7200,
) -> GcodeCommandResult:
    return GcodeCommandResult(
        command=command,
        returncode=returncode,
        stdout='{"mode": "repair"}',
        stderr=stderr,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
        duration_seconds=1.0,
        timeout_seconds=timeout_seconds,
        timed_out=timed_out,
    )


class NightlyStorage:
    def __init__(self, projects: list[IndexedProject]) -> None:
        self.projects = projects

    def list_indexed_projects(self) -> list[IndexedProject]:
        return self.projects


class NightlyGateway:
    def __init__(
        self,
        *,
        result: GcodeCommandResult | None = None,
        delay: float = 0,
    ) -> None:
        self.result = result
        self.delay = delay
        self.calls: list[tuple[Path, float | None]] = []
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def nightly_repair(
        self,
        project_root: Path,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> GcodeCommandResult:
        del env
        self.calls.append((project_root, timeout))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.delay:
                await self.release.wait()
            return self.result or _gcode_result(
                (
                    "/tmp/gcode",
                    "repair",
                    "--project",
                    str(project_root),
                    "--format",
                    "json",
                ),
                timeout_seconds=timeout,
            )
        finally:
            self.active -= 1


@contextmanager
def _dummy_launch(project_id: str, *, timeout_seconds: float) -> Iterator[ManagedLaunch]:
    del project_id, timeout_seconds
    yield ManagedLaunch(
        grant_path=Path("/tmp/grant.json"),
        env={"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/tmp/grant.json"},
    )


class DummyLaunchFactory:
    def open(
        self, project_id: str, *, timeout_seconds: float
    ) -> AbstractContextManager[ManagedLaunch]:
        return _dummy_launch(project_id, timeout_seconds=timeout_seconds)


_DEFAULT_LAUNCH_FACTORY = DummyLaunchFactory()


class NightlyContext:
    def __init__(
        self,
        *,
        projects: list[IndexedProject],
        gateway: NightlyGateway | None,
        log_file: Path,
        concurrency: int = 1,
        timeout: int = 7200,
        launch_factory: DummyLaunchFactory | None = _DEFAULT_LAUNCH_FACTORY,
    ) -> None:
        self.storage = NightlyStorage(projects)
        self.gcode_gateway = gateway
        self.launch_factory = launch_factory
        self.config = SimpleNamespace(
            nightly_repair_concurrency=concurrency,
            nightly_repair_timeout_seconds=timeout,
            maintenance_log_file=str(log_file),
        )

    async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def _project(project_id: str, root_path: Path) -> IndexedProject:
    return IndexedProject(
        id=project_id,
        root_path=str(root_path),
        total_files=1,
        total_symbols=1,
    )


@pytest.mark.asyncio
async def test_nightly_repair_runs_per_project_command_and_writes_log(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    gateway = NightlyGateway()
    context = NightlyContext(
        projects=[_project("proj-1", root)],
        gateway=gateway,
        log_file=tmp_path / "maintenance.log",
        timeout=42,
    )
    repairer = CodeIndexNightlyRepairer(context)  # type: ignore[arg-type]

    result = await repairer.run_once()

    assert "completed=1 failed=0 skipped=0" in result
    assert gateway.calls == [(root, 42)]
    log_text = (tmp_path / "maintenance.log").read_text(encoding="utf-8")
    assert '"event": "nightly_repair"' in log_text
    assert '"status": "completed"' in log_text
    assert '"repair"' in log_text


@pytest.mark.asyncio
async def test_nightly_repair_single_flight_skips_concurrent_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    gateway = NightlyGateway(delay=1)
    context = NightlyContext(
        projects=[_project("proj-1", root)],
        gateway=gateway,
        log_file=tmp_path / "maintenance.log",
    )
    repairer = CodeIndexNightlyRepairer(context)  # type: ignore[arg-type]

    first = asyncio.create_task(repairer.run_once())
    await gateway.started.wait()
    second = await repairer.run_once()
    gateway.release.set()
    first_result = await first

    assert second == "Code index nightly repair skipped: already running"
    assert "completed=1" in first_result
    assert gateway.calls == [(root, 7200)]


@pytest.mark.asyncio
async def test_nightly_repair_concurrency_and_timeout_failure(tmp_path: Path) -> None:
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    root_one.mkdir()
    root_two.mkdir()
    gateway = NightlyGateway(
        result=_gcode_result(
            ("/tmp/gcode", "repair"),
            returncode=None,
            stderr="timed out",
            timed_out=True,
        )
    )
    context = NightlyContext(
        projects=[_project("proj-1", root_one), _project("proj-2", root_two)],
        gateway=gateway,
        log_file=tmp_path / "maintenance.log",
        concurrency=1,
        timeout=5,
    )
    repairer = CodeIndexNightlyRepairer(context)  # type: ignore[arg-type]

    result = await repairer.run_once()

    assert "completed=0 failed=2 skipped=0" in result
    assert gateway.calls == [(root_one, 5), (root_two, 5)]
    assert gateway.max_active == 1
    log_text = (tmp_path / "maintenance.log").read_text(encoding="utf-8")
    assert log_text.count('"status": "timed_out"') == 2


@pytest.mark.asyncio
async def test_nightly_repair_isolates_per_project_exception(tmp_path: Path) -> None:
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    root_one.mkdir()
    root_two.mkdir()

    class FailingOnceGateway(NightlyGateway):
        async def nightly_repair(
            self,
            project_root: Path,
            *,
            timeout: float | None = None,
            env: dict[str, str] | None = None,
        ) -> GcodeCommandResult:
            del env
            self.calls.append((project_root, timeout))
            if project_root == root_one:
                raise RuntimeError("boom")
            return _gcode_result(("/tmp/gcode", "repair"), timeout_seconds=timeout)

    gateway = FailingOnceGateway()
    context = NightlyContext(
        projects=[_project("proj-1", root_one), _project("proj-2", root_two)],
        gateway=gateway,
        log_file=tmp_path / "maintenance.log",
    )
    repairer = CodeIndexNightlyRepairer(context)  # type: ignore[arg-type]

    result = await repairer.run_once()

    assert "completed=1 failed=1 skipped=0" in result
    assert gateway.calls == [(root_one, 7200), (root_two, 7200)]


@pytest.mark.asyncio
async def test_nightly_repair_does_not_call_gateway_without_launch_factory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    gateway = NightlyGateway()
    context = NightlyContext(
        projects=[_project("proj-1", root)],
        gateway=gateway,
        log_file=tmp_path / "maintenance.log",
        launch_factory=None,
    )
    repairer = CodeIndexNightlyRepairer(cast(Any, context))

    result = await repairer.run_once()

    assert "skipped: launch factory is not configured" in result
    assert gateway.calls == []


def test_register_nightly_repair_cron_creates_global_system_job() -> None:
    class CronStorage:
        def __init__(self) -> None:
            self.created: dict[str, Any] | None = None

        def get_job_by_name(self, name: str) -> None:
            assert name == CODE_INDEX_NIGHTLY_REPAIR_JOB_NAME
            return None

        def create_job(self, **kwargs: Any) -> None:
            self.created = kwargs

    class CronExecutor:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}

        def register_handler(self, name: str, handler: Any) -> None:
            self.handlers[name] = handler

    storage = CronStorage()
    executor = CronExecutor()
    config = CodeIndexConfig()
    repairer = CodeIndexNightlyRepairer(
        NightlyContext(  # type: ignore[arg-type]
            projects=[], gateway=NightlyGateway(), log_file=Path("/tmp/log")
        )
    )

    register_code_index_nightly_repair_cron(
        cron_storage=storage,  # type: ignore[arg-type]
        cron_executor=executor,
        repairer=repairer,
        config=config,
        project_id="personal",
    )

    assert CODE_INDEX_NIGHTLY_REPAIR_JOB_NAME == "gobby:code-index-nightly-repair"
    assert CODE_INDEX_NIGHTLY_REPAIR_HANDLER == "code-index:nightly-repair"
    assert CODE_INDEX_NIGHTLY_REPAIR_HANDLER in executor.handlers
    assert storage.created is not None
    assert storage.created["name"] == CODE_INDEX_NIGHTLY_REPAIR_JOB_NAME
    assert storage.created["schedule_type"] == "cron"
    assert storage.created["cron_expr"] == "0 2 * * *"
    # Unconfigured schedules read as host-local wall clock, not UTC.
    assert storage.created["timezone"] == resolve_local_timezone()
    assert storage.created["enabled"] is True
    assert storage.created["is_system"] is True
    assert storage.created["action_config"]["handler"] == CODE_INDEX_NIGHTLY_REPAIR_HANDLER
    assert storage.created["action_config"]["timeout_seconds"] == 8 * 60 * 60


def test_register_nightly_repair_cron_reconciles_timeout() -> None:
    class CronStorage:
        def __init__(self) -> None:
            self.reconciled: dict[str, Any] | None = None

        def get_job_by_name(self, name: str) -> SimpleNamespace:
            assert name == CODE_INDEX_NIGHTLY_REPAIR_JOB_NAME
            return SimpleNamespace(id="existing-job", is_system=True)

        def reconcile_system_job_definition(self, job_id: str, **kwargs: Any) -> None:
            assert job_id == "existing-job"
            self.reconciled = kwargs

    class CronExecutor:
        def register_handler(self, _name: str, _handler: Any) -> None:
            pass

    storage = CronStorage()
    repairer = CodeIndexNightlyRepairer(
        NightlyContext(  # type: ignore[arg-type]
            projects=[], gateway=NightlyGateway(), log_file=Path("/tmp/log")
        )
    )

    register_code_index_nightly_repair_cron(
        cron_storage=storage,  # type: ignore[arg-type]
        cron_executor=CronExecutor(),
        repairer=repairer,
        config=CodeIndexConfig(),
        project_id="personal",
    )

    assert storage.reconciled is not None
    assert storage.reconciled["action_config"]["timeout_seconds"] == 8 * 60 * 60

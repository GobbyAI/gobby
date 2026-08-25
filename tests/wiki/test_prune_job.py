from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.gwiki_gateway import GwikiCommandResult, GwikiUnavailableError
from gobby.wiki.prune_job import (
    WIKI_PRUNE_HANDLER,
    WIKI_PRUNE_INTERVAL_SECONDS,
    WIKI_PRUNE_JOB_NAME,
    create_wiki_prune_handler,
    register_wiki_prune_cron,
)

pytestmark = pytest.mark.unit


def _result(
    *,
    returncode: int | None = 0,
    stderr: str = "",
    timed_out: bool = False,
) -> GwikiCommandResult:
    now = datetime.now(UTC).isoformat()
    return GwikiCommandResult(
        command=("gwiki", "prune", "--force"),
        returncode=returncode,
        stdout="prune output",
        stderr=stderr,
        started_at=now,
        completed_at=now,
        duration_seconds=0.1,
        timeout_seconds=120,
        timed_out=timed_out,
    )


class RecordingExecutor:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register_handler(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


class RecordingGateway:
    def __init__(self, result: GwikiCommandResult) -> None:
        self.result = result
        self.timeouts: list[float | None] = []

    async def prune_all_scopes(self, *, timeout: float | None = None) -> GwikiCommandResult:
        self.timeouts.append(timeout)
        return self.result


def test_register_wiki_prune_cron_creates_hourly_system_job() -> None:
    class Storage:
        def __init__(self) -> None:
            self.created: dict[str, Any] | None = None

        def get_job_by_name(self, _name: str) -> None:
            return None

        def create_job(self, **fields: Any) -> None:
            self.created = fields

    storage = Storage()
    executor = RecordingExecutor()
    gateway = RecordingGateway(_result())

    register_wiki_prune_cron(
        cron_storage=storage,  # type: ignore[arg-type]
        cron_executor=executor,
        gateway=gateway,
        project_id="personal",
    )

    assert storage.created is not None
    assert storage.created["name"] == WIKI_PRUNE_JOB_NAME
    assert storage.created["interval_seconds"] == WIKI_PRUNE_INTERVAL_SECONDS
    assert storage.created["action_config"]["handler"] == WIKI_PRUNE_HANDLER
    assert storage.created["is_system"] is True
    assert WIKI_PRUNE_HANDLER in executor.handlers


@pytest.mark.asyncio
async def test_registered_wiki_prune_handler_is_callable() -> None:
    gateway = RecordingGateway(_result())
    handler = create_wiki_prune_handler(gateway)

    result = await handler(SimpleNamespace())  # type: ignore[arg-type]

    assert result["success"] is True
    assert result["status"] == "completed"
    assert gateway.timeouts == [120]


@pytest.mark.parametrize(("enabled", "expected_wakes"), [(False, []), (True, ["wiki-prune"])])
def test_register_wiki_prune_cron_preserves_toggle_and_wakes_only_enabled_rows(
    enabled: bool,
    expected_wakes: list[str],
) -> None:
    existing = SimpleNamespace(
        id="wiki-prune",
        is_system=True,
        enabled=enabled,
        next_run_at=None,
    )

    class Storage:
        def __init__(self) -> None:
            self.definition: dict[str, Any] | None = None
            self.wakes: list[str] = []

        def get_job_by_name(self, _name: str) -> Any:
            return existing

        def reconcile_system_job_definition(self, _job_id: str, **fields: Any) -> Any:
            self.definition = fields
            return existing

        def wake_system_job(self, job_id: str) -> None:
            self.wakes.append(job_id)

    storage = Storage()

    register_wiki_prune_cron(
        cron_storage=storage,  # type: ignore[arg-type]
        cron_executor=RecordingExecutor(),
        gateway=RecordingGateway(_result()),
        project_id="personal",
    )

    assert storage.definition is not None
    assert "enabled" not in storage.definition
    assert existing.enabled is enabled
    assert storage.wakes == expected_wakes


@pytest.mark.asyncio
async def test_wiki_prune_handler_reports_command_failure() -> None:
    gateway = RecordingGateway(_result(returncode=2, stderr="qdrant unavailable"))
    handler = create_wiki_prune_handler(gateway)

    result = await handler(SimpleNamespace())  # type: ignore[arg-type]

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["returncode"] == 2
    assert result["timed_out"] is False
    assert result["stderr"] == "qdrant unavailable"


@pytest.mark.asyncio
async def test_wiki_prune_handler_reports_timeout_and_unavailable() -> None:
    timed_out = create_wiki_prune_handler(
        RecordingGateway(_result(returncode=None, stderr="timed out", timed_out=True))
    )

    timeout_result = await timed_out(SimpleNamespace())  # type: ignore[arg-type]

    class UnavailableGateway:
        async def prune_all_scopes(self, *, timeout: float | None = None) -> GwikiCommandResult:
            raise GwikiUnavailableError("gwiki is not installed")

    unavailable = await create_wiki_prune_handler(UnavailableGateway())(
        SimpleNamespace()  # type: ignore[arg-type]
    )

    assert timeout_result["success"] is False
    assert timeout_result["status"] == "timed_out"
    assert timeout_result["timed_out"] is True
    assert unavailable["success"] is False
    assert unavailable["status"] == "unavailable"
    assert unavailable["unavailable"] is True
    assert unavailable["error"] == "gwiki is not installed"

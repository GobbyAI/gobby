"""Tests for nightly codewiki cron registration."""

from __future__ import annotations

import signal
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from gobby.code_index import codewiki_nightly
from gobby.code_index.codewiki_nightly import (
    CODEWIKI_NIGHTLY_AI,
    CODEWIKI_NIGHTLY_GCODE_TIMEOUT_SECONDS,
    CODEWIKI_NIGHTLY_GWIKI_TIMEOUT_SECONDS,
    codewiki_nightly_handler_name,
    codewiki_nightly_job_name,
    create_codewiki_nightly_handler,
    nightly_refresh_service,
    register_codewiki_nightly_cron,
    register_codewiki_nightly_crons,
)
from gobby.code_index.codewiki_refresh import (
    CodewikiRefreshRequest,
    CodewikiRefreshResult,
    CodewikiRefreshService,
)
from gobby.code_index.gcode_gateway import GcodeCommandError, GcodeGatewayError
from gobby.config.wiki import WikiConfig
from gobby.shutdown_intent import ShutdownIntent, ShutdownIntentRecord
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import LocalProjectManager

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"
PROJECT_NAME = "gobby"


def test_nightly_codewiki_requires_daemon_ai() -> None:
    assert CODEWIKI_NIGHTLY_AI == "daemon"


class FakeCronExecutor:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register_handler(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


class FakeRefreshService(CodewikiRefreshService):
    def __init__(self, *, changed_count: int = 0, error: Exception | None = None) -> None:
        self.changed_count = changed_count
        self.error = error
        self.requests: list[CodewikiRefreshRequest] = []

    def resolve_out_dir(self, root: Path, out_dir: str | None) -> Path:
        value = out_dir or "wiki"
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        return path.resolve(strict=False)

    async def refresh(self, request: CodewikiRefreshRequest) -> CodewikiRefreshResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        root = Path(request.root_path).resolve(strict=False)
        out_dir = self.resolve_out_dir(root, request.out_dir)
        changed_paths = tuple(out_dir / f"page-{index}.md" for index in range(self.changed_count))
        return CodewikiRefreshResult(
            root=root,
            out_dir=out_dir,
            changed_paths=changed_paths,
            ingested_paths=(),
            indexed=bool(changed_paths),
        )


def test_nightly_refresh_service_uses_generation_sized_gateway_timeouts(
    tmp_path: Path,
) -> None:
    """Nightly gateways must outlive a full generation pass, not interactive defaults.

    A full first run LLM-summarizes thousands of pages; the interactive
    GcodeGateway rebuild timeout (120s) killed even a 7-page scoped run (168s
    measured), so the nightly path builds its own generation-sized gateways.
    """
    service = nightly_refresh_service()

    gcode = service._gcode_gateway_factory()
    assert gcode._rebuild_timeout_seconds == CODEWIKI_NIGHTLY_GCODE_TIMEOUT_SECONDS
    assert CODEWIKI_NIGHTLY_GCODE_TIMEOUT_SECONDS >= 4 * 60 * 60.0

    gwiki = service._gwiki_gateway_factory(tmp_path)
    assert gwiki._timeout_seconds == CODEWIKI_NIGHTLY_GWIKI_TIMEOUT_SECONDS
    assert CODEWIKI_NIGHTLY_GWIKI_TIMEOUT_SECONDS >= 10 * 60.0


def test_wiki_config_nightly_defaults_to_enabled_local_schedule() -> None:
    config = WikiConfig()

    assert config.codewiki_nightly_enabled is True
    assert config.codewiki_nightly_schedule_cron == "0 3 * * *"
    assert config.codewiki_nightly_timezone is None


def test_register_codewiki_nightly_cron_reconciles_single_utc_system_job(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    storage = CronJobStorage(temp_db)
    executor = FakeCronExecutor()

    registered = register_codewiki_nightly_cron(
        cron_storage=storage,
        cron_executor=executor,
        project_id=PROJECT_ID,
        project_name=PROJECT_NAME,
        repo_path=tmp_path,
        wiki_config=WikiConfig(
            codewiki_nightly_enabled=True,
            codewiki_nightly_timezone="America/Chicago",
            codewiki_project_scopes_by_name={PROJECT_NAME: ["crates", "web", "src"]},
        ),
    )

    assert registered == 1
    handler_name = codewiki_nightly_handler_name(PROJECT_ID)
    assert set(executor.handlers) == {handler_name}
    job = storage.get_job_by_name(codewiki_nightly_job_name(PROJECT_ID))
    assert job is not None
    assert job.is_system is True
    assert job.enabled is True
    assert job.schedule_type == "cron"
    assert job.cron_expr == "0 3 * * *"
    assert job.timezone == "America/Chicago"
    assert job.action_type == "handler"
    assert job.action_config == {
        "handler": handler_name,
        "project_id": PROJECT_ID,
        "project_name": PROJECT_NAME,
        "root_path": str(tmp_path.resolve(strict=False)),
        "out_dir": str((tmp_path / "wiki").resolve(strict=False)),
        "ai": CODEWIKI_NIGHTLY_AI,
        "scopes": ["crates", "web", "src"],
    }
    assert job.next_run_at is not None
    assert job.next_run_at.utcoffset() == timedelta(0)

    register_codewiki_nightly_cron(
        cron_storage=storage,
        cron_executor=executor,
        project_id=PROJECT_ID,
        project_name=PROJECT_NAME,
        repo_path=tmp_path,
        wiki_config=WikiConfig(
            codewiki_nightly_enabled=True,
            codewiki_nightly_schedule_cron="0 4 * * *",
            codewiki_nightly_timezone="America/Chicago",
            codewiki_project_scopes_by_name={PROJECT_NAME: ["src"]},
        ),
    )

    jobs = [
        item
        for item in storage.list_jobs(project_id=PROJECT_ID, is_system=True)
        if item.name == codewiki_nightly_job_name(PROJECT_ID)
    ]
    assert len(jobs) == 1
    updated = jobs[0]
    assert updated.cron_expr == "0 4 * * *"
    assert updated.next_run_at is not None
    assert updated.next_run_at.utcoffset() == timedelta(0)


def test_register_codewiki_nightly_cron_enables_previously_disabled_job_on_default_flip(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """A job row created under the old disabled default is re-enabled on reconcile."""
    storage = CronJobStorage(temp_db)
    executor = FakeCronExecutor()

    register_codewiki_nightly_cron(
        cron_storage=storage,
        cron_executor=executor,
        project_id=PROJECT_ID,
        project_name=PROJECT_NAME,
        repo_path=tmp_path,
        wiki_config=WikiConfig(codewiki_nightly_enabled=False),
    )
    disabled = storage.get_job_by_name(codewiki_nightly_job_name(PROJECT_ID))
    assert disabled is not None
    assert disabled.enabled is False

    register_codewiki_nightly_cron(
        cron_storage=storage,
        cron_executor=executor,
        project_id=PROJECT_ID,
        project_name=PROJECT_NAME,
        repo_path=tmp_path,
        wiki_config=WikiConfig(),
    )

    enabled = storage.get_job_by_name(codewiki_nightly_job_name(PROJECT_ID))
    assert enabled is not None
    assert enabled.enabled is True
    assert enabled.next_run_at is not None
    assert enabled.next_run_at.utcoffset() == timedelta(0)


def test_register_codewiki_nightly_crons_covers_each_project_once(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Batch registration covers every memory-bearing repo, deduped, repo-gated."""
    pm = LocalProjectManager(temp_db)
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    project_a = pm.create(name="codewiki-a", repo_path=str(repo_a))
    project_b = pm.create(name="codewiki-b", repo_path=str(repo_b))
    duplicate_repo_project = "44444444-4444-4444-4444-444444444444"
    # A repo-less project ID that does not exist in ``projects``: it must be
    # skipped on the empty-repo gate before any FK-touching cron write.
    no_repo_project = "33333333-3333-3333-3333-333333333333"

    storage = CronJobStorage(temp_db)
    executor = FakeCronExecutor()

    registered = register_codewiki_nightly_crons(
        cron_storage=storage,
        cron_executor=executor,
        # project_a appears twice (dedup) and no_repo_project has no repo path.
        projects=[
            (project_a.id, project_a.name, repo_a),
            (project_b.id, project_b.name, repo_b),
            (project_a.id, project_a.name, repo_a),
            (duplicate_repo_project, "duplicate-repo", repo_a),
            (no_repo_project, "no-repo", ""),
        ],
        wiki_config=WikiConfig(codewiki_nightly_enabled=True),
    )

    assert registered == 2
    assert set(executor.handlers) == {
        codewiki_nightly_handler_name(project_a.id),
        codewiki_nightly_handler_name(project_b.id),
    }
    assert storage.get_job_by_name(codewiki_nightly_job_name(project_a.id)) is not None
    assert storage.get_job_by_name(codewiki_nightly_job_name(project_b.id)) is not None
    assert storage.get_job_by_name(codewiki_nightly_job_name(duplicate_repo_project)) is None
    assert storage.get_job_by_name(codewiki_nightly_job_name(no_repo_project)) is None


@pytest.mark.asyncio
async def test_codewiki_nightly_handler_returns_success_output(tmp_path: Path) -> None:
    service = FakeRefreshService(changed_count=2)
    handler = create_codewiki_nightly_handler(
        project_id=PROJECT_ID,
        root_path=tmp_path,
        out_dir=tmp_path / "wiki",
        scopes=["crates", "web", "src"],
        refresh_service=service,
    )

    output = await handler(_cron_job())

    assert output == f"codewiki nightly refresh completed for {PROJECT_ID}: 2 changed doc(s)"
    assert service.requests == [
        CodewikiRefreshRequest(
            root_path=str(tmp_path),
            project_id=PROJECT_ID,
            out_dir=str(tmp_path / "wiki"),
            ai=CODEWIKI_NIGHTLY_AI,
            scopes=["crates", "web", "src"],
        )
    ]


@pytest.mark.asyncio
async def test_codewiki_nightly_handler_raises_on_refresh_failure(tmp_path: Path) -> None:
    service = FakeRefreshService(error=GcodeGatewayError("gcode failed"))
    handler = create_codewiki_nightly_handler(
        project_id=PROJECT_ID,
        root_path=tmp_path,
        out_dir=tmp_path / "wiki",
        refresh_service=service,
    )

    with pytest.raises(GcodeGatewayError, match="gcode failed"):
        await handler(_cron_job())


async def test_codewiki_nightly_handler_treats_shutdown_sigterm_as_benign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gcode -15 while the daemon is shutting down is child-reap collateral,
    # not a refresh failure; it must not raise (which would storm the retry).
    sigterm_error = GcodeCommandError(["gcode", "codewiki"], -signal.SIGTERM, "")
    service = FakeRefreshService(error=sigterm_error)
    handler = create_codewiki_nightly_handler(
        project_id=PROJECT_ID,
        root_path=tmp_path,
        out_dir=tmp_path / "wiki",
        refresh_service=service,
    )
    monkeypatch.setattr(
        codewiki_nightly,
        "read_active_shutdown_intent",
        lambda *args, **kwargs: ShutdownIntentRecord(
            intent=ShutdownIntent.RESTART,
            source="cli_restart",
            sender_pid=123,
            timestamp=1.0,
        ),
    )

    output = await handler(_cron_job())

    assert output == (
        f"codewiki nightly refresh for {PROJECT_ID} skipped: daemon shutdown in progress"
    )


async def test_codewiki_nightly_handler_raises_sigterm_without_active_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A gcode SIGTERM with no active shutdown marker is a genuine failure and
    # must still surface (so it is not silently swallowed as benign).
    sigterm_error = GcodeCommandError(["gcode", "codewiki"], -signal.SIGTERM, "")
    service = FakeRefreshService(error=sigterm_error)
    handler = create_codewiki_nightly_handler(
        project_id=PROJECT_ID,
        root_path=tmp_path,
        out_dir=tmp_path / "wiki",
        refresh_service=service,
    )
    monkeypatch.setattr(
        codewiki_nightly,
        "read_active_shutdown_intent",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(GcodeCommandError, match="gcode exited -15"):
        await handler(_cron_job())


def _cron_job() -> CronJob:
    now = "2026-01-01T00:00:00+00:00"
    return CronJob(
        id="cj-test",
        project_id=PROJECT_ID,
        name=codewiki_nightly_job_name(PROJECT_ID),
        schedule_type="cron",
        action_type="handler",
        action_config={"handler": codewiki_nightly_handler_name(PROJECT_ID)},
        created_at=now,
        updated_at=now,
        cron_expr="0 3 * * *",
        timezone="UTC",
    )

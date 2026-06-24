"""Tests for nightly codewiki cron registration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from gobby.code_index.codewiki_nightly import (
    CODEWIKI_NIGHTLY_AI,
    codewiki_nightly_handler_name,
    codewiki_nightly_job_name,
    create_codewiki_nightly_handler,
    register_codewiki_nightly_cron,
    register_codewiki_nightly_crons,
)
from gobby.code_index.codewiki_refresh import (
    CodewikiRefreshRequest,
    CodewikiRefreshResult,
    CodewikiRefreshService,
)
from gobby.code_index.gcode_gateway import GcodeGatewayError
from gobby.config.wiki import WikiConfig
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import LocalProjectManager

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


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
        value = out_dir or "gobby-wiki"
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


def test_wiki_config_nightly_defaults_to_opt_in_local_schedule() -> None:
    config = WikiConfig()

    assert config.codewiki_nightly_enabled is False
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
        repo_path=tmp_path,
        wiki_config=WikiConfig(
            codewiki_nightly_enabled=True,
            codewiki_nightly_timezone="America/Chicago",
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
        "root_path": str(tmp_path.resolve(strict=False)),
        "out_dir": str((tmp_path / "gobby-wiki").resolve(strict=False)),
        "ai": CODEWIKI_NIGHTLY_AI,
    }
    assert job.next_run_at is not None
    assert job.next_run_at.endswith("+00:00")

    register_codewiki_nightly_cron(
        cron_storage=storage,
        cron_executor=executor,
        project_id=PROJECT_ID,
        repo_path=tmp_path,
        wiki_config=WikiConfig(
            codewiki_nightly_enabled=True,
            codewiki_nightly_schedule_cron="0 4 * * *",
            codewiki_nightly_timezone="America/Chicago",
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
    assert updated.next_run_at.endswith("+00:00")


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
            (project_a.id, repo_a),
            (project_b.id, repo_b),
            (project_a.id, repo_a),
            (no_repo_project, ""),
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
    assert storage.get_job_by_name(codewiki_nightly_job_name(no_repo_project)) is None


@pytest.mark.asyncio
async def test_codewiki_nightly_handler_returns_success_output(tmp_path: Path) -> None:
    service = FakeRefreshService(changed_count=2)
    handler = create_codewiki_nightly_handler(
        project_id=PROJECT_ID,
        root_path=tmp_path,
        out_dir=tmp_path / "gobby-wiki",
        refresh_service=service,
    )

    output = await handler(_cron_job())

    assert output == f"codewiki nightly refresh completed for {PROJECT_ID}: 2 changed doc(s)"
    assert service.requests == [
        CodewikiRefreshRequest(
            root_path=str(tmp_path),
            project_id=PROJECT_ID,
            out_dir=str(tmp_path / "gobby-wiki"),
            ai=CODEWIKI_NIGHTLY_AI,
        )
    ]


@pytest.mark.asyncio
async def test_codewiki_nightly_handler_raises_on_refresh_failure(tmp_path: Path) -> None:
    service = FakeRefreshService(error=GcodeGatewayError("gcode failed"))
    handler = create_codewiki_nightly_handler(
        project_id=PROJECT_ID,
        root_path=tmp_path,
        out_dir=tmp_path / "gobby-wiki",
        refresh_service=service,
    )

    with pytest.raises(GcodeGatewayError, match="gcode failed"):
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

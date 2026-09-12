"""Daily Dream reports preserve decisions without replaying maintenance."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.memory.dream.candidates import memory_to_candidate
from gobby.memory.dream.decisions import DreamDecisionStore
from gobby.memory.dream.models import DreamAction
from gobby.memory.dream.options import DreamRunOptions
from gobby.memory.dream.related import RelatedEvidenceSession
from gobby.memory.dream.report import report_path
from gobby.memory.dream.service import MemoryDreamService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.projects import LocalProjectManager
from tests.memory.test_dream import _as_dream_manager, _sweep_config


@pytest.fixture
def report_service(temp_db: HubDatabase) -> MemoryDreamService:
    manager = MagicMock(wraps=LocalMemoryManager(temp_db))
    manager.db = temp_db
    return MemoryDreamService(
        memory_manager=_as_dream_manager(manager),
        dream_config=_sweep_config(),
        llm_service=MagicMock(),
    )


def _run(
    service: MemoryDreamService,
    project_id: str,
    *,
    narrative: str,
    created_at: datetime,
    dry_run: bool = False,
) -> dict[str, Any]:
    options = DreamRunOptions(project_id=project_id, dry_run=dry_run, include_global=False)
    run_id = service.store.create_run(
        project_id=project_id, dry_run=dry_run, options=options.to_dict()
    )
    run = service.store.update_run(
        run_id,
        created_at=created_at.isoformat(),
        status="completed",
        summary={"narrative": narrative, "mutations": 0, "noops": 1},
    )
    assert run is not None
    return run


async def test_daily_report_preserves_prior_runs_and_seeds_next_planner(
    temp_db: HubDatabase,
    tmp_path: Path,
    report_service: MemoryDreamService,
) -> None:
    project = LocalProjectManager(temp_db).create(name="daily-report")
    start = datetime.now().replace(hour=10, minute=0).astimezone(UTC)
    first = _run(
        report_service, project.id, narrative="Retain portable conventions.", created_at=start
    )
    with patch.object(report_service, "_resolve_repo_path", return_value=str(tmp_path)):
        report_service._write_scope_summary(first)
        second = _run(
            report_service,
            project.id,
            narrative="Retain conventions and repair stale paths.",
            created_at=start + timedelta(hours=1),
        )
        related = RelatedEvidenceSession()
        try:
            orchestrator = await report_service._build_orchestrator(
                second["id"],
                DreamRunOptions(**second["options"]),
                related,
            )
            assert orchestrator.totals.narrative == "Retain portable conventions."
        finally:
            await related.aclose()
        report_service._write_scope_summary(second)
        output = report_path(str(tmp_path), second)
        markdown = output.read_text()
        report_service._write_scope_summary(second)
        assert output.read_text() == markdown
    assert markdown.count("# Dream —") == 1
    assert markdown.count("## Synthesis") == 1
    assert "Retain conventions and repair stale paths." in markdown
    assert "mutations: 0, noops: 2, skipped: 0, errors: 0" in markdown
    assert first["id"] in markdown and second["id"] in markdown
    assert list(output.parent.glob("*.md")) == [output]


def test_day_scope_and_dry_run_reports_stay_separate(
    temp_db: HubDatabase,
    tmp_path: Path,
    report_service: MemoryDreamService,
) -> None:
    project = LocalProjectManager(temp_db).create(name="scoped-report")
    other = LocalProjectManager(temp_db).create(name="other-report")
    start = datetime(2026, 9, 11, 23, 59).astimezone(UTC)
    first = _run(report_service, project.id, narrative="First day.", created_at=start)
    next_day = _run(
        report_service, project.id, narrative="Next day.", created_at=start + timedelta(minutes=2)
    )
    preview = _run(
        report_service, project.id, narrative="Preview only.", created_at=start, dry_run=True
    )
    _run(report_service, other.id, narrative="Other project private evidence.", created_at=start)
    with patch.object(report_service, "_resolve_repo_path", return_value=str(tmp_path)):
        for run in (first, next_day, preview):
            report_service._write_scope_summary(run)
    files = [report_path(str(tmp_path), run) for run in (first, next_day, preview)]
    assert [file.relative_to(tmp_path).as_posix() for file in files] == [
        ".gobby/reports/dream/gobby-dream-20260911.md",
        ".gobby/reports/dream/gobby-dream-20260912.md",
        ".gobby/reports/dream/dry-run/gobby-dream-20260911.md",
    ]
    for file, run in zip(files, (first, next_day, preview), strict=True):
        markdown = file.read_text()
        assert run["summary"]["narrative"] in markdown
        assert "noops: 1" in markdown
        assert "Other project private evidence" not in markdown
    assert "Dry run" in files[2].read_text()


def test_decision_evidence_is_complete_and_overrides_stale_counts(
    temp_db: HubDatabase,
    tmp_path: Path,
    report_service: MemoryDreamService,
) -> None:
    project = LocalProjectManager(temp_db).create(name="evidence-report")
    run = _run(
        report_service,
        project.id,
        narrative="Keep useful conventions.",
        created_at=datetime.now(UTC),
    )
    memory = LocalMemoryManager(temp_db).create_memory(
        content="Use portable paths", project_id=project.id
    )
    candidate = memory_to_candidate(memory, datetime.now(UTC))
    ledger = DreamDecisionStore(temp_db)
    actions = [
        DreamAction(action="keep", memory_id=memory.id, reason=f"Reason {i}") for i in range(101)
    ]
    ids = ledger.stage(run["id"], actions, [candidate])
    for decision_id in ids:
        ledger.finish(decision_id, "skipped", mutations=0, reason="Selected memory changed")
    ledger.finish(ids[-1], "skipped", error="Projection unavailable")
    with patch.object(report_service, "_resolve_repo_path", return_value=str(tmp_path)):
        report_service._write_scope_summary(run)
    markdown = report_path(str(tmp_path), run).read_text()
    assert all(markdown.count(decision_id) == 1 for decision_id in ids)
    assert "mutations: 0, noops: 0, skipped: 101, errors: 1" in markdown
    assert "Selected memory changed" in markdown
    assert "Projection unavailable" in markdown


def test_report_write_failure_preserves_previous_file_and_can_retry(
    temp_db: HubDatabase,
    tmp_path: Path,
    report_service: MemoryDreamService,
) -> None:
    project = LocalProjectManager(temp_db).create(name="write-report")
    run = _run(
        report_service, project.id, narrative="Preserved narrative.", created_at=datetime.now(UTC)
    )
    with patch.object(report_service, "_resolve_repo_path", return_value=str(tmp_path)):
        report_service._write_scope_summary(run)
        output = report_path(str(tmp_path), run)
        before = output.read_text()
        with patch("pathlib.Path.replace", side_effect=OSError("disk unavailable")):
            report_service._write_scope_summary(run)
        failed = report_service.store.get_run(run["id"])
        assert failed is not None
        assert failed["summary"]["summary_file_error"] == "disk unavailable"
        assert failed["status"] == "completed"
        assert output.read_text() == before
        report_service._write_scope_summary(failed)
    saved = report_service.store.get_run(run["id"])
    assert saved is not None and "summary_file_error" not in saved["summary"]
    assert output.read_text() == before

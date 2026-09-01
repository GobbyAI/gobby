"""FeedbackReviewService behavior against the isolated hub with a stubbed LLM."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.config.sessions import FeedbackReviewConfig
from gobby.feedback.service import FINDINGS_EPIC_TITLE, FeedbackReviewService
from gobby.feedback.storage import FeedbackReviewStore
from gobby.prompts.sync import sync_bundled_prompts
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

MACHINE_ID = "20000000-0000-4000-8000-000000000004"

_T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


@pytest.fixture
def session_id(temp_db: HubDatabase, tmp_path: Path) -> str:
    sync_bundled_prompts(temp_db)
    checkout = tmp_path / "gobby"
    checkout.mkdir()
    project = LocalProjectManager(temp_db).create(name="gobby", repo_path=str(checkout))
    SessionManager(temp_db).register_session(
        external_id="feedback-service-session",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project.id,
    )
    row = temp_db.fetchone(
        "SELECT id FROM sessions WHERE external_id = %s", ("feedback-service-session",)
    )
    assert row is not None
    return str(row["id"])


class _FakeLLM:
    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        json_schema: dict[str, Any],
        max_tokens: int | None = None,
        caller: str | None = None,
        total_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "prompt": prompt,
                "max_tokens": max_tokens,
                "caller": caller,
                "total_timeout_seconds": total_timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class _FakeTaskManager:
    def __init__(
        self,
        existing_open_titles: tuple[str, ...] = (),
        existing_epic_id: str | None = None,
    ) -> None:
        self.existing_open_titles = existing_open_titles
        self.existing_epic_id = existing_epic_id
        self.created: list[SimpleNamespace] = []
        self.epics: list[SimpleNamespace] = []

    def list_tasks(
        self,
        *,
        project_id: str | None = None,
        closed: bool | None = None,
        title_like: str | None = None,
        limit: int = 50,
    ) -> list[Any]:
        assert closed is False
        wanted = (title_like or "").casefold()
        rows = [
            SimpleNamespace(title=title, task_type="task")
            for title in self.existing_open_titles
            if wanted in title.casefold()
        ]
        if self.existing_epic_id is not None and wanted in FINDINGS_EPIC_TITLE.casefold():
            rows.append(
                SimpleNamespace(
                    id=self.existing_epic_id,
                    title=FINDINGS_EPIC_TITLE,
                    task_type="epic",
                )
            )
        return rows[:limit]

    def create_task(
        self,
        project_id: str,
        title: str,
        description: str | None = None,
        *,
        priority: int = 2,
        labels: list[str] | None = None,
        category: str | None = None,
        validation_criteria: str | None = None,
        parent_task_id: str | None = None,
        task_type: str = "task",
    ) -> Any:
        registry = self.epics if task_type == "epic" else self.created
        task = SimpleNamespace(
            id=f"{task_type}-{len(registry) + 1}",
            project_id=project_id,
            title=title,
            description=description,
            priority=priority,
            labels=labels,
            category=category,
            validation_criteria=validation_criteria,
            parent_task_id=parent_task_id,
            task_type=task_type,
        )
        registry.append(task)
        return task


def _insert_feedback(
    db: HubDatabase,
    session_id: str,
    *,
    kind: str = "friction",
    kind_other_label: str | None = None,
    created_at: datetime = _T0,
    disposition: str | None = None,
) -> str:
    feedback_id = str(uuid4())
    db.execute(
        """
        INSERT INTO session_feedback (
            id, session_id, source, kind, kind_other_label, evidence, impact,
            frequency, suggestion, disposition, reviewed, created_at
        )
        VALUES (%s, %s, 'survey', %s, %s, 'close gate re-ran validation', 'lost ten minutes',
                'repeated', NULL, %s, FALSE, %s)
        """,
        (feedback_id, session_id, kind, kind_other_label, disposition, created_at),
    )
    return feedback_id


def _cluster(
    observation_ids: list[str],
    *,
    classification: str = "defect",
    title: str | None = None,
    priority: int | None = None,
    theme: str = "close-gate validation reruns",
) -> dict[str, Any]:
    proposed: dict[str, Any] | None = None
    if title is not None:
        proposed = {"title": title, "description": "Observed repeatedly by agents."}
        if priority is not None:
            proposed["priority"] = priority
    return {
        "observation_ids": observation_ids,
        "theme": theme,
        "classification": classification,
        "proposed_task": proposed,
        "digest_note": "Agents lose time to redundant validation reruns.",
    }


def _service(
    temp_db: HubDatabase,
    llm: _FakeLLM,
    task_manager: _FakeTaskManager | None,
    **config_overrides: Any,
) -> FeedbackReviewService:
    config = FeedbackReviewConfig(**config_overrides)
    return FeedbackReviewService(temp_db, llm, config, task_manager)


async def test_run_review_files_tasks_marks_rows_and_renders_digest(
    temp_db: HubDatabase, session_id: str
) -> None:
    first = _insert_feedback(temp_db, session_id)
    second = _insert_feedback(temp_db, session_id, created_at=_T0 + timedelta(minutes=1))
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster([first, second], title="Stop re-running validation at close"),
            ]
        }
    )
    task_manager = _FakeTaskManager()
    service = _service(temp_db, llm, task_manager)

    result = await service.run_review()

    assert result["status"] == "completed"
    assert result["rows_considered"] == 2
    assert result["tasks_filed"] == 1
    assert result["deduplicated"] == 0

    # The distill call carries the review contract.
    call = llm.calls[0]
    assert call["caller"] == "feedback.review"
    assert call["max_tokens"] == 8192
    assert call["total_timeout_seconds"] == 900.0
    # The bundled prompt rendered the observation payload verbatim.
    assert "close gate re-ran validation" in call["prompt"]
    assert first in call["prompt"]

    task = task_manager.created[0]
    assert task.title == "Stop re-running validation at close"
    assert task.labels == ["feedback-review"]
    assert task.category == "research"
    assert first in str(task.description)

    rows = temp_db.fetchall(
        "SELECT reviewed, review_run_id FROM session_feedback WHERE id = ANY(%s)",
        ([first, second],),
    )
    assert all(row["reviewed"] for row in rows)
    assert {str(row["review_run_id"]) for row in rows} == {result["run_id"]}

    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None
    assert run.status == "completed"
    assert run.actions is not None
    assert run.actions["rows_marked_reviewed"] == 2
    assert run.digest_md is not None
    assert "Stop re-running validation at close" in run.digest_md
    assert "friction 2" in run.digest_md


async def test_run_review_empty_backlog_creates_no_run_row(temp_db: HubDatabase) -> None:
    llm = _FakeLLM(response={"clusters": []})
    service = _service(temp_db, llm, _FakeTaskManager())

    result = await service.run_review()

    assert result == {"status": "no_rows", "run_id": None, "rows_considered": 0}
    assert llm.calls == []
    count = temp_db.fetchone("SELECT COUNT(*) AS count FROM feedback_review_runs")
    assert count is not None and count["count"] == 0


async def test_run_review_dedupes_open_titles_and_in_batch_duplicates(
    temp_db: HubDatabase, session_id: str
) -> None:
    first = _insert_feedback(temp_db, session_id)
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster([first], title="fix CLOSE-gate latency"),
                _cluster([first], title="Improve digest wording"),
                _cluster([first], title="improve digest WORDING"),
            ]
        }
    )
    task_manager = _FakeTaskManager(existing_open_titles=("Fix close-gate latency",))
    service = _service(temp_db, llm, task_manager)

    result = await service.run_review()

    # One dedupe against the open task, one against the batch itself.
    assert result["deduplicated"] == 2
    assert [task.title for task in task_manager.created] == ["Improve digest wording"]


async def test_run_review_dry_run_writes_digest_but_files_and_flips_nothing(
    temp_db: HubDatabase, session_id: str
) -> None:
    first = _insert_feedback(temp_db, session_id)
    llm = _FakeLLM(response={"clusters": [_cluster([first], title="File me")]})
    task_manager = _FakeTaskManager()
    service = _service(temp_db, llm, task_manager)

    result = await service.run_review(dry_run=True)

    assert result["status"] == "completed"
    assert result["dry_run"] is True
    assert result["tasks_filed"] == 0
    assert task_manager.created == []

    row = temp_db.fetchone(
        "SELECT reviewed, review_run_id FROM session_feedback WHERE id = %s", (first,)
    )
    assert row is not None
    assert row["reviewed"] is False
    assert row["review_run_id"] is None

    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None
    assert run.dry_run is True
    assert run.status == "completed"
    assert run.actions is not None
    assert run.actions["skipped"] == ["dry_run: no tasks filed"]
    assert run.digest_md is not None
    assert "**Dry run**" in run.digest_md


async def test_run_review_respects_task_cap_and_notes_overflow(
    temp_db: HubDatabase, session_id: str
) -> None:
    first = _insert_feedback(temp_db, session_id)
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster([first], title="First proposal"),
                _cluster([first], title="Second proposal"),
            ]
        }
    )
    task_manager = _FakeTaskManager()
    service = _service(temp_db, llm, task_manager, max_tasks_per_run=1)

    result = await service.run_review()

    assert result["tasks_filed"] == 1
    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None
    assert run.actions is not None
    assert run.actions["skipped"] == ["task cap reached; 1 proposal(s) deferred"]


async def test_run_review_guidance_gap_gets_needs_decision_label(
    temp_db: HubDatabase, session_id: str
) -> None:
    first = _insert_feedback(temp_db, session_id)
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster(
                    [first],
                    classification="guidance-gap",
                    title="Clarify close-gate docs",
                    priority=3,
                ),
            ]
        }
    )
    task_manager = _FakeTaskManager()
    service = _service(temp_db, llm, task_manager)

    await service.run_review()

    task = task_manager.created[0]
    assert task.labels == ["feedback-review", "needs-decision"]
    assert task.priority == 3


async def test_run_review_noise_and_praise_never_file(
    temp_db: HubDatabase, session_id: str
) -> None:
    first = _insert_feedback(temp_db, session_id)
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster([first], classification="noise", title="Should be ignored"),
                _cluster([first], classification="praise", title="Also ignored"),
            ]
        }
    )
    task_manager = _FakeTaskManager()
    service = _service(temp_db, llm, task_manager)

    result = await service.run_review()

    assert result["tasks_filed"] == 0
    assert task_manager.created == []
    # Rows are still consumed: noise and praise are reviewed, not re-batched.
    row = temp_db.fetchone("SELECT reviewed FROM session_feedback WHERE id = %s", (first,))
    assert row is not None and row["reviewed"] is True


async def test_run_review_without_gobby_project_degrades_to_digest_only(
    temp_db: HubDatabase, session_id: str
) -> None:
    temp_db.execute("UPDATE projects SET name = 'not-gobby' WHERE name = 'gobby'")
    first = _insert_feedback(temp_db, session_id)
    llm = _FakeLLM(response={"clusters": [_cluster([first], title="File me")]})
    task_manager = _FakeTaskManager()
    service = _service(temp_db, llm, task_manager)

    result = await service.run_review()

    assert result["tasks_filed"] == 0
    assert task_manager.created == []
    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None
    assert run.actions is not None
    assert run.actions["skipped"] == ["no project named 'gobby'; digest only"]


async def test_run_review_failed_distill_finalizes_run_failed_and_reraises(
    temp_db: HubDatabase, session_id: str
) -> None:
    first = _insert_feedback(temp_db, session_id)
    llm = _FakeLLM(error=RuntimeError("provider unavailable"))
    service = _service(temp_db, llm, _FakeTaskManager())

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await service.run_review()

    store = FeedbackReviewStore(temp_db)
    run = store.latest_run()
    assert run is not None
    assert run.status == "failed"
    assert run.error == "provider unavailable"
    # The batch stays unreviewed so the next run re-picks it.
    row = temp_db.fetchone("SELECT reviewed FROM session_feedback WHERE id = %s", (first,))
    assert row is not None and row["reviewed"] is False


async def test_digest_other_label_audit_flags_recurring_labels(
    temp_db: HubDatabase, session_id: str
) -> None:
    ids = [
        _insert_feedback(temp_db, session_id, kind="other", kind_other_label="doc-drift"),
        _insert_feedback(
            temp_db,
            session_id,
            kind="other",
            kind_other_label="doc-drift",
            created_at=_T0 + timedelta(minutes=1),
        ),
        _insert_feedback(
            temp_db,
            session_id,
            kind="other",
            kind_other_label="one-off",
            created_at=_T0 + timedelta(minutes=2),
        ),
    ]
    llm = _FakeLLM(response={"clusters": [_cluster(ids, classification="noise")]})
    service = _service(temp_db, llm, _FakeTaskManager())

    result = await service.run_review()

    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None
    assert run.digest_md is not None
    assert "`doc-drift`: 2 — recurring; consider promoting to the kind enum" in run.digest_md
    assert "`one-off`: 1\n" in run.digest_md + "\n"
    assert "consider promoting" not in run.digest_md.split("`one-off`", 1)[1]


async def test_run_review_creates_named_epic_and_parents_filed_tasks(
    temp_db: HubDatabase, session_id: str
) -> None:
    obs = _insert_feedback(temp_db, session_id)
    llm = _FakeLLM(response={"clusters": [_cluster([obs], title="Fix close-gate rerun")]})
    task_manager = _FakeTaskManager()
    service = _service(temp_db, llm, task_manager)

    result = await service.run_review()

    assert len(task_manager.epics) == 1
    epic = task_manager.epics[0]
    assert epic.title == FINDINGS_EPIC_TITLE
    assert epic.task_type == "epic"
    assert epic.parent_task_id is None
    assert task_manager.created[0].parent_task_id == epic.id

    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None
    assert run.actions is not None
    assert run.actions["epic_task_id"] == epic.id


async def test_run_review_reuses_existing_open_findings_epic(
    temp_db: HubDatabase, session_id: str
) -> None:
    obs = _insert_feedback(temp_db, session_id)
    llm = _FakeLLM(response={"clusters": [_cluster([obs], title="Fix close-gate rerun")]})
    task_manager = _FakeTaskManager(existing_epic_id="epic-existing")
    service = _service(temp_db, llm, task_manager)

    await service.run_review()

    assert task_manager.epics == []
    assert task_manager.created[0].parent_task_id == "epic-existing"


async def test_digest_flags_shirked_found_work(temp_db: HubDatabase, session_id: str) -> None:
    shirked_obs = _insert_feedback(temp_db, session_id)
    filed_obs = _insert_feedback(
        temp_db, session_id, created_at=_T0 + timedelta(minutes=1), disposition="filed-task"
    )
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster([shirked_obs], title="Fix close-gate rerun", theme="deferred defect"),
                _cluster([filed_obs], theme="already filed defect"),
            ]
        }
    )
    service = _service(temp_db, llm, _FakeTaskManager())

    result = await service.run_review()

    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None
    assert run.digest_md is not None
    assert "## Shirked found work" in run.digest_md
    assert "**deferred defect** (1 obs; dispositions: none 1)" in run.digest_md
    assert "**already filed defect** (" not in run.digest_md

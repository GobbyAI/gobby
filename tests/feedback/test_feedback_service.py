"""FeedbackReviewService behavior against the isolated hub with a stubbed LLM."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.config.sessions import FeedbackReviewConfig
from gobby.feedback.service import (
    _RECENT_CLOSED_TASK_LIMIT,
    FINDINGS_EPIC_TITLE,
    FeedbackReviewService,
)
from gobby.feedback.storage import FeedbackReviewStore
from gobby.prompts.sync import sync_bundled_prompts
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import write_project_marker

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
    project_id = str(uuid4())
    write_project_marker(checkout, project_id=project_id, name="gobby")
    project = LocalProjectManager(temp_db).create(
        name="gobby", repo_path=str(checkout), project_id=project_id
    )
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
        tasks_by_ref: dict[str, Any] | None = None,
        existing_tasks: list[SimpleNamespace] | None = None,
        existing_closed_tasks: list[SimpleNamespace] | None = None,
    ) -> None:
        self.existing_open_titles = existing_open_titles
        self.existing_epic_id = existing_epic_id
        self.tasks_by_ref = tasks_by_ref or {}
        self.existing_tasks = existing_tasks or [
            SimpleNamespace(
                id=f"existing-{index}",
                title=title,
                description="",
                labels=["feedback-review"],
                task_type="task",
            )
            for index, title in enumerate(existing_open_titles, start=1)
        ]
        self.existing_closed_tasks = existing_closed_tasks or []
        self.created: list[SimpleNamespace] = []
        self.epics: list[SimpleNamespace] = []
        self.updated: list[SimpleNamespace] = []
        self.list_calls: list[dict[str, Any]] = []

    def get_task(self, task_id: str, project_id: str | None = None) -> Any | None:
        return self.tasks_by_ref.get(task_id)

    def list_tasks(
        self,
        *,
        project_id: str | None = None,
        closed: bool | None = None,
        title_like: str | None = None,
        label: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "hierarchy",
        sort_order: str = "asc",
    ) -> list[Any]:
        self.list_calls.append(
            {
                "closed": closed,
                "limit": limit,
                "offset": offset,
                "sort_by": sort_by,
                "sort_order": sort_order,
            }
        )
        wanted = (title_like or "").casefold()
        rows = (
            [*self.existing_closed_tasks]
            if closed is True
            else [*self.existing_tasks, *self.created]
        )
        if closed is not True and self.existing_epic_id is not None:
            rows.append(
                SimpleNamespace(
                    id=self.existing_epic_id,
                    title=FINDINGS_EPIC_TITLE,
                    description="",
                    labels=["feedback-review"],
                    task_type="epic",
                )
            )
        if wanted:
            rows = [row for row in rows if wanted in str(row.title).casefold()]
        if label:
            rows = [row for row in rows if label in (row.labels or [])]
        return rows[offset : offset + limit]

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

    def update_task(self, task_id: str, *, description: str) -> Any:
        task = next(
            task for task in [*self.existing_tasks, *self.created] if str(task.id) == task_id
        )
        task.description = description
        self.updated.append(task)
        return task


def _insert_feedback(
    db: HubDatabase,
    session_id: str,
    *,
    kind: str = "friction",
    kind_other_label: str | None = None,
    created_at: datetime = _T0,
    disposition: str | None = None,
    evidence: str = "close gate re-ran validation",
) -> str:
    feedback_id = str(uuid4())
    db.execute(
        """
        INSERT INTO session_feedback (
            id, session_id, source, kind, kind_other_label, evidence, impact,
            frequency, suggestion, disposition, reviewed, created_at
        )
        VALUES (%s, %s, 'survey', %s, %s, %s, 'lost ten minutes',
                'repeated', NULL, %s, FALSE, %s)
        """,
        (feedback_id, session_id, kind, kind_other_label, evidence, disposition, created_at),
    )
    return feedback_id


def _cluster(
    observation_ids: list[str],
    *,
    classification: str = "defect",
    title: str | None = None,
    priority: int | None = None,
    theme: str = "close-gate validation reruns",
    cited_paths: list[str] | None = None,
) -> dict[str, Any]:
    proposed: dict[str, Any] | None = None
    if title is not None:
        proposed = {"title": title, "description": "Observed repeatedly by agents."}
        if priority is not None:
            proposed["priority"] = priority
    return {
        "observation_ids": observation_ids,
        "cited_paths": cited_paths or [],
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


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=env,
        text=True,
    )
    return result.stdout.strip()


def _commit_file(repo: Path, relative_path: str, committed_at: datetime) -> str:
    if not (repo / ".git").exists():
        _git(repo, "init", "--initial-branch=main")
        _git(repo, "config", "user.email", "feedback-tests@example.com")
        _git(repo, "config", "user.name", "Feedback Tests")
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"content at {committed_at.isoformat()}\n", encoding="utf-8")
    _git(repo, "add", "--", relative_path)
    timestamp = committed_at.isoformat()
    commit_env = {**os.environ, "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp}
    _git(repo, "commit", "-m", f"Update {relative_path}", env=commit_env)
    return _git(repo, "rev-parse", "HEAD")


def _closed_task(
    *,
    task_id: str,
    title: str,
    commits: list[str],
    theme: str = "close-gate validation reruns",
    validation_status: str = "valid",
    seq_num: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        seq_num=seq_num,
        title=title,
        description=(
            "Prior finding.\n\n"
            "Filed by the session-feedback review loop.\n"
            f"Theme: {theme}\n"
            "Observations (session_feedback.id): older-observation"
        ),
        labels=["feedback-review"],
        task_type="bug",
        validation_status=validation_status,
        commits=commits,
    )


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
    assert task.labels == ["feedback-review", "llm-reviewed", "awaiting-human-review"]
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


@pytest.mark.asyncio
async def test_marked_temporary_checkout_reaches_digest(
    temp_db: HubDatabase, session_id: str
) -> None:
    observation_id = _insert_feedback(temp_db, session_id)
    llm = _FakeLLM(
        response={"clusters": [_cluster([observation_id], classification="noise", theme="fixture")]}
    )

    result = await _service(temp_db, llm, _FakeTaskManager()).run_review()

    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None
    assert run.digest_md is not None
    assert "**fixture** [noise, 1 obs]" in run.digest_md


async def test_run_review_suppresses_proposal_with_fixed_disposition(
    temp_db: HubDatabase, session_id: str
) -> None:
    observation_id = _insert_feedback(
        temp_db,
        session_id,
        disposition="fixed",
        evidence="Fixed under #42 before feedback review",
    )
    task_manager = _FakeTaskManager()
    llm = _FakeLLM(
        response={"clusters": [_cluster([observation_id], title="Refix resolved behavior")]}
    )

    result = await _service(temp_db, llm, task_manager).run_review()

    assert result["tasks_filed"] == 0
    assert task_manager.created == []
    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None and run.actions is not None
    assert run.actions["suppressed"] == [
        {
            "title": "Refix resolved behavior",
            "observation_ids": [observation_id],
            "disposition": "fixed",
            "matched_task_ref": "#42",
            "reason": f"observation {observation_id} has resolved fixed disposition",
        }
    ]
    assert run.digest_md is not None
    assert "Suppressed Refix resolved behavior (#42)" in run.digest_md


async def test_run_review_suppresses_proposal_with_valid_filed_task_disposition(
    temp_db: HubDatabase, session_id: str
) -> None:
    observation_id = _insert_feedback(
        temp_db,
        session_id,
        disposition="filed-task",
        evidence="Filed decision task #42",
    )
    referenced_task = SimpleNamespace(labels=["needs-decision"], closed_at=None)
    task_manager = _FakeTaskManager(tasks_by_ref={"#42": referenced_task})
    llm = _FakeLLM(
        response={"clusters": [_cluster([observation_id], title="Duplicate decision task")]}
    )

    result = await _service(temp_db, llm, task_manager).run_review()

    assert result["tasks_filed"] == 0
    assert task_manager.created == []
    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None and run.actions is not None
    assert run.actions["suppressed"][0] == {
        "title": "Duplicate decision task",
        "observation_ids": [observation_id],
        "disposition": "filed-task",
        "matched_task_ref": "#42",
        "reason": f"observation {observation_id} has resolved filed-task disposition",
    }


async def test_run_review_dedupes_open_titles_and_in_batch_duplicates(
    temp_db: HubDatabase, session_id: str
) -> None:
    first = _insert_feedback(temp_db, session_id)
    second = _insert_feedback(temp_db, session_id, created_at=_T0 + timedelta(minutes=1))
    third = _insert_feedback(temp_db, session_id, created_at=_T0 + timedelta(minutes=2))
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster([first], title="fix CLOSE-gate latency"),
                _cluster([second], title="Improve digest wording"),
                _cluster([third], title="improve digest WORDING"),
            ]
        }
    )
    task_manager = _FakeTaskManager(existing_open_titles=("Fix close-gate latency",))
    service = _service(temp_db, llm, task_manager)

    result = await service.run_review()

    # One dedupe against the open task, one against the batch itself.
    assert result["deduplicated"] == 2
    assert [task.title for task in task_manager.created] == ["Improve digest wording"]
    assert first in str(task_manager.existing_tasks[0].description)
    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None and run.actions is not None
    assert run.actions["suppressed"][0]["matched_task_ref"] == "existing-1"
    assert run.actions["suppressed"][0]["reason"] == "matched open task"


async def test_run_review_appends_observations_to_open_task_matching_theme(
    temp_db: HubDatabase, session_id: str
) -> None:
    current = _insert_feedback(temp_db, session_id)
    existing = SimpleNamespace(
        id="existing-theme",
        title="Reduce redundant close validation",
        description=(
            "Prior finding.\n\n"
            "Filed by the session-feedback review loop.\n"
            "Theme: close-gate validation reruns\n"
            "Observations (session_feedback.id): older-observation"
        ),
        labels=["feedback-review"],
        task_type="task",
    )
    task_manager = _FakeTaskManager(existing_tasks=[existing])
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster(
                    [current],
                    title="Stop repeated close-gate checks",
                    theme="close gate validation reruns",
                )
            ]
        }
    )

    result = await _service(temp_db, llm, task_manager).run_review()

    assert result["deduplicated"] == 1
    assert task_manager.created == []
    assert len(task_manager.updated) == 1
    assert "older-observation" in existing.description
    assert current in existing.description


async def test_run_review_suppresses_duplicate_of_reachable_recent_closed_task(
    temp_db: HubDatabase,
    session_id: str,
    tmp_path: Path,
) -> None:
    commit_sha = _commit_file(tmp_path / "gobby", "src/gobby/example.py", _T0)
    closed_task = _closed_task(
        task_id="closed-fix",
        seq_num=21999,
        title="Stop redundant close validation",
        commits=[commit_sha],
    )
    observation_id = _insert_feedback(temp_db, session_id, created_at=_T0 + timedelta(hours=1))
    task_manager = _FakeTaskManager(existing_closed_tasks=[closed_task])
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster(
                    [observation_id],
                    title="Stop repeated close-gate checks",
                    theme="close gate validation reruns",
                )
            ]
        }
    )

    result = await _service(temp_db, llm, task_manager).run_review()

    assert result["tasks_filed"] == 0
    assert result["deduplicated"] == 1
    assert task_manager.created == []
    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None and run.actions is not None
    assert run.actions["suppressed"] == [
        {
            "title": "Stop repeated close-gate checks",
            "observation_ids": [observation_id],
            "matched_task_ref": "#21999",
            "matched_commits": [commit_sha],
            "reason": (
                "matched recently closed valid task whose linked commits are reachable from HEAD"
            ),
        }
    ]


async def test_run_review_files_when_closed_duplicate_commit_is_absent_from_head(
    temp_db: HubDatabase,
    session_id: str,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "gobby"
    _commit_file(repo, "README.md", _T0 - timedelta(days=2))
    _git(repo, "switch", "-c", "resolved-elsewhere")
    absent_commit = _commit_file(repo, "src/gobby/example.py", _T0 - timedelta(days=1))
    _git(repo, "switch", "main")
    closed_task = _closed_task(
        task_id="closed-elsewhere",
        title="Stop redundant close validation",
        commits=[absent_commit],
    )
    observation_id = _insert_feedback(temp_db, session_id)
    task_manager = _FakeTaskManager(existing_closed_tasks=[closed_task])
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster(
                    [observation_id],
                    title="Stop repeated close-gate checks",
                    theme="close gate validation reruns",
                )
            ]
        }
    )

    result = await _service(temp_db, llm, task_manager).run_review()

    assert result["tasks_filed"] == 1
    assert result["deduplicated"] == 0
    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None and run.actions is not None
    assert run.actions["suppressed"] == []


async def test_run_review_bounds_recent_closed_task_lookup(
    temp_db: HubDatabase,
    session_id: str,
    tmp_path: Path,
) -> None:
    commit_sha = _commit_file(tmp_path / "gobby", "README.md", _T0)
    closed_tasks = [
        _closed_task(
            task_id=f"recent-{index}",
            title=f"Unrelated recent task {index}",
            theme=f"unrelated theme {index}",
            commits=[commit_sha],
        )
        for index in range(_RECENT_CLOSED_TASK_LIMIT)
    ]
    closed_tasks.append(
        _closed_task(
            task_id="too-old-match",
            title="Stop redundant close validation",
            commits=[commit_sha],
        )
    )
    observation_id = _insert_feedback(temp_db, session_id)
    task_manager = _FakeTaskManager(existing_closed_tasks=closed_tasks)
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster(
                    [observation_id],
                    title="Stop repeated close-gate checks",
                    theme="close gate validation reruns",
                )
            ]
        }
    )

    result = await _service(temp_db, llm, task_manager).run_review()

    assert result["tasks_filed"] == 1
    closed_call = next(call for call in task_manager.list_calls if call["closed"] is True)
    assert closed_call == {
        "closed": True,
        "limit": _RECENT_CLOSED_TASK_LIMIT,
        "offset": 0,
        "sort_by": "updated_at",
        "sort_order": "desc",
    }


async def test_run_review_marks_missing_cited_path_unverified_at_priority_three(
    temp_db: HubDatabase,
    session_id: str,
    tmp_path: Path,
) -> None:
    _commit_file(tmp_path / "gobby", "README.md", _T0 - timedelta(days=1))
    observation_id = _insert_feedback(temp_db, session_id)
    task_manager = _FakeTaskManager()
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster(
                    [observation_id],
                    title="Verify missing feedback path",
                    priority=1,
                    cited_paths=["src/gobby/missing.py"],
                )
            ]
        }
    )

    await _service(temp_db, llm, task_manager).run_review()

    task = task_manager.created[0]
    assert task.priority == 3
    assert "unverified-premise" in task.labels
    assert "Premise verification: missing at HEAD: src/gobby/missing.py" in task.description


async def test_run_review_marks_path_touched_after_observation_possibly_fixed(
    temp_db: HubDatabase,
    session_id: str,
    tmp_path: Path,
) -> None:
    observation_id = _insert_feedback(temp_db, session_id, created_at=_T0)
    commit_sha = _commit_file(
        tmp_path / "gobby",
        "src/gobby/example.py",
        _T0 + timedelta(hours=1),
    )
    task_manager = _FakeTaskManager(
        existing_closed_tasks=[
            _closed_task(
                task_id="recent-unrelated",
                title="Repair unrelated task indexing",
                theme="task index refresh",
                commits=[commit_sha],
            )
        ]
    )
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster(
                    [observation_id],
                    title="Recheck recently changed feedback premise",
                    cited_paths=["src/gobby/example.py"],
                )
            ]
        }
    )

    result = await _service(temp_db, llm, task_manager).run_review()

    task = task_manager.created[0]
    assert "possibly-fixed" in task.labels
    assert commit_sha in task.description
    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None and run.actions is not None
    assert run.actions["suppressed"] == []


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
    assert task.labels == [
        "feedback-review",
        "llm-reviewed",
        "awaiting-human-review",
        "needs-decision",
    ]
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
        temp_db,
        session_id,
        created_at=_T0 + timedelta(minutes=1),
        disposition="filed-task",
        evidence="Filed #21484",
    )
    llm = _FakeLLM(
        response={
            "clusters": [
                _cluster([shirked_obs], title="Fix close-gate rerun", theme="deferred defect"),
                _cluster([filed_obs], theme="already filed defect"),
            ]
        }
    )
    task_manager = _FakeTaskManager(
        tasks_by_ref={
            "#21484": SimpleNamespace(
                closed_at=None,
                claimed_by_session_id=None,
                labels=[],
            )
        }
    )
    service = _service(temp_db, llm, task_manager)

    result = await service.run_review()

    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None
    assert run.digest_md is not None
    assert "## Shirked found work" in run.digest_md
    assert "**deferred defect** (1 obs; dispositions: none 1)" in run.digest_md
    assert (
        "**already filed defect** (1 obs; dispositions: filed-task 1; #21484 filed unclaimed)"
    ) in run.digest_md


@pytest.mark.asyncio
async def test_digest_accepts_labeled_rung_three_filing(
    temp_db: HubDatabase, session_id: str
) -> None:
    filed_obs = _insert_feedback(
        temp_db,
        session_id,
        disposition="filed-task",
        evidence="Filed #21485",
    )
    llm = _FakeLLM(response={"clusters": [_cluster([filed_obs], theme="decision-bound defect")]})
    task_manager = _FakeTaskManager(
        tasks_by_ref={
            "#21485": SimpleNamespace(
                closed_at=None,
                claimed_by_session_id=None,
                labels=["needs-decision"],
            )
        }
    )

    result = await _service(temp_db, llm, task_manager).run_review()

    run = FeedbackReviewStore(temp_db).get_run(result["run_id"])
    assert run is not None
    assert run.digest_md is not None
    assert "**decision-bound defect** (" not in run.digest_md

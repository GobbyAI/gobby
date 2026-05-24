from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _stage(stage_name: str, state: str, updated_at: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=stage_name,
        stage_name=stage_name,
        state=state,
        updated_at=updated_at,
        position=0,
    )


def _candidate(stage_state: str = "in_progress") -> SimpleNamespace:
    stage = _stage("development", stage_state, "2026-05-02T00:00:00+00:00")
    return SimpleNamespace(
        id="task-1",
        lifecycle="in_development",
        status="open",
        current_stage=stage,
        stages=[stage],
    )


def test_snapshot_match_api() -> None:
    from gobby.dispatch.mutex import RuntimeDispatchMutex

    field_names = {field.name for field in fields(RuntimeDispatchMutex)}
    assert {
        "expected_stage_name",
        "expected_stage_state",
        "expected_stage_updated_at",
    } <= field_names
    assert "expected_lifecycle" not in field_names
    assert "expected_status" not in field_names

    assert RuntimeDispatchMutex.candidate_snapshot_matches(
        _candidate(),
        stage_name="development",
        stage_state="in_progress",
        stage_updated_at="2026-05-02T00:00:00+00:00",
    )
    assert not RuntimeDispatchMutex.candidate_snapshot_matches(
        _candidate("ready"),
        stage_name="development",
        stage_state="in_progress",
        stage_updated_at="2026-05-02T00:00:00+00:00",
    )
    assert not RuntimeDispatchMutex.candidate_snapshot_matches(
        _candidate("done"),
        stage_name="development",
        stage_state="done",
        stage_updated_at="2026-05-02T00:00:00+00:00",
    )
    assert not hasattr(RuntimeDispatchMutex, "candidate_tuple_matches")


async def test_heartbeat_passes_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.dispatch import dispatcher

    candidate = _candidate("needs_review")
    heartbeat_db = SimpleNamespace()
    captured: dict[str, object] = {}

    class SpyMutex:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def __enter__(self) -> SpyMutex:
            return self

        def release(self) -> bool:
            captured["released"] = True
            return True

        def attach(self, run_id: str) -> None:
            captured["run_id"] = run_id

        @staticmethod
        def candidate_tuple_matches(*args: object, **kwargs: object) -> bool:
            return True

        @staticmethod
        def candidate_stage_snapshot_matches(*args: object, **kwargs: object) -> bool:
            return True

    class SpyWriteSetGuard:
        @classmethod
        def load(cls, db: object, *, project_id: str | None = None) -> SpyWriteSetGuard:
            captured["write_set_db"] = db
            captured["write_set_project_id"] = project_id
            return cls()

        def action_reserves_write_set(self, action: object, task: object) -> bool:
            return False

        def conflict_for(self, task_id: str) -> object:
            raise AssertionError("conflict_for should not run when no action reserves files")

        def reserve(self, task_id: str) -> None:
            raise AssertionError("reserve should not run when no action reserves files")

    monkeypatch.setattr(dispatcher, "RuntimeDispatchMutex", SpyMutex)
    monkeypatch.setattr(dispatcher, "list_automation_candidates", lambda *a, **k: [candidate])
    monkeypatch.setattr(dispatcher, "sweep_stale_claims", lambda *a, **k: 0)
    monkeypatch.setattr(dispatcher, "sweep_orphan_no_run_dispatch_mutexes", lambda *a, **k: 0)
    monkeypatch.setattr(dispatcher, "count_active_agents", lambda *a, **k: 0)
    monkeypatch.setattr(dispatcher, "reload_candidate", lambda *a, **k: candidate)
    monkeypatch.setattr(dispatcher, "build_context", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *a, **k: None)
    monkeypatch.setattr(dispatcher, "DispatchWriteSetGuard", SpyWriteSetGuard)

    result = await dispatcher.run_heartbeat(db=heartbeat_db, project_id="project-1")

    assert result.skipped == 1
    assert captured["expected_stage_name"] == "development"
    assert captured["expected_stage_state"] == "needs_review"
    assert captured["expected_stage_updated_at"] == "2026-05-02T00:00:00+00:00"
    assert captured["write_set_db"] is heartbeat_db
    assert captured["write_set_project_id"] == "project-1"
    assert "expected_lifecycle" not in captured
    assert "expected_status" not in captured

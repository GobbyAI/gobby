"""Tests for the expansion QA coverage integration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.plans.coverage import evaluate
from gobby.plans.coverage_manifest import write_manifest
from gobby.tasks import expansion_qa_coverage as qa_module

pytestmark = pytest.mark.unit

_PROJECT_ID = "project"
_ROOT_REF = "127"


def _plan_file(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "plan.md"
    path.write_text(
        """> **Plan ID:** plan

## A1 Work [category: code]
`kind: deliverable`

Implement the covered behavior.

**Acceptance:**
- A1.1 - Behavior exists. file: `src/behavior.py`
""",
        encoding="utf-8",
    )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _task_records() -> list[dict[str, object]]:
    return [
        {"ref": f"#{_ROOT_REF}", "path_cache": _ROOT_REF},
        {
            "ref": "#128",
            "path_cache": f"{_ROOT_REF}.128",
            "labels": ["covers:plan:A1:A1.1"],
            "validation_criteria": "Touches src/behavior.py.",
        },
    ]


@dataclass
class _FakeTask:
    id: str = "root-task-uuid"


@dataclass
class _FakeRun:
    id: str = "run-uuid"
    parent_task_id: str = "root-task-uuid"
    status: str = "running"


@dataclass
class _FakeArtifacts:
    worktree_path: None = None
    clone_path: None = None
    plan_file_hash: None = None
    base_commit_sha: None = None


class _FakeTaskManager:
    db = object()

    def __init__(self) -> None:
        self.requested_refs: list[str] = []

    def get_task(self, ref: str, project_id: str | None = None) -> _FakeTask:
        self.requested_refs.append(ref)
        return _FakeTask()


class _FakeArtifactManager:
    def __init__(self, db: object) -> None:
        pass

    def get_artifacts(self, task_id: str) -> _FakeArtifacts:
        return _FakeArtifacts()

    def set_artifacts_atomic(self, task_id: str, **fields: object) -> None:
        pass


class _FakeRunManager:
    def __init__(self, db: object) -> None:
        pass

    def save_qa_result(self, run_id: str, qa_result: dict[str, object]) -> _FakeRun:
        return _FakeRun(status="completed")


def test_hash_prefixed_root_ref_matches_registry_written_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A '#N' root_task ref must not collide with the unprefixed manifest identity.

    gobby-plans writes the coverage manifest with the registry's unprefixed
    root_task_ref; the QA path previously forwarded the caller's '#N' spelling
    into the report header, and write_manifest treats the two spellings as
    distinct identities (PathIdentityMismatchError).
    """
    plan_path, plan_hash = _plan_file(tmp_path)

    registry_report = evaluate(
        plan=plan_path,
        plan_id="plan",
        plan_hash=plan_hash,
        task_tree="db",
        root_task_ref=_ROOT_REF,
        project_id=_PROJECT_ID,
        task_records=_task_records(),
    )
    existing_manifest = write_manifest(registry_report, tmp_path)

    monkeypatch.setattr(qa_module, "TaskArtifactManager", _FakeArtifactManager)
    monkeypatch.setattr(qa_module, "LocalExpansionRunManager", _FakeRunManager)

    seen_refs: list[str] = []

    def _evaluator(**kwargs: Any) -> Any:
        seen_refs.append(kwargs["root_task_ref"])
        return evaluate(
            plan=kwargs["plan_path"],
            plan_id=kwargs["plan_id"],
            plan_hash=kwargs["plan_hash"],
            task_tree=kwargs["task_tree"],
            root_task_ref=kwargs["root_task_ref"],
            project_id=kwargs["project_id"],
            task_records=_task_records(),
        )

    task_manager = _FakeTaskManager()
    result = qa_module.run_expansion_qa_coverage(
        task_manager=task_manager,  # type: ignore[arg-type]
        run=_FakeRun(),  # type: ignore[arg-type]
        repo_path=tmp_path,
        plan_path="plan.md",
        plan_id="plan",
        plan_hash=plan_hash,
        root_task_ref=f"#{_ROOT_REF}",
        project_id=_PROJECT_ID,
        evaluator=_evaluator,
    )

    assert result["ok"] is True
    assert result["passed"] is True
    assert seen_refs == [_ROOT_REF]
    assert task_manager.requested_refs == [_ROOT_REF]
    assert result["review_action"]["arguments"]["task_id"] == _ROOT_REF

    raw = yaml.safe_load(existing_manifest.read_text(encoding="utf-8"))
    assert raw["header"]["root_task_ref"] == _ROOT_REF

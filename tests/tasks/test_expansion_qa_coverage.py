"""Tests for the expansion QA coverage integration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import _expansion_registry as registry_module
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.plans.coverage import evaluate
from gobby.plans.coverage_manifest import write_manifest
from gobby.storage.expansion_runs import ExpansionRun
from gobby.storage.plans import PlanRecord
from gobby.storage.tasks import LocalTaskManager
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
    project_id: str = _PROJECT_ID
    plan_file: str | None = "plan.md"
    status: str = "running"


@dataclass
class _FakeArtifacts:
    worktree_path: None = None
    clone_path: None = None
    plan_file_hash: None = None
    base_commit_sha: None = None


class _FakeDb:
    """Hub double with an empty plan registry: every registry lookup misses."""

    def fetchone(self, _sql: str, _params: tuple[object, ...] = ()) -> None:
        return None


class _FakeTaskManager:
    db = _FakeDb()

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


def test_interactive_coverage_persists_inapplicable_review_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    saved_results: list[dict[str, object]] = []

    class _CapturingRunManager(_FakeRunManager):
        def save_qa_result(self, run_id: str, qa_result: dict[str, object]) -> _FakeRun:
            saved_results.append(qa_result)
            return super().save_qa_result(run_id, qa_result)

    monkeypatch.setattr(qa_module, "TaskArtifactManager", _FakeArtifactManager)
    monkeypatch.setattr(qa_module, "LocalExpansionRunManager", _CapturingRunManager)

    result = qa_module.run_expansion_qa_coverage(
        task_manager=cast(LocalTaskManager, _FakeTaskManager()),
        run=cast(ExpansionRun, _FakeRun()),
        repo_path=tmp_path,
        plan_path=str(plan_path),
        plan_id="plan",
        plan_hash=plan_hash,
        root_task_ref=_ROOT_REF,
        project_id=_PROJECT_ID,
        evaluator=lambda **_kwargs: evaluate(
            plan=plan_path,
            plan_id="plan",
            plan_hash=plan_hash,
            task_tree="db",
            root_task_ref=_ROOT_REF,
            project_id=_PROJECT_ID,
            task_records=_task_records(),
        ),
        is_spawned_agent=False,
    )

    expected_action = {
        "applicable": False,
        "reason": "Review actions require a spawned expansion-stage reviewer.",
    }
    assert result["ok"] is True
    assert result["passed"] is True
    assert result["manifest_path"]
    assert result["review_action"] == expected_action
    assert result["qa_result"]["failures"] == []
    assert result["qa_result"]["review_action"] == expected_action
    assert saved_results[0]["review_action"] == expected_action


@pytest.mark.parametrize(
    ("expected_passed", "expected_tool"),
    ((True, "approve_review"), (False, "reject_review")),
)
def test_spawned_coverage_persists_legacy_review_action_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_passed: bool,
    expected_tool: str,
) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    saved_results: list[dict[str, object]] = []

    class _CapturingRunManager(_FakeRunManager):
        def save_qa_result(self, run_id: str, qa_result: dict[str, object]) -> _FakeRun:
            saved_results.append(qa_result)
            return super().save_qa_result(run_id, qa_result)

    task_records = (
        _task_records() if expected_passed else [{"ref": f"#{_ROOT_REF}", "path_cache": _ROOT_REF}]
    )
    monkeypatch.setattr(qa_module, "TaskArtifactManager", _FakeArtifactManager)
    monkeypatch.setattr(qa_module, "LocalExpansionRunManager", _CapturingRunManager)

    result = qa_module.run_expansion_qa_coverage(
        task_manager=cast(LocalTaskManager, _FakeTaskManager()),
        run=cast(ExpansionRun, _FakeRun()),
        repo_path=tmp_path,
        plan_path=str(plan_path),
        plan_id="plan",
        plan_hash=plan_hash,
        root_task_ref=_ROOT_REF,
        project_id=_PROJECT_ID,
        evaluator=lambda **_kwargs: evaluate(
            plan=plan_path,
            plan_id="plan",
            plan_hash=plan_hash,
            task_tree="db",
            root_task_ref=_ROOT_REF,
            project_id=_PROJECT_ID,
            task_records=task_records,
        ),
        is_spawned_agent=True,
    )

    expected_action = qa_module._review_action(
        _ROOT_REF,
        result["manifest_path"],
        result["qa_result"]["failures"],
    )
    assert result["passed"] is expected_passed
    assert expected_action["server"] == "gobby-tasks-ops"
    assert expected_action["tool"] == expected_tool
    assert expected_action["arguments"]["task_id"] == _ROOT_REF
    assert expected_action["arguments"]["stage_name"] == "expansion"
    assert result["review_action"] == expected_action
    assert result["qa_result"]["review_action"] == expected_action
    assert saved_results[0]["review_action"] == expected_action


@pytest.mark.asyncio
async def test_registry_coverage_derives_bound_plan_from_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _FakeRun()
    root_task = SimpleNamespace(
        id=run.parent_task_id,
        project_id=run.project_id,
        seq_num=int(_ROOT_REF),
        parent_task_id=None,
        path_cache=_ROOT_REF,
    )
    task_manager = SimpleNamespace(
        db=object(),
        get_task=lambda _task_id: root_task,
    )
    plan = PlanRecord(
        id="plan-record-uuid",
        project_id=run.project_id,
        plan_id="plan",
        plan_path="plan.md",
        plan_hash="a" * 64,
        plan_kind="implementation",
        state="active",
        root_task_ref=_ROOT_REF,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class _RegistryRunManager:
        def __init__(self, _db: object) -> None:
            pass

        def get(self, _run_id: str) -> _FakeRun:
            return run

    class _RegistryPlanManager:
        def __init__(self, _db: object) -> None:
            pass

        def list_plans(self, **_filters: object) -> list[PlanRecord]:
            return [plan]

    captured: dict[str, object] = {}

    def _run_qa_coverage(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=task_manager,
            resolve_session_id=lambda _session_ref: "session-uuid",
            session_var_manager=SimpleNamespace(
                get_variables=lambda _session_id: {"is_spawned_agent": False}
            ),
            session_manager=SimpleNamespace(
                get=lambda _session_id: SimpleNamespace(agent_run_id=None, agent_depth=0)
            ),
            get_project_repo_path=lambda _project_id, _machine_id: str(tmp_path),
            checkout_machine_id=lambda _project_id, _session_ref: "machine-id",
        ),
    )
    registry = InternalToolRegistry("expansion-qa-test")
    registry_module._register_qa_tools(registry, ctx)
    monkeypatch.setattr(registry_module, "LocalExpansionRunManager", _RegistryRunManager)
    monkeypatch.setattr(registry_module, "LocalPlanManager", _RegistryPlanManager)
    monkeypatch.setattr(registry_module, "run_qa_coverage", _run_qa_coverage)
    monkeypatch.setattr(registry_module, "get_current_session_id", lambda: "session-ref")

    result = await registry.call("run_expansion_qa_coverage", {"run_id": run.id})

    schema = registry.get_schema("run_expansion_qa_coverage")
    assert schema is not None
    assert schema["inputSchema"]["required"] == ["run_id"]
    assert result == {"ok": True}
    assert captured["plan_path"] == plan.plan_path
    assert captured["plan_id"] == plan.plan_id
    assert captured["plan_hash"] == plan.plan_hash
    assert captured["root_task_ref"] == plan.root_task_ref
    assert captured["project_id"] == plan.project_id
    assert captured["is_spawned_agent"] is False


def test_bound_plan_derivation_surfaces_ambiguous_active_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_ambiguous(
        _ctx: RegistryContext,
        _task_id: str,
        plan_file: str | None,
        *,
        reset_output: bool,
    ) -> tuple[str, str | None]:
        assert plan_file is None
        assert reset_output is False
        raise ValueError("Task #127 is covered by multiple active registered plans")

    monkeypatch.setattr(registry_module, "_bind_registered_plan", _raise_ambiguous)

    with pytest.raises(ValueError, match="multiple active registered plans"):
        registry_module._bound_plan_for_run(
            cast(RegistryContext, SimpleNamespace()),
            cast(ExpansionRun, _FakeRun()),
        )


def _deferred_plan_file(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "plan.md"
    path.write_text(
        """> **Plan ID:** plan

## A1 Deferred Work
`kind: deferred`

```yaml
deferral:
  task_ref: "#999"
  reason: "covered by follow-up"
  owner: "backend"
  original_acceptance_items:
    - A1.1
```

## A2 Uncovered Work [category: code]
`kind: deliverable`

Target: `src/uncovered.py`

Implement the uncovered behavior.

**Acceptance:**
- A2.1 - Behavior exists. file: `src/uncovered.py`
""",
        encoding="utf-8",
    )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_coverage_failures_report_deferral_validator_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA failures name why a deferral failed, keeping the status for rows without detail."""
    _, plan_hash = _deferred_plan_file(tmp_path)

    monkeypatch.setattr(qa_module, "TaskArtifactManager", _FakeArtifactManager)
    monkeypatch.setattr(qa_module, "LocalExpansionRunManager", _FakeRunManager)

    def _evaluator(**kwargs: Any) -> Any:
        return evaluate(
            plan=kwargs["plan_path"],
            plan_id=kwargs["plan_id"],
            plan_hash=kwargs["plan_hash"],
            task_tree=kwargs["task_tree"],
            root_task_ref=kwargs["root_task_ref"],
            project_id=kwargs["project_id"],
            task_records=[
                {"ref": f"#{_ROOT_REF}", "path_cache": _ROOT_REF, "dependencies": ["#999"]},
                {
                    "ref": "#999",
                    "path_cache": f"{_ROOT_REF}.999",
                    "state": "ready",
                    "labels": [],
                    "validation_criteria": "Follow-up owns A1.1.",
                },
            ],
        )

    result = qa_module.run_expansion_qa_coverage(
        task_manager=cast(LocalTaskManager, _FakeTaskManager()),
        run=cast(ExpansionRun, _FakeRun()),
        repo_path=tmp_path,
        plan_path="plan.md",
        plan_id="plan",
        plan_hash=plan_hash,
        root_task_ref=f"#{_ROOT_REF}",
        project_id=_PROJECT_ID,
        evaluator=_evaluator,
    )

    assert result["passed"] is False
    failures = result["qa_result"]["failures"]
    assert [(failure["item_id"], failure["status"], failure["detail"]) for failure in failures] == [
        ("A1.1", "invalid", "task labels do not include 'deferred-from:plan:A1'"),
        ("A2.1", "missing", "coverage status missing"),
    ]

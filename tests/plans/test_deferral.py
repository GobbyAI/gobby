from __future__ import annotations

import inspect

import pytest

from gobby.plans.deferral import DeferralValidationResult, TaskStoreProtocol, validate_deferral
from gobby.plans.parser import AcceptanceItem, ArtifactKind, Deferral

pytestmark = pytest.mark.unit

PLAN_ID = "plan"
SECTION_ID = "A1"
RECOVERY_EPIC_REF = "#epic"
DEFERRED_TASK_REF = "#20"
PROVENANCE_LABEL = f"deferred-from:{PLAN_ID}:{SECTION_ID}"
ITEM = AcceptanceItem(
    item_id="A1.1",
    prose="deferred file item",
    artifact_kind=ArtifactKind.file,
    artifact_ref="src/deferred.py",
    source_line=9,
)


class FakeStore:
    def __init__(
        self,
        *,
        tasks: dict[str, dict] | None = None,
        labels: dict[str, list[str]] | None = None,
        dependencies: dict[str, list[str]] | None = None,
    ) -> None:
        self.tasks = tasks or {}
        self.labels = labels or {}
        self.dependencies = dependencies or {}
        self.task_calls: list[str] = []
        self.label_calls: list[str] = []
        self.dependency_calls: list[str] = []

    def get_task(self, task_ref: str) -> dict | None:
        self.task_calls.append(task_ref)
        return self.tasks.get(task_ref)

    def get_task_labels(self, task_ref: str) -> list[str]:
        self.label_calls.append(task_ref)
        return self.labels.get(task_ref, [])

    def get_task_dependencies(self, task_ref: str) -> list[str]:
        self.dependency_calls.append(task_ref)
        return self.dependencies.get(task_ref, [])


def _deferral(*, reason: str = "covered elsewhere", owner: str = "owner") -> Deferral:
    return Deferral(
        task_ref=DEFERRED_TASK_REF,
        reason=reason,
        owner=owner,
        original_acceptance_items=(ITEM,),
        raw_block="",
    )


def _task(*, status: str = "open", criteria: str = "Validate src/deferred.py") -> dict:
    return {"status": status, "validation_criteria": criteria}


def _validate(deferral: Deferral, store: TaskStoreProtocol) -> DeferralValidationResult:
    return validate_deferral(
        deferral,
        PLAN_ID,
        SECTION_ID,
        store,
        recovery_epic_ref=RECOVERY_EPIC_REF,
    )


def _passing_store(*, dependencies: dict[str, list[str]] | None = None) -> FakeStore:
    return FakeStore(
        tasks={DEFERRED_TASK_REF: _task()},
        labels={DEFERRED_TASK_REF: [PROVENANCE_LABEL]},
        dependencies=dependencies or {RECOVERY_EPIC_REF: [DEFERRED_TASK_REF]},
    )


def test_validate_task_missing() -> None:
    store = FakeStore()

    result = _validate(_deferral(), store)

    assert result.status == "task_missing"
    assert store.label_calls == []
    assert store.dependency_calls == []


def test_validate_task_closed() -> None:
    store = FakeStore(tasks={DEFERRED_TASK_REF: _task(status="closed")})

    result = _validate(_deferral(), store)

    assert result.status == "task_closed"
    assert store.label_calls == []
    assert store.dependency_calls == []


def test_validate_missing_provenance_label() -> None:
    store = FakeStore(tasks={DEFERRED_TASK_REF: _task()}, labels={DEFERRED_TASK_REF: []})

    result = _validate(_deferral(), store)

    assert result.status == "missing_provenance_label"
    assert store.dependency_calls == []


def test_validate_criteria_does_not_duplicate() -> None:
    store = FakeStore(
        tasks={DEFERRED_TASK_REF: _task(criteria="Validate the deferred task broadly.")},
        labels={DEFERRED_TASK_REF: [PROVENANCE_LABEL]},
    )

    result = _validate(_deferral(), store)

    assert result.status == "validation_criteria_does_not_duplicate"
    assert store.dependency_calls == []


def test_validate_missing_reason_or_owner() -> None:
    store = _passing_store()

    result = _validate(_deferral(reason=" ", owner="owner"), store)

    assert result.status == "missing_reason_or_owner"
    assert store.dependency_calls == []


def test_validate_missing_dependency_or_cited_parent() -> None:
    store = _passing_store(dependencies={RECOVERY_EPIC_REF: []})

    result = _validate(_deferral(), store)

    assert result.status == "missing_dependency_or_cited_parent"


def test_validate_dependency_path() -> None:
    parameter = inspect.signature(validate_deferral).parameters["recovery_epic_ref"]
    store = _passing_store(
        dependencies={RECOVERY_EPIC_REF: ["#intermediate"], "#intermediate": [DEFERRED_TASK_REF]}
    )

    result = _validate(_deferral(), store)

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert result.status == "valid"


def test_validate_cited_parent_path() -> None:
    store = FakeStore(
        tasks={
            DEFERRED_TASK_REF: _task(),
            "#parent": _task(criteria="Parent task"),
        },
        labels={
            DEFERRED_TASK_REF: [PROVENANCE_LABEL, "cited-parent:#parent"],
            "#parent": [f"out-of-scope-for:{RECOVERY_EPIC_REF}"],
        },
        dependencies={RECOVERY_EPIC_REF: []},
    )

    result = _validate(_deferral(), store)

    assert result.status == "valid"


def test_validate_cited_parent_without_out_of_scope_label_rejected() -> None:
    store = FakeStore(
        tasks={
            DEFERRED_TASK_REF: _task(),
            "#parent": _task(criteria="Parent task"),
        },
        labels={
            DEFERRED_TASK_REF: [PROVENANCE_LABEL, "cited-parent:#parent"],
            "#parent": [],
        },
        dependencies={RECOVERY_EPIC_REF: []},
    )

    result = _validate(_deferral(), store)

    assert result.status == "missing_dependency_or_cited_parent"


def test_validate_cited_parent_inside_dependency_closure_rejected() -> None:
    store = FakeStore(
        tasks={
            DEFERRED_TASK_REF: _task(),
            "#parent": _task(criteria="Parent task"),
        },
        labels={
            DEFERRED_TASK_REF: [PROVENANCE_LABEL, "cited-parent:#parent"],
            "#parent": [f"out-of-scope-for:{RECOVERY_EPIC_REF}"],
        },
        dependencies={RECOVERY_EPIC_REF: ["#parent"]},
    )

    result = _validate(_deferral(), store)

    assert result.status == "missing_dependency_or_cited_parent"


def test_validate_happy_path() -> None:
    result = _validate(_deferral(), _passing_store())

    assert result.status == "valid"

from __future__ import annotations

import inspect
from typing import Any

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
        tasks: dict[str, dict[str, Any]] | None = None,
        labels: dict[str, list[str]] | None = None,
        dependencies: dict[str, list[str]] | None = None,
    ) -> None:
        self.tasks = tasks or {}
        self.labels = labels or {}
        self.dependencies = dependencies or {}
        self.task_calls: list[str] = []
        self.label_calls: list[str] = []
        self.dependency_calls: list[str] = []

    def get_task(self, task_ref: str) -> dict[str, Any] | None:
        self.task_calls.append(task_ref)
        return self.tasks.get(task_ref)

    def get_task_labels(self, task_ref: str) -> list[str]:
        self.label_calls.append(task_ref)
        return self.labels.get(task_ref, [])

    def get_task_dependencies(self, task_ref: str) -> list[str]:
        self.dependency_calls.append(task_ref)
        return self.dependencies.get(task_ref, [])


def _deferral(
    *,
    task_ref: str = DEFERRED_TASK_REF,
    reason: str = "covered elsewhere",
    owner: str = "owner",
    item: AcceptanceItem = ITEM,
) -> Deferral:
    return Deferral(
        task_ref=task_ref,
        reason=reason,
        owner=owner,
        original_acceptance_items=(item,),
        raw_block="",
    )


def _task(
    *,
    state: str = "ready",
    criteria: str = "Validate src/deferred.py",
    closed_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "closed_reason": closed_reason,
        "validation_criteria": criteria,
    }


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
    store = FakeStore(tasks={DEFERRED_TASK_REF: _task(state="closed")})

    result = _validate(_deferral(), store)

    assert result.status == "task_closed"
    assert store.label_calls == []
    assert store.dependency_calls == []


@pytest.mark.parametrize("closed_reason", ["completed", "already_implemented"])
def test_validate_closed_target_that_delivered_its_obligation(closed_reason: str) -> None:
    """A deferral names work another task owns, so that task finishing is the success case."""
    store = FakeStore(
        tasks={DEFERRED_TASK_REF: _task(state="closed", closed_reason=closed_reason)},
        labels={DEFERRED_TASK_REF: [PROVENANCE_LABEL]},
        dependencies={RECOVERY_EPIC_REF: [DEFERRED_TASK_REF]},
    )

    result = _validate(_deferral(), store)

    assert result.status == "valid"


@pytest.mark.parametrize("closed_reason", ["wont_fix", "obsolete", "out_of_repo", "duplicate"])
def test_validate_closed_target_that_abandoned_its_obligation(closed_reason: str) -> None:
    """Abandonment leaves the obligation unowned; `duplicate` moves it to an unnamed task."""
    store = FakeStore(
        tasks={DEFERRED_TASK_REF: _task(state="closed", closed_reason=closed_reason)},
        labels={DEFERRED_TASK_REF: [PROVENANCE_LABEL]},
        dependencies={RECOVERY_EPIC_REF: [DEFERRED_TASK_REF]},
    )

    result = _validate(_deferral(), store)

    assert result.status == "task_closed"
    assert closed_reason in result.detail
    assert store.label_calls == []


def test_validate_closed_target_without_a_recorded_reason_fails_closed() -> None:
    store = FakeStore(
        tasks={DEFERRED_TASK_REF: _task(state="closed", closed_reason=None)},
        labels={DEFERRED_TASK_REF: [PROVENANCE_LABEL]},
        dependencies={RECOVERY_EPIC_REF: [DEFERRED_TASK_REF]},
    )

    result = _validate(_deferral(), store)

    assert result.status == "task_closed"


def test_validate_delivered_target_still_runs_the_remaining_gates() -> None:
    """Delivery waives only the open-state gate: provenance and criteria still apply."""
    store = FakeStore(
        tasks={DEFERRED_TASK_REF: _task(state="closed", closed_reason="completed")},
        labels={DEFERRED_TASK_REF: []},
        dependencies={RECOVERY_EPIC_REF: [DEFERRED_TASK_REF]},
    )

    result = _validate(_deferral(), store)

    assert result.status == "missing_provenance_label"


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


@pytest.mark.parametrize(
    ("item", "criteria", "expected_status"),
    [
        (
            AcceptanceItem(
                item_id="A1.1",
                prose="deferred symbol",
                artifact_kind=ArtifactKind.symbol,
                artifact_ref="gobby.plans.parser.parse",
                source_line=9,
            ),
            "The parser is great.",
            "validation_criteria_does_not_duplicate",
        ),
        (
            AcceptanceItem(
                item_id="A1.1",
                prose="deferred file",
                artifact_kind=ArtifactKind.file,
                artifact_ref="test.py",
                source_line=9,
            ),
            "Run latest.py.",
            "validation_criteria_does_not_duplicate",
        ),
        (
            AcceptanceItem(
                item_id="A1.1",
                prose="deferred symbol",
                artifact_kind=ArtifactKind.symbol,
                artifact_ref="gobby.plans.parser.parse",
                source_line=9,
            ),
            "Call parse().",
            "valid",
        ),
        (
            AcceptanceItem(
                item_id="A1.1",
                prose="deferred file",
                artifact_kind=ArtifactKind.file,
                artifact_ref="test.py",
                source_line=9,
            ),
            "Run tests/test.py.",
            "valid",
        ),
    ],
    ids=["symbol-prefix", "file-suffix", "bounded-symbol", "bounded-file"],
)
def test_validate_criteria_uses_artifact_boundaries(
    item: AcceptanceItem, criteria: str, expected_status: str
) -> None:
    store = FakeStore(
        tasks={DEFERRED_TASK_REF: _task(criteria=criteria)},
        labels={DEFERRED_TASK_REF: [PROVENANCE_LABEL]},
        dependencies={RECOVERY_EPIC_REF: [DEFERRED_TASK_REF]},
    )

    result = _validate(_deferral(item=item), store)

    assert result.status == expected_status


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


@pytest.mark.parametrize(
    "task_ref",
    ["#12345", "12345"],
    ids=["canonical", "unquoted-integer-form"],
)
def test_validate_numeric_task_ref_formats_match_dependency_closure(task_ref: str) -> None:
    canonical_ref = "#12345"
    store = FakeStore(
        tasks={canonical_ref: _task()},
        labels={canonical_ref: [PROVENANCE_LABEL]},
        dependencies={RECOVERY_EPIC_REF: [canonical_ref]},
    )

    result = _validate(_deferral(task_ref=task_ref), store)

    assert result.status == "valid"
    assert store.task_calls[0] == canonical_ref
    assert store.label_calls[0] == canonical_ref


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


def test_validate_cited_parent_non_active_state_rejected() -> None:
    store = FakeStore(
        tasks={
            DEFERRED_TASK_REF: _task(),
            "#parent": _task(state="blocked", criteria="Parent task"),
        },
        labels={
            DEFERRED_TASK_REF: [PROVENANCE_LABEL, "cited-parent:#parent"],
            "#parent": [f"out-of-scope-for:{RECOVERY_EPIC_REF}"],
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


@pytest.mark.parametrize(
    "cited_parent_label",
    ["cited-parent:#12345", "cited-parent:12345"],
    ids=["canonical", "unquoted-integer-form"],
)
def test_validate_numeric_cited_parent_inside_dependency_closure_rejected(
    cited_parent_label: str,
) -> None:
    canonical_ref = "#12345"
    plain_ref = "12345"
    out_of_scope_label = f"out-of-scope-for:{RECOVERY_EPIC_REF}"
    store = FakeStore(
        tasks={
            DEFERRED_TASK_REF: _task(),
            canonical_ref: _task(criteria="Parent task"),
            plain_ref: _task(criteria="Parent task"),
        },
        labels={
            DEFERRED_TASK_REF: [PROVENANCE_LABEL, cited_parent_label],
            canonical_ref: [out_of_scope_label],
            plain_ref: [out_of_scope_label],
        },
        dependencies={RECOVERY_EPIC_REF: [canonical_ref]},
    )

    result = _validate(_deferral(), store)

    assert result.status == "missing_dependency_or_cited_parent"


def test_validate_happy_path() -> None:
    result = _validate(_deferral(), _passing_store())

    assert result.status == "valid"

"""Contract tests for the Phase 2 stage registry manager."""

from __future__ import annotations

import inspect

import pytest

from tests.storage.tasks._stage_test_helpers import require_stage_registry_types

pytestmark = pytest.mark.unit


def test_stage_registry_manager_exposes_listed_methods(temp_db) -> None:
    StageRegistryEntry, StageRegistryManager = require_stage_registry_types()

    manager = StageRegistryManager(temp_db)

    assert inspect.signature(StageRegistryEntry).parameters.keys() >= {
        "name",
        "display_label",
        "description",
        "category",
        "default_agent",
        "reviewer_agent",
        "reviewer_agent_selector_json",
        "review_policy",
        "dispatch_type",
        "dispatch_target",
        "dispatch_inputs_json",
        "position_hint",
        "requires_human",
        "is_terminal",
        "default_max_work_attempts",
        "default_max_review_rounds",
    }
    for method_name in {
        "list_all",
        "get",
        "upsert",
        "list_default_stages",
        "set_default_stages",
    }:
        assert callable(getattr(manager, method_name))


def test_stage_registry_upsert_round_trips_review_policy_and_caps(temp_db) -> None:
    StageRegistryEntry, StageRegistryManager = require_stage_registry_types()
    manager = StageRegistryManager(temp_db)
    entry = StageRegistryEntry(
        name="operator_review",
        display_label="Operator Review",
        description="A local operator review gate.",
        category="verification",
        default_agent=None,
        reviewer_agent="qa-reviewer",
        reviewer_agent_selector_json=None,
        review_policy="optional",
        dispatch_type="pipeline",
        dispatch_target="operator-review",
        dispatch_inputs_json='{"task_id": "${{ task_id }}"}',
        position_hint=999,
        requires_human=True,
        is_terminal=False,
        default_max_work_attempts=2,
        default_max_review_rounds=7,
    )

    manager.upsert(entry, bundled_hash=None)

    actual = manager.get("operator_review")
    assert actual == entry


def test_stage_registry_upsert_accepts_reviewer_selector(temp_db) -> None:
    StageRegistryEntry, StageRegistryManager = require_stage_registry_types()
    manager = StageRegistryManager(temp_db)
    selector_json = (
        '{"default": "qa-reviewer", '
        '"rules": [{"category": "docs", "reviewer_agent": "doc-reviewer"}]}'
    )
    entry = StageRegistryEntry(
        name="development_docs",
        display_label="Development Docs",
        description="Development with category-aware docs review.",
        category="implementation",
        default_agent="backend-developer",
        reviewer_agent=None,
        reviewer_agent_selector_json=selector_json,
        review_policy="required",
        dispatch_type=None,
        dispatch_target=None,
        dispatch_inputs_json=None,
        position_hint=1000,
        requires_human=False,
        is_terminal=False,
        default_max_work_attempts=3,
        default_max_review_rounds=5,
    )

    manager.upsert(entry)

    actual = manager.get("development_docs")
    assert actual == entry


def test_stage_registry_default_stages_are_sorted_and_replace_atomically(temp_db) -> None:
    _, StageRegistryManager = require_stage_registry_types()
    manager = StageRegistryManager(temp_db)

    manager.set_default_stages(
        "contract_task",
        [("merge", 2), ("development", 0), ("pr", 1)],
    )
    assert manager.list_default_stages("contract_task") == [
        ("development", 0),
        ("pr", 1),
        ("merge", 2),
    ]

    manager.set_default_stages("contract_task", [("planning", 0), ("development", 1)])
    assert manager.list_default_stages("contract_task") == [
        ("planning", 0),
        ("development", 1),
    ]


def test_review_anchor_default_stage_is_planning(temp_db) -> None:
    _, StageRegistryManager = require_stage_registry_types()
    manager = StageRegistryManager(temp_db)

    assert manager.list_default_stages("review_anchor") == [("planning", 0)]

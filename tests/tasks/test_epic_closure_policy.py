"""Bundled epic completion checks the exact candidate and every descendant author."""

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.closure_policy import ClosureSnapshot, evaluate_closure_policy
from tests.tasks.test_closure_policy import _bundled_closure_rules, _record, _selected_policy

pytestmark = pytest.mark.unit
_SHA = "a" * 40


@pytest.mark.parametrize(
    "fault", [None, "candidate", "subtree", "author", "open", "evidence", "base"]
)
@pytest.mark.parametrize("event", ["task_close", "task_land"])
def test_epic_final_approval(temp_db: HubDatabase, fault: str | None, event: str) -> None:
    records = tuple(
        _record(
            kind,
            "reviewer" if kind == "review" else "coordinator",
            index,
            reference={
                "scheme": "git",
                "value": "b" * 40 if fault == "candidate" and kind == "review" else _SHA,
            },
            evidence={
                "subtree_revision": "stale"
                if fault == "subtree" and kind == "review"
                else "revision"
            },
        )
        for index, kind in enumerate(
            ["implementation", "integration-validation", "self-review", "review", "closing"], 1
        )
    )
    facts: dict[str, Any] = {
        "task_files_clean": True,
        "attribution_complete": True,
        "integration_ancestry": True,
        "candidate_contains_integration_base": fault != "base",
        "candidate_covers_linked_commits": True,
        "candidate_commit_sha": _SHA,
        "subtree_revision": "revision",
        "descendants_closed": fault != "open",
        "children_closed": True,
        "descendants": [
            {
                "task": {"id": "leaf", "parent_task_id": "task-1"},
                "attestations": []
                if fault == "evidence"
                else [
                    _record(
                        "implementation", "reviewer" if fault == "author" else "developer", 1
                    ).model_dump(mode="json")
                ],
            }
        ],
    }
    snapshot = ClosureSnapshot.model_validate(
        {
            "event": event,
            "task": {
                "id": "task-1",
                "task_type": "epic",
                "category": "manual",
                "labels": ["epic-devteam"],
                "commits": [_SHA],
            },
            "attestations": records,
            "facts": facts,
        }
    )
    decision = evaluate_closure_policy(
        snapshot,
        agent=_selected_policy("task-closure-general"),
        rules=_bundled_closure_rules(temp_db),
    )
    assert decision.allowed is (fault is None or (fault == "base" and event == "task_close")), (
        decision.blockers
    )

"""Session automatic-title fallback and reasoning-effort lifecycle tests."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.sessions.reasoning_effort import observed_reasoning_effort
from gobby.sessions.title_lifecycle import (
    recompute_automatic_title,
    update_title_for_claim,
)
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import get_machine_id


def _session(session_manager: SessionManager, project_id: str, external_id: str) -> Any:
    return session_manager.register(
        external_id=external_id,
        machine_id=get_machine_id(),
        source="codex",
        project_id=project_id,
    )


def test_recompute_automatic_title_falls_back_to_provisional_after_close(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    from gobby.sessions import title_lifecycle

    assert [name for name in vars(title_lifecycle) if "heuristic" in name.lower()] == []

    session = _session(session_manager, sample_project["id"], "provisional-after-close")
    task_manager = LocalTaskManager(session_manager.db)
    task = task_manager.create_task(
        sample_project["id"],
        "Short-lived work",
        claimed_by_session_id=session.id,
        validation_criteria="The task title shows only while the claim is open.",
    )
    claimed = update_title_for_claim(session_manager, session.id, task)
    assert claimed is not None and claimed.title_source == "task"

    task_manager.close_task(task.id, force=True, closed_in_session_id=session.id)
    recomputed = recompute_automatic_title(session_manager, session.id)

    assert recomputed.title == f"test-project#{session.seq_num}: Codex"
    assert recomputed.title_source == "provisional"


def test_reasoning_effort_prefers_effective_then_requested() -> None:
    assert (
        observed_reasoning_effort(
            {
                "requested_reasoning_effort": {"level": " medium "},
                "launch_metadata": {"effective_reasoning_effort": {"level": " high "}},
            }
        )
        == "high"
    )
    assert (
        observed_reasoning_effort(
            {
                "effective_reasoning_effort": {"level": ""},
                "requested_reasoning_effort": "medium",
            }
        )
        == "medium"
    )


def test_reasoning_effort_accepts_strings_and_structured_levels() -> None:
    assert observed_reasoning_effort({"reasoningEffort": " xhigh "}) == "xhigh"
    assert observed_reasoning_effort({"effort": {"level": " xhigh "}}) == "xhigh"
    assert observed_reasoning_effort({}) is None


@pytest.mark.parametrize(
    "effort",
    ["", "   ", {}, {"level": ""}, {"level": "   "}, {"level": 3}, {"other": "high"}],
)
def test_reasoning_effort_ignores_empty_or_malformed_values(effort: Any) -> None:
    assert observed_reasoning_effort({"effort": effort}) is None

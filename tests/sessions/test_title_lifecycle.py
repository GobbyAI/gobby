"""Session heuristic-title and reasoning-effort lifecycle tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from gobby.sessions.reasoning_effort import observed_reasoning_effort
from gobby.sessions.title_lifecycle import (
    heuristic_title_suffix,
    promote_heuristic_title,
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


def test_heuristic_suffix_filters_only_confirmed_english_stopwords() -> None:
    assert (
        heuristic_title_suffix("Could you please réparer le système rapidement demain")
        == "réparer le système rapidement"
    )
    assert heuristic_title_suffix("/clear") is None
    assert heuristic_title_suffix("mcp__gobby__call_tool") is None


def test_heuristic_suffix_caps_four_words_at_sixty_characters() -> None:
    suffix = heuristic_title_suffix("extraordinaryword " * 4)

    assert suffix is not None
    assert len(suffix) == 60
    assert suffix.endswith("…")


def test_first_heuristic_wins_and_survives_task_precedence(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session = _session(session_manager, sample_project["id"], "heuristic-precedence")
    task_manager = LocalTaskManager(session_manager.db)
    task = task_manager.create_task(
        sample_project["id"],
        "Active work",
        claimed_by_session_id=session.id,
        validation_criteria="Task title remains visible while the task is open.",
    )
    task_title = update_title_for_claim(session_manager, session.id, task)
    assert task_title is not None and task_title.title_source == "task"

    promoted = promote_heuristic_title(
        session_manager,
        session.id,
        "Please investigate the failing session title race",
    )

    assert promoted.title_source == "task"
    assert promoted.heuristic_title == (
        f"test-project#{session.seq_num}: investigate failing session title"
    )

    promote_heuristic_title(session_manager, session.id, "Replace this saved heuristic")
    task_manager.close_task(task.id, force=True, closed_in_session_id=session.id)
    recomputed = recompute_automatic_title(session_manager, session.id)
    assert recomputed.title == promoted.heuristic_title
    assert recomputed.title_source == "heuristic"


def test_manual_title_keeps_display_while_first_heuristic_is_saved(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session = session_manager.register(
        external_id="manual-heuristic",
        machine_id=get_machine_id(),
        source="codex",
        project_id=sample_project["id"],
        title="Pinned title",
    )

    promoted = promote_heuristic_title(session_manager, session.id, "Inspect Arabic عنوان mixing")

    assert promoted.title == "Pinned title"
    assert promoted.title_source == "manual"
    assert (
        promoted.heuristic_title == f"test-project#{session.seq_num}: Inspect Arabic عنوان mixing"
    )


def test_concurrent_promotions_persist_one_complete_heuristic(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session = _session(session_manager, sample_project["id"], "concurrent-heuristic")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda prompt: promote_heuristic_title(session_manager, session.id, prompt),
                ("First concurrent prompt", "Second concurrent prompt"),
            )
        )

    saved = session_manager.get(session.id)
    assert saved is not None
    assert saved.heuristic_title in {
        f"test-project#{session.seq_num}: First concurrent prompt",
        f"test-project#{session.seq_num}: Second concurrent prompt",
    }
    assert all(result.heuristic_title == saved.heuristic_title for result in results)


def test_reasoning_effort_prefers_effective_then_requested() -> None:
    assert (
        observed_reasoning_effort(
            {
                "requested_reasoning_effort": "medium",
                "launch_metadata": {"effective_reasoning_effort": "high"},
            }
        )
        == "high"
    )
    assert observed_reasoning_effort({"reasoningEffort": "xhigh"}) == "xhigh"
    assert observed_reasoning_effort({}) is None

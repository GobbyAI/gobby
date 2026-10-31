"""A plain link keeps the declared candidate as the tip; an ancestor never displaces it."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.tools.task_commits import create_commit_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.commit_order import link_candidate, same_commit
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.landing_case import LandingCase, make_landing_case

pytestmark = pytest.mark.unit


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


def _in_order(shas: list[str], expected: list[str]) -> bool:
    # Storage keeps Git's canonical short SHA for a plain link, so compare by prefix.
    return len(shas) == len(expected) and all(
        same_commit(left, right) for left, right in zip(shas, expected, strict=True)
    )


async def _link_plainly(case: LandingCase, task_id: str, sha: str) -> dict[str, Any]:
    with (
        session_context_for_test(case.claimant),
        patch(
            "gobby.mcp_proxy.tools.task_commits.resolve_task_repo_path",
            return_value=str(case.repo),
        ),
    ):
        registry = create_commit_registry(
            LocalTaskManager(case.db), session_manager=SessionManager(case.db)
        )
        return await registry.call("link_commit", {"task_id": task_id, "commit_sha": sha})


async def test_plain_link_of_an_ancestor_keeps_the_declared_candidate_tip(
    case: LandingCase,
) -> None:
    ancestor = case.candidate("lane-tests", {"tests/test_change.py": "RED"})
    candidate = case.candidate("lane-impl", {"docs/change.md": "GREEN"}, base=ancestor)
    task = case.task()
    manager = LocalTaskManager(case.db)
    link_candidate(manager, task.id, candidate, str(case.repo))

    linked = await _link_plainly(case, task.id, ancestor)

    assert "error" not in linked, linked
    assert _in_order(linked["commits"], [ancestor, candidate]), linked
    assert _in_order(manager.get_task(task.id).commits or [], [ancestor, candidate])


async def test_plain_link_of_a_newer_commit_becomes_the_tip(case: LandingCase) -> None:
    older = case.candidate("lane-first", {"docs/first.md": "one"})
    newer = case.candidate("lane-second", {"docs/second.md": "two"}, base=older)
    task = case.task()

    await _link_plainly(case, task.id, older)
    linked = await _link_plainly(case, task.id, newer)

    assert "error" not in linked, linked
    assert _in_order(linked["commits"], [older, newer]), linked

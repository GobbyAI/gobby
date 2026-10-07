"""A handed-off claim stops holding a session's claim capacity (#23665).

A claim is handed off once a reviewer recorded an independent_review_approval
receipt for it and none of its attributed paths is uncommitted. The session keeps
at most one active claim, and that claim receives its edits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_receipts import INDEPENDENT_REVIEW_APPROVAL, record_close_receipt
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

_REVIEWED_SHA = "a" * 40


def _task(manager: LocalTaskManager, project: dict[str, Any], title: str) -> Task:
    return manager.create_task(
        project["id"],
        title,
        validation_criteria="The handed-off claim behavior is observable.",
    )


async def _call(registry: Any, session: Session, tool: str, **arguments: Any) -> dict[str, Any]:
    with session_context_for_test(session.id):
        result: dict[str, Any] = await registry.call(tool, arguments)
    return result


async def _claim(registry: Any, session: Session, task: Task) -> None:
    claimed = await _call(registry, session, "claim_task", task_id=task.id)
    assert claimed.get("success") is True, claimed


@pytest.fixture
def reviewer(temp_db: HubDatabase, sample_project: dict[str, Any]) -> Session:
    return SessionManager(temp_db).register(
        external_id="handed-off-claim-reviewer",
        machine_id="21000000-0000-4000-8000-00000000000b",
        source="codex",
        project_id=sample_project["id"],
        title="Reviewer",
    )


def _approve(db: HubDatabase, task: Task, reviewer: Session) -> None:
    record_close_receipt(
        db,
        task=task,
        author_session_id=reviewer.id,
        kind=INDEPENDENT_REVIEW_APPROVAL,
        commit_sha=_REVIEWED_SHA,
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _repo_with_committed_file(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.py").write_text("one\n")
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _variables(db: HubDatabase, session: Session) -> dict[str, Any]:
    return SessionVariableManager(db).get_variables(session.id)


@pytest.mark.asyncio
async def test_handed_off_claim_lets_the_session_claim_another_task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    reviewer: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    landed = _task(manager, sample_project, "Landed")
    nxt = _task(manager, sample_project, "Next")
    await _claim(registry, canonical_task_session, landed)
    _approve(temp_db, landed, reviewer)

    claimed = await _call(registry, canonical_task_session, "claim_task", task_id=nxt.id)

    assert claimed == {"success": True, "task_id": nxt.id, "title": "Next"}
    assert manager.get_task(landed.id).claimed_by_session_id == canonical_task_session.id
    assert manager.get_task(nxt.id).claimed_by_session_id == canonical_task_session.id
    assert _variables(temp_db, canonical_task_session)["active_task_id"] == nxt.id


@pytest.mark.asyncio
async def test_handed_off_claim_lets_the_session_create_and_claim_a_task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    reviewer: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    landed = _task(manager, sample_project, "Landed")
    await _claim(registry, canonical_task_session, landed)
    _approve(temp_db, landed, reviewer)

    created = await _call(
        registry,
        canonical_task_session,
        "create_task",
        title="Created next",
        category="research",
        claim=True,
        validation_criteria="The created task is claimed.",
    )

    assert "error" not in created, created
    assert manager.get_task(created["id"]).claimed_by_session_id == canonical_task_session.id
    assert _variables(temp_db, canonical_task_session)["active_task_id"] == created["id"]


@pytest.mark.asyncio
async def test_several_active_claims_and_created_tasks_accumulate(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    reviewer: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    landed = _task(manager, sample_project, "Landed")
    active = _task(manager, sample_project, "Active")
    third = _task(manager, sample_project, "Third")
    await _claim(registry, canonical_task_session, landed)
    _approve(temp_db, landed, reviewer)
    await _claim(registry, canonical_task_session, active)

    claimed = await _call(registry, canonical_task_session, "claim_task", task_id=third.id)
    created = await _call(
        registry,
        canonical_task_session,
        "create_task",
        title="Additional active claim",
        category="research",
        claim=True,
        validation_criteria="Claims accumulate while earlier tasks stay active.",
    )

    assert claimed["success"] is True
    assert manager.get_task(third.id).claimed_by_session_id == canonical_task_session.id
    assert manager.get_task(created["id"]).claimed_by_session_id == canonical_task_session.id
    assert manager.get_task(active.id).claimed_by_session_id == canonical_task_session.id


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["dirty", "git_unavailable", "unreviewed"])
async def test_dirty_unreviewed_or_unavailable_claims_do_not_block_accumulation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    reviewer: Session,
    tmp_path: Path,
    state: str,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    held = _task(manager, sample_project, "Held")
    nxt = _task(manager, sample_project, "Next")
    await _claim(registry, canonical_task_session, held)
    if state == "git_unavailable":
        checkout = tmp_path / "not-a-repo"
        checkout.mkdir()
    else:
        checkout = _repo_with_committed_file(tmp_path / "repo")
        (checkout / "a.py").write_text("two\n")
    SessionVariableManager(temp_db).record_edited_files(
        canonical_task_session.id, ["a.py"], checkout_root=str(checkout)
    )
    if state != "unreviewed":
        _approve(temp_db, held, reviewer)
    if state == "unreviewed":
        _git(checkout, "commit", "-q", "-am", "held work")

    claimed = await _call(registry, canonical_task_session, "claim_task", task_id=nxt.id)
    assert claimed["success"] is True
    assert manager.get_task(nxt.id).claimed_by_session_id == canonical_task_session.id
    assert _variables(temp_db, canonical_task_session)["task_edited_files"][held.id] == ["a.py"]


@pytest.mark.asyncio
async def test_committing_the_attributed_paths_completes_the_hand_off(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    reviewer: Session,
    tmp_path: Path,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    held = _task(manager, sample_project, "Held")
    nxt = _task(manager, sample_project, "Next")
    await _claim(registry, canonical_task_session, held)
    checkout = _repo_with_committed_file(tmp_path / "repo")
    (checkout / "a.py").write_text("two\n")
    SessionVariableManager(temp_db).record_edited_files(
        canonical_task_session.id, ["a.py"], checkout_root=str(checkout)
    )
    _approve(temp_db, held, reviewer)
    _git(checkout, "commit", "-q", "-am", "held work")

    claimed = await _call(registry, canonical_task_session, "claim_task", task_id=nxt.id)

    assert claimed.get("success") is True, claimed


@pytest.mark.asyncio
async def test_edits_after_the_new_claim_attribute_only_to_it(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    reviewer: Session,
    tmp_path: Path,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    variables = SessionVariableManager(temp_db)
    landed = _task(manager, sample_project, "Landed")
    nxt = _task(manager, sample_project, "Next")
    await _claim(registry, canonical_task_session, landed)
    checkout = _repo_with_committed_file(tmp_path / "repo")
    variables.record_edited_files(canonical_task_session.id, ["a.py"], checkout_root=str(checkout))
    _approve(temp_db, landed, reviewer)
    landed_before = _variables(temp_db, canonical_task_session)["task_edited_files"][landed.id]
    await _claim(registry, canonical_task_session, nxt)

    variables.record_edited_files(canonical_task_session.id, ["b.py"], checkout_root=str(checkout))

    edited = _variables(temp_db, canonical_task_session)["task_edited_files"]
    assert edited[landed.id] == landed_before == ["a.py"]
    assert edited[nxt.id] == ["b.py"]


@pytest.mark.asyncio
async def test_releasing_the_newer_claim_makes_the_sole_survivor_the_edit_target(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    reviewer: Session,
    tmp_path: Path,
) -> None:
    """No claim call marks this return, so close evidence keeps the owner's edits."""
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    variables = SessionVariableManager(temp_db)
    landed = _task(manager, sample_project, "Landed")
    nxt = _task(manager, sample_project, "Next")
    await _claim(registry, canonical_task_session, landed)
    _approve(temp_db, landed, reviewer)
    await _claim(registry, canonical_task_session, nxt)

    escalated = await _call(
        registry,
        canonical_task_session,
        "escalate_task",
        task_id=nxt.id,
        reason="Blocked on an external decision",
    )
    checkout = _repo_with_committed_file(tmp_path / "repo")
    variables.record_edited_files(canonical_task_session.id, ["a.py"], checkout_root=str(checkout))

    assert escalated == {}
    assert manager.get_task(nxt.id).claimed_by_session_id is None
    assert _variables(temp_db, canonical_task_session)["active_task_id"] == landed.id
    assert _variables(temp_db, canonical_task_session)["task_edited_files"] == {landed.id: ["a.py"]}


@pytest.mark.asyncio
async def test_reclaim_selects_a_task_while_another_claim_remains_active(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    reviewer: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    landed = _task(manager, sample_project, "Landed")
    active = _task(manager, sample_project, "Active")
    await _claim(registry, canonical_task_session, landed)
    _approve(temp_db, landed, reviewer)
    await _claim(registry, canonical_task_session, active)

    selected = await _call(registry, canonical_task_session, "claim_task", task_id=landed.id)
    assert selected["success"] is True
    assert selected["already_claimed"] is True
    assert _variables(temp_db, canonical_task_session)["active_task_id"] == landed.id
    assert manager.get_task(active.id).claimed_by_session_id == canonical_task_session.id

    _approve(temp_db, active, reviewer)
    reactivated = await _call(registry, canonical_task_session, "claim_task", task_id=landed.id)

    assert reactivated["success"] is True
    assert reactivated["task_id"] == landed.id
    assert _variables(temp_db, canonical_task_session)["active_task_id"] == landed.id
    again = await _call(registry, canonical_task_session, "claim_task", task_id=landed.id)
    assert again["already_claimed"] is True

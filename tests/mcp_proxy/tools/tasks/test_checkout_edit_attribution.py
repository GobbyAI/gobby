"""Tool-start checkout ownership survives unrelated claim selection."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.tasks._claim_release import _uncommitted_refusal
from gobby.mcp_proxy.tools.tasks._close_context import capture_attribution
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.checkout_task_attribution import checkout_owned_by_other_claim
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import task_edited_file_set_for_checkout
from gobby.workflows.task_tool_bindings import TaskToolBindings

pytestmark = pytest.mark.unit


def edit_event(session: Session, project_id: str, root: Path) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CODEX,
        session_id=session.external_id,
        project_id=project_id,
        cwd=str(root),
        timestamp=datetime.now(UTC),
        request_id="checkout-edit",
        data={
            "tool_name": "Edit",
            "canonical_repo_mutation": True,
            "canonical_tool_kind": "write",
            "canonical_edit_file_paths": [str(root / "owned.py")],
        },
    )


@pytest.mark.parametrize(
    "registration,release_selected,native_turn,omit_project",
    [
        ("leaf", False, False, False),
        ("leaf", True, False, False),
        ("main", False, False, False),
        ("unregistered", False, False, False),
        ("leaf", False, True, False),
        ("leaf", False, False, True),
        ("leaf", False, True, True),
    ],
)
def test_checkout_owner_overrides_later_selection_only_in_registered_tree(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
    registration: str,
    release_selected: bool,
    native_turn: bool,
    omit_project: bool,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(sample_project["id"], "First checkout owner")
    second = tasks.create_task(sample_project["id"], "Other selected claim")
    session_id = canonical_task_session.id
    tasks.claim_task_for_agent(first.id, session_id)
    tasks.claim_task_for_agent(second.id, session_id)
    checkout = LocalProjectCheckoutManager(temp_db).get(require_machine_id(), sample_project["id"])
    assert checkout is not None
    root = Path(checkout.root_path) if registration == "main" else tmp_path / "checkout"
    root.mkdir(parents=True, exist_ok=True)
    (root / "owned.py").write_text("value = 1\n")
    if registration == "leaf":
        LocalWorktreeManager(temp_db).create(
            sample_project["id"], "first", str(root), task_id=first.id
        )
    variables = SessionVariableManager(temp_db)
    event = edit_event(canonical_task_session, sample_project["id"], root)
    if omit_project:
        event.project_id = None
    binding = TaskToolBindings(variables, session_id)
    if native_turn:
        event.data["turn_id"] = "native-turn"
        binding.begin_turn(event)
        event = replace(event, request_id=None, data={**event.data, "item_type": "fileChange"})
    else:
        binding.start(event)
    if release_selected:
        tasks.release_task_claim(second.id)
    variables.record_edited_files(
        session_id,
        ["owned.py"],
        checkout_root=str(root),
        started_at=binding.started_at(event),
    )
    saved = variables.get_variables(session_id)
    expected, other = (first.id, second.id) if registration == "leaf" else (second.id, first.id)
    assert task_edited_file_set_for_checkout(saved, expected, str(root)) == {"owned.py"}
    assert task_edited_file_set_for_checkout(saved, other, str(root)) == set()


@pytest.mark.parametrize("release", [False, True])
def test_shared_epic_retains_tool_start_descendant_after_selection_or_release(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
    release: bool,
) -> None:
    tasks = LocalTaskManager(temp_db)
    epic = tasks.create_task(sample_project["id"], "Shared implementation", task_type="epic")
    first = tasks.create_task(sample_project["id"], "First child", parent_task_id=epic.id)
    second = tasks.create_task(sample_project["id"], "Second child", parent_task_id=epic.id)
    session_id = canonical_task_session.id
    tasks.claim_task_for_agent(first.id, session_id)
    LocalWorktreeManager(temp_db).create(
        sample_project["id"], "shared", str(tmp_path), task_id=epic.id
    )
    variables = SessionVariableManager(temp_db)
    binding = TaskToolBindings(variables, session_id)
    event = edit_event(canonical_task_session, sample_project["id"], tmp_path)
    binding.start(event)
    if release:
        tasks.release_task_claim(first.id)
    tasks.claim_task_for_agent(second.id, session_id)
    (tmp_path / "owned.py").write_text("value = 2\n")
    started_at = TaskToolBindings(variables, session_id).started_at(event)
    variables.record_edited_files(
        session_id,
        ["owned.py"],
        checkout_root=str(tmp_path),
        started_at=started_at,
        attribute_to_task=started_at is not None,
    )
    saved = variables.get_variables(session_id)
    assert task_edited_file_set_for_checkout(saved, first.id, str(tmp_path)) == (
        set() if release else {"owned.py"}
    )
    assert task_edited_file_set_for_checkout(saved, second.id, str(tmp_path)) == set()
    assert task_edited_file_set_for_checkout(saved, epic.id, str(tmp_path)) == set()
    if release:
        assert str(tmp_path) in saved["session_unclaimed_edits"]


@pytest.mark.asyncio
async def test_close_and_claim_release_ignore_another_claims_registered_leaf_dirt(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(sample_project["id"], "Actual checkout owner")
    second = tasks.create_task(sample_project["id"], "Historically misattributed claim")
    session_id = canonical_task_session.id
    tasks.claim_task_for_agent(first.id, session_id)
    tasks.claim_task_for_agent(second.id, session_id)
    LocalWorktreeManager(temp_db).create(
        sample_project["id"], "first", str(tmp_path), task_id=first.id
    )
    (tmp_path / "owned.py").write_text("uncommitted = True\n")
    # Reproduce the ledger produced by the old last-claim attribution.
    variables = SessionVariableManager(temp_db)
    variables.record_edited_files(session_id, ["owned.py"], checkout_root=str(tmp_path))
    context = RegistryContext(task_manager=tasks)
    snapshot = await capture_attribution(
        context,
        task=tasks.get_task(second.id),
        resolved_session_id=session_id,
        repo_path=str(tmp_path),
        prospective_commit_shas=[],
        machine_id=canonical_task_session.machine_id,
    )
    assert snapshot.complete is True
    assert snapshot.clean_checkouts == {}
    assert _uncommitted_refusal(context, canonical_task_session, second.id) is None


@pytest.mark.parametrize("role,claimed", [("epic", True), ("task", False)])
def test_ambiguous_or_foreign_checkout_is_not_exempt_from_dirty_checks(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
    role: str,
    claimed: bool,
) -> None:
    tasks = LocalTaskManager(temp_db)
    owner = tasks.create_task(sample_project["id"], "Checkout owner", task_type=role)
    current = tasks.create_task(sample_project["id"], "Current claim")
    if claimed:
        tasks.claim_task_for_agent(owner.id, canonical_task_session.id)
    tasks.claim_task_for_agent(current.id, canonical_task_session.id)
    LocalWorktreeManager(temp_db).create(
        sample_project["id"], "owned", str(tmp_path), task_id=owner.id
    )
    variables = SessionVariableManager(temp_db).get_variables(canonical_task_session.id)
    assert (
        checkout_owned_by_other_claim(
            temp_db, variables, sample_project["id"], current.id, str(tmp_path)
        )
        is False
    )

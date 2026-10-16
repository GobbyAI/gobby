"""A program the classifier cannot read earns no ownership of the paths it touches.

``uv run python tool.py out.txt`` carries no writer evidence: its arguments may
be inputs it only reads, and a path that turns dirty while it runs may be an
operator's or another session's edit. Absent an independently resolved
mutation target, every such path stays unowned, so rule 3 keeps guarding it.
Redirections and other canonical write targets still attribute as before.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.daemon_git import daemon_git
from gobby.workflows.commit_guard import foreign_dirty_edit_conflict
from gobby.workflows.ledger_reconcile import session_dirty_file_set_for_checkout
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000003"
UNOWNED = "no live session owns it"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@dataclass(frozen=True)
class Checkout:
    db: HubDatabase
    repo: Path
    project_id: str
    session_id: str
    foreign_session_id: str
    task_id: str

    def tool_event(
        self, event_type: HookEventType, command: str, *, failed: bool = False
    ) -> HookEvent:
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {"command": command, "cwd": str(self.repo)},
            "project_path": str(self.repo),
        }
        normalize_tool_fields(data)
        return HookEvent(
            event_type=event_type,
            session_id="current-external",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            cwd=str(self.repo),
            project_id=self.project_id,
            data=data,
            metadata={"_platform_session_id": self.session_id, "is_failure": failed},
            request_id="toolu_opaque",
        )

    def run(
        self,
        handlers: EventHandlers,
        command: str,
        effects: Callable[[], None] | None = None,
        *,
        failed: bool = False,
    ) -> None:
        """Drive the real before/after handlers around ``effects``, the call's interleaving."""
        assert (
            handlers.handle_before_tool(
                self.tool_event(HookEventType.BEFORE_TOOL, command)
            ).decision
            == "allow"
        )
        if effects is not None:
            effects()
        handlers.handle_after_tool(
            self.tool_event(HookEventType.AFTER_TOOL, command, failed=failed)
        )

    def write(self, rel_path: str, text: str) -> None:
        (self.repo / rel_path).write_text(text, encoding="utf-8")

    def ledger(self) -> set[str]:
        variables = SessionVariableManager(self.db).get_variables(self.session_id)
        return session_dirty_file_set_for_checkout(variables, str(self.repo))

    def task_ledger(self) -> set[str]:
        variables = SessionVariableManager(self.db).get_variables(self.session_id)
        checkouts = variables.get("task_edited_file_checkouts", {}).get(self.task_id, {})
        return set(checkouts.get(str(self.repo), []))

    def foreign_ledger_lands(self, *paths: str) -> None:
        held = ["foreign.txt", *paths]
        SessionVariableManager(self.db).merge_variables(
            self.foreign_session_id,
            {"session_dirty_files": held, "session_dirty_file_checkouts": {str(self.repo): held}},
        )

    def rule_3_reason(self, rel_path: str) -> str:
        data: dict[str, Any] = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(self.repo / rel_path)},
        }
        normalize_tool_fields(data)
        assert data["canonical_tool_kind"] == "write"
        edit = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="current-external",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            cwd=str(self.repo),
            project_id=self.project_id,
            data=data,
            metadata={"_platform_session_id": self.session_id},
        )
        return asyncio.run(
            foreign_dirty_edit_conflict(
                self.db,
                edit,
                session_id=self.session_id,
                project_id=self.project_id,
                project_path=str(self.repo),
            )
        )


@pytest.fixture
def checkout(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase, tmp_path: Path
) -> Checkout:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    _git(repo, "init", "-q")
    project = isolated_checkout_factory(temp_db, "opaque-writes", root=repo).project
    for name in (
        "base.txt",
        "owned.txt",
        "foreign.txt",
        "stale.txt",
        "operator.txt",
        "racer.txt",
        "pkg/gone.txt",
    ):
        (repo / name).write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@gobby.local", "-c", "user.name=T", "commit", "-qm", "base")

    sessions = SessionManager(temp_db)
    current, foreign = (
        sessions.register(
            external_id=external_id, machine_id=MACHINE_ID, source="claude", project_id=project.id
        )
        for external_id in ("current-external", "foreign-external")
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=project.id,
        title="Run a script",
        task_type="bug",
        category="code",
        implementation_domain="backend",
        validation_criteria="Opaque program outputs earn no ownership.",
        claimed_by_session_id=current.id,
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        current.id,
        {
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "active_task_id": task.id,
            "task_selection_history": [{"task_id": task.id, "epoch": "1970-01-01T00:00:00+00:00"}],
            "session_dirty_files": ["owned.txt"],
            "session_dirty_file_checkouts": {str(repo): ["owned.txt"]},
        },
    )
    variables.merge_variables(
        foreign.id,
        {
            "session_dirty_files": ["foreign.txt"],
            "session_dirty_file_checkouts": {str(repo): ["foreign.txt"]},
        },
    )
    # Dirt before the call: the session's own, and an operator edit no session owns.
    for name in ("owned.txt", "stale.txt"):
        (repo / name).write_text("edited\n", encoding="utf-8")
    return Checkout(temp_db, repo, project.id, current.id, foreign.id, task.id)


def _handlers(mock_dependencies: dict[str, Any], db: HubDatabase) -> EventHandlers:
    sessions = SessionManager(db)
    return EventHandlers(
        **{**mock_dependencies, "session_manager": sessions, "session_storage": sessions}
    )


@pytest.mark.parametrize(
    "command",
    [
        "uv run python /tmp/no_write.py operator.txt racer.txt",
        "uv run python /tmp/no_write.py --input=operator.txt --input=racer.txt",
    ],
)
def test_named_operands_edited_by_others_during_the_call_are_not_adopted(
    mock_dependencies: dict[str, Any], checkout: Checkout, command: str
) -> None:
    """``no_write.py`` only reads its arguments; others edit them while it runs."""

    def others_write() -> None:
        checkout.write("operator.txt", "operator edit\n")
        # The foreign writer's ledger lands only after this call's after-tool.
        checkout.write("racer.txt", "foreign edit\n")

    checkout.run(_handlers(mock_dependencies, checkout.db), command, others_write)

    assert checkout.ledger() == {"owned.txt"}
    assert not checkout.task_ledger()
    for rel_path in ("operator.txt", "racer.txt"):
        assert f"{rel_path} — {UNOWNED}" in checkout.rule_3_reason(rel_path)
    checkout.foreign_ledger_lands("racer.txt")
    reason = checkout.rule_3_reason("racer.txt")
    assert "racer.txt — session" in reason, reason
    assert f"operator.txt — {UNOWNED}" in checkout.rule_3_reason("operator.txt")


@pytest.mark.parametrize("failed", [False, True])
def test_opaque_program_outputs_without_provenance_stay_unattributed(
    mock_dependencies: dict[str, Any], checkout: Checkout, failed: bool
) -> None:
    def program_writes() -> None:
        checkout.write("new.txt", "generated\n")
        checkout.write("base.txt", "rewritten\n")
        (checkout.repo / "pkg" / "gone.txt").unlink()
        checkout.write("stale.txt", "rewritten\n")
        checkout.write("pkg/made.txt", "generated\n")

    checkout.run(
        _handlers(mock_dependencies, checkout.db),
        "uv run python /tmp/tool.py new.txt base.txt pkg/gone.txt stale.txt --out=pkg pkg",
        program_writes,
        failed=failed,
    )

    assert checkout.ledger() == {"owned.txt"}
    assert not checkout.task_ledger()
    for rel_path in ("new.txt", "base.txt", "pkg/gone.txt", "stale.txt", "pkg/made.txt"):
        assert f"{rel_path} — {UNOWNED}" in checkout.rule_3_reason(rel_path)


def test_independently_resolved_mutations_still_attribute(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    """A redirection names its target before the call, so the opaque program's output counts."""
    command = "uv run python /tmp/tool.py operator.txt > out.txt"

    def program_writes() -> None:
        checkout.write("out.txt", "printed\n")

    checkout.run(_handlers(mock_dependencies, checkout.db), command, program_writes)

    assert checkout.ledger() == {"owned.txt", "out.txt"}
    assert checkout.task_ledger() == {"out.txt"}
    reason = checkout.rule_3_reason("out.txt")
    assert reason == "", reason


@pytest.mark.parametrize(
    "command",
    ["uv run python /tmp/tool.py", "uv run python /tmp/tool.py operator.txt --out=new.txt"],
)
def test_opaque_program_runs_no_status(
    mock_dependencies: dict[str, Any], checkout: Checkout, command: str
) -> None:
    status = AsyncMock()

    with patch.object(daemon_git, "status", status):
        checkout.run(_handlers(mock_dependencies, checkout.db), command)

    status.assert_not_awaited()
    assert checkout.ledger() == {"owned.txt"}

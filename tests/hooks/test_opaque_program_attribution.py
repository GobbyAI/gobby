"""Writes from a program the classifier cannot read are attributed after it runs.

``uv run python tool.py out.txt`` names no write paths up front. The operands it
takes from clean to dirty must join the running claim's ledger afterwards, or
rule 3 refuses the session's own files as dirt no live session owns. Nothing
else is adopted: dirt that predates the call, files under an operand directory,
and paths an operator or another session changed meanwhile stay unowned or
theirs, so rule 3 still guards them.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, replace
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
from gobby.workflows.commit_guard import foreign_dirty_edit_conflict
from gobby.workflows.ledger_reconcile import session_dirty_file_set_for_checkout
from gobby.workflows.opaque_write_attribution import opaque_program_writes, operand_baseline
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000003"
OPERANDS = {"new.txt", "base.txt", "pkg/gone.txt", "stale.txt", "foreign.txt", "gen.txt", "pkg"}
PROGRAM = (
    "uv run python /tmp/tool.py new.txt base.txt pkg/gone.txt stale.txt foreign.txt "
    "--out=gen.txt pkg"
)
# The program's own outputs: operands created, rewritten from clean, and deleted.
PROGRAM_WRITES = {"new.txt", "base.txt", "pkg/gone.txt", "gen.txt"}


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

    def program_ran(self) -> None:
        """``tool.py``'s writes and the other writers active while it ran.

        The program also rewrites a pre-dirty and a foreign-held operand and
        creates a file under its ``pkg`` directory operand. Meanwhile the
        operator edits ``operator.txt`` and the foreign session writes
        ``racer.txt`` before its ledger records it.
        """
        (self.repo / "new.txt").write_text("generated\n", encoding="utf-8")
        (self.repo / "base.txt").write_text("rewritten\n", encoding="utf-8")
        (self.repo / "pkg" / "gone.txt").unlink()
        (self.repo / "gen.txt").write_text("generated\n", encoding="utf-8")
        (self.repo / "stale.txt").write_text("rewritten\n", encoding="utf-8")
        (self.repo / "foreign.txt").write_text("rewritten\n", encoding="utf-8")
        (self.repo / "pkg" / "made.txt").write_text("generated\n", encoding="utf-8")
        (self.repo / "operator.txt").write_text("operator edit\n", encoding="utf-8")
        (self.repo / "racer.txt").write_text("foreign edit\n", encoding="utf-8")

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

    def ledger(self) -> set[str]:
        variables = SessionVariableManager(self.db).get_variables(self.session_id)
        return session_dirty_file_set_for_checkout(variables, str(self.repo))

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
        validation_criteria="Script outputs are attributed.",
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
    "command,operands",
    [
        ("uv run python tool.py", []),
        ("uv run --no-sync python3 -W ignore tool.py out.txt", ["out.txt"]),
        ("python3 -um pytest tests/x.py -q", ["tests/x.py"]),
        ("python3 -mpytest tests/x.py", ["tests/x.py"]),
        ("node build.js --out=dist/a.js --minify", ["dist/a.js"]),
        ("python3 tool.py > out.txt", []),
        ("python3 -W ignore", None),
        ("python3 --version", None),
        ("python3 - <<'EOF'\nprint(1)\nEOF", None),
        ("python3 -c 'print(1)'", None),
        ("ls -la", None),
    ],
)
def test_interpreter_running_a_file_names_its_operands(
    tmp_path: Path, command: str, operands: list[str] | None
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(tmp_path)},
        "project_path": str(tmp_path),
    }
    normalize_tool_fields(data)

    assert data.get("canonical_opaque_program_operands") == operands


@pytest.mark.asyncio
async def test_sweep_returns_only_unheld_operands_dirtied_since_the_baseline(
    checkout: Checkout,
) -> None:
    root = str(checkout.repo)
    baseline = await operand_baseline({root: OPERANDS})
    assert baseline == {root: {"operands": sorted(OPERANDS), "dirty": ["stale.txt"]}}
    checkout.program_ran()
    variables = SessionVariableManager(checkout.db).get_variables(checkout.session_id)

    paths = await opaque_program_writes(
        checkout.db, variables, session_id=checkout.session_id, baseline=baseline
    )

    assert paths == [str(checkout.repo / name) for name in sorted(PROGRAM_WRITES)]


@pytest.mark.parametrize("failed", [False, True])
def test_opaque_program_writes_join_the_claim_and_clear_rule_3(
    mock_dependencies: dict[str, Any], checkout: Checkout, failed: bool
) -> None:
    handlers = _handlers(mock_dependencies, checkout.db)
    before = checkout.tool_event(HookEventType.BEFORE_TOOL, PROGRAM)
    assert set(before.data["canonical_opaque_program_operands"]) == OPERANDS

    assert handlers.handle_before_tool(before).decision == "allow"
    checkout.program_ran()
    handlers.handle_after_tool(
        checkout.tool_event(HookEventType.AFTER_TOOL, PROGRAM, failed=failed)
    )

    assert checkout.ledger() == {"owned.txt", *PROGRAM_WRITES}
    variables = SessionVariableManager(checkout.db).get_variables(checkout.session_id)
    task_paths = variables["task_edited_file_checkouts"][checkout.task_id][str(checkout.repo)]
    assert set(task_paths) == PROGRAM_WRITES
    for rel_path in ("base.txt", "new.txt", "gen.txt"):
        reason = checkout.rule_3_reason(rel_path)
        assert reason == "", reason
    # Pre-existing unowned dirt and files under a directory operand are not the program's.
    for rel_path in ("stale.txt", "pkg/made.txt"):
        assert "no live session owns it" in checkout.rule_3_reason(rel_path)


def test_writes_by_others_during_the_call_are_not_adopted(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    handlers = _handlers(mock_dependencies, checkout.db)

    handlers.handle_before_tool(checkout.tool_event(HookEventType.BEFORE_TOOL, PROGRAM))
    checkout.program_ran()
    handlers.handle_after_tool(checkout.tool_event(HookEventType.AFTER_TOOL, PROGRAM))

    assert not checkout.ledger() & {"operator.txt", "racer.txt"}
    assert "operator.txt — no live session owns it" in checkout.rule_3_reason("operator.txt")
    # The foreign writer's ledger lands after the program's after-tool.
    SessionVariableManager(checkout.db).merge_variables(
        checkout.foreign_session_id,
        {
            "session_dirty_files": ["foreign.txt", "racer.txt"],
            "session_dirty_file_checkouts": {str(checkout.repo): ["foreign.txt", "racer.txt"]},
        },
    )
    reason = checkout.rule_3_reason("racer.txt")
    assert "racer.txt — session" in reason, reason


def test_no_adoption_without_a_start_time_snapshot(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    checkout.program_ran()

    _handlers(mock_dependencies, checkout.db).handle_after_tool(
        checkout.tool_event(HookEventType.AFTER_TOOL, PROGRAM)
    )

    assert checkout.ledger() == {"owned.txt"}


@pytest.mark.parametrize("command", ["ls -la", "python3 --version", "uv run python tool.py"])
def test_no_status_without_program_operands(
    mock_dependencies: dict[str, Any], checkout: Checkout, command: str
) -> None:
    handlers = _handlers(mock_dependencies, checkout.db)
    status = AsyncMock()

    with patch("gobby.workflows.opaque_write_attribution.task_dirty_paths_async", status):
        handlers.handle_before_tool(checkout.tool_event(HookEventType.BEFORE_TOOL, command))
        checkout.program_ran()
        handlers.handle_after_tool(checkout.tool_event(HookEventType.AFTER_TOOL, command))

    status.assert_not_awaited()
    assert checkout.ledger() == {"owned.txt"}


def test_unclaimed_session_owns_its_program_writes_without_a_task(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    """Like a structured edit, an unclaimed session's writes enter only its session ledger."""
    idle = SessionManager(checkout.db).register(
        external_id="idle-external",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=checkout.project_id,
    )
    unclaimed = replace(checkout, session_id=idle.id)
    handlers = _handlers(mock_dependencies, checkout.db)

    handlers.handle_before_tool(unclaimed.tool_event(HookEventType.BEFORE_TOOL, PROGRAM))
    unclaimed.program_ran()
    handlers.handle_after_tool(unclaimed.tool_event(HookEventType.AFTER_TOOL, PROGRAM))

    assert unclaimed.ledger() == PROGRAM_WRITES
    variables = SessionVariableManager(checkout.db).get_variables(idle.id)
    assert not variables.get("task_edited_file_checkouts")

"""Opaque shell writes stay fail closed except in the caller's own task worktree.

``uv run python tool.py out.txt`` carries no writer evidence: its arguments may
be inputs it only reads, and a path that turns dirty while it runs may be an
operator's or another session's edit. Absent an independently resolved
mutation target, every such path stays unowned, so rule 3 keeps guarding it.
Redirections and other canonical write targets still attribute as before.
In a registered task worktree, call snapshots attribute changed paths unless
another session's write or Bash call overlaps.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.daemon_git import daemon_git
from gobby.workflows.commit_guard import foreign_dirty_edit_conflict
from gobby.workflows.ledger_reconcile import session_dirty_file_set_for_checkout
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory, write_project_marker

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000003"
UNOWNED = "no live session owns it"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _blob_hash(path: Path) -> str | None:
    """Return git's blob hash of a file as stored in the ledger, or None when it is absent."""
    if not path.exists():
        return None
    result = subprocess.run(
        ["git", "hash-object", "--no-filters", "--", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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

    def rule_3_reason(self, rel_path: str, *, command: str | None = None) -> str:
        """Rule 3's verdict on an Edit of ``rel_path``, or on a Bash ``command`` writing it."""
        data: dict[str, Any] = (
            {"tool_name": "Bash", "tool_input": {"command": command, "cwd": str(self.repo)}}
            if command is not None
            else {"tool_name": "Edit", "tool_input": {"file_path": str(self.repo / rel_path)}}
        )
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
    # The claimant's Gobby edit of owned.txt left this content hash; stale.txt has no record.
    variables.merge_variables(
        current.id,
        {
            "task_edited_checkout_hashes": {
                task.id: {str(repo): {"owned.txt": _blob_hash(repo / "owned.txt")}}
            }
        },
    )
    return Checkout(temp_db, repo, project.id, current.id, foreign.id, task.id)


@pytest.fixture
def task_worktree(checkout: Checkout) -> Checkout:
    """Use a real linked checkout registered to the caller's claimed task."""
    root = (checkout.repo.parent / "task-worktree").resolve()
    _git(checkout.repo, "worktree", "add", "-q", "-b", "task-script-writes", str(root))
    write_project_marker(root, project_id=checkout.project_id, name="opaque-writes")
    LocalWorktreeManager(checkout.db).create(
        project_id=checkout.project_id,
        branch_name="task-script-writes",
        worktree_path=str(root),
        task_id=checkout.task_id,
        agent_session_id=checkout.session_id,
    )
    SessionManager(checkout.db).update(checkout.session_id, workspace_path=str(root))
    variables = SessionVariableManager(checkout.db)
    for session_id, paths in (
        (checkout.session_id, ["owned.txt"]),
        (checkout.foreign_session_id, ["foreign.txt"]),
    ):
        variables.merge_variables(session_id, {"session_dirty_file_checkouts": {str(root): paths}})
    linked = replace(checkout, repo=root)
    linked.write("owned.txt", "edited\n")
    linked.write("stale.txt", "edited\n")
    variables.merge_variables(
        checkout.session_id,
        {
            "task_edited_checkout_hashes": {
                checkout.task_id: {str(root): {"owned.txt": _blob_hash(root / "owned.txt")}}
            }
        },
    )
    return linked


def _handlers(mock_dependencies: dict[str, Any], db: HubDatabase) -> EventHandlers:
    sessions = SessionManager(db)
    return EventHandlers(
        **{**mock_dependencies, "session_manager": sessions, "session_storage": sessions}
    )


def _shell_write_refused(handlers: EventHandlers, event: HookEvent) -> bool:
    """Include the bundled scope gate alongside the real before-tool handler."""
    if handlers.handle_before_tool(event).decision != "allow":
        return True
    rule_path = (
        get_bundled_rules_path() / "task-enforcement/block-unresolved-scope-shell-write.yaml"
    )
    rule = yaml.safe_load(rule_path.read_text())["rules"]["block-unresolved-scope-shell-write"]
    evaluator = SafeExpressionEvaluator(
        {"event": SimpleNamespace(data=event.data)}, build_condition_helpers()
    )
    return bool(rule["enabled"] and evaluator.evaluate(rule["when"]))


@pytest.mark.parametrize("prefix", ["python", "uv run python"])
@pytest.mark.parametrize("rel_path", ["base.txt", "owned.txt", "generated.txt"])
@pytest.mark.parametrize("failed", [False, True])
def test_script_write_in_task_worktree_is_owned_before_next_mutation(
    mock_dependencies: dict[str, Any],
    task_worktree: Checkout,
    prefix: str,
    rel_path: str,
    failed: bool,
) -> None:
    """Even a failed script owns its changes, allowing Edit, restore and deletion."""
    checkout = task_worktree
    script = checkout.repo.parent / "rewrite.py"
    script.write_text(
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('script write\\n')\n"
        f"sys.exit({int(failed)})\n",
        encoding="utf-8",
    )
    target = checkout.repo / rel_path
    command = f"{prefix} {script} {rel_path}"
    handlers = _handlers(mock_dependencies, checkout.db)
    assert not _shell_write_refused(
        handlers, checkout.tool_event(HookEventType.BEFORE_TOOL, command)
    ), "Scripts must remain allowed in the task worktree"

    result = subprocess.run([sys.executable, str(script), rel_path], cwd=checkout.repo, check=False)
    assert result.returncode == int(failed)
    handlers.handle_after_tool(
        checkout.tool_event(HookEventType.AFTER_TOOL, command, failed=failed)
    )

    assert target.read_text() == "script write\n"
    assert rel_path in checkout.ledger()
    assert rel_path in checkout.task_ledger()
    assert checkout.rule_3_reason(rel_path) == "", "The script writer is locked out of its own file"
    for mutation in (f"git restore -- {rel_path}", f"git checkout -- {rel_path}", f"rm {rel_path}"):
        assert checkout.rule_3_reason(rel_path, command=mutation) == ""
    assert "stale.txt" not in checkout.ledger(), "Unchanged dirt must not be adopted"


@pytest.mark.parametrize("operation", ["format", "check --fix"])
def test_literal_ruff_write_remains_owned(
    mock_dependencies: dict[str, Any], task_worktree: Checkout, operation: str
) -> None:
    """Literal formatter targets keep provenance through the next structured Edit."""
    checkout = task_worktree
    checkout.write("format_me.py", "import os\nx=1\n")
    SessionVariableManager(checkout.db).record_edited_files(
        checkout.session_id, ["format_me.py"], checkout_root=str(checkout.repo)
    )
    command = f"uv run ruff {operation} format_me.py"
    handlers = _handlers(mock_dependencies, checkout.db)
    assert not _shell_write_refused(
        handlers, checkout.tool_event(HookEventType.BEFORE_TOOL, command)
    )
    subprocess.run(
        [sys.executable, "-m", "ruff", *operation.split(), "format_me.py"],
        cwd=checkout.repo,
        check=True,
        capture_output=True,
    )
    handlers.handle_after_tool(checkout.tool_event(HookEventType.AFTER_TOOL, command))

    assert checkout.rule_3_reason("format_me.py") == ""
    assert "format_me.py" in checkout.ledger()


def test_script_call_does_not_adopt_another_sessions_write(
    mock_dependencies: dict[str, Any], task_worktree: Checkout
) -> None:
    """An overlapping foreign writer stays foreign even if its ledger arrives late."""
    checkout = task_worktree
    handlers = _handlers(mock_dependencies, checkout.db)
    command = "uv run python /tmp/read_only_probe.py racer.txt"
    assert not _shell_write_refused(
        handlers, checkout.tool_event(HookEventType.BEFORE_TOOL, command)
    )
    checkout.write("racer.txt", "foreign edit\n")
    # The foreign session's structured write completes before this call does.
    SessionVariableManager(checkout.db).record_edited_files(
        checkout.foreign_session_id, ["racer.txt"], checkout_root=str(checkout.repo)
    )
    handlers.handle_after_tool(checkout.tool_event(HookEventType.AFTER_TOOL, command))
    checkout.foreign_ledger_lands("racer.txt")

    assert "racer.txt" not in checkout.ledger()
    assert "racer.txt" not in checkout.task_ledger()
    assert "racer.txt — session" in checkout.rule_3_reason("racer.txt")


@pytest.mark.parametrize("foreign_finishes_first", [False, True])
def test_overlapping_bash_calls_leave_changed_paths_foreign(
    mock_dependencies: dict[str, Any], task_worktree: Checkout, foreign_finishes_first: bool
) -> None:
    """Unknown writer paths stay guarded for both possible after-tool orderings."""
    checkout = task_worktree
    handlers = _handlers(mock_dependencies, checkout.db)
    foreign = replace(checkout, session_id=checkout.foreign_session_id)
    command = "uv run python /tmp/rewrite.py racer.txt"
    current_before = checkout.tool_event(HookEventType.BEFORE_TOOL, command)
    foreign_before = replace(
        foreign.tool_event(HookEventType.BEFORE_TOOL, command),
        session_id="foreign-external",
        request_id="toolu_foreign",
    )
    assert not _shell_write_refused(handlers, current_before)
    assert not _shell_write_refused(handlers, foreign_before)
    checkout.write("racer.txt", "overlapping write\n")
    current_after = checkout.tool_event(HookEventType.AFTER_TOOL, command)
    foreign_after = replace(foreign_before, event_type=HookEventType.AFTER_TOOL)
    events = (
        (foreign_after, current_after) if foreign_finishes_first else (current_after, foreign_after)
    )
    for event in events:
        handlers.handle_after_tool(event)

    assert "racer.txt" not in checkout.ledger()
    assert "racer.txt" not in checkout.task_ledger()
    reason = checkout.rule_3_reason("racer.txt")
    assert "racer.txt" in reason
    assert re.search(r"\boverlap(?:ped|ping)?\b", reason, re.IGNORECASE), reason


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


def test_claimant_cp_p_rewrite_of_its_ledgered_file_stays_owned(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    """A claimant rewrite that restores an older mtime (cp -p) is still its own edit (#25161)."""
    checkout.write("base.txt", "gobby edit\n")
    SessionVariableManager(checkout.db).record_edited_files(
        checkout.session_id, ["base.txt"], checkout_root=str(checkout.repo)
    )
    older = time.time() - 3600.0

    def cp_p_rewrites() -> None:
        checkout.write("base.txt", "cp -p rewrite\n")
        os.utime(checkout.repo / "base.txt", (older, older))

    checkout.run(
        _handlers(mock_dependencies, checkout.db),
        "uv run python /tmp/tool.py base.txt",
        cp_p_rewrites,
    )

    assert checkout.task_ledger() == {"base.txt"}
    assert checkout.rule_3_reason("base.txt") == ""


def test_claimant_formatter_between_its_edits_keeps_the_path_owned(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    """A claimant formatter rewrite between two of its Gobby edits keeps the path its own."""
    variables = SessionVariableManager(checkout.db)
    checkout.write("base.txt", "gobby edit\n")
    variables.record_edited_files(
        checkout.session_id, ["base.txt"], checkout_root=str(checkout.repo)
    )

    def formatter_rewrites() -> None:
        checkout.write("base.txt", "gobby edit, formatted\n")

    checkout.run(
        _handlers(mock_dependencies, checkout.db),
        "uv run python /tmp/format.py base.txt",
        formatter_rewrites,
    )
    checkout.write("base.txt", "gobby edit, formatted, edited again\n")
    variables.record_edited_files(
        checkout.session_id, ["base.txt"], checkout_root=str(checkout.repo)
    )

    assert checkout.rule_3_reason("base.txt") == ""


def test_outside_delete_then_sibling_create_in_claimant_call_stays_unowned(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    """An outside deletion before a claimant call stays unowned when the call makes a sibling."""
    checkout.write("pkg/gone.txt", "gobby edit\n")
    SessionVariableManager(checkout.db).record_edited_files(
        checkout.session_id, ["pkg/gone.txt"], checkout_root=str(checkout.repo)
    )
    (checkout.repo / "pkg" / "gone.txt").unlink()

    def sibling_create() -> None:
        checkout.write("pkg/sibling.txt", "generated\n")

    checkout.run(
        _handlers(mock_dependencies, checkout.db),
        "uv run python /tmp/tool.py",
        sibling_create,
    )

    assert f"pkg/gone.txt — {UNOWNED}" in checkout.rule_3_reason("pkg/gone.txt")


def test_split_assembly_attributes_only_the_output(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    prefix = "crates/gterminals/src/host/connect"
    (checkout.repo / "crates/gterminals/src/host").mkdir(parents=True)
    for part in ("head", "body"):
        checkout.write(f"{prefix}.{part}", f"{part}\n")

    def assemble() -> None:
        checkout.write(f"{prefix}.rs", "head\nbody\n")

    checkout.run(
        _handlers(mock_dependencies, checkout.db),
        f"cat {prefix}.head {prefix}.body > {prefix}.rs",
        assemble,
    )

    expected = {f"{prefix}.rs"}
    assert checkout.task_ledger() == expected
    assert checkout.ledger() == {"owned.txt"} | expected


@pytest.mark.parametrize("switches", ["-0pi -e", "-i.bak -pe"])
def test_perl_in_place_edit_attributes_to_its_author(
    mock_dependencies: dict[str, Any], checkout: Checkout, switches: str
) -> None:
    """#24504: the author of a perl in-place edit can keep editing or revert the file."""

    def perl_writes() -> None:
        checkout.write("base.txt", "perl edit\n")

    command = f"perl {switches} 's/base/perl edit/' base.txt"
    checkout.run(_handlers(mock_dependencies, checkout.db), command, perl_writes)

    assert checkout.ledger() == {"owned.txt", "base.txt"}
    assert checkout.task_ledger() == {"base.txt"}
    for reason in (
        checkout.rule_3_reason("base.txt"),
        checkout.rule_3_reason("base.txt", command="git checkout -- base.txt"),
    ):
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

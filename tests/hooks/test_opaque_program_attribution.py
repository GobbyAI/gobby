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
import json
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

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.codex_impl.execution_chain import ExecutionChainCorrelator
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.codex_impl.item_normalization import build_pre_tool_lifecycle_payload
from gobby.hooks._path_scope import current_tool_cwd
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.daemon_git import daemon_git
from gobby.workflows.commit_guard import foreign_dirty_edit_conflict, foreign_staged_commit_conflict
from gobby.workflows.ledger_reconcile import session_dirty_file_set_for_checkout
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path
from gobby.workflows.task_tool_bindings import TaskToolBindings, cleanup_task_tool_bindings
from gobby.workflows.worktree_shell_attribution import (
    OVERLAPS_VARIABLE,
    begin_structured_edit_calls,
)
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


def _native_write_event(checkout: Checkout, rel_path: str) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(checkout.repo / rel_path), "content": "native\n"},
        "project_path": str(checkout.repo),
    }
    normalize_tool_fields(data)
    return replace(
        checkout.tool_event(HookEventType.BEFORE_TOOL, ""),
        data=data,
        request_id="toolu_native_write",
    )


def test_native_write_before_claim_is_adopted_only_by_its_author(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    """Recovery of a slipped-through native write must preserve its writer identity."""
    tasks = LocalTaskManager(checkout.db)
    tasks.release_task_claim(checkout.task_id)
    handlers = _handlers(mock_dependencies, checkout.db)
    native = _native_write_event(checkout, "preclaim.txt")
    # Simulate the historical gate bypass; the after-hook still knows the writer.
    handlers.handle_before_tool(native)
    checkout.write("preclaim.txt", "native\n")
    handlers.handle_after_tool(replace(native, event_type=HookEventType.AFTER_TOOL))

    tasks.claim_task(checkout.task_id, checkout.session_id)

    assert checkout.rule_3_reason("preclaim.txt") == ""
    assert "preclaim.txt" in checkout.task_ledger()
    foreign = replace(checkout, session_id=checkout.foreign_session_id)
    assert "preclaim.txt" in foreign.rule_3_reason("preclaim.txt")
    assert "stale.txt" not in checkout.task_ledger()


@pytest.mark.parametrize("foreign_finishes_first", [False, True])
def test_native_write_keeps_ownership_during_foreign_bash(
    mock_dependencies: dict[str, Any], task_worktree: Checkout, foreign_finishes_first: bool
) -> None:
    """An opaque call's snapshot cannot erase a native write's explicit writer."""
    checkout = task_worktree
    handlers = _handlers(mock_dependencies, checkout.db)
    foreign = replace(checkout, session_id=checkout.foreign_session_id)
    bash = replace(
        foreign.tool_event(HookEventType.BEFORE_TOOL, "uv run python /tmp/probe.py"),
        session_id="foreign-external",
        request_id="toolu_foreign",
    )
    native = _native_write_event(checkout, "native-new.txt")
    assert handlers.handle_before_tool(bash).decision == "allow"
    # The author's concurrent read-only Bash probe must not turn native authorship
    # into ambiguous shell-diff ownership when its result arrives first.
    probe = replace(
        checkout.tool_event(HookEventType.BEFORE_TOOL, "git status --short"),
        request_id="toolu_author_probe",
    )
    assert handlers.handle_before_tool(probe).decision == "allow"
    # WorkflowHookHandler registers native edits before evaluating their rules.
    begin_structured_edit_calls(SessionVariableManager(checkout.db), checkout.session_id, native)
    assert handlers.handle_before_tool(native).decision == "allow"
    checkout.write("native-new.txt", "native\n")
    handlers.handle_after_tool(replace(probe, event_type=HookEventType.AFTER_TOOL))
    native_after = replace(native, event_type=HookEventType.AFTER_TOOL)
    bash_after = replace(bash, event_type=HookEventType.AFTER_TOOL)
    for event in (
        (bash_after, native_after) if foreign_finishes_first else (native_after, bash_after)
    ):
        handlers.handle_after_tool(event)

    assert checkout.rule_3_reason("native-new.txt") == ""
    assert "native-new.txt" in checkout.task_ledger()
    assert "native-new.txt" not in foreign.ledger()
    assert "native-new.txt" in foreign.rule_3_reason("native-new.txt")


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
@pytest.mark.parametrize("correlation", ["provider", "execution"])
@pytest.mark.parametrize("completion_order", ["sequential", "reader_first", "formatter_first"])
def test_literal_ruff_write_remains_owned(
    mock_dependencies: dict[str, Any],
    task_worktree: Checkout,
    operation: str,
    correlation: str,
    completion_order: str,
) -> None:
    """Literal formatter targets keep provenance through the next structured Edit."""
    checkout = task_worktree
    checkout.write("format_me.py", "import os\nx=1\n")
    SessionVariableManager(checkout.db).record_edited_files(
        checkout.session_id, ["format_me.py"], checkout_root=str(checkout.repo)
    )
    command = f"uv run ruff {operation} format_me.py"
    handlers = _handlers(mock_dependencies, checkout.db)
    reader = checkout.tool_event(HookEventType.BEFORE_TOOL, "uv run mypy format_me.py")
    if correlation == "execution":
        reader = replace(
            reader,
            source=SessionSource.CODEX,
            request_id=None,
            data={**reader.data, "verification_execution_id": "exec-reader"},
        )
    if completion_order != "sequential":
        assert not _shell_write_refused(handlers, reader)
    before = checkout.tool_event(HookEventType.BEFORE_TOOL, command)
    if correlation == "execution":
        before = replace(
            before,
            source=SessionSource.CODEX,
            request_id=None,
            data={**before.data, "verification_execution_id": "exec-formatter"},
        )
    assert not _shell_write_refused(handlers, before)
    subprocess.run(
        [sys.executable, "-m", "ruff", *operation.split(), "format_me.py"],
        cwd=checkout.repo,
        check=True,
        capture_output=True,
    )
    # A fresh handler reconstructs the persisted observation after a daemon restart.
    resumed = _handlers(mock_dependencies, checkout.db)
    if completion_order == "reader_first":
        resumed.handle_after_tool(
            replace(reader, event_type=HookEventType.AFTER_TOOL, timestamp=datetime.now(UTC))
        )
    resumed.handle_after_tool(
        replace(before, event_type=HookEventType.AFTER_TOOL, timestamp=datetime.now(UTC))
    )
    if completion_order == "formatter_first":
        resumed.handle_after_tool(
            replace(reader, event_type=HookEventType.AFTER_TOOL, timestamp=datetime.now(UTC))
        )

    assert checkout.rule_3_reason("format_me.py") == ""
    assert "format_me.py" in checkout.ledger()


@pytest.mark.parametrize("wrapped", [False, True])
def test_codex_completion_resolves_workdir_from_event_cwd(
    mock_dependencies: dict[str, Any],
    checkout: Checkout,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrapped: bool,
) -> None:
    """The unmanaged AFTER_TOOL path must not use the daemon process directory."""
    monkeypatch.chdir(tmp_path)
    arguments = {"cmd": "touch output.txt", "workdir": "pkg"}
    item: dict[str, Any] = {
        "id": "relative-output",
        "type": "dynamicToolCall",
        "namespace": "functions",
        "tool": "exec" if wrapped else "exec_command",
        "arguments": f"text(await tools.exec_command({json.dumps(arguments)}))"
        if wrapped
        else arguments,
        "status": "completed",
        "success": True,
        "contentItems": [{"type": "inputText", "text": '{"exit_code":0}'}],
    }
    before = CodexHooksAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "session_id": "current-external",
                "cwd": str(checkout.repo),
                "tool_use_id": item["id"],
                "tool_name": "exec_command",
                "tool_input": arguments,
            },
        }
    )
    assert before is not None
    before = replace(
        before,
        project_id=checkout.project_id,
        metadata={**before.metadata, "_platform_session_id": checkout.session_id},
    )
    assert not _shell_write_refused(_handlers(mock_dependencies, checkout.db), before)
    event = CodexAdapter().translate_to_hook_event(
        {
            "method": "item/completed",
            "params": {"threadId": "current-external", "cwd": str(checkout.repo), "item": item},
        }
    )
    assert event is not None
    event = replace(
        event,
        project_id=checkout.project_id,
        metadata={**event.metadata, "_platform_session_id": checkout.session_id},
    )
    checkout.write("pkg/output.txt", "created\n")
    _handlers(mock_dependencies, checkout.db).handle_after_tool(event)

    assert current_tool_cwd(event.data) == checkout.repo / "pkg"
    assert event.data["canonical_repo_mutation"] is True
    assert checkout.rule_3_reason("pkg/output.txt") == ""
    assert "pkg/output.txt" in checkout.ledger()


def test_codex_wrapped_formatter_uses_its_task_checkout(
    mock_dependencies: dict[str, Any],
    task_worktree: Checkout,
    tmp_path: Path,
) -> None:
    """A literal workdir survives before/after hooks and other sequential wrapper calls."""
    checkout = task_worktree
    checkout.write("format_me.py", "x=1\n")
    manager = SessionVariableManager(checkout.db)
    manager.record_edited_files(
        checkout.session_id, ["format_me.py"], checkout_root=str(checkout.repo)
    )
    command = f"uv run --no-sync ruff format {checkout.repo / 'format_me.py'}"
    arguments = (
        f"text(await tools.exec_command({json.dumps({'cmd': command, 'workdir': str(checkout.repo)})}));\n"
        'text(await tools.apply_patch("*** Begin Patch\\n*** End Patch"));\n'
        'text(await tools.mcp__gobby__call_tool({server_name:"gobby-agents",tool_name:"get_inter_session_messages"}));'
    )
    item: dict[str, Any] = {
        "id": "wrapped-formatter",
        "type": "dynamicToolCall",
        "namespace": "functions",
        "tool": "exec",
        "arguments": arguments,
    }
    payload = build_pre_tool_lifecycle_payload({"item": item})
    assert payload is not None
    name, tool_input = payload
    before = replace(
        checkout.tool_event(HookEventType.BEFORE_TOOL, command),
        source=SessionSource.CODEX,
        cwd=str(tmp_path),
        data=normalize_tool_fields(
            {"cwd": str(tmp_path), "tool_name": name, "tool_input": tool_input}
        ),
        request_id=item["id"],
    )
    before = replace(
        before,
        project_id=checkout.project_id,
        metadata={**before.metadata, "_platform_session_id": checkout.session_id},
    )
    assert not _shell_write_refused(_handlers(mock_dependencies, checkout.db), before)
    assert manager.worktree_shell_call_root(checkout.session_id, item["id"]) == str(
        checkout.repo
    ), (
        before.data,
        manager.get_variables(checkout.session_id).get("worktree_shell_calls"),
    )
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", str(checkout.repo / "format_me.py")],
        cwd=checkout.repo,
        check=True,
        capture_output=True,
    )
    after = CodexAdapter().translate_to_hook_event(
        {
            "method": "item/completed",
            "params": {
                "threadId": "current-external",
                "cwd": str(tmp_path),
                "item": {
                    **item,
                    "status": "completed",
                    "success": True,
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": '{"exit_code":0,"output":"1 file reformatted"}',
                        },
                        {"type": "inputText", "text": "{}"},
                        {"type": "inputText", "text": '{"success":true,"result":{}}'},
                    ],
                },
            },
        }
    )
    assert after is not None
    after = replace(
        after,
        project_id=checkout.project_id,
        metadata={**after.metadata, "_platform_session_id": checkout.session_id},
    )
    _handlers(mock_dependencies, checkout.db).handle_after_tool(after)

    assert checkout.rule_3_reason("format_me.py") == ""
    assert not manager.get_variables(checkout.session_id).get("claimed_shell_snapshots")
    assert manager.worktree_shell_call_root(checkout.session_id, item["id"]) is None


@pytest.mark.parametrize("command", ["git merge --no-edit HEAD", "touch out.txt"])
@pytest.mark.parametrize(
    "sequence",
    [
        "inner_first",
        "outer_first",
        "missing_outer",
        "abandoned_inner",
        "missing_both",
        "outer_only",
        "yielded_outer",
        "yielded_abandoned",
    ],
)
def test_native_codex_outer_and_inner_have_one_shell_owner(
    mock_dependencies: dict[str, Any],
    task_worktree: Checkout,
    tmp_path: Path,
    command: str,
    sequence: str,
) -> None:
    checkout = task_worktree
    manager = SessionVariableManager(checkout.db)
    abandoned = sequence in {"abandoned_inner", "missing_both", "yielded_abandoned"}
    arguments = f"text(await tools.exec_command({json.dumps({'cmd': command, 'workdir': str(checkout.repo)})}));"
    transcript = tmp_path / "native-sequence.jsonl"
    records: list[dict[str, Any]] = [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "outer-exec",
                "input": arguments,
            },
        }
    ]
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))

    def event(hook_type: str, *, inner: bool, yielded: bool = False) -> HookEvent:
        translated = CodexHooksAdapter().translate_to_hook_event(
            {
                "hook_type": hook_type,
                "input_data": {
                    "session_id": "current-external",
                    "cwd": str(tmp_path),
                    "tool_use_id": "exec-inner" if inner else "outer-exec",
                    "tool_name": "Bash" if inner else "functions.exec",
                    "tool_input": {"command": command} if inner else {"arguments": arguments},
                    "tool_response": "Script running with cell ID cell-1" if yielded else {},
                    "transcript_path": str(transcript),
                },
            }
        )
        assert translated is not None
        return replace(
            translated,
            project_id=checkout.project_id,
            metadata={**translated.metadata, "_platform_session_id": checkout.session_id},
        )

    handlers = _handlers(mock_dependencies, checkout.db)
    outer_before = event("PreToolUse", inner=False)
    assert handlers.handle_before_tool(outer_before).decision == "allow"
    for key in ("worktree_shell_calls", "claimed_shell_snapshots", "git_operation_ownership"):
        assert not manager.get_variables(checkout.session_id).get(key), key
    if sequence != "outer_only":
        inner_before = event("PreToolUse", inner=True)
        response = handlers.handle_before_tool(inner_before)
        assert response.decision == "allow", response.reason
        assert manager.worktree_shell_call_root(checkout.session_id, "exec-inner") == str(
            checkout.repo
        )
        operations = manager.get_variables(checkout.session_id).get("git_operation_ownership") or {}
        assert {entry["call_id"] for entry in operations.values()} == (
            {"exec-inner"} if command.startswith("git ") else set()
        )
        if sequence in {"outer_first", "yielded_outer", "yielded_abandoned"}:
            handlers.handle_after_tool(
                event("PostToolUse", inner=False, yielded=sequence.startswith("yielded_"))
            )
            assert manager.worktree_shell_call_root(checkout.session_id, "exec-inner") == str(
                checkout.repo
            )
            if sequence.startswith("yielded_"):
                binding = manager.get_variables(checkout.session_id)["task_tool_bindings"][
                    "codex:outer-exec"
                ]
                assert binding["pending"] is True and binding["background"] is True
                assert binding["aliases"] == ["codex:cell:cell-1"]
        if abandoned:
            checkout.write("unproven.txt", "No terminal receipt attributes this write.\n")
        else:
            subprocess.run(command.split(), cwd=checkout.repo, check=True, capture_output=True)
            records.append(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "CommandExecution",
                            "id": "exec-inner",
                            "command": ["/bin/zsh", "-lc", command],
                            "cwd": checkout.repo.as_uri(),
                            "status": "completed",
                            "exit_code": 0,
                            "aggregated_output": "",
                        },
                    },
                }
            )
            transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
            _handlers(mock_dependencies, checkout.db).handle_after_tool(
                event("PostToolUse", inner=True)
            )
            if command == "touch out.txt":
                assert checkout.rule_3_reason("out.txt") == ""
    if sequence in {"inner_first", "abandoned_inner", "outer_only", "yielded_outer"}:
        handlers.handle_after_tool(event("PostToolUse", inner=False))
    if not abandoned:
        for key in ("worktree_shell_calls", "claimed_shell_snapshots", "git_operation_ownership"):
            assert not manager.get_variables(checkout.session_id).get(key), key
    cleanup_task_tool_bindings(
        replace(outer_before, event_type=HookEventType.STOP), manager, checkout.session_id, {}
    )
    for key in ("worktree_shell_calls", "claimed_shell_snapshots", "git_operation_ownership"):
        assert not manager.get_variables(checkout.session_id).get(key), key
    if abandoned:
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "exec-inner",
                        "command": ["/bin/zsh", "-lc", command],
                        "cwd": checkout.repo.as_uri(),
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": "",
                    },
                },
            }
        )
        transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        handlers.handle_after_tool(event("PostToolUse", inner=True))
        assert "unproven.txt" not in checkout.ledger()
        assert UNOWNED in checkout.rule_3_reason("unproven.txt")
    if sequence == "yielded_abandoned":
        variables = manager.get_variables(checkout.session_id)
        assert set(variables["task_tool_bindings"]) == {"codex:outer-exec"}
        handlers.handle_after_tool(event("PostToolUse", inner=False))


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("git status --short", True),
        ("rg -n value format_me.py", True),
        ("uv run python -c 'print(1)'", True),
        ("cat format_me.py; unknown-program", False),
        ("rg --pre ./preprocess value format_me.py", False),
        ("bash misleading.sh", False),
    ],
)
@pytest.mark.parametrize("outer_hook", [False, True])
def test_native_computed_cwd_reads_never_borrow_launcher_ownership(
    mock_dependencies: dict[str, Any],
    task_worktree: Checkout,
    tmp_path: Path,
    command: str,
    allowed: bool,
    outer_hook: bool,
) -> None:
    checkout = task_worktree
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    (launcher / "misleading.sh").write_text("cat format_me.py\n")
    checkout.write("misleading.sh", "touch foreign.py\n")
    transcript = tmp_path / "computed-read.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "computed-read",
                    "input": (
                        f"text(await tools.exec_command({{cmd:{json.dumps(command)},"
                        'workdir:load("worktree")}));'
                    ),
                },
            }
        )
        + "\n"
    )
    hook_input = {
        "session_id": "current-external",
        "cwd": str(launcher),
        "tool_use_id": "exec-computed-read",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "transcript_path": str(transcript),
    }
    adapter = CodexHooksAdapter()
    outer_input = {
        **hook_input,
        "tool_use_id": "computed-read",
        "tool_name": "functions.exec",
        "tool_input": {"arguments": json.loads(transcript.read_text())["payload"]["input"]},
    }
    if outer_hook:
        outer = adapter.translate_to_hook_event(
            {"hook_type": "PreToolUse", "input_data": outer_input}
        )
        assert outer is not None
        outer_response = _handlers(mock_dependencies, checkout.db).handle_before_tool(
            replace(
                outer,
                project_id=checkout.project_id,
                metadata={**outer.metadata, "_platform_session_id": checkout.session_id},
            )
        )
        assert outer_response.decision == ("allow" if allowed else "block")
    event = adapter.translate_to_hook_event({"hook_type": "PreToolUse", "input_data": hook_input})
    assert event is not None
    event = replace(
        event,
        project_id=checkout.project_id,
        metadata={**event.metadata, "_platform_session_id": checkout.session_id},
    )
    response = _handlers(mock_dependencies, checkout.db).handle_before_tool(event)
    assert response.decision == ("allow" if allowed else "block"), response.reason
    manager = SessionVariableManager(checkout.db)
    for key in ("worktree_shell_calls", "claimed_shell_snapshots", "git_operation_ownership"):
        assert not manager.get_variables(checkout.session_id).get(key), key
    if allowed:
        checkout.write("foreign.py", "A concurrent writer owns these bytes.\n")
        transcript.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "CommandExecution",
                            "id": hook_input["tool_use_id"],
                            "command": ["/bin/zsh", "-lc", command],
                            "cwd": checkout.repo.as_uri(),
                            "status": "completed",
                            "exit_code": 0,
                            "aggregated_output": "",
                        },
                    },
                }
            )
            + "\n"
        )
        after = adapter.translate_to_hook_event(
            {"hook_type": "PostToolUse", "input_data": hook_input}
        )
        assert after is not None
        _handlers(mock_dependencies, checkout.db).handle_after_tool(
            replace(
                after,
                project_id=checkout.project_id,
                metadata={**after.metadata, "_platform_session_id": checkout.session_id},
            )
        )
        assert "foreign.py" not in checkout.ledger()
        assert UNOWNED in checkout.rule_3_reason("foreign.py")
        if outer_hook:
            outer_after = adapter.translate_to_hook_event(
                {
                    "hook_type": "PostToolUse",
                    "input_data": {**outer_input, "tool_response": {"exit_code": 0}},
                }
            )
            assert outer_after is not None
            _handlers(mock_dependencies, checkout.db).handle_after_tool(
                replace(
                    outer_after,
                    project_id=checkout.project_id,
                    metadata={**outer_after.metadata, "_platform_session_id": checkout.session_id},
                )
            )
    TaskToolBindings(manager, checkout.session_id).assert_can_select(None)


@pytest.mark.parametrize("relative_operands", [False, True])
@pytest.mark.parametrize("foreign_finishes_first", [None, False, True])
@pytest.mark.parametrize("computed_first", [False, True])
def test_native_codex_formatter_receipt_keeps_worktree_ownership(
    mock_dependencies: dict[str, Any],
    task_worktree: Checkout,
    tmp_path: Path,
    foreign_finishes_first: bool | None,
    relative_operands: bool,
    computed_first: bool,
) -> None:
    """A native inner CommandExecution supplies cwd missing from the shell hook."""
    checkout = task_worktree
    checkout.write("format_me.py", "x=1\n")
    manager = SessionVariableManager(checkout.db)
    manager.record_edited_files(
        checkout.session_id, ["format_me.py"], checkout_root=str(checkout.repo)
    )
    operand = "format_me.py" if relative_operands else str(checkout.repo / "format_me.py")
    command = (
        f"UV_PROJECT_ENVIRONMENT={tmp_path / '.venv'} PYTHONPATH=src "
        f"uv run --no-sync ruff format {operand}"
    )
    arguments = (
        'text(await tools.apply_patch("*** Begin Patch\\n*** End Patch"));\n'
        f"text(await tools.exec_command({json.dumps({'cmd': command, 'workdir': str(checkout.repo)})}));\n"
        'text(await tools.write_stdin({session_id:123,chars:"",max_output_tokens:1500}));'
    )
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "outer-formatter",
                    "input": arguments,
                },
            }
        )
        + "\n"
    )
    hook_input = {
        "session_id": "current-external",
        "cwd": str(tmp_path),
        "tool_use_id": "exec-native-formatter",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "transcript_path": str(transcript),
    }
    adapter = CodexHooksAdapter()
    if computed_first:
        # #18515 call_97279653d017412582776ccb65fac403: the workdir was load(),
        # not the resolved literal reported in the original incident message.
        literal_transcript = transcript.read_text()
        transcript.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "outer-computed",
                        "input": (
                            f"text(await tools.exec_command({{cmd:{json.dumps(command)},"
                            '"workdir":load("worktree"),"max_output_tokens":2000}));'
                        ),
                    },
                }
            )
            + "\n"
        )
        refused = adapter.translate_to_hook_event(
            {"hook_type": "PreToolUse", "input_data": hook_input}
        )
        assert refused is not None
        refused = replace(
            refused,
            project_id=checkout.project_id,
            metadata={**refused.metadata, "_platform_session_id": checkout.session_id},
        )
        response = _handlers(mock_dependencies, checkout.db).handle_before_tool(refused)
        assert response.decision == "block"
        assert "literal" in (response.reason or "") and "workdir" in (response.reason or "")
        assert (checkout.repo / "format_me.py").read_text() == "x=1\n"
        for key in ("worktree_shell_calls", "claimed_shell_snapshots", "git_operation_ownership"):
            assert not manager.get_variables(checkout.session_id).get(key), key
        TaskToolBindings(manager, checkout.session_id).assert_can_select(None)
        assert checkout.rule_3_reason("format_me.py") == ""
        # Retry through the normal literal wrapper, with no ownership recovery.
        transcript.write_text(literal_transcript)
    outer_input = {
        **hook_input,
        "tool_use_id": "outer-formatter",
        "tool_name": "functions.exec",
        "tool_input": {"arguments": arguments},
    }
    outer_before = adapter.translate_to_hook_event(
        {"hook_type": "PreToolUse", "input_data": outer_input}
    )
    assert outer_before is not None
    outer_before = replace(
        outer_before,
        project_id=checkout.project_id,
        metadata={"_platform_session_id": checkout.session_id},
    )
    assert (
        _handlers(mock_dependencies, checkout.db).handle_before_tool(outer_before).decision
        == "allow"
    )
    assert not manager.get_variables(checkout.session_id).get("worktree_shell_calls")
    before = adapter.translate_to_hook_event({"hook_type": "PreToolUse", "input_data": hook_input})
    assert before is not None
    before = replace(
        before,
        project_id=checkout.project_id,
        metadata={**before.metadata, "_platform_session_id": checkout.session_id},
    )
    assert not _shell_write_refused(_handlers(mock_dependencies, checkout.db), before)
    assert manager.worktree_shell_call_root(checkout.session_id, "exec-native-formatter") == str(
        checkout.repo
    )
    foreign = replace(checkout, session_id=checkout.foreign_session_id)
    competing = replace(
        foreign.tool_event(HookEventType.BEFORE_TOOL, "uv run python tool.py"),
        session_id="foreign-external",
        request_id="foreign-script",
    )
    if foreign_finishes_first is not None:
        assert not _shell_write_refused(_handlers(mock_dependencies, checkout.db), competing)
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", str(checkout.repo / "format_me.py")],
        cwd=checkout.repo,
        check=True,
        capture_output=True,
    )
    if foreign_finishes_first is not None:
        checkout.write("format_me.py", "x = 2\n")
        if foreign_finishes_first:
            _handlers(mock_dependencies, checkout.db).handle_after_tool(
                replace(competing, event_type=HookEventType.AFTER_TOOL)
            )
    transcript.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": hook_input["tool_use_id"],
                        "command": ["/bin/zsh", "-lc", command.replace("uv run", "rtk uv run", 1)],
                        "cwd": checkout.repo.as_uri(),
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": "1 file reformatted\n",
                    },
                },
            }
        )
        + "\n"
    )
    after = adapter.translate_to_hook_event(
        {
            "hook_type": "PostToolUse",
            "input_data": {**hook_input, "transcript_path": str(transcript)},
        }
    )
    assert after is not None
    after = replace(
        after,
        project_id=checkout.project_id,
        metadata={**after.metadata, "_platform_session_id": checkout.session_id},
    )
    assert current_tool_cwd(after.data) == checkout.repo
    assert after.data["canonical_formatter_mutation"] is True
    _handlers(mock_dependencies, checkout.db).handle_after_tool(after)

    outer_after = adapter.translate_to_hook_event(
        {
            "hook_type": "PostToolUse",
            "input_data": {**outer_input, "tool_response": {"exit_code": 0}},
        }
    )
    assert outer_after is not None
    outer_after = replace(
        outer_after,
        project_id=checkout.project_id,
        metadata={"_platform_session_id": checkout.session_id},
    )
    _handlers(mock_dependencies, checkout.db).handle_after_tool(outer_after)
    assert not manager.get_variables(checkout.session_id).get("worktree_shell_calls")
    assert not manager.get_variables(checkout.session_id).get("claimed_shell_snapshots")

    if foreign_finishes_first is False:
        _handlers(mock_dependencies, checkout.db).handle_after_tool(
            replace(competing, event_type=HookEventType.AFTER_TOOL)
        )
    reason = checkout.rule_3_reason("format_me.py")
    if foreign_finishes_first is None:
        assert reason == ""
    else:
        assert "overlap" in reason and "format_me.py" in reason


def test_owner_checkpoint_recovers_settled_formatter_output(
    mock_dependencies: dict[str, Any],
    task_worktree: Checkout,
) -> None:
    """An unchanged formatter rerun leaves stale hashes; committing proven bytes frees edits."""
    checkout = task_worktree
    checkout.write("format_me.py", "x=1\n")
    SessionVariableManager(checkout.db).record_edited_files(
        checkout.session_id, ["format_me.py"], checkout_root=str(checkout.repo)
    )
    checkout.write("format_me.py", "x = 1\n")
    command = "uv run ruff format format_me.py"
    before = checkout.tool_event(HookEventType.BEFORE_TOOL, command)
    assert (
        asyncio.run(
            foreign_dirty_edit_conflict(
                checkout.db,
                before,
                session_id=checkout.session_id,
                project_id=checkout.project_id,
                project_path=str(checkout.repo),
            )
        )
        == ""
    )
    handlers = _handlers(mock_dependencies, checkout.db)
    assert not _shell_write_refused(handlers, before)
    handlers.handle_after_tool(replace(before, event_type=HookEventType.AFTER_TOOL))
    assert UNOWNED in checkout.rule_3_reason("format_me.py")

    _git(checkout.repo, "add", "--", "format_me.py")
    commit = checkout.tool_event(HookEventType.BEFORE_TOOL, "git commit -m checkpoint")
    assert (
        asyncio.run(
            foreign_staged_commit_conflict(
                checkout.db,
                commit,
                session_id=checkout.session_id,
                project_id=checkout.project_id,
                project_path=str(checkout.repo),
            )
        )
        == ""
    )
    _git(
        checkout.repo,
        "-c",
        "user.email=t@gobby.local",
        "-c",
        "user.name=T",
        "commit",
        "-qm",
        "checkpoint",
    )
    assert (checkout.repo / "format_me.py").read_text() == "x = 1\n"
    assert checkout.rule_3_reason("format_me.py") == ""


@pytest.mark.parametrize("wrapped", [False, True])
def test_yielded_codex_formatter_keeps_snapshot_until_terminal_poll(
    mock_dependencies: dict[str, Any], task_worktree: Checkout, wrapped: bool
) -> None:
    checkout = task_worktree
    checkout.write("format_me.py", "x=1\n")
    manager = SessionVariableManager(checkout.db)
    manager.record_edited_files(
        checkout.session_id, ["format_me.py"], checkout_root=str(checkout.repo)
    )
    before = replace(
        checkout.tool_event(HookEventType.BEFORE_TOOL, "uv run ruff format format_me.py"),
        source=SessionSource.CODEX,
        request_id=None,
    )
    before.data["verification_execution_id"] = "yield-formatter"
    handlers = _handlers(mock_dependencies, checkout.db)
    assert not _shell_write_refused(handlers, before)
    chain = ExecutionChainCorrelator()
    pending = chain.correlate(
        {
            "tool_name": "functions.exec" if wrapped else "exec_command",
            "call_id": "yield-formatter",
            "arguments": (
                'text(await tools.exec_command({cmd: "uv run ruff format format_me.py"}))'
                if wrapped
                else {"cmd": "uv run ruff format format_me.py"}
            ),
            "tool_output": "Script running with cell ID format-cell"
            if wrapped
            else "Chunk ID: first\nWall time: 1s\nProcess running with session ID 99\nOutput:\n",
        }
    )
    assert pending["_verification_pending"] is True
    handlers.handle_after_tool(replace(before, event_type=HookEventType.AFTER_TOOL, data=pending))
    if wrapped:
        running = chain.correlate(
            {
                "tool_name": "functions.wait",
                "call_id": "formatter-cell-poll",
                "arguments": {"cell_id": "format-cell"},
                "tool_output": {"session_id": 99},
            }
        )
        handlers.handle_after_tool(
            replace(before, event_type=HookEventType.AFTER_TOOL, data=running)
        )
    truncated = chain.correlate(
        {
            "tool_name": "write_stdin",
            "call_id": "truncated-poll",
            "arguments": {"session_id": 99},
            "tool_output": "Output chunk without a terminal receipt",
        }
    )
    handlers.handle_after_tool(replace(before, event_type=HookEventType.AFTER_TOOL, data=truncated))
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", "format_me.py"],
        cwd=checkout.repo,
        check=True,
        capture_output=True,
    )
    resumed_chain = ExecutionChainCorrelator()
    resumed_chain.hydrate_state(chain.snapshot_state())
    terminal = resumed_chain.correlate(
        {
            "tool_name": "write_stdin",
            "call_id": "formatter-poll",
            "arguments": {"session_id": 99},
            "tool_output": {"exit_code": 0}
            if wrapped
            else "Chunk ID: last\nWall time: 1s\nProcess exited with code 0\nOutput:\n",
        }
    )
    assert terminal["verification_execution_id"] == "yield-formatter"
    _handlers(mock_dependencies, checkout.db).handle_after_tool(
        replace(before, event_type=HookEventType.AFTER_TOOL, data=terminal)
    )

    assert checkout.rule_3_reason("format_me.py") == ""
    assert not manager.get_variables(checkout.session_id).get("claimed_shell_snapshots")
    assert manager.worktree_shell_call_root(checkout.session_id, "yield-formatter") is None


def _prepare_incoming(checkout: Checkout, path: str) -> None:
    repo = checkout.repo
    _git(repo, "config", "user.email", "t@gobby.local")
    _git(repo, "config", "user.name", "T")
    _git(repo, "checkout", "-b", "incoming")
    checkout.write(path, "incoming\n")
    _git(repo, "add", path)
    _git(repo, "-c", "user.email=t@gobby.local", "-c", "user.name=T", "commit", "-m", "incoming")
    _git(repo, "checkout", "-")


def test_git_operation_receipt_survives_pending_and_handler_restart(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    repo = checkout.repo
    _prepare_incoming(checkout, "foreign.txt")
    manager = SessionVariableManager(checkout.db)
    manager.record_edited_files(
        checkout.foreign_session_id, ["foreign.txt"], checkout_root=str(repo)
    )
    command = "git merge --no-ff --no-commit incoming"
    before = replace(
        checkout.tool_event(HookEventType.BEFORE_TOOL, command),
        source=SessionSource.CODEX,
        request_id=None,
    )
    before.data["verification_execution_id"] = "git-operation"
    handlers = _handlers(mock_dependencies, checkout.db)
    assert handlers.handle_before_tool(before).decision == "allow"
    _git(repo, "merge", "--no-ff", "--no-commit", "incoming")
    pending = replace(
        before,
        event_type=HookEventType.AFTER_TOOL,
        data={**before.data, "_verification_pending": True},
    )
    handlers.handle_after_tool(pending)
    terminal = replace(before, event_type=HookEventType.AFTER_TOOL)
    _handlers(mock_dependencies, checkout.db).handle_after_tool(terminal)
    assert checkout.rule_3_reason("foreign.txt") == ""
    checkout.write("foreign.txt", "foreign edit after the merge stopped\n")
    manager.record_edited_files(
        checkout.foreign_session_id, ["foreign.txt"], checkout_root=str(repo)
    )
    assert "foreign.txt" in checkout.rule_3_reason("foreign.txt")


def test_claude_background_git_waits_for_its_terminal_output(
    mock_dependencies: dict[str, Any], checkout: Checkout, tmp_path: Path
) -> None:
    _prepare_incoming(checkout, "foreign.txt")
    manager = SessionVariableManager(checkout.db)
    manager.record_edited_files(
        checkout.foreign_session_id, ["foreign.txt"], checkout_root=str(checkout.repo)
    )
    command = "git merge --no-ff --no-commit incoming"
    handlers = _handlers(mock_dependencies, checkout.db)
    before = checkout.tool_event(HookEventType.BEFORE_TOOL, command)
    assert handlers.handle_before_tool(before).decision == "allow"
    output = tmp_path / "git-job.output"
    output.write_text("running\n", encoding="utf-8")
    receipt = (
        "Command did not complete within its 10s timeout and was moved to the background "
        f"(ID: git-job). Output is being written to: {output}. "
    )
    handlers.handle_after_tool(
        replace(
            before,
            event_type=HookEventType.AFTER_TOOL,
            data={**before.data, "tool_output": receipt},
        )
    )
    TaskToolBindings(manager, checkout.session_id).clear_pending("prompt")
    _git(checkout.repo, "merge", "--no-ff", "--no-commit", "incoming")
    assert "foreign.txt" in checkout.rule_3_reason("foreign.txt")
    output.write_text("merge stopped\n[exited with code 0]\n", encoding="utf-8")
    asyncio.run(
        SessionVariableManager(checkout.db).settle_background_git_operations(checkout.session_id)
    )
    assert checkout.rule_3_reason("foreign.txt") == ""


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_git_operation_before_hook_refuses_failed_provenance_inspection(
    mock_dependencies: dict[str, Any], checkout: Checkout, cleanup_fails: bool
) -> None:
    handlers = _handlers(mock_dependencies, checkout.db)
    event = checkout.tool_event(HookEventType.BEFORE_TOOL, "git merge incoming")
    with (
        patch.object(
            SessionVariableManager,
            "begin_git_operation_call",
            AsyncMock(side_effect=ValueError("inspection unavailable")),
        ),
        patch.object(
            SessionVariableManager,
            "cancel_git_operation_call",
            side_effect=RuntimeError("cleanup unavailable") if cleanup_fails else None,
        ),
    ):
        result = handlers.handle_before_tool(event)
    assert result.decision == "block"
    assert result.reason is not None and "operation ownership" in result.reason


def test_git_operation_without_execution_id_is_refused(
    mock_dependencies: dict[str, Any], checkout: Checkout
) -> None:
    event = replace(
        checkout.tool_event(HookEventType.BEFORE_TOOL, "git merge incoming"), request_id=None
    )
    result = _handlers(mock_dependencies, checkout.db).handle_before_tool(event)
    assert result.decision == "block"
    assert result.reason is not None and "execution ID" in result.reason


def test_git_operation_conflict_keeps_its_actionable_reason(
    mock_dependencies: dict[str, Any],
    checkout: Checkout,
) -> None:
    manager = SessionVariableManager(checkout.db)
    asyncio.run(
        manager.begin_git_operation_call(
            checkout.foreign_session_id, "foreign-operation", [str(checkout.repo)]
        )
    )
    response = _handlers(mock_dependencies, checkout.db).handle_before_tool(
        checkout.tool_event(HookEventType.BEFORE_TOOL, "git merge incoming")
    )
    assert response.decision == "block"
    assert "Another live session owns the Git operation" in (response.reason or "")
    TaskToolBindings(manager, checkout.session_id).assert_can_select(None)
    foreign_operations = manager.get_variables(checkout.foreign_session_id)[
        "git_operation_ownership"
    ]
    assert foreign_operations[str(checkout.repo)]["call_id"] == "foreign-operation"


@pytest.mark.parametrize("mode", ["--no-rebase", "--rebase"])
def test_local_pull_conflict_retains_operation_provenance(
    mock_dependencies: dict[str, Any], checkout: Checkout, mode: str
) -> None:
    _prepare_incoming(checkout, "foreign.txt")
    checkout.write("foreign.txt", "local\n")
    _git(checkout.repo, "add", "owned.txt", "stale.txt", "foreign.txt")
    _git(
        checkout.repo,
        "-c",
        "user.email=t@gobby.local",
        "-c",
        "user.name=T",
        "commit",
        "-m",
        "local",
    )
    manager = SessionVariableManager(checkout.db)
    manager.record_edited_files(
        checkout.foreign_session_id, ["foreign.txt"], checkout_root=str(checkout.repo)
    )
    command = f"git pull {mode} . incoming"
    handlers = _handlers(mock_dependencies, checkout.db)
    before = checkout.tool_event(HookEventType.BEFORE_TOOL, command)
    assert handlers.handle_before_tool(before).decision == "allow"
    result = subprocess.run(
        ["git", "pull", mode, ".", "incoming"], cwd=checkout.repo, capture_output=True, text=True
    )
    assert result.returncode == 1, result.stderr
    handlers.handle_after_tool(checkout.tool_event(HookEventType.AFTER_TOOL, command, failed=True))
    assert checkout.rule_3_reason("foreign.txt") == ""
    checkout.write("foreign.txt", "foreign after pull\n")
    manager.record_edited_files(
        checkout.foreign_session_id, ["foreign.txt"], checkout_root=str(checkout.repo)
    )
    assert "foreign.txt" in checkout.rule_3_reason("foreign.txt")


@pytest.mark.parametrize("foreign_finishes_first", [False, True])
def test_git_operation_cannot_exempt_an_overlapping_unattributed_write(
    mock_dependencies: dict[str, Any], task_worktree: Checkout, foreign_finishes_first: bool
) -> None:
    checkout = task_worktree
    _prepare_incoming(checkout, "racer.txt")
    handlers = _handlers(mock_dependencies, checkout.db)
    before = checkout.tool_event(
        HookEventType.BEFORE_TOOL, "git merge --no-ff --no-commit incoming"
    )
    foreign = replace(checkout, session_id=checkout.foreign_session_id)
    foreign_before = replace(
        foreign.tool_event(HookEventType.BEFORE_TOOL, "uv run python rewrite.py racer.txt"),
        session_id="foreign-external",
        request_id="foreign-shell",
    )
    assert not _shell_write_refused(handlers, before)
    assert not _shell_write_refused(handlers, foreign_before)
    _git(checkout.repo, "merge", "--no-ff", "--no-commit", "incoming")
    checkout.write("racer.txt", "foreign concurrent write\n")
    after = replace(before, event_type=HookEventType.AFTER_TOOL)
    foreign_after = replace(foreign_before, event_type=HookEventType.AFTER_TOOL)
    events = (foreign_after, after) if foreign_finishes_first else (after, foreign_after)
    for event in events:
        handlers.handle_after_tool(event)
    assert "racer.txt" not in checkout.ledger()
    assert "racer.txt" in checkout.rule_3_reason("racer.txt")
    _git(checkout.repo, "add", "racer.txt")
    reason = asyncio.run(
        foreign_staged_commit_conflict(
            checkout.db,
            checkout.tool_event(HookEventType.BEFORE_TOOL, "git commit -m merge"),
            session_id=checkout.session_id,
            project_id=checkout.project_id,
            project_path=str(checkout.repo),
        )
    )
    assert "racer.txt" in reason
    assert "overlapping" in reason


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


def _assert_refused_for_overlap(checkout: Checkout, rel_path: str) -> None:
    """Edit, restore and deletion of ``rel_path`` are each refused, and the refusal names the overlap."""
    for command in (None, f"git restore -- {rel_path}", f"rm {rel_path}"):
        reason = checkout.rule_3_reason(rel_path, command=command)
        assert rel_path in reason, reason
        assert re.search(r"\boverlap(?:ped|ping)?\b", reason, re.IGNORECASE), reason


def test_script_deleting_a_clean_tracked_file_attributes_the_deletion(
    mock_dependencies: dict[str, Any], task_worktree: Checkout
) -> None:
    """A script that deletes a clean tracked file owns the deletion, so its restore is not refused."""
    checkout = task_worktree

    def delete_tracked() -> None:
        (checkout.repo / "base.txt").unlink()

    checkout.run(
        _handlers(mock_dependencies, checkout.db),
        "uv run python /tmp/delete_tracked.py base.txt",
        delete_tracked,
    )

    assert "base.txt" in checkout.ledger()
    assert "base.txt" in checkout.task_ledger()
    assert checkout.rule_3_reason("base.txt") == ""
    for mutation in ("git restore -- base.txt", "rm base.txt"):
        assert checkout.rule_3_reason("base.txt", command=mutation) == ""


@pytest.mark.parametrize("foreign_finishes_first", [False, True])
def test_overlapping_literal_write_stays_foreign(
    mock_dependencies: dict[str, Any], task_worktree: Checkout, foreign_finishes_first: bool
) -> None:
    """A literal write an overlapping call also changed stays foreign, in either completion order."""
    checkout = task_worktree
    handlers = _handlers(mock_dependencies, checkout.db)
    foreign = replace(checkout, session_id=checkout.foreign_session_id)
    command = "printf 'literal write\\n' > racer.txt"
    current_before = checkout.tool_event(HookEventType.BEFORE_TOOL, command)
    foreign_before = replace(
        foreign.tool_event(HookEventType.BEFORE_TOOL, "uv run python /tmp/rewrite.py racer.txt"),
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
    _assert_refused_for_overlap(checkout, "racer.txt")


@pytest.mark.parametrize("foreign_finishes_first", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "uv run python /tmp/rewrite.py owned.txt",
        "printf 'script rewrite\\n' > owned.txt",
        "uv run ruff format owned.txt",
    ],
)
def test_overlapping_rewrite_of_an_owned_path_is_refused_with_overlap_named(
    mock_dependencies: dict[str, Any],
    task_worktree: Checkout,
    command: str,
    foreign_finishes_first: bool,
) -> None:
    """A rewrite of a path the task owns, overlapped by another session's write, stays foreign."""
    checkout = task_worktree
    handlers = _handlers(mock_dependencies, checkout.db)
    foreign = replace(checkout, session_id=checkout.foreign_session_id)
    current_before = checkout.tool_event(HookEventType.BEFORE_TOOL, command)
    foreign_before = replace(
        foreign.tool_event(HookEventType.BEFORE_TOOL, "uv run python /tmp/rewrite.py owned.txt"),
        session_id="foreign-external",
        request_id="toolu_foreign",
    )
    assert not _shell_write_refused(handlers, current_before)
    assert not _shell_write_refused(handlers, foreign_before)
    checkout.write("owned.txt", "foreign rewrite\n")
    checkout.write("owned.txt", "script rewrite\n")
    current_after = checkout.tool_event(HookEventType.AFTER_TOOL, command)
    foreign_after = replace(foreign_before, event_type=HookEventType.AFTER_TOOL)
    events = (
        (foreign_after, current_after) if foreign_finishes_first else (current_after, foreign_after)
    )
    for event in events:
        handlers.handle_after_tool(event)

    _assert_refused_for_overlap(checkout, "owned.txt")


def test_overlap_record_refuses_an_owned_path_whose_content_still_matches(
    task_worktree: Checkout,
) -> None:
    """An overlap record refuses its path even when the caller's own record matches its content."""
    SessionVariableManager(task_worktree.db).merge_variables(
        task_worktree.session_id,
        {OVERLAPS_VARIABLE: {str(task_worktree.repo): ["owned.txt"]}},
    )

    _assert_refused_for_overlap(task_worktree, "owned.txt")


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

"""Deterministic inventory of intentional synchronous Git boundaries."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPO_ROOT / "src" / "gobby"
_SYNC_GIT_HELPERS = {
    "get_git_branch",
    "get_git_metadata",
    "get_github_url",
    "is_path_gitignored",
    "normalize_commit_sha",
    "run_git_command",
}
_SUBPROCESS_CALLS = {"call", "check_call", "check_output", "Popen", "run"}
# gobby.utils.spawn's blocking entry points (#22815).
_SPAWN_CALLS = {"popen", "run"}


@dataclass(frozen=True, order=True)
class SyncGitUse:
    path: str
    scope: str
    mechanism: str


@dataclass(frozen=True, order=True)
class SyncFacadeCall:
    path: str
    scope: str
    facade: str


class _SyncGitVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scopes: list[str] = []
        self.git_argv_names: list[set[str]] = [set()]
        self.git_helper_names: dict[str, str] = {}
        self.git_module_names: set[str] = set()
        self.subprocess_module_names: set[str] = set()
        self.subprocess_call_names: dict[str, str] = {}
        self.spawn_module_names: set[str] = set()
        self.await_depth = 0
        self.uses: set[SyncGitUse] = set()

    @property
    def scope(self) -> str:
        return ".".join(self.scopes) or "<module>"

    def _record(self, mechanism: str) -> None:
        self.uses.add(SyncGitUse(self.path, self.scope, mechanism))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".")[0]
            if alias.name == "subprocess":
                self.subprocess_module_names.add(local_name)
            elif alias.name == "gobby.utils.git":
                self.git_module_names.add(local_name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "gobby.utils.git":
            for alias in node.names:
                if alias.name in _SYNC_GIT_HELPERS:
                    self.git_helper_names[alias.asname or alias.name] = alias.name
        elif node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SUBPROCESS_CALLS:
                    self.subprocess_call_names[alias.asname or alias.name] = alias.name
        elif node.module == "gobby.utils":
            for alias in node.names:
                if alias.name == "spawn":
                    self.spawn_module_names.add(alias.asname or alias.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(node.name)
        self.git_argv_names.append(set())
        self.generic_visit(node)
        self.git_argv_names.pop()
        self.scopes.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                if self._is_git_argv(node.value):
                    self.git_argv_names[-1].add(target.id)
                else:
                    self.git_argv_names[-1].discard(target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            if self._is_git_argv(node.value):
                self.git_argv_names[-1].add(node.target.id)
            else:
                self.git_argv_names[-1].discard(node.target.id)
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        self.await_depth += 1
        self.generic_visit(node)
        self.await_depth -= 1

    def visit_Call(self, node: ast.Call) -> None:
        helper = self._sync_helper_call(node.func)
        if helper is not None:
            self._record(f"gobby.utils.git.{helper}")

        subprocess_call = self._subprocess_call(node.func)
        if subprocess_call is not None and node.args and self._is_git_argv(node.args[0]):
            self._record(f"subprocess.{subprocess_call}")

        if self._attribute_name(node.func) == "run_git_command" and self.await_depth == 0:
            self._record("unawaited run_git_command")

        callback = self._executor_callback(node)
        if callback is not None:
            callback_helper = self._sync_helper_reference(callback)
            if callback_helper is not None:
                self._record(f"executor callback {callback_helper}")

        self.generic_visit(node)

    def _sync_helper_call(self, func: ast.expr) -> str | None:
        if isinstance(func, ast.Name):
            return self.git_helper_names.get(func.id)
        if isinstance(func, ast.Attribute) and func.attr in _SYNC_GIT_HELPERS:
            if isinstance(func.value, ast.Name) and func.value.id in self.git_module_names:
                return func.attr
        return None

    def _subprocess_call(self, func: ast.expr) -> str | None:
        if isinstance(func, ast.Name):
            return self.subprocess_call_names.get(func.id)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.attr in _SUBPROCESS_CALLS and func.value.id in self.subprocess_module_names:
                return func.attr
            if func.attr in _SPAWN_CALLS and func.value.id in self.spawn_module_names:
                return func.attr
        return None

    def _executor_callback(self, node: ast.Call) -> ast.expr | None:
        name = self._attribute_name(node.func)
        if name == "to_thread" and node.args:
            return node.args[0]
        if name == "run_in_executor" and len(node.args) > 1:
            return node.args[1]
        return None

    def _sync_helper_reference(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            helper = self.git_helper_names.get(node.id)
            return f"gobby.utils.git.{helper}" if helper else None
        if isinstance(node, ast.Attribute):
            if node.attr == "run_git_command":
                return "run_git_command"
            helper = self._sync_helper_call(node)
            return f"gobby.utils.git.{helper}" if helper else None
        return None

    def _is_git_argv(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return any(node.id in names for names in reversed(self.git_argv_names))
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            return isinstance(first, ast.Constant) and first.value == "git"
        return False

    @staticmethod
    def _attribute_name(node: ast.expr) -> str | None:
        return node.attr if isinstance(node, ast.Attribute) else None


def _sync_git_inventory() -> list[SyncGitUse]:
    uses: set[SyncGitUse] = set()
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(_REPO_ROOT).as_posix()
        visitor = _SyncGitVisitor(relative)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=relative))
        uses.update(visitor.uses)
    return sorted(uses)


_SYNC_GIT_FACADES = {
    "_init_no_marker",
    "clone_skill_repo",
    "committable_task_paths",
    "get_dirty_files",
    "get_dirty_files_categorized",
    "get_file_changes",
    "get_git_diff_summary",
    "get_git_status",
    "get_recent_git_commits",
    "has_committable_edits",
    "initialize_project",
    "load_from_github",
    "resolve_evidence",
    "resolve_git_worktree_root",
    "task_dirty_paths",
    "verify_bundled_integrity",
}


class _SyncFacadeCallVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scopes: list[str] = []
        self.async_aliases: set[str] = set()
        self.calls: set[SyncFacadeCall] = set()

    @property
    def scope(self) -> str:
        return ".".join(self.scopes) or "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name.endswith("_async"):
                self.async_aliases.add(alias.asname or alias.name)

    def visit_Call(self, node: ast.Call) -> None:
        name = self._call_name(node.func)
        if name not in self.async_aliases and (
            name in _SYNC_GIT_FACADES or self._is_skill_updater_call(node.func, name)
        ):
            self.calls.add(SyncFacadeCall(self.path, self.scope, name))
        self.generic_visit(node)

    @staticmethod
    def _call_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    @staticmethod
    def _is_skill_updater_call(node: ast.expr, name: str) -> bool:
        if name not in {"update_all", "update_skill"} or not isinstance(node, ast.Attribute):
            return False
        receiver = node.value
        if isinstance(receiver, ast.Name):
            return receiver.id == "updater"
        return isinstance(receiver, ast.Attribute) and receiver.attr == "updater"


def _sync_facade_calls() -> list[SyncFacadeCall]:
    calls: set[SyncFacadeCall] = set()
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(_REPO_ROOT).as_posix()
        visitor = _SyncFacadeCallVisitor(relative)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=relative))
        calls.update(visitor.calls)
    return sorted(calls)


_ALLOWED_SYNC_GIT_BOUNDARIES = {
    # Offline Click commands and build-time verification entry points.
    ("src/gobby/cli/init.py", "_git_toplevel"),
    ("src/gobby/cli/plan.py", "_CliEvidenceContext.get_commit_range_diff"),
    ("src/gobby/cli/sessions.py", "summarize_session"),
    ("src/gobby/cli/tasks/commits.py", "link_commit"),
    ("src/gobby/cli/tasks/commits.py", "unlink_commit"),
    ("src/gobby/plans/evidence.py", "_run_git"),
    ("src/gobby/sync/integrity.py", "verify_bundled_integrity"),
    ("src/gobby/utils/project_init.py", "_init_no_marker"),
    # Explicit synchronous facades. Daemon callers use the adjacent async APIs.
    ("src/gobby/skills/_loader_github.py", "clone_skill_repo"),
    ("src/gobby/workflows/git_utils.py", "get_dirty_files_categorized"),
    ("src/gobby/workflows/git_utils.py", "get_file_changes"),
    ("src/gobby/workflows/git_utils.py", "get_git_diff_summary"),
    ("src/gobby/workflows/git_utils.py", "get_git_status"),
    ("src/gobby/workflows/git_utils.py", "get_recent_git_commits"),
    ("src/gobby/workflows/git_utils.py", "resolve_git_worktree_root"),
    ("src/gobby/workflows/task_dirty_state.py", "committable_task_paths"),
    ("src/gobby/workflows/task_dirty_state.py", "task_dirty_paths"),
}

_ALLOWED_SYNC_FACADE_CALLERS = {
    ("src/gobby/skills/loader.py", "SkillLoader.load_from_github", "clone_skill_repo"),
    (
        "src/gobby/skills/updater.py",
        "SkillUpdater._fetch_from_github",
        "clone_skill_repo",
    ),
    ("src/gobby/utils/project_init.py", "_initialize_project", "_init_no_marker"),
    (
        "src/gobby/workflows/git_utils.py",
        "get_dirty_files",
        "get_dirty_files_categorized",
    ),
    (
        "src/gobby/workflows/git_utils.py",
        "get_dirty_files_categorized",
        "resolve_git_worktree_root",
    ),
    (
        "src/gobby/workflows/task_dirty_state.py",
        "has_committable_edits",
        "task_dirty_paths",
    ),
}


def test_runtime_git_has_no_unapproved_synchronous_boundaries() -> None:
    inventory = _sync_git_inventory()
    unapproved = [
        use for use in inventory if (use.path, use.scope) not in _ALLOWED_SYNC_GIT_BOUNDARIES
    ]
    assert not unapproved, "Unapproved synchronous Git boundaries:\n" + "\n".join(
        f"- {use.path}:{use.scope} ({use.mechanism})" for use in unapproved
    )

    observed_boundaries = {(use.path, use.scope) for use in inventory}
    stale = sorted(_ALLOWED_SYNC_GIT_BOUNDARIES - observed_boundaries)
    assert not stale, "Stale synchronous Git allowlist entries:\n" + "\n".join(
        f"- {path}:{scope}" for path, scope in stale
    )


def test_daemon_runtime_does_not_call_synchronous_git_facades() -> None:
    calls = _sync_facade_calls()
    unapproved = [
        call
        for call in calls
        if not call.path.startswith("src/gobby/cli/")
        and (call.path, call.scope, call.facade) not in _ALLOWED_SYNC_FACADE_CALLERS
    ]
    assert not unapproved, "Daemon callers of synchronous Git facades:\n" + "\n".join(
        f"- {call.path}:{call.scope} ({call.facade})" for call in unapproved
    )

    observed = {(call.path, call.scope, call.facade) for call in calls}
    stale = sorted(_ALLOWED_SYNC_FACADE_CALLERS - observed)
    assert not stale, "Stale synchronous Git facade callers:\n" + "\n".join(
        f"- {path}:{scope} ({facade})" for path, scope, facade in stale
    )

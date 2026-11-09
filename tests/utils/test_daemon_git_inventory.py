"""Deterministic inventory of intentional synchronous Git boundaries."""

from __future__ import annotations

import ast
import functools
import gc
from collections.abc import Iterator
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
        elif node.module == "gobby.utils.spawn":
            for alias in node.names:
                if alias.name in _SPAWN_CALLS:
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


@functools.cache
def _source_texts() -> tuple[tuple[str, str], ...]:
    """Read src/gobby once per test session. Source text is small; parsed trees are not."""
    return tuple(
        (path.relative_to(_REPO_ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
    )


def _parsed_sources(*needles: str) -> Iterator[tuple[str, ast.Module]]:
    """Yield (path, tree) for each src/gobby file whose text contains a needle.

    A scan records a construct only when one of its names or literals appears in the
    file text, so files without any needle are skipped unparsed. Trees are yielded one
    at a time and released as the caller moves on: keeping every tree alive put millions
    of AST objects on the GC heap, and each later gc.collect() then took tens of seconds.
    """
    for relative, text in _source_texts():
        if any(needle in text for needle in needles):
            yield relative, ast.parse(text, filename=relative)


@functools.cache
def _sync_git_inventory() -> tuple[SyncGitUse, ...]:
    uses: set[SyncGitUse] = set()
    for relative, tree in _parsed_sources("git", *_SYNC_GIT_HELPERS):
        visitor = _SyncGitVisitor(relative)
        visitor.visit(tree)
        uses.update(visitor.uses)
    return tuple(sorted(uses))


_SYNC_GIT_FACADES = {
    "clone_skill_repo",
    "close_task_with_commit",
    "committable_task_paths",
    "get_dirty_files",
    "get_dirty_files_categorized",
    "get_file_changes",
    "get_git_diff_summary",
    "get_git_status",
    "get_recent_git_commits",
    "has_committable_edits",
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


@functools.cache
def _sync_facade_calls() -> tuple[SyncFacadeCall, ...]:
    calls: set[SyncFacadeCall] = set()
    for relative, tree in _parsed_sources(*_SYNC_GIT_FACADES, "update_all", "update_skill"):
        visitor = _SyncFacadeCallVisitor(relative)
        visitor.visit(tree)
        calls.update(visitor.calls)
    return tuple(sorted(calls))


_ALLOWED_SYNC_GIT_BOUNDARIES = {
    # Offline Click commands and build-time verification entry points.
    ("src/gobby/cli/init.py", "_git_toplevel"),
    ("src/gobby/cli/plan.py", "_CliEvidenceContext.get_commit_range_diff"),
    ("src/gobby/cli/sessions.py", "summarize_session"),
    ("src/gobby/cli/tasks/commits.py", "link_commit"),
    ("src/gobby/cli/tasks/commits.py", "unlink_commit"),
    ("src/gobby/plans/evidence.py", "_run_git"),
    ("src/gobby/sync/integrity.py", "verify_bundled_integrity"),
    # Reached only from the `gobby test-types suppressions` Click command
    # (test_cli_only_git_boundaries_are_reached_only_from_cli below).
    ("src/gobby/test_types/suppressions.py", "_git_python_files"),
    # Explicit synchronous facades. Daemon callers use the adjacent async APIs.
    ("src/gobby/hooks/checkout_reads.py", "run_git"),
    ("src/gobby/skills/_loader_github.py", "clone_skill_repo"),
    ("src/gobby/workflows/git_utils.py", "get_dirty_files_categorized"),
    ("src/gobby/workflows/git_utils.py", "get_file_changes"),
    ("src/gobby/workflows/git_utils.py", "get_git_diff_summary"),
    ("src/gobby/workflows/git_utils.py", "get_git_status"),
    ("src/gobby/workflows/git_utils.py", "get_recent_git_commits"),
    ("src/gobby/workflows/git_utils.py", "resolve_git_worktree_root"),
    ("src/gobby/workflows/task_dirty_state.py", "committable_task_paths"),
    ("src/gobby/workflows/task_dirty_state.py", "task_dirty_paths"),
    # Status report git reads run only through compose_report, which the status-report
    # route calls with asyncio.to_thread (mcp_proxy/tools/communications.py); no other
    # caller reaches collect_status.
    ("src/gobby/status_report/collect.py", "_run_git"),
    # Daemon boundaries that run only on worker threads, each with a test proving
    # no event loop runs there.
    # Only navigation_requires_index asks, from rule conditions on the rule-engine
    # executor (tests/workflows/engine/test_condition_git_off_loop.py).
    ("src/gobby/hooks/code_navigation_recovery.py", "_git_ignored"),
    # Reached only from the `gobby status` Click command
    # (test_bin_freshness_git_is_reached_only_from_cli_status below).
    ("src/gobby/install/bin_freshness_promotion.py", "last_source_commit_time"),
    # Reached only with a workspace_root, which only install and cutover Click
    # commands pass; the daemon's bin freshness loop promotes release archives
    # with workspace_root=None (test_cli_only_git_boundaries_are_reached_only_from_cli).
    ("src/gobby/install/bin_release_identity.py", "workspace_source_identity"),
    # Canonical shell normalization runs off the loop: HookManager and the rule
    # engine offload it, chat _fire_lifecycle and Codex approvals use
    # asyncio.to_thread (tests/adapters/test_adapters_codex.py
    # test_checkout_approval_git_runs_off_the_event_loop), and event-loop display
    # callers use normalize_tool_display_fields
    # (test_display_normalization_callers_read_no_canonical_metadata below;
    # chat base.py keeps only mcp_server/mcp_tool, pinned by
    # tests/servers/websocket/chat/test_provider_backends.py
    # test_post_tool_lifecycle_adds_only_mcp_display_fields). handle_after_tool
    # derives shell metadata on the hook worker (tests/adapters/test_adapters_codex.py
    # test_item_completed_shell_write_marks_had_edits_without_rule_pass).
    ("src/gobby/hooks/_normalization_git.py", "_positional_checkout_is_path"),
    # checkout_reads.run_git spawns git only for a local delivery, from the sync callers above
    # (_git_ignored, _positional_checkout_is_path) on worker threads. run_git_async
    # runs it through asyncio.to_thread (tests/hooks/test_node_origin_git.py
    # test_local_git_spawn_runs_off_the_event_loop).
    ("src/gobby/hooks/checkout_reads.py", "run_git"),
    # Sync plan tools run through InternalToolRegistry.call's worker; async plan
    # tools resolve projects inside _run_db_call or their complete review-evidence
    # service worker (test_plans_tools covers sync and async checkout forms).
    ("src/gobby/mcp_proxy/tools/plans/__init__.py", "_optional_project_id"),
    # The HTTP close route passes this synchronous storage facade to server.run_db.
    # The facade inventory below guards new direct daemon calls; it does not
    # inspect higher-order uses such as run_db(facade, ...).
    (
        "src/gobby/storage/tasks/_transitions_facade.py",
        "TaskTransitionsMixin.close_task_with_commit",
    ),
    # Plan validation runs on MCP/coverage workers and the compile offload;
    # offline validation remains synchronous (test_expansion_service_compile).
    ("src/gobby/tasks/expansion/_validate.py", "_task_has_landed_commit"),
    # FoundWorkGateAnalyzer offloads the complete cover calculation through
    # _uncovered_failure_commands (test_found_work_gate's off-loop regression).
    ("src/gobby/workflows/validation_cover.py", "_tracked_at_head"),
    # commit_workspace runs ensure_workspace, the only caller of _init_git_dir, through
    # asyncio.to_thread before it commits, so the git init never runs on the loop.
    ("src/gobby/agents/agent_workspaces.py", "_init_git_dir"),
    # _checkout_root is reached through assigned_run_checkout and _team_checkout from
    # spawn_workspace_write_paths, which grant_seat_workspace_paths calls through
    # asyncio.to_thread in the spawn and resume executors and through run_db in the
    # websocket launch, and from assigned_task_worktree, which only the workflow condition
    # helper calls under the evaluator's offload, so the checkout lookup never runs on the loop.
    ("src/gobby/agents/agent_workspaces.py", "_checkout_root"),
    # Built-in observers run through asyncio.to_thread in WorkflowHookHandler._evaluate_rules
    # (tests/workflows/test_hooks.py
    # test_sync_hook_collaborators_run_outside_event_loop_thread), so the authored-merge
    # HEAD read never runs on the loop.
    ("src/gobby/workflows/observer_commits.py", "_record_authored_merge"),
}

_ALLOWED_SYNC_FACADE_CALLERS = {
    # Only release_task_claim and hand_off_task_claim call it. Both are sync tools, so
    # InternalToolRegistry.call runs them through asyncio.to_thread
    # (tests/mcp_proxy/tools/tasks/test_coordinator_claim_release.py
    # test_release_dirty_check_runs_off_the_event_loop and tests/agents/test_team_review_loop.py
    # test_hand_off_dirty_check_runs_off_the_event_loop).
    (
        "src/gobby/mcp_proxy/tools/tasks/_claim_release.py",
        "_uncommitted_refusal",
        "task_dirty_paths",
    ),
    ("src/gobby/skills/loader.py", "SkillLoader.load_from_github", "clone_skill_repo"),
    (
        "src/gobby/skills/updater.py",
        "SkillUpdater._fetch_from_github",
        "clone_skill_repo",
    ),
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
    # Daemon startup runs the refusal through asyncio.to_thread before any bundled
    # content is published (tests/runner_init/test_config_runtime_startup.py).
    (
        "src/gobby/sync/integrity.py",
        "dirty_bundled_content_refusal",
        "verify_bundled_integrity",
    ),
}


def test_scanner_sees_git_argv_through_a_name_imported_from_spawn() -> None:
    visitor = _SyncGitVisitor("sample.py")
    visitor.visit(ast.parse('from gobby.utils.spawn import run\nrun(["git", "status"])\n'))
    assert [use.scope for use in visitor.uses] == ["<module>"]


def test_source_scan_keeps_no_parsed_tree_alive() -> None:
    # Parsed trees retained after a scan put millions of AST objects on the GC heap;
    # every later gc.collect() then took tens of seconds and stalled session teardown.
    _sync_git_inventory()
    live_trees = [obj for obj in gc.get_objects() if isinstance(obj, ast.Module)]
    assert not live_trees, f"{len(live_trees)} parsed source trees outlived the scan"


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


def _referenced_names(tree: ast.Module) -> frozenset[str]:
    """Names, attributes and import aliases one module uses."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name)
    return frozenset(names)


def _modules_referencing(name: str) -> set[str]:
    return {relative for relative, tree in _parsed_sources(name) if name in _referenced_names(tree)}


def test_bin_freshness_git_is_reached_only_from_cli_status() -> None:
    # last_source_commit_time is approved as a CLI-only boundary: its one chain
    # ends in the `gobby status` Click command, which runs no event loop.
    assert _modules_referencing("last_source_commit_time") == {
        "src/gobby/install/bin_freshness_promotion.py"
    }
    assert _modules_referencing("native_bin_predates_source") == {"src/gobby/utils/deps.py"}
    assert _modules_referencing("collect_all_deps") == {"src/gobby/cli/daemon_lifecycle.py"}


def test_cli_only_git_boundaries_are_reached_only_from_cli() -> None:
    # _git_python_files backs scan_suppressions, which only the Click group in
    # test_types/cli.py calls (src/gobby/cli/test_types.py mounts it).
    assert _modules_referencing("_git_python_files") == {"src/gobby/test_types/suppressions.py"}
    assert _modules_referencing("scan_suppressions") == {"src/gobby/test_types/cli.py"}
    # workspace_source_identity runs from cutover or from check_binary_release
    # given a workspace_root. Only
    # preflight_binary_promotions supplies one; the daemon's release path
    # (_stage_and_promote) passes None.
    assert _modules_referencing("workspace_source_identity") == {
        "src/gobby/cli/cutover.py",
        "src/gobby/install/bin_release_identity.py",
    }
    # agent_activation_refusal runs only from the stop, start, restart and cutover commands.
    assert _modules_referencing("agent_activation_refusal") == {
        "src/gobby/cli/cutover.py",
        "src/gobby/cli/daemon_lifecycle.py",
        "src/gobby/cli/daemon_start.py",
    }
    assert _modules_referencing("check_binary_release") == {
        "src/gobby/install/bin_freshness_promotion.py"
    }
    assert _modules_referencing("preflight_binary_promotions") == {
        "src/gobby/cli/cutover.py",
        "src/gobby/install/bin_freshness_promotion.py",
        "src/gobby/install/bin_set_coherence.py",
    }
    # preflight's callers are workspace entry points reached only from Click commands;
    # bin_set_coherence's own use sits inside promote_workspace_binary_set.
    entry_points = (
        "promote_workspace_binary_set",
        "install_staged_cargo_binary",
        "workspace_binary_is_current",
        "stage_and_promote_binary_file",
    )
    callers = set().union(*(_modules_referencing(name) for name in entry_points))
    assert {module for module in callers if not module.startswith("src/gobby/cli/")} == {
        "src/gobby/install/bin_set_coherence.py"
    }


_DISPLAY_NORMALIZATION_MODULES = frozenset(
    {
        "src/gobby/adapters/codex_impl/app_server_adapter.py",
        "src/gobby/adapters/codex_impl/execution_chain.py",
        "src/gobby/adapters/codex_impl/item_normalization.py",
        "src/gobby/servers/websocket/chat/backends/agy.py",
        "src/gobby/servers/websocket/chat/backends/base.py",
        "src/gobby/servers/websocket/chat/backends/codex_events.py",
        "src/gobby/servers/websocket/chat/backends/codex_turns.py",
        "src/gobby/servers/websocket/chat/backends/droid.py",
    }
)


def test_display_normalization_callers_read_no_canonical_metadata() -> None:
    # These modules normalize on the event loop without shell metadata, so none of
    # them may read a canonical_* field; HookManager derives those off the loop.
    # base.py's mcp_server/mcp_tool-only copy is pinned by
    # test_post_tool_lifecycle_adds_only_mcp_display_fields.
    callers = _modules_referencing("normalize_tool_display_fields")
    assert callers - {"src/gobby/hooks/normalization.py"} <= _DISPLAY_NORMALIZATION_MODULES
    for relative in sorted(_DISPLAY_NORMALIZATION_MODULES):
        tree = ast.parse((_REPO_ROOT / relative).read_text(encoding="utf-8"), filename=relative)
        keys = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("canonical_")
        }
        assert not keys, f"{relative} reads canonical metadata: {sorted(keys)}"

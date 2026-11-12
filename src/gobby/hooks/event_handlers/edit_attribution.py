from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.hooks._normalization_canonical import CANONICAL_WRITE_TOOL_NAMES
from gobby.hooks._path_scope import checkout_root, current_tool_cwd, resolve_tool_path
from gobby.hooks.checkout_reads import CheckoutReadUnavailable, node_origin
from gobby.hooks.dispatchers import run_coro_blocking
from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, tool_execution_id
from gobby.hooks.normalization import is_shell_tool
from gobby.hooks.shell_execution import literal_shell_workdir
from gobby.sessions.background_notices import background_receipt
from gobby.sessions.owner_process_tree import capture_job_tree
from gobby.workflows.checkout_task_attribution import checkout_task_at_start
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    assert_task_edit_paths_available,
    effective_tool_cwd,
    normalize_task_checkout_root,
    sanctioned_worktree_root,
    task_edited_file_set_for_checkout,
)
from gobby.workflows.task_dirty_state import paths_committed_after_async
from gobby.workflows.task_tool_bindings import TaskToolBindings
from gobby.workflows.worktree_shell_attribution import (
    attribute_settled_call,
    is_registered_worktree,
    mark_session_had_edits_if_claimed,
    overlapped_worktree_paths,
    release_structured_edit_calls,
    structured_edit_paths,
    worktree_digests,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

EDIT_TOOLS = CANONICAL_WRITE_TOOL_NAMES


def _seat_path(value: str) -> Path:
    """Resolve a seat's cwd or workspace on the hub, but keep a node's as sent.

    The hub's symlinks must not rewrite a node's paths before the node-backed
    checkout primitives see them.
    """
    return Path(value) if node_origin() else Path(value).resolve()


def _call_worktree_root(event: HookEvent) -> str | None:
    """The checkout a Bash call writes, normalized the way the task ledgers key it.

    A Git mutation's proven checkout (``git -C <worktree> merge``) wins over the shell's cwd;
    Git mutations in different checkouts leave the call with no single root.
    """
    git_cwds = event.data.get("canonical_git_mutation_cwds")
    if isinstance(git_cwds, list) and git_cwds:
        roots = {
            checkout_root(Path(git_cwd).resolve(strict=False))
            for git_cwd in git_cwds
            if isinstance(git_cwd, str)
        }
        root = roots.pop() if len(roots) == 1 else None
        return normalize_task_checkout_root(str(root)) if root is not None else None
    cwd = effective_tool_cwd(event.cwd, event.data)
    tool_input = event.data.get("tool_input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command", tool_input.get("cmd"))
        if isinstance(command, str):
            cwd = literal_shell_workdir(command) or cwd
    root = checkout_root(Path(cwd).resolve(strict=False)) if cwd is not None else None
    return normalize_task_checkout_root(str(root)) if root is not None else None


class EditAttributionMixin(EventHandlersBase):
    """Enforce and record tool mutations against checkout ownership."""

    def _assert_edit_path_selection(self, event: HookEvent, session_id: str) -> None:
        """Reject an edit to another active claim's live checkout/path pairs."""
        data = event.data
        edit_paths = data.get("canonical_edit_file_paths")
        sequencer_paths = data.get("canonical_git_sequencer_scope_paths")
        if isinstance(sequencer_paths, list):
            # Sequencer recovery has repository scope but does not author a file.
            # Permit that exemption only within this seat's own checkout.
            try:
                cwd = _seat_path(event.cwd) if event.cwd else current_tool_cwd(data)
                session = self._session_manager.get(session_id) if self._session_manager else None
                workspace = session.workspace_path if session is not None else None
                own_cwd = _seat_path(workspace) if isinstance(workspace, str) and workspace else cwd
                own_root = (checkout_root(own_cwd) or own_cwd) if own_cwd is not None else None
                for path in sequencer_paths:
                    if isinstance(edit_paths, list) and path in edit_paths:
                        continue
                    target = resolve_tool_path(path, cwd)
                    target_root = checkout_root(target) if target is not None else None
                    if (
                        target is None
                        or own_root is None
                        or (target_root is not None and target_root != own_root)
                        or not target.is_relative_to(own_root)
                    ):
                        raise ValueError(
                            "Sequencer recovery outside the seat's own checkout is blocked."
                        )
            except CheckoutReadUnavailable as exc:
                # A node that cannot answer leaves the seat's checkout unproven, so block.
                raise ValueError(
                    "Sequencer recovery outside the seat's own checkout is blocked."
                ) from exc
        if str(data.get("tool_name", "")).lower() not in EDIT_TOOLS and not (
            data.get("canonical_tool_kind") == "write"
            and data.get("canonical_repo_mutation") is True
        ):
            return
        tool_input = data.get("tool_input")
        paths = data.get("canonical_edit_file_paths", data.get("canonical_file_paths"))
        if not isinstance(paths, list):
            path = None
            if isinstance(tool_input, dict):
                path = (
                    tool_input.get("file_path")
                    or tool_input.get("target_file")
                    or tool_input.get("path")
                )
            paths = [path] if isinstance(path, str) else []
        db = getattr(self._session_manager, "db", None)
        if db is None:
            return
        variables = SessionVariableManager(db).get_variables(session_id)
        from gobby.workflows.task_claim_state import effective_tool_cwd, resolve_edit_pair

        for path in paths:
            if not isinstance(path, str):
                continue
            resolved = resolve_edit_pair(
                path,
                effective_tool_cwd(event.cwd, data),
                project_id=event.project_id,
                db=db,
                variables=variables,
            )
            if resolved is not None:
                root, relative_path = resolved
                task_id = checkout_task_at_start(
                    variables, os.fspath(root), event.timestamp.timestamp()
                )
                assert_task_edit_paths_available(
                    variables, task_id, [relative_path], os.fspath(root)
                )

    def _snapshot_shell_writes(self, event: HookEvent, session_id: str) -> None:
        """Remember the claimed-task ledger paths this Bash call may rewrite, before it runs."""
        db = getattr(self._session_manager, "db", None)
        root = _call_worktree_root(event)
        if db is None or root is None:
            return
        call_id = tool_execution_id(event)
        if call_id is None:
            self.logger.debug("Bash call has no tool-call id; claimed task edits not snapshotted")
            return
        try:
            SessionVariableManager(db).snapshot_claimed_shell_edits(
                session_id, os.fspath(root), call_id
            )
        except Exception:
            self.logger.warning("Failed to snapshot claimed task edits", exc_info=True)

    def _adopt_shell_writes(
        self, event: HookEvent, session_id: str, *, job_tree: dict[str, Any] | None = None
    ) -> None:
        """Let the claimed tasks own the ledger paths this Bash call rewrote."""
        if event.data.get("_verification_pending") is True:
            return
        db = getattr(self._session_manager, "db", None)
        call_id = tool_execution_id(event)
        if db is None or call_id is None:
            return
        try:
            receipt = background_receipt(event.data)
            SessionVariableManager(db).adopt_claimed_shell_edits(
                session_id,
                call_id,
                output_path=receipt.output_path if receipt is not None else None,
                job_id=receipt.job_id if receipt is not None else None,
                job_tree=job_tree,
            )
        except Exception:
            self.logger.warning("Failed to adopt shell writes to claimed tasks", exc_info=True)

    def _receipt_job_tree(self, event: HookEvent, session_id: str) -> dict[str, Any] | None:
        """Record the jobs a backgrounded Bash call started, while their processes still run.

        A graceful owner end stops only the jobs recorded here, so a receipt that records none
        leaves its call foreign.
        """
        receipt = background_receipt(event.data)
        if receipt is None or self._session_manager is None:
            return None
        try:
            session = self._session_manager.get(session_id)
            tree = capture_job_tree(
                session.terminal_context if session is not None else None, receipt.output_path
            )
        except Exception:
            self.logger.warning("Failed to record the jobs of a background call", exc_info=True)
            return None
        return tree.record() if tree is not None else None

    def _begin_worktree_shell_call(
        self, event: HookEvent, session_id: str, project_id: str | None
    ) -> None:
        """Register a Bash call run in a linked worktree, so that overlapping calls see each other.

        A failure registers nothing, so the changed paths stay foreign rather than block the call.
        """
        try:
            db = getattr(self._session_manager, "db", None)
            root = _call_worktree_root(event)
            call_id = tool_execution_id(event)
            if db is None or root is None or call_id is None:
                return
            if not is_registered_worktree(db, project_id, root):
                return
            before = run_coro_blocking(
                worktree_digests(root), self._event_loop, self.logger, label="worktree_digests"
            )
            if before is None:
                return
            manager = SessionVariableManager(db)
            attributable = project_id is not None and sanctioned_worktree_root(
                db, manager.get_variables(session_id), root, project_id
            )
            manager.begin_worktree_shell_call(
                session_id,
                root,
                call_id,
                before=before,
                attributable=attributable,
                now=time.time(),
                task_started_at=TaskToolBindings(manager, session_id).started_at(event),
                read_only=event.data.get("canonical_read_only") is True,
            )
        except Exception:
            self.logger.warning("Failed to register worktree shell call", exc_info=True)

    def _record_worktree_shell_writes(
        self, event: HookEvent, session_id: str, *, job_tree: dict[str, Any] | None = None
    ) -> None:
        """Attribute what a settled Bash call changed in its linked worktree to the caller.

        A call whose process still runs defers instead: its owner settles it on the next tool
        call, once the output file shows the process ended.
        """
        try:
            db = getattr(self._session_manager, "db", None)
            call_id = tool_execution_id(event)
            if db is None or call_id is None:
                return
            manager = SessionVariableManager(db)
            root = manager.worktree_shell_call_root(session_id, call_id)
            if root is None:
                return
            receipt = background_receipt(event.data)
            if receipt is not None or event.data.get("_verification_pending") is True:
                manager.defer_worktree_shell_call(
                    session_id,
                    call_id,
                    output_path=receipt.output_path if receipt is not None else None,
                    job_id=receipt.job_id if receipt is not None else None,
                    job_tree=job_tree,
                )
                return
            after = run_coro_blocking(
                worktree_digests(root), self._event_loop, self.logger, label="worktree_digests"
            )
            settled = manager.settle_worktree_shell_call(
                session_id, root, call_id, after=after, now=time.time()
            )
            paths = attribute_settled_call(
                manager, session_id, root, settled, edited_at=event.timestamp.timestamp()
            )
            if paths:
                self._mark_session_had_edits_if_claimed(session_id)
        except Exception:
            self.logger.warning("Failed to attribute worktree shell writes", exc_info=True)

    def _release_worktree_edit_calls(self, event: HookEvent, session_id: str) -> None:
        """Retire this call's structured-edit entries once its write completed or was refused."""
        db = getattr(self._session_manager, "db", None)
        call_id = tool_execution_id(event)
        if db is None or call_id is None:
            return
        try:
            release_structured_edit_calls(SessionVariableManager(db), session_id, call_id)
        except Exception:
            self.logger.warning("Failed to release structured edit in flight", exc_info=True)

    def _record_successful_file_mutation(
        self,
        event: HookEvent,
        session_id: str,
        *,
        is_canonical_edit: bool,
    ) -> None:
        """Persist one successful mutation observation and all committable paths."""
        input_data = event.data
        file_paths = structured_edit_paths(input_data, is_canonical=is_canonical_edit)

        from gobby.workflows.task_claim_state import resolve_edit_pair

        db = getattr(self._session_manager, "db", None)
        variables = SessionVariableManager(db).get_variables(session_id) if db is not None else {}
        committable_paths: list[str] = []
        paths_by_checkout: dict[str, list[str]] = {}
        for file_path in file_paths:
            repo_edit = resolve_edit_pair(
                file_path,
                effective_tool_cwd(event.cwd, event.data),
                project_id=event.project_id,
                db=db,
                variables=variables,
            )
            if repo_edit is None:
                continue
            repo_root, repo_relative_path = repo_edit

            if repo_relative_path not in committable_paths:
                committable_paths.append(repo_relative_path)
                self._notify_code_index(Path(repo_root), repo_relative_path)
            checkout_paths = paths_by_checkout.setdefault(os.fspath(repo_root), [])
            if repo_relative_path not in checkout_paths:
                checkout_paths.append(repo_relative_path)

        structured_mutation = input_data.get("canonical_structured_mutation") is True
        if structured_mutation and not file_paths:
            self.logger.warning(
                "Successful structured mutation had no attributable file paths",
                extra={
                    "event": "file_mutation_attribution_unavailable",
                    "session_id": session_id,
                    "tool_name": input_data.get("tool_name"),
                    "mcp_server": input_data.get("mcp_server"),
                    "mcp_tool": input_data.get("mcp_tool"),
                },
            )
        if not committable_paths:
            return

        db = getattr(self._session_manager, "db", None)
        if db is not None:
            variable_manager = SessionVariableManager(db)
            started_at = TaskToolBindings(variable_manager, session_id).started_at(event)
            if started_at is None:
                self.logger.warning(
                    "Refusing edit attribution without a proven tool-start task binding; "
                    "select the task with claim_task and retry the edit with a call ID."
                )
            edited_at = event.timestamp.timestamp()
            variables = variable_manager.get_variables(session_id)
            # A native write names its paths, so an overlapping shell call's diff cannot make
            # them foreign: the author owns them, and its overlap record gives way.
            structured = not is_shell_tool(input_data.get("tool_name"))
            for checkout_root, paths in paths_by_checkout.items():
                task_id = checkout_task_at_start(variables, checkout_root, started_at)
                # Paths an overlapping shell call changed stay foreign; this call skips them.
                overlapped = overlapped_worktree_paths(variables, checkout_root)
                if structured and not overlapped.isdisjoint(paths):
                    variable_manager.clear_overlapped_paths(session_id, checkout_root, paths)
                    overlapped = overlapped.difference(paths)
                paths = [path for path in paths if path not in overlapped]
                if task_id is not None:
                    landed = self._paths_landed_before_edit(
                        variables, task_id, checkout_root, paths, edited_at
                    )
                    if landed:
                        self.logger.info(
                            "Skipping edit attribution for paths committed after the edit "
                            "event (replayed envelope)",
                            extra={
                                "event": "file_mutation_attribution_stale",
                                "session_id": session_id,
                                "task_id": task_id,
                                "checkout_root": checkout_root,
                                "paths": sorted(landed),
                            },
                        )
                        paths = [path for path in paths if path not in landed]
                if paths:
                    variable_manager.record_edited_files(
                        session_id,
                        paths,
                        checkout_root=checkout_root,
                        edited_at=edited_at,
                        started_at=started_at,
                        attribute_to_task=started_at is not None,
                    )

        self._mark_session_had_edits_if_claimed(session_id)

    def _paths_landed_before_edit(
        self,
        variables: dict[str, Any],
        task_id: str,
        checkout_root: str,
        paths: list[str],
        edited_at: float,
    ) -> set[str]:
        """Return unattributed paths whose last commit postdates the edit event.

        An edit hook observed after a later commit touching the path is a replayed
        outage-queued envelope: its content is already landed, so attributing it
        would re-open the task ledger for committed work. Paths the task already
        holds are left to ``record_edited_files``, which keeps the newest stamp.

        This runs on a hook worker thread, so the supervised Git query is driven
        through the daemon loop. A bridge failure yields ``None``; keeping the
        attribution is the safe direction, the same as a Git timeout.
        """
        attributed = task_edited_file_set_for_checkout(variables, task_id, checkout_root)
        candidates = {path for path in paths if path not in attributed}
        if not candidates:
            return set()
        landed = run_coro_blocking(
            paths_committed_after_async(candidates, checkout_root, edited_at),
            self._event_loop,
            self.logger,
            label="paths_committed_after",
        )
        return landed if isinstance(landed, set) else set()

    def _notify_code_index(self, repo_root: Path, repo_relative_path: str) -> None:
        if node_origin() or self._code_index_trigger is None:
            return
        try:
            from gobby.hooks.project_context import ProjectIdResolver

            root_path = os.fspath(repo_root)
            project_id = ProjectIdResolver(
                session_manager=self._session_manager, logger=self.logger
            ).resolve_for_index(root_path)
            if project_id:
                self._code_index_trigger.notify_file_changed(
                    file_path=repo_relative_path,
                    project_id=project_id,
                    root_path=root_path,
                )
        except Exception as exc:
            self.logger.debug("Failed to trigger code index update: %s", exc)

    def _mark_session_had_edits_if_claimed(self, session_id: str) -> None:
        mark_session_had_edits_if_claimed(
            self._task_manager, self._session_manager, session_id, self.logger
        )

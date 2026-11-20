"""Domain faults a family, observer or hardcoded case injects at a production seam.

A case's ``faults`` list holds one spec per fault: ``{"kind": <FAULT_KINDS key>, ...}``
plus filters. A call that fails a filter passes through unfaulted. Shared filters:

- ``in_stack: "module:qualname"``: the call is faulted only while that function runs in
  the current logical call. The function is wrapped at every binding site with a wrapper
  that adds its name to a ``ContextVar`` for the call's duration; tasks and
  ``asyncio.to_thread`` copy the context, so a call made in a new task still sees it.
- ``caller: "module:qualname"``: the immediate Python caller of the patched callable is
  that function (synchronous callers only).
- ``pass: i``: the fault applies to pass ``i`` only.

Each faulted call appends ``{"fault": kind, "index": i, <filters>, ..., "outcome":
"failed"}`` to the hit log; the ``fault_hit`` guard reads it.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from unittest import mock

import psycopg

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._models import TaskNotFoundError
from gobby.tasks.transcript_evidence_models import TranscriptEvidenceUnavailable
from gobby.utils import git as git_module
from gobby.utils.daemon_git import DaemonGitService, GitFailed
from tests.contracts import rules_paths

SHARED_FILTERS = frozenset({"in_stack", "caller", "pass"})
FAULT_MESSAGE = "synthetic fault"


def _transcript_unavailable() -> BaseException:
    return TranscriptEvidenceUnavailable(FAULT_MESSAGE, source="synthetic", attempted_paths=())


# The exceptions a ``raise_at`` fault may raise, by name; the loader refuses any other.
RAISES: dict[str, Callable[[], BaseException]] = {
    "RuntimeError": lambda: RuntimeError(FAULT_MESSAGE),
    "OSError": lambda: OSError(FAULT_MESSAGE),
    "OperationalError": lambda: psycopg.OperationalError(FAULT_MESSAGE),
    "TaskNotFoundError": lambda: TaskNotFoundError(FAULT_MESSAGE),
    "TranscriptEvidenceUnavailable": _transcript_unavailable,
}


@dataclass(frozen=True)
class FaultSpec:
    """The filter keys a fault kind accepts and the ones it requires."""

    filters: frozenset[str]
    required: frozenset[str] = frozenset()

    @property
    def keys(self) -> frozenset[str]:
        return self.filters | {"kind"}


FAULT_KINDS: dict[str, FaultSpec] = {
    # DaemonGitService._execute, which daemon_git.run and daemon_git.status reach.
    "git": FaultSpec(SHARED_FILTERS | {"argv_contains", "argv_subsequence"}),
    # gobby.utils.git.run_git_command at every binding site; returns None.
    "git_command": FaultSpec(SHARED_FILTERS),
    # The callable named by ``target`` raises ``raises`` (a RAISES name).
    "raise_at": FaultSpec(SHARED_FILTERS | {"target", "raises"}, frozenset({"target", "raises"})),
    # get_variables on the handler's own session-variable manager raises.
    "session_variables": FaultSpec(SHARED_FILTERS),
    # SessionManager.get raises.
    "session_read": FaultSpec(SHARED_FILTERS),
    # LocalTaskManager.get_task and resolve_task_reference raise TaskNotFoundError.
    "task_read": FaultSpec(SHARED_FILTERS),
    # PostgresHubDatabase.fetchall / fetchone raise OperationalError for matching SQL.
    "hub_query": FaultSpec(SHARED_FILTERS | {"sql_contains"}, frozenset({"sql_contains"})),
}

_IN_STACK: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "rules_fault_in_stack", default=frozenset()
)


def validate_faults(case_id: str, faults: Any) -> None:
    """Raise ``ValueError`` naming the case and the offending kind, filter or name."""
    if not isinstance(faults, list):
        raise ValueError(f"{case_id}: faults must be a list, got {faults!r}")
    for fault in faults:
        if not isinstance(fault, Mapping):
            raise ValueError(f"{case_id}: a faults entry is not a mapping: {fault!r}")
        kind = fault.get("kind")
        spec = FAULT_KINDS.get(str(kind))
        if spec is None:
            raise ValueError(f"{case_id}: unknown fault kind {kind!r}")
        unknown = sorted(set(fault) - spec.keys)
        if unknown:
            raise ValueError(f"{case_id}: fault {kind!r} has unknown filter key(s) {unknown}")
        missing = sorted(spec.required - set(fault))
        if missing:
            raise ValueError(f"{case_id}: fault {kind!r} needs {missing}")
        if kind == "raise_at" and fault.get("raises") not in RAISES:
            raise ValueError(
                f"{case_id}: raise_at raises {fault.get('raises')!r}, not one of {sorted(RAISES)}"
            )
        for key in ("in_stack", "caller", "target"):
            if key in fault:
                rules_paths.owner_and_attr(str(fault[key]))
        if "pass" in fault and (not isinstance(fault["pass"], int) or fault["pass"] < 0):
            raise ValueError(f"{case_id}: fault pass must be a pass index, got {fault['pass']!r}")


class FaultSession:
    """The hit log and the current pass index the fault filters read."""

    def __init__(self, specs: list[Mapping[str, Any]], log: list[dict[str, Any]]) -> None:
        self.specs = specs
        self.log = log
        self.pass_index = 0

    def admits(self, index: int, caller: Any) -> bool:
        spec = self.specs[index]
        if "pass" in spec and spec["pass"] != self.pass_index:
            return False
        if "in_stack" in spec and spec["in_stack"] not in _IN_STACK.get():
            return False
        if "caller" in spec:
            module, _, qualname = str(spec["caller"]).partition(":")
            if caller is None or (caller.f_globals.get("__name__"), caller.f_code.co_qualname) != (
                module,
                qualname,
            ):
                return False
        return True

    def hit(self, index: int, **matched: Any) -> None:
        spec = self.specs[index]
        entry: dict[str, Any] = {"fault": spec["kind"], "index": index}
        entry.update({key: value for key, value in spec.items() if key != "kind"})
        entry.update(matched)
        entry["outcome"] = "failed"
        self.log.append(entry)


def _caller_frame() -> Any:
    # Frame 0 is this helper, 1 the patched wrapper, 2 its caller.
    return sys._getframe(2)


def _as_bound_like(current: Any, wrapped: Any) -> Any:
    """Give ``wrapped`` the descriptor kind of ``current``, so a class binding stays bound."""
    if isinstance(current, staticmethod):
        return staticmethod(wrapped)
    if isinstance(current, classmethod):
        return classmethod(wrapped)
    return wrapped


def _in_stack_wrapper(name: str, current: Any) -> Any:
    original = current.__func__ if isinstance(current, staticmethod | classmethod) else current
    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def marked(*args: Any, **kwargs: Any) -> Any:
            token = _IN_STACK.set(_IN_STACK.get() | {name})
            try:
                return await original(*args, **kwargs)
            finally:
                _IN_STACK.reset(token)

        wrapped: Any = marked
    else:

        @functools.wraps(original)
        def sync_marked(*args: Any, **kwargs: Any) -> Any:
            token = _IN_STACK.set(_IN_STACK.get() | {name})
            try:
                return original(*args, **kwargs)
            finally:
                _IN_STACK.reset(token)

        wrapped = sync_marked
    return _as_bound_like(current, wrapped)


def _mark_in_stack(name: str, stack: contextlib.ExitStack) -> None:
    for site in sorted(rules_paths.binding_sites(name) | {name}):
        owner, attr = rules_paths.owner_and_attr(site)
        current = inspect.getattr_static(owner, attr)
        stack.enter_context(mock.patch.object(owner, attr, _in_stack_wrapper(name, current)))


def _argv_matches(spec: Mapping[str, Any], argv: tuple[str, ...]) -> bool:
    if "argv_contains" in spec and spec["argv_contains"] not in argv:
        return False
    run = spec.get("argv_subsequence")
    if run is not None:
        width = len(run)
        return any(list(argv[i : i + width]) == list(run) for i in range(len(argv) - width + 1))
    return True


def _patch_git(session: FaultSession, indexes: list[int], stack: contextlib.ExitStack) -> None:
    original = DaemonGitService._execute

    async def execute(owner: Any, argv: tuple[str, ...], **kwargs: Any) -> Any:
        for index in indexes:
            if _argv_matches(session.specs[index], argv) and session.admits(index, None):
                session.hit(index, argv=list(argv[1:]))
                return GitFailed("failed", argv, 128, "", "synthetic git fault")
        return await original(owner, argv, **kwargs)

    stack.enter_context(mock.patch.object(DaemonGitService, "_execute", execute))


def _patch_git_command(
    session: FaultSession, indexes: list[int], stack: contextlib.ExitStack
) -> None:
    original = git_module.run_git_command

    def run_git_command(command: list[str], *args: Any, **kwargs: Any) -> str | None:
        caller = _caller_frame()
        for index in indexes:
            if session.admits(index, caller):
                session.hit(index, argv=list(command[1:]))
                return None
        return original(command, *args, **kwargs)

    for site in sorted(rules_paths.binding_sites("gobby.utils.git:run_git_command")):
        owner, attr = rules_paths.owner_and_attr(site)
        stack.enter_context(mock.patch.object(owner, attr, run_git_command))


def _raising(
    session: FaultSession, indexes: list[int], current: Any, raises: Callable[[], BaseException]
) -> Any:
    original = current.__func__ if isinstance(current, staticmethod | classmethod) else current

    def pick() -> int | None:
        caller = _caller_frame_outer()
        for index in indexes:
            if session.admits(index, caller):
                return index
        return None

    if inspect.iscoroutinefunction(original):

        async def raiser(*args: Any, **kwargs: Any) -> Any:
            index = pick()
            if index is None:
                return await original(*args, **kwargs)
            session.hit(index)
            raise raises()

        wrapped: Any = raiser
    else:

        def sync_raiser(*args: Any, **kwargs: Any) -> Any:
            index = pick()
            if index is None:
                return original(*args, **kwargs)
            session.hit(index)
            raise raises()

        wrapped = sync_raiser
    return _as_bound_like(current, wrapped)


def _caller_frame_outer() -> Any:
    # Frame 0 is this helper, 1 ``pick``, 2 the raiser, 3 the raiser's caller.
    return sys._getframe(3)


def _patch_raise(
    session: FaultSession,
    owner: Any,
    attr: str,
    indexes: list[int],
    raises: Callable[[], BaseException],
    stack: contextlib.ExitStack,
) -> None:
    current = inspect.getattr_static(owner, attr)
    stack.enter_context(mock.patch.object(owner, attr, _raising(session, indexes, current, raises)))


def _patch_hub_query(
    session: FaultSession, indexes: list[int], stack: contextlib.ExitStack
) -> None:
    for attr in ("fetchall", "fetchone"):
        original = getattr(PostgresHubDatabase, attr)

        def query(
            owner: Any, sql: str, *args: Any, _original: Any = original, **kwargs: Any
        ) -> Any:
            for index in indexes:
                if str(session.specs[index]["sql_contains"]) in sql and session.admits(index, None):
                    session.hit(index)
                    raise psycopg.OperationalError(FAULT_MESSAGE)
            return _original(owner, sql, *args, **kwargs)

        stack.enter_context(mock.patch.object(PostgresHubDatabase, attr, query))


@contextlib.contextmanager
def faulted(
    specs: list[Mapping[str, Any]], log: list[dict[str, Any]], *, handler: Any = None
) -> Iterator[FaultSession]:
    """Install every fault of ``specs``; yield the session whose ``pass_index`` filters read."""
    session = FaultSession(list(specs), log)
    by_kind: dict[str, list[int]] = {}
    for index, spec in enumerate(session.specs):
        by_kind.setdefault(str(spec["kind"]), []).append(index)
    with contextlib.ExitStack() as stack:
        if "git" in by_kind:
            _patch_git(session, by_kind["git"], stack)
        if "git_command" in by_kind:
            _patch_git_command(session, by_kind["git_command"], stack)
        for index in by_kind.get("raise_at", []):
            owner, attr = rules_paths.owner_and_attr(str(session.specs[index]["target"]))
            raises = RAISES[str(session.specs[index]["raises"])]
            _patch_raise(session, owner, attr, [index], raises, stack)
        if "session_variables" in by_kind:
            manager = getattr(handler, "_session_var_manager", None)
            if manager is None:
                raise ValueError("a session_variables fault needs the handler's variable manager")
            current = manager.get_variables

            def failing(*args: Any, **kwargs: Any) -> Any:
                caller = sys._getframe(1)
                for index in by_kind["session_variables"]:
                    if session.admits(index, caller):
                        session.hit(index)
                        raise RuntimeError("synthetic session variables fault")
                return current(*args, **kwargs)

            stack.enter_context(mock.patch.object(manager, "get_variables", failing))
        if "session_read" in by_kind:
            _patch_raise(
                session,
                SessionManager,
                "get",
                by_kind["session_read"],
                RAISES["RuntimeError"],
                stack,
            )
        if "task_read" in by_kind:
            for attr in ("get_task", "resolve_task_reference"):
                _patch_raise(
                    session,
                    LocalTaskManager,
                    attr,
                    by_kind["task_read"],
                    RAISES["TaskNotFoundError"],
                    stack,
                )
        if "hub_query" in by_kind:
            _patch_hub_query(session, by_kind["hub_query"], stack)
        # The in-stack markers wrap last, so a marker surrounds every patched binding.
        for name in sorted({str(spec["in_stack"]) for spec in session.specs if "in_stack" in spec}):
            _mark_in_stack(name, stack)
        yield session

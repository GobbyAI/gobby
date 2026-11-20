"""The production paths the family, observer, transition and hardcoded cases name.

Each id is ``<kind>.<name>`` or ``<kind>.<name>.<sub>``, ``<kind>`` one of family, observer,
transition and hardcoded. A ``CallablePath`` is a production function with one stub, the
neutral constant a case must change under; ``targets`` are every binding site the stub
patches together. A ``BranchPath`` is an inline production branch a case pins by a twin or
by two of its own passes; ``fault=True`` marks a production fault branch, which a case
proves only by a fault, its hit, the degraded output and its unfaulted twin.

The registry names production functions only. Every rule, agent and step the cases define
is synthetic.
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import inspect
import pkgutil
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any
from unittest import mock

from gobby.workflows.code_review_scope import CommitReviewScope
from gobby.workflows.engine.block_batching import BLOCK_COUNT_KEY

# The packages whose modules can bind a production function the handler or engine calls.
# Every module below them is imported before a scan, so the scan does not depend on what an
# earlier test happened to import.
BINDING_PACKAGES = (
    "gobby.adapters",
    "gobby.agents",
    "gobby.hooks",
    "gobby.mcp_proxy",
    "gobby.sessions",
    "gobby.storage",
    "gobby.tasks",
    "gobby.utils",
    "gobby.workflows",
)

_ACTIVE_STUBS: list[str] = []


def _split(ref: str) -> tuple[str, list[str]]:
    module_name, _, qualname = ref.partition(":")
    if not module_name or not qualname:
        raise ValueError(f"{ref!r} is not 'module:qualname'")
    return module_name, qualname.split(".")


def owner_and_attr(ref: str) -> tuple[Any, str]:
    """Return the module or class that holds ``ref``'s last name, and that name."""
    module_name, parts = _split(ref)
    owner: Any = importlib.import_module(module_name)
    for part in parts[:-1]:
        owner = getattr(owner, part)
    return owner, parts[-1]


def _raw(ref: str) -> Any:
    owner, attr = owner_and_attr(ref)
    return inspect.getattr_static(owner, attr)


def _function(raw: Any) -> Any:
    return raw.__func__ if isinstance(raw, staticmethod | classmethod) else raw


def resolve(ref: str) -> Any:
    """Return the function ``ref`` names, unwrapped from a staticmethod."""
    return _function(_raw(ref))


@dataclass(frozen=True)
class CallablePath:
    targets: tuple[str, ...]
    stub: Callable[..., Any]
    canonical: str

    def stub_for_target(self) -> Any:
        """The stub as patched on a class attribute or module global.

        Wrapped in ``async def`` when the canonical is a coroutine function and used as is
        otherwise, so the kind always follows the canonical. Wrapped in ``staticmethod``
        when the canonical is a class attribute defined as one, so a call through ``self``
        does not bind it.
        """
        stub = self.stub
        if inspect.iscoroutinefunction(resolve(self.canonical)):

            async def replacement(*args: Any, **kwargs: Any) -> Any:
                return stub(*args, **kwargs)

            function: Any = replacement
        else:
            function = stub
        if isinstance(_raw(self.canonical), staticmethod):
            return staticmethod(function)
        return function


@dataclass(frozen=True)
class BranchPath:
    description: str
    fault: bool = False


RegistryPath = CallablePath | BranchPath


def _empty(*args: Any, **kwargs: Any) -> str:
    return ""


def _none(*args: Any, **kwargs: Any) -> None:
    return None


def _false(*args: Any, **kwargs: Any) -> bool:
    return False


def _true(*args: Any, **kwargs: Any) -> bool:
    return True


def _empty_list(*args: Any, **kwargs: Any) -> list[Any]:
    return []


def _empty_set(*args: Any, **kwargs: Any) -> set[Any]:
    return set()


def _empty_tuple(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
    return ()


def _zero(*args: Any, **kwargs: Any) -> int:
    return 0


def _not_reviewable(*args: Any, **kwargs: Any) -> CommitReviewScope:
    return CommitReviewScope(has_reviewable_paths=False, session_owned_reviewable_paths=())


def _count_unchanged(scope: dict[str, Any], **kwargs: Any) -> int:
    return int(scope.get(BLOCK_COUNT_KEY, 0) or 0)


def _response_unchanged(owner: Any, response: Any, *args: Any, **kwargs: Any) -> Any:
    return response


def _instance_unchanged(db: Any, instance: Any, *args: Any, **kwargs: Any) -> Any:
    return instance


def _failed_proxy_call(proxy: Any, tool: Any, args: Any) -> dict[str, Any]:
    return {"success": False, "error": "stubbed"}


def _notice_unchanged(event: Any, variables: Any, notice: Any) -> Any:
    return notice


_W = "gobby.workflows"
_E = "gobby.workflows.engine"
_COMMIT_GUARD = f"{_W}.commit_guard"
_REVIEW_SCOPE = f"{_W}.code_review_scope:inspect_commit_review_scope"
_PLAN_MODE = f"{_W}.observer_plan_mode"
_COMPLETION = f"{_E}.enforcement_completion:EnforcementCompletionMixin"
_CHECKS = f"{_E}.enforcement_checks:EnforcementCheckMixin"
_EVALUATION = f"{_E}.evaluation:EvaluationMixin"
_DISPATCH_MCP = "gobby.hooks.dispatchers.mcp"


def _callable(canonical: str, stub: Callable[..., Any], *extra: str) -> CallablePath:
    return CallablePath(targets=(canonical, *extra), stub=stub, canonical=canonical)


def _fault(description: str) -> BranchPath:
    return BranchPath(description=description, fault=True)


PATHS: dict[str, RegistryPath] = {
    # --- families ---------------------------------------------------------------------
    "family.commit_guard": _callable(f"{_COMMIT_GUARD}:foreign_staged_commit_conflict", _empty),
    "family.review_scope": _callable(_REVIEW_SCOPE, _not_reviewable),
    "family.ledger_reconcile": _callable(
        f"{_W}.ledger_reconcile:reconcile_edit_ledgers", _empty_list
    ),
    "family.ledger_reconcile.status_unavailable": _fault(
        "reconcile_edit_ledgers releases nothing when git status is unavailable"
        " (ledger_reconcile.py, the dirty-is-None return)"
    ),
    "family.dirty_state": _callable(f"{_W}.ledger_reconcile:session_dirty_file_set", _empty_set),
    "family.dirty_state.close_skip": BranchPath(
        "the handler skips the ledger dirty state for a gobby-tasks close_task call"
        " (workflows/hooks.py, skip_ledger_dirty_state)"
    ),
    "family.dirty_state.variable_load_fail_open": _fault(
        "a session-variable load failure on a non-STOP event evaluates with empty variables"
        " (workflows/hooks.py, variable_load_failed)"
    ),
    "family.dirty_state.stop_refusal": _fault(
        "a session-variable load failure on STOP blocks with the variable-load-failure"
        " refusal before any rule runs (workflows/hooks.py)"
    ),
    "family.claim": _callable(f"{_W}.task_claim_state:claims_without_edit_target", _false),
    "family.claim.variable_load_fail_open": _fault(
        "a session-variable load failure on before_tool leaves the claim map empty and the"
        " edit allowed (workflows/hooks.py, variable_load_failed)"
    ),
    "family.binding": _callable(
        f"{_W}.task_tool_bindings:pending_fencing_calls",
        _empty_list,
        f"{_W}.condition_helpers_selection:pending_fencing_calls",
    ),
    "family.dirty_grant": _callable(
        f"{_W}.dirty_edit_grants:approved_dirty_paths",
        _empty_set,
        f"{_COMMIT_GUARD}:approved_dirty_paths",
    ),
    "family.dirty_grant.conflict": _callable(
        f"{_COMMIT_GUARD}:foreign_dirty_edit_conflict", _empty
    ),
    "family.edit_ledger": _callable(
        f"{_W}.session_edit_ledger:SessionEditLedger.release_session_dirty_files", _empty_list
    ),
    "family.found_work": _callable(
        f"{_W}.found_work_gate:FoundWorkStopAnalyzer.unclaimed_found_work", _empty_tuple
    ),
    "family.found_work.query_fail_open": _fault(
        "unclaimed_found_work returns no task when its hub query fails (found_work_gate.py)"
    ),
    "family.validation": _callable(
        f"{_W}.found_work_gate:unresolved_validation_failures", _empty_tuple
    ),
    "family.validation.wrapper": _callable(
        "gobby.tasks.transcript_outcomes:wrapped_validation_command",
        _none,
        f"{_W}.condition_helpers:_tasks_wrapped_validation_command",
    ),
    "family.validation.evidence_unavailable": _fault(
        "the terminal validation failures are empty when the latest-turn transcript evidence"
        " is unavailable (found_work_gate.py, _terminal_failures)"
    ),
    "family.monolith": _callable(f"{_W}.monolith_guard:projected_monolith_paths", _empty_list),
    "family.monolith.unreadable_file": _fault(
        "a file the monolith guard cannot read gets no projection and is not reported"
        " (monolith_guard.py, _projection_for_path)"
    ),
    "family.memory_review": _callable(
        f"{_W}.memory_review_conditions:pending_memory_reviews_complete",
        _false,
        f"{_W}.state_manager:pending_memory_reviews_complete",
    ),
    "family.memory_review.variable_load_fail_open": _fault(
        "a session-variable load failure evaluates the memory-review rule with empty"
        " variables (workflows/hooks.py, variable_load_failed)"
    ),
    "family.session_target": _callable(
        f"{_W}.condition_helpers_sessions:send_message_target_allowed", _true
    ),
    # --- observers --------------------------------------------------------------------
    "observer.plan_mode": _callable(f"{_PLAN_MODE}:resolve_plan_mode", _none),
    "observer.plan_mode.native": _callable(f"{_PLAN_MODE}:reconcile_native_mode", _none),
    "observer.plan_mode.load_failure": _fault(
        "the plan-mode observer sets nothing when its session read fails"
        " (observer_plan_mode.py, _load_session)"
    ),
    "observer.context_usage": _callable(
        f"{_W}.observer_context_usage:detect_context_compact_guidance", _none
    ),
    "observer.context_usage.read_failure": _fault(
        "the context-usage observer writes no band when its session read fails"
        " (observer_context_usage.py, _load_session)"
    ),
    "observer.mcp": _callable(
        f"{_W}.observer_mcp:detect_mcp_call", _none, f"{_W}.observers:detect_mcp_call"
    ),
    "observer.commits": _callable(
        f"{_W}.observer_commits:detect_bash_commit", _false, f"{_W}.observers:detect_bash_commit"
    ),
    "observer.turn_interrupt": _callable(
        "gobby.sessions.transcript_interrupt:turn_interrupt_initiated",
        _false,
        f"{_W}.turn_interrupt_observer:turn_interrupt_initiated",
    ),
    "observer.turn_interrupt.unreadable_transcript": _fault(
        "an unreadable transcript tail reads as no records, so the turn is not interrupted"
        " (transcript_interrupt.py, _tail_records)"
    ),
    # --- transitions ------------------------------------------------------------------
    "transition.claim": _callable(f"{_W}.observers:detect_task_claim", _none),
    "transition.review_gate": _callable(_REVIEW_SCOPE, _not_reviewable),
    "transition.planmode": _callable(f"{_PLAN_MODE}:reconcile_native_mode", _none),
    # --- hardcoded engine behaviour ---------------------------------------------------
    "hardcoded.enforcement_off": BranchPath(
        "rules.enforcement_enabled false lets a block through (engine/core.py)"
    ),
    "hardcoded.aggregate_off": BranchPath(
        "rules.aggregate_blocks false keeps only the first block reason (engine/core.py)"
    ),
    "hardcoded.survey_inject": _callable(
        f"{_W}.session_feedback_survey:inject_survey_active",
        _none,
        f"{_E}.core:inject_survey_active",
    ),
    "hardcoded.grok_post_compact": BranchPath(
        "a grok post_compact event takes the engine's compact branch (engine/core.py)"
    ),
    "hardcoded.compact_turn_bypass": _callable(
        f"{_E}.event_utils:_is_manual_compact_event", _false, f"{_E}.core:_is_manual_compact_event"
    ),
    "hardcoded.commit_review_default": BranchPath(
        "commit_has_reviewable_paths defaults to true when the context omits it (engine/core.py)"
    ),
    "hardcoded.blocked_tool_retry": _callable(
        f"{_E}.block_batching:register_blocked_attempt",
        _count_unchanged,
        f"{_E}.core:register_blocked_attempt",
    ),
    "hardcoded.after_tool_recovery": _callable(
        f"{_EVALUATION}._manage_after_tool_recovery_state", _none
    ),
    "hardcoded.after_tool_step": _callable(f"{_COMPLETION}._process_step_after_tool", _none),
    "hardcoded.batch_reset": _callable(
        f"{_E}.block_batching:close_response_batch",
        _none,
        f"{_E}.blocked_tool_recovery:close_response_batch",
        f"{_E}.core:close_response_batch",
    ),
    "hardcoded.auto_discover_servers": BranchPath(
        "the first turn start appends the server-listing call and sets servers_listed; the"
        " next does not (engine/core.py)"
    ),
    "hardcoded.auto_discover_proxy_self_call": BranchPath(
        "the engine emits the proxy self-call mcp_calls entry only while servers_listed is"
        " unset (engine/evaluation.py)"
    ),
    "hardcoded.auto_discover_proxy_self_call.dispatch": _callable(
        f"{_DISPATCH_MCP}:dispatch_mcp_calls",
        _empty_list,
        "gobby.hooks.dispatchers:dispatch_mcp_calls",
    ),
    "hardcoded.auto_discover_proxy_self_call.proxy_route": _callable(
        f"{_DISPATCH_MCP}:proxy_self_call",
        _failed_proxy_call,
        "gobby.hooks.dispatchers:proxy_self_call",
    ),
    "hardcoded.turn_end_attempts": _callable(
        "gobby.storage.coordination_waits:CoordinationWaitManager.count_active_waits", _zero
    ),
    "hardcoded.stop_gates": BranchPath(
        "a pending tool block holds the stop (engine/core.py, tool_block_pending)"
    ),
    "hardcoded.stop_gates.force_allow": BranchPath(
        "force_allow_stop lets the stop through unless a task is claimed (engine/core.py)"
    ),
    "hardcoded.stop_gates.circuit_breaker": BranchPath(
        "the edit-write circuit breaker opens after repeated stops (engine/core.py)"
    ),
    "hardcoded.late_mcp_injection": _callable(
        f"{_E}.mcp_injections:CachedMcpInjectionMixin._deliver_late_mcp_injections", _none
    ),
    "hardcoded.terminal_denial": _callable(f"{_CHECKS}._flush_pending_terminal_denial", _none),
    "hardcoded.block_response": _callable(
        f"{_EVALUATION}._finalize_block_response", _response_unchanged
    ),
    "hardcoded.agent_tool_enforcement": _callable(
        f"{_CHECKS}._check_agent_tool_enforcement", _none
    ),
    "hardcoded.lost_binding_rewind": _callable(
        f"{_W}.step_binding:release_lost_binding",
        _instance_unchanged,
        f"{_E}.enforcement_completion:release_lost_binding",
        f"{_W}.step_context:release_lost_binding",
    ),
    "hardcoded.step_transition_notice": _callable(
        f"{_COMPLETION}._route_step_transition_notice", _notice_unchanged
    ),
    "hardcoded.step_transition_notice.deferred": BranchPath(
        "a proxy-direct after_tool stores the transition notice and the next model-context"
        " pass delivers it (engine/enforcement_completion.py)"
    ),
    "hardcoded.no_rule": BranchPath(
        "with no rule a pending tool block still holds the stop (engine/core.py)"
    ),
}


@functools.cache
def _binding_modules() -> tuple[str, ...]:
    for package_name in BINDING_PACKAGES:
        package = importlib.import_module(package_name)
        for info in pkgutil.walk_packages(package.__path__, package_name + "."):
            importlib.import_module(info.name)
    return BINDING_PACKAGES


def _gobby_modules() -> list[tuple[str, ModuleType]]:
    return sorted(
        (name, module)
        for name, module in list(sys.modules.items())
        if (name == "gobby" or name.startswith("gobby.")) and isinstance(module, ModuleType)
    )


def binding_sites(canonical: str) -> set[str]:
    """Every ``module:attr`` or ``module:Class.attr`` under ``gobby`` that is the canonical.

    Every module of ``BINDING_PACKAGES`` is imported first. A class attribute is reported
    under the class's own module, once.
    """
    _binding_modules()
    function = resolve(canonical)
    sites: set[str] = set()
    for module_name, module in _gobby_modules():
        for attr, value in list(vars(module).items()):
            if value is function:
                sites.add(f"{module_name}:{attr}")
            elif isinstance(value, type) and value.__module__ == module_name:
                for name, member in list(vars(value).items()):
                    if _function(member) is function:
                        sites.add(f"{module_name}:{value.__qualname__}.{name}")
    return sites


def stub_active() -> bool:
    """True while a ``stubbed`` context is open; probes never run under a stub."""
    return bool(_ACTIVE_STUBS)


@contextlib.contextmanager
def stubbed(path: CallablePath) -> Iterator[None]:
    """Patch every target of ``path`` with its stub for the duration of the context."""
    replacement = path.stub_for_target()
    plain = _function(replacement)
    with contextlib.ExitStack() as stack:
        for target in path.targets:
            owner, attr = owner_and_attr(target)
            new = replacement if isinstance(owner, type) else plain
            stack.enter_context(mock.patch.object(owner, attr, new))
        _ACTIVE_STUBS.append(path.canonical)
        try:
            yield
        finally:
            _ACTIVE_STUBS.pop()


def _spy(path_id: str, current: Any, log: list[str]) -> Any:
    original = _function(current)
    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def spy(*args: Any, **kwargs: Any) -> Any:
            log.append(path_id)
            return await original(*args, **kwargs)

        wrapped: Any = spy
    else:

        @functools.wraps(original)
        def sync_spy(*args: Any, **kwargs: Any) -> Any:
            log.append(path_id)
            return original(*args, **kwargs)

        wrapped = sync_spy
    return staticmethod(wrapped) if isinstance(current, staticmethod) else wrapped


@contextlib.contextmanager
def probed(paths: Iterable[str]) -> Iterator[list[str]]:
    """Wrap each callable target with a pass-through spy that logs the path id per call.

    Yields the log. Refuses to run while a stub is active, so a stubbed replay never
    records a probe.
    """
    if stub_active():
        raise RuntimeError("probed() cannot run while a path is stubbed")
    log: list[str] = []
    with contextlib.ExitStack() as stack:
        for path_id in paths:
            path = PATHS[path_id]
            if not isinstance(path, CallablePath):
                continue
            for target in path.targets:
                owner, attr = owner_and_attr(target)
                current = inspect.getattr_static(owner, attr)
                stack.enter_context(mock.patch.object(owner, attr, _spy(path_id, current, log)))
        yield log

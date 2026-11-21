"""The pass loop of a rule-pass or handler-pass case, and the handler it drives.

An engine-entry pass is one ``RuleEngine.evaluate`` call, as the corpus has always run it.
A handler-entry pass runs the event through ``WorkflowHookHandler.evaluate_async`` over the
real managers on the case's isolated schema, with its evaluation timeout off and no
terminal manager, so a slow pass cannot become a timeout and terminal hosting stays out of
the context-pressure band.

Every pass records its response, the slices of the recorder's logs it appended, its
variable delta (the stored row of the case's own session for a handler pass), the probe
and fault hit logs, and for a handler pass the eval-context facts the handler built, the
observers that failed and the ``observe`` rows. Faults are installed before probes, so a
fault's in-stack marker scans production bindings, not spies. Record and plain replay check
the case's ``requires`` guards; a stubbed replay neither probes nor checks them.

This module imports ``rules_recorder`` lazily, and the recorder imports it lazily.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from gobby.hooks.events import HookEvent, HookResponse
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.contracts import rules_domain, rules_faults, rules_paths, rules_seed

SELF = rules_domain.SELF
# The entry key value of a case whose passes run through the hook handler on its domain.
HANDLER_ENTRY = "handler"
CANONICAL_KEYS = ("canonical_tool_kind", "mcp_server", "mcp_tool")
GUARD_NAMES = frozenset(
    {
        "fact",
        "fact_nonempty",
        "reached",
        "fault_hit",
        "variable",
        "canonical",
        "step",
        "peer_inactive",
    }
)
# The guards a seam caller's one pass can hold: it records reached paths and fault hits only.
CALLER_GUARD_NAMES = frozenset({"reached", "fault_hit"})
Scrub = Callable[[Any], Any]


@dataclass
class PassObservation:
    """What a pass left behind that the guards read; never recorded."""

    variables: dict[str, Any]
    facts: dict[str, Any] = field(default_factory=dict)
    canonical: dict[str, Any] = field(default_factory=dict)
    step: str | None = None
    peer_status: str | None = None


@dataclass
class CaseRun:
    response: HookResponse | None
    error: str | None
    passes: list[dict[str, Any]]
    observations: list[PassObservation]
    fault_log: list[dict[str, Any]]
    initial: dict[str, Any]
    final: dict[str, Any]
    scrub: Scrub


def build_handler(db: PostgresHubDatabase, *, engine: RuleEngine) -> WorkflowHookHandler:
    """The production handler over real managers, evaluation timeout off, no terminals."""
    return WorkflowHookHandler(
        timeout=0,
        enabled=True,
        rule_engine=engine,
        task_manager=LocalTaskManager(db),
        session_manager=SessionManager(db),
        session_task_manager=SessionTaskManager(db),
        config=None,
        terminal_manager=None,
    )


def probe_ids(case: Mapping[str, Any]) -> list[str]:
    """The callable path ids a case names under ``reaches``, ``control_for`` or ``seam_of``.

    A seam case's helper is probed too, so its ``reached`` guard can pin that the helper
    ran even though the case claims no mutation property for it.
    """
    seam = [case["seam_of"]] if isinstance(case.get("seam_of"), str) else []
    listed = [*case.get("reaches", []), *case.get("control_for", []), *seam]
    return [
        pid
        for pid in dict.fromkeys(listed)
        if isinstance(rules_paths.PATHS.get(pid), rules_paths.CallablePath)
    ]


@contextlib.contextmanager
def instrumented(
    case: Mapping[str, Any], *, handler: Any = None
) -> Iterator[tuple[rules_faults.FaultSession | None, list[str], bool]]:
    """Install the case's faults, then its probes unless a path is stubbed."""
    faults = case.get("faults")
    ids = probe_ids(case)
    probing = bool(ids) and not rules_paths.stub_active()
    with contextlib.ExitStack() as stack:
        session = None
        if faults:
            session = stack.enter_context(rules_faults.faulted(faults, [], handler=handler))
        log = stack.enter_context(rules_paths.probed(ids)) if probing else []
        yield session, log, probing


# --- requires guards --------------------------------------------------------------------


def _guard_entries(case: Mapping[str, Any]) -> list[tuple[str, Any]]:
    requires = case.get("requires")
    entries = [requires] if isinstance(requires, Mapping) else list(requires or [])
    return [(name, value) for entry in entries for name, value in dict(entry).items()]


def validate_requires(case_id: str, requires: Any, faults: Any) -> None:
    """Raise ``ValueError`` naming the case for a malformed guard list or unknown guard.

    A ``fault_hit`` guard is ``true`` or a mapping, and the case declares faults: the guard
    checks each case fault, so with none it would hold vacuously, and ``false`` would read
    as the same check as ``true``.
    """
    entries = [requires] if isinstance(requires, Mapping) else requires
    if not isinstance(entries, list) or not all(isinstance(e, Mapping) for e in entries):
        raise ValueError(f"{case_id}: requires must be a mapping or a list of mappings")
    for entry in entries:
        unknown = sorted(set(entry) - GUARD_NAMES)
        if unknown:
            raise ValueError(f"{case_id}: requires has unknown guard(s) {unknown}")
        if "fault_hit" not in entry:
            continue
        value = entry["fault_hit"]
        if value is not True and not isinstance(value, Mapping):
            raise ValueError(f"{case_id}: a fault_hit guard is true or a mapping, got {value!r}")
        if not faults:
            raise ValueError(f"{case_id}: a fault_hit guard needs the case faults it checks")


def check_requires(case: Mapping[str, Any], run: CaseRun) -> None:
    """Raise ``AssertionError`` naming the case and the guard that does not hold."""
    case_id = str(case.get("id"))
    count = len(run.passes)
    for name, value in _guard_entries(case):
        index = int(value["pass"]) if isinstance(value, Mapping) and "pass" in value else count - 1
        if not 0 <= index < count:
            raise AssertionError(f"{case_id}: guard {name} names pass {index} of {count}")
        problem = _guard_problem(name, value, run.passes[index], run.observations[index], case)
        if problem:
            raise AssertionError(f"{case_id}: guard {name} (pass {index}) does not hold: {problem}")


def check_caller_requires(
    case: Mapping[str, Any], reached: list[str], hits: list[dict[str, Any]]
) -> None:
    """Check a seam caller's guards on its one pass: the paths it reached and its fault hits.

    A seam caller runs no rule pass, so only the ``reached`` and ``fault_hit`` guards can
    hold on it; any other guard is refused, since its empty observation could hold vacuously.
    """
    case_id = str(case.get("id"))
    other = sorted({name for name, _value in _guard_entries(case)} - CALLER_GUARD_NAMES)
    if other:
        raise AssertionError(f"{case_id}: a seam caller cannot hold guard(s) {other}")
    entry = {"reached": list(reached), "faults": list(hits)}
    run = CaseRun(
        response=None,
        error=None,
        passes=[entry],
        observations=[PassObservation(variables={})],
        fault_log=list(hits),
        initial={},
        final={},
        scrub=lambda value: value,
    )
    check_requires(case, run)


def _guard_problem(
    name: str,
    value: Any,
    entry: Mapping[str, Any],
    seen: PassObservation,
    case: Mapping[str, Any],
) -> str | None:
    spec = value if isinstance(value, Mapping) else {}
    if name == "reached":
        wanted = spec.get("id") if spec else value
        logs = [*entry.get("reached", []), *entry.get("between_reached", [])]
        return None if wanted in logs else f"{wanted!r} not in {logs}"
    if name == "fault_hit":
        # The hits of the guard's pass only: the last pass, or the pass the guard names.
        pass_hits = entry.get("faults", [])
        for index, fault in enumerate(case.get("faults", [])):
            hits = [hit for hit in pass_hits if hit.get("index") == index]
            if not hits:
                return f"fault {index} ({fault.get('kind')}) was never hit"
            if "count" in spec and len(hits) != int(spec["count"]):
                return f"fault {index} was hit {len(hits)} times, not {spec['count']}"
        return None
    if name in ("fact", "fact_nonempty"):
        key = str(spec.get("key") if spec else value)
        if key not in seen.facts:
            return f"no fact {key!r} in {sorted(seen.facts)}"
        return _compare(seen.facts[key], spec, nonempty=name == "fact_nonempty")
    if name == "variable":
        return _compare(seen.variables.get(str(spec["name"])), spec)
    if name == "canonical":
        wanted = {key: spec[key] for key in CANONICAL_KEYS if key in spec}
        got = {key: seen.canonical.get(key) for key in wanted}
        return None if got == wanted else f"{got} != {wanted}"
    if name == "step":
        wanted = spec.get("equals") if spec else value
        return None if seen.step == wanted else f"{seen.step!r} != {wanted!r}"
    if name == "peer_inactive":
        return None if seen.peer_status not in (None, "active") else f"peer is {seen.peer_status}"
    return f"unknown guard {name!r}"


def _compare(got: Any, spec: Mapping[str, Any], *, nonempty: bool = False) -> str | None:
    if nonempty:
        return None if got else f"{got!r} is empty"
    if "equals" in spec and not _same(got, spec["equals"]):
        return f"{got!r} != {spec['equals']!r}"
    if "not_equals" in spec and _same(got, spec["not_equals"]):
        return f"{got!r} == {spec['not_equals']!r}"
    if "startswith" in spec and not str(got).startswith(str(spec["startswith"])):
        return f"{got!r} does not start with {spec['startswith']!r}"
    return None


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    return bool(left == right)


# --- the pass loop ----------------------------------------------------------------------


@dataclass
class _PassHooks:
    """What differs between the engine and the handler entry for one pass."""

    scrub: Scrub
    snapshot: Callable[[], dict[str, Any]]
    between: Callable[[Mapping[str, Any]], Awaitable[None]]
    evaluate: Callable[[Mapping[str, Any]], Awaitable[tuple[HookResponse, HookEvent | None]]]
    extras: Callable[[dict[str, Any], HookEvent | None], PassObservation]


async def _run_passes(
    case: Mapping[str, Any],
    recorder: Any,
    hooks: _PassHooks,
    *,
    session: rules_faults.FaultSession | None,
    probe_log: list[str],
    probing: bool,
) -> CaseRun:
    from tests.contracts import rules_recorder

    fault_log = session.log if session is not None else []
    specs = [case["event"], *case.get("sequence", [])]
    initial = hooks.snapshot()
    passes: list[dict[str, Any]] = []
    observations: list[PassObservation] = []
    response: HookResponse | None = None
    error: str | None = None
    for index, spec in enumerate(specs):
        if session is not None:
            session.pass_index = index
        mark = recorder.mark()
        probe_start, fault_start = len(probe_log), len(fault_log)
        before = hooks.snapshot()
        recorder.effect_log.append({"event": spec["type"]})
        for op in spec.get("between", []):
            await hooks.between(op)
        probe_mid = len(probe_log)
        response, event = None, None
        try:
            response, event = await hooks.evaluate(spec)
        except Exception as exc:
            error = type(exc).__name__
            recorder.effect_log.append({"raised": error})
        changed, deleted = rules_recorder.variable_delta(
            before, hooks.snapshot(), scrub=hooks.scrub
        )
        entry = rules_recorder.pass_entry(
            spec, response, error, recorder, mark, scrub=hooks.scrub, delta=changed, deleted=deleted
        )
        if probing:
            entry["reached"] = probe_log[probe_mid:]
            entry["between_reached"] = probe_log[probe_start:probe_mid]
        if session is not None:
            entry["faults"] = hooks.scrub(fault_log[fault_start:])
        observations.append(hooks.extras(entry, event))
        passes.append(entry)
        if error is not None:
            break
    return CaseRun(
        response=response,
        error=error,
        passes=passes,
        observations=observations,
        fault_log=fault_log,
        initial=initial,
        final=hooks.snapshot(),
        scrub=hooks.scrub,
    )


async def run_engine_case(
    db: PostgresHubDatabase,
    case: Mapping[str, Any],
    recorder: Any,
    *,
    engine: RuleEngine,
    deadline: Any,
) -> CaseRun:
    """Run each event of an engine-entry case through ``RuleEngine.evaluate``."""
    from tests.contracts import rules_recorder

    variables = copy.deepcopy(dict(case.get("variables", {})))
    eval_context = case.get("eval_context")

    async def between(op: Mapping[str, Any]) -> None:
        await rules_domain.apply_between(db, op, None, engine=engine)

    async def evaluate(spec: Mapping[str, Any]) -> tuple[HookResponse, HookEvent | None]:
        event = rules_recorder._event_from(spec)
        response = await engine.evaluate(
            event,
            rules_seed.SESSION_ID,
            variables,
            eval_context=copy.deepcopy(dict(eval_context)) if eval_context else None,
            blocking_deadline=deadline,
        )
        return response, event

    def extras(entry: dict[str, Any], event: HookEvent | None) -> PassObservation:
        return PassObservation(variables=rules_recorder._jsonable(variables))

    hooks = _PassHooks(
        scrub=rules_recorder._jsonable,
        snapshot=lambda: copy.deepcopy(variables),
        between=between,
        evaluate=evaluate,
        extras=extras,
    )
    with instrumented(case) as (session, probe_log, probing):
        return await _run_passes(
            case, recorder, hooks, session=session, probe_log=probe_log, probing=probing
        )


async def run_handler_case(
    db: PostgresHubDatabase,
    case: Mapping[str, Any],
    state: rules_domain.DomainState,
    recorder: Any,
    *,
    engine: RuleEngine,
) -> CaseRun:
    """Run each event of a handler-entry case through ``WorkflowHookHandler.evaluate_async``."""
    from tests.contracts import rules_recorder

    replace = rules_domain.scrubber(state)

    def scrub(value: Any) -> Any:
        return rules_domain.mask_clock(rules_recorder._jsonable(value, replace=replace))

    handler = build_handler(db, engine=engine)
    own = state.session()
    facts: list[dict[str, Any]] = []
    failures: list[list[str]] = []
    original_evaluate = engine.evaluate
    original_observers = handler._run_observers

    async def capture_facts(*args: Any, **kwargs: Any) -> HookResponse:
        context = kwargs.get("eval_context")
        facts.append(dict(context) if isinstance(context, Mapping) else {})
        return await original_evaluate(*args, **kwargs)

    def capture_failures(*args: Any, **kwargs: Any) -> set[str]:
        failed = original_observers(*args, **kwargs)
        failures.append(sorted(failed))
        return failed

    async def between(op: Mapping[str, Any]) -> None:
        await rules_domain.apply_between(db, op, state, engine=engine)

    async def evaluate(raw: Mapping[str, Any]) -> tuple[HookResponse, HookEvent | None]:
        facts.clear()
        failures.clear()
        spec = rules_domain.substitute({k: v for k, v in raw.items() if k != "between"}, state)
        key = str(spec.get("session", SELF))
        cwd = spec.get("cwd", str(state.repo) if state.repo is not None else None)
        event = rules_recorder._event_from(
            spec, session_id=state.session(key), project_id=state.project_id, cwd=cwd
        )
        event.session_id = state.externals[key]
        return await handler.evaluate_async(event), event

    def extras(entry: dict[str, Any], event: HookEvent | None) -> PassObservation:
        context = facts[-1] if facts else {}
        entry["facts"] = scrub({k: v for k, v in context.items() if k != "_blocking_deadline"})
        entry["observer_failures"] = failures[-1] if failures else []
        if case.get("observe"):
            entry["observe"] = scrub(rules_domain.observe_rows(db, case["observe"], state))
        instance = AgentStepInstanceManager(db).get_for_session(own)
        peer = state.sessions.get("peer")
        peer_row = SessionManager(db).get(peer) if peer is not None else None
        data = event.data if event is not None and isinstance(event.data, dict) else {}
        return PassObservation(
            variables=scrub(rules_domain.stored_variables(db, own)),
            facts=entry["facts"],
            canonical={key: data.get(key) for key in CANONICAL_KEYS},
            step=instance.current_step if instance is not None else None,
            peer_status=peer_row.status if peer_row is not None else None,
        )

    hooks = _PassHooks(
        scrub=scrub,
        snapshot=lambda: rules_domain.stored_variables(db, own),
        between=between,
        evaluate=evaluate,
        extras=extras,
    )
    with (
        mock.patch.object(engine, "evaluate", capture_facts),
        mock.patch.object(handler, "_run_observers", capture_failures),
        instrumented(case, handler=handler) as (session, probe_log, probing),
    ):
        return await _run_passes(
            case, recorder, hooks, session=session, probe_log=probe_log, probing=probing
        )

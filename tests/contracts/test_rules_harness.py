"""Recorder and loader contracts of the rules corpus harness itself.

Each test pins one property the recorder or the case loader must hold whatever the corpus
holds: a case runs through the entry it names, a handler case carries the domain it runs
on, a seam caller's guards are evaluated, a fault list is never empty, a ``differs_at``
path exists in at least one twin, nothing reaches the recorder's logs after the final pass,
and a fault-hit guard reads the last pass's hits unless it names a pass. Every case here is
synthetic and built in this file.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from tests.contracts import rules_corpus, rules_handler, rules_recorder
from tests.contracts import test_rules_paths as path_tests

pytestmark = pytest.mark.slow

ENGINE_ENTRY = "engine"
HANDLER_ENTRY = path_tests.HANDLER_ENTRY
# Keys only a handler-entry pass records (the eval-context facts and the failed observers).
HANDLER_ONLY_PASS_KEYS = ("facts", "observer_failures")
BASE_ID = "synthetic-harness-base"
DISPATCH_CASE_ID = path_tests.DISPATCH_CASE_ID
DISPATCH_PATH = path_tests.DISPATCH_PATH
# A hub-query fault on the seeded-session read the dispatch caller makes first. The case
# provides it and the read raises, so the caller stops after exactly one hit.
SESSION_READ_FAULT = {"kind": "hub_query", "sql_contains": "SELECT id FROM sessions WHERE id"}
LATE_ENTRY = {"synthetic_harness": "appended after the final pass"}


def _handler_case(case_id: str, **overrides: Any) -> dict[str, Any]:
    """A handler-entry case that carries the domain it runs on."""
    return path_tests._path_case(case_id, domain=path_tests._domain(), **overrides)


def _engine_case(case_id: str, **overrides: Any) -> dict[str, Any]:
    return path_tests._path_case(case_id, kind="rule_pass", entry=ENGINE_ENTRY, **overrides)


def _base() -> dict[str, Any]:
    return _handler_case(
        BASE_ID, reaches=["family.commit_guard"], requires={"reached": "family.commit_guard"}
    )


def _load_problem(root: Path, cases: list[dict[str, Any]]) -> str | None:
    """Load ``cases`` as a corpus; return the refusal text, or None when it loads."""
    try:
        rules_corpus.load_corpus(path_tests._write_path_corpus(root, cases))
    except ValueError as exc:
        return str(exc)
    return None


def _refusal_problems(
    root: Path, label: str, cases: list[dict[str, Any]], case_id: str, needle: str
) -> list[str]:
    message = _load_problem(root, cases)
    if message is None:
        return [f"{label}: the loader accepted it"]
    missing = [part for part in (case_id, needle) if part not in message]
    return [f"{label}: the refusal does not name {missing}: {message!r}"] if missing else []


def _accepted_problems(root: Path, label: str, cases: list[dict[str, Any]]) -> list[str]:
    message = _load_problem(root, cases)
    return [] if message is None else [f"{label}: the loader refused it: {message!r}"]


# --- a case runs through the entry it names ----------------------------------------------


def test_a_case_runs_through_the_entry_it_names(tmp_path: Path) -> None:
    problems: list[str] = []
    problems += _accepted_problems(
        tmp_path / "with-domain", "a handler case with a domain", [_base()]
    )
    problems += _refusal_problems(
        tmp_path / "no-domain",
        "a handler case without a domain",
        [_base(), path_tests._path_case("synthetic-harness-no-domain")],
        "synthetic-harness-no-domain",
        "domain",
    )
    domain = {"sessions": {rules_handler.SELF: {"source": "claude"}}}
    engine = _engine_case("synthetic-harness-engine-domain", domain=domain)
    passes = rules_corpus.replay_case(copy.deepcopy(engine))["passes"]
    handler_keys = sorted(
        {key for entry in passes for key in HANDLER_ONLY_PASS_KEYS if key in entry}
    )
    if handler_keys:
        problems.append(
            f"an engine-entry case with a domain ran through the hook handler: its passes"
            f" record {handler_keys}"
        )
    assert not problems, "\n".join(problems)


# --- a seam caller's requires guards are evaluated ---------------------------------------


def _dispatch_case(
    requires: list[dict[str, Any]], faults: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    cases = [case for case in path_tests._cases() if case["id"] == DISPATCH_CASE_ID]
    assert len(cases) == 1, f"one {DISPATCH_CASE_ID} case is required, found {len(cases)}"
    case = copy.deepcopy(cases[0])
    case["requires"] = requires
    if faults is not None:
        case["faults"] = faults
    return case


def _caller_refusal(
    requires: list[dict[str, Any]], faults: list[dict[str, Any]] | None = None
) -> str | None:
    try:
        rules_corpus.replay_case(_dispatch_case(requires, faults))
    except AssertionError as exc:
        return str(exc)
    return None


def test_a_seam_caller_evaluates_each_requires_guard() -> None:
    problems: list[str] = []
    refusal = _caller_refusal([{"reached": {"id": DISPATCH_PATH}}])
    if refusal is not None:
        problems.append(f"a reached guard written as a mapping holds but was refused: {refusal}")
    faults = [dict(SESSION_READ_FAULT)]
    refusal = _caller_refusal([{"fault_hit": {"count": 1}}], faults)
    if refusal is not None:
        problems.append(f"a fault_hit guard of one hit holds but was refused: {refusal}")
    refusal = _caller_refusal([{"fault_hit": {"count": 2}}], faults)
    if refusal is None or "fault_hit" not in refusal or "hit 1 times" not in refusal:
        problems.append(
            f"a fault_hit guard of two hits must fail on the one hit it saw, got: {refusal!r}"
        )
    # A caller records no peer, step, variable or fact, so such a guard could hold vacuously.
    refusal = _caller_refusal([{"peer_inactive": True}])
    if refusal is None or "peer_inactive" not in refusal:
        problems.append(f"a peer_inactive guard on a seam caller must be refused, got: {refusal!r}")
    assert not problems, "\n".join(problems)


# --- a fault list is never empty ---------------------------------------------------------


def test_the_loader_refuses_an_empty_fault_list(tmp_path: Path) -> None:
    seam = {
        "twin_of": BASE_ID,
        "differs_at": ["faults"],
        "seam_of": "family.ledger_reconcile",
        "reaches": ["family.ledger_reconcile.status_unavailable"],
        "requires": {"fault_hit": True, "variable": {"name": "synthetic_x", "equals": True}},
    }
    raised = {
        "twin_of": BASE_ID,
        "differs_at": ["faults"],
        "reaches": ["observer.turn_interrupt.unreadable_transcript"],
        "requires": {"fault_hit": True, "variable": {"name": "synthetic_x", "equals": False}},
    }
    problems: list[str] = []
    for case_id, label, keys, fault in (
        ("synthetic-harness-seam", "a seam_of case", seam, path_tests.GIT_FAULT),
        ("synthetic-harness-raise", "a fault branch case", raised, path_tests.OPEN_FAULT),
    ):
        root = tmp_path / case_id
        faulted = _handler_case(case_id, faults=[dict(fault)], **copy.deepcopy(keys))
        problems += _accepted_problems(
            root / "faulted", f"{label} with a fault", [_base(), faulted]
        )
        empty = _handler_case(case_id, faults=[], **copy.deepcopy(keys))
        problems += _refusal_problems(
            root / "empty", f"{label} with an empty fault list", [_base(), empty], case_id, "faults"
        )
    assert not problems, "\n".join(problems)


# --- a differs_at path exists in at least one twin ---------------------------------------


def _twin(metadata: Mapping[str, Any]) -> dict[str, Any]:
    event = path_tests._event_with("git status")
    event["metadata"] = dict(metadata)
    return _handler_case(
        "synthetic-harness-twin",
        event=event,
        twin_of=BASE_ID,
        differs_at=["event.metadata.synthetic_flag"],
        control_for=["family.commit_guard"],
        requires={"reached": "family.commit_guard"},
    )


def test_the_loader_refuses_a_differs_at_path_absent_from_both_twins(tmp_path: Path) -> None:
    # A twin may differ from its base by the presence of the key itself, so a path one
    # member carries is valid; only a path neither member carries is refused.
    flagged = _base()
    flagged["event"]["metadata"] = {"synthetic_flag": False}
    problems = _accepted_problems(
        tmp_path / "both", "a path in both twins", [flagged, _twin({"synthetic_flag": True})]
    )
    problems += _accepted_problems(
        tmp_path / "twin-only",
        "a path only the twin carries",
        [_base(), _twin({"synthetic_flag": True})],
    )
    problems += _accepted_problems(
        tmp_path / "base-only", "a path only the base carries", [flagged, _twin({})]
    )
    problems += _refusal_problems(
        tmp_path / "neither",
        "a path neither twin carries",
        [_base(), _twin({})],
        "synthetic-harness-twin",
        "differs_at",
    )
    assert not problems, "\n".join(problems)


# --- nothing reaches the recorder's logs after the final pass ----------------------------


def _appending_after_the_final_pass(log: str) -> Any:
    """``run_engine_case`` that appends one entry to the recorder's ``log`` once it returns."""
    original = rules_handler.run_engine_case

    async def late(
        db: Any, case: Mapping[str, Any], recorder: Any, **kwargs: Any
    ) -> rules_handler.CaseRun:
        run = await original(db, case, recorder, **kwargs)
        getattr(recorder, log).append(dict(LATE_ENTRY))
        return run

    return late


def test_the_recorder_refuses_a_log_entry_after_the_final_pass() -> None:
    problems: list[str] = []
    for log in ("effect_log", "audit_records"):
        case = _engine_case(f"synthetic-harness-late-{log.replace('_', '-')}")
        with mock.patch.object(
            rules_handler, "run_engine_case", _appending_after_the_final_pass(log)
        ):
            try:
                rules_recorder.replay_case(copy.deepcopy(case))
            except (RuntimeError, AssertionError) as exc:
                if case["id"] not in str(exc):
                    problems.append(f"{log}: the refusal does not name the case: {exc}")
                continue
        problems.append(f"{log}: an entry appended after the final pass was recorded")
    assert not problems, "\n".join(problems)


# --- a fault-hit guard reads the last pass's hits unless it names a pass -----------------


def _faulted_run(hits_per_pass: list[int]) -> rules_handler.CaseRun:
    passes: list[dict[str, Any]] = []
    fault_log: list[dict[str, Any]] = []
    for count in hits_per_pass:
        hits = [{"fault": "git", "index": 0, "outcome": "raised"} for _ in range(count)]
        passes.append({"outcome_class": "allow", "faults": hits})
        fault_log += copy.deepcopy(hits)
    return rules_handler.CaseRun(
        response=None,
        error=None,
        passes=passes,
        observations=[rules_handler.PassObservation(variables={}) for _ in passes],
        fault_log=fault_log,
        initial={},
        final={},
        scrub=lambda value: value,
    )


def _guard_refusal(requires: Mapping[str, Any], run: rules_handler.CaseRun) -> str | None:
    case = {"id": "synthetic-harness-fault-pass", "faults": [dict(path_tests.GIT_FAULT)]}
    try:
        rules_handler.check_requires({**case, "requires": dict(requires)}, run)
    except AssertionError as exc:
        return str(exc)
    return None


def test_a_fault_hit_guard_reads_the_last_pass_unless_it_names_one() -> None:
    problems: list[str] = []
    for hits, requires, holds in (
        ([1, 0], {"fault_hit": True}, False),
        ([1, 0], {"fault_hit": {"pass": 0}}, True),
        ([0, 1], {"fault_hit": True}, True),
        ([0, 1], {"fault_hit": {"pass": 0}}, False),
        ([1, 1], {"fault_hit": {"pass": 0, "count": 1}}, True),
    ):
        refusal = _guard_refusal(requires, _faulted_run(hits))
        if holds and refusal is not None:
            problems.append(f"hits {hits}: {requires} holds but was refused: {refusal}")
        if not holds and refusal is None:
            problems.append(f"hits {hits}: {requires} does not hold but was accepted")
    assert not problems, "\n".join(problems)

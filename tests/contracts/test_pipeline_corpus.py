"""Hook pipeline corpus contracts: manifest validation, required cases, replay and coverage.

The committed corpus is read through ``pipeline_corpus``. Negative coverage checks copy it to
``tmp_path`` and remove one required item. The required event types come from introspection
of the handler map and of ``HookEventType``; the collaborator kinds and scenario names are
constants in this file, so editing the committed JSON cannot relax them. Each case count floor
is asserted before any case is indexed or copied.

A case must change outcome when the production path it exercises is broken. The path is
derived here from the case's event type and scenario, never read from the case. The handler
map holds bound methods captured when ``EventHandlers`` is built, so the checks patch the
class attribute and rely on ``replay_case`` building a fresh ``HookManager`` on every call.
Every rule a case carries is a synthetic definition written in the case.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import shutil
from functools import cache
from pathlib import Path
from typing import Any, NoReturn

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType
from gobby.hooks.factory import HookManagerComponents
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.session_lookup import SessionLookupService
from tests.contracts import pipeline_corpus

CONTRACTS_DIR = Path(__file__).resolve().parent
CORPUS_DIR = CONTRACTS_DIR / "pipeline"
MIN_CASES = 8
CASE_KINDS = ("event", "collaborator_failure")
# Every collaborator the pipeline names, by the attribute it is injected through. Each name must
# be a real parameter or field of the handler, manager or component types (checked below), and
# each is one the handlers call while handling an event, so a failing one can change a response.
COLLABORATOR_KINDS = (
    "session_manager",
    "workflow_handler",
    "session_task_manager",
    "task_manager",
    "agent_run_manager",
    "worktree_manager",
    "memory_manager",
    "message_processor_resolver",
    "session_coordinator",
    "progress_tracker",
    "session_end_auto_link_worker",
    "skill_manager",
    "workflow_config_resolver",
    "get_machine_id",
    "resolve_project_id",
    "code_index_trigger",
    "attention_metadata_store",
    "terminal_manager",
    "terminal_runtime_registry",
    "webhook_dispatcher",
    "broadcaster",
)
# Injected into EventHandlers but read by no handler, so no failure of it can reach a response.
# The guard below fails once a handler reads it, and the kind then needs a failing case.
UNREAD_COLLABORATORS = {"call_tool": "_call_tool"}
HOOKS_SOURCE = CONTRACTS_DIR.parents[1] / "src" / "gobby" / "hooks"
REQUIRED_SCENARIOS = (
    "session_start_new",
    "session_start_resumed",
    "session_start_cleared",
    "session_start_compacted",
    "session_start_system_message",
    "node_session_missing_row",
    "deferred_activation",
    "blocking_webhook",
    "rule_block_short_circuit",
)
RULE_SHORT_CIRCUIT = "rule_block_short_circuit"
# The path each scenario names besides the event's handler. Breaking it must change the outcome.
SCENARIO_PATHS: dict[str, tuple[type, str]] = {
    RULE_SHORT_CIRCUIT: (HookManager, "_evaluate_workflow_rules"),
    "blocking_webhook": (HookManager, "_evaluate_blocking_webhooks"),
    "deferred_activation": (EventHandlers, "_activate_materialized_session"),
    "node_session_missing_row": (SessionLookupService, "resolve"),
}
# Scenarios decided before the event's handler runs: breaking the handler must not change them.
HANDLER_BYPASSED = frozenset({RULE_SHORT_CIRCUIT, "blocking_webhook", "node_session_missing_row"})
SYNTHETIC_RULE_PREFIX = "synthetic-"
# A collaborator failure is injected only through these case keys, never in the script, so
# removing them yields the healthy twin of the case.
FAILURE_KEY = "failure"
FAILURE_INJECTION_KEYS = ("failing_collaborator", FAILURE_KEY)
# Text no recorded pipeline output contains, so a stubbed path cannot pass as recorded output.
BROKEN_PATH = "corpus reach check: production path broken"


class _PathBrokenError(Exception):
    """Raised by a production path the reach checks have stubbed out."""


def _broken(*_args: object, **_kwargs: object) -> NoReturn:
    raise _PathBrokenError(BROKEN_PATH)


def _outcome(case: dict[str, Any]) -> object:
    try:
        return pipeline_corpus.replay_case(case)
    except _PathBrokenError:
        return BROKEN_PATH


@cache
def _handler_names() -> dict[str, str]:
    """Map each handler-map event to the ``EventHandlers`` method that handles it."""
    return {
        event.value: handler.__name__
        for event, handler in EventHandlers().get_handler_map().items()
    }


def _committed_cases() -> list[dict[str, Any]]:
    _manifest, cases = pipeline_corpus.load_corpus(CORPUS_DIR)
    return cases


def _collaborator_attributes() -> set[str]:
    handler_parameters = set(inspect.signature(EventHandlers.__init__).parameters)
    manager_parameters = set(inspect.signature(HookManager.__init__).parameters)
    component_fields = {field.name for field in dataclasses.fields(HookManagerComponents)}
    return handler_parameters | manager_parameters | component_fields


def _handler_map_events() -> set[str]:
    return set(_handler_names())


def _write_corpus(tmp_path: Path, manifest: dict[str, Any], cases: list[dict[str, Any]]) -> Path:
    corpus = tmp_path / "pipeline"
    (corpus / "cases").mkdir(parents=True)
    (corpus / "manifest.json").write_text(json.dumps(manifest))
    (corpus / "cases" / "synthetic.json").write_text(json.dumps({"cases": cases}))
    return corpus


def _synthetic_case(case_id: str, kind: str, collaborator: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "kind": kind,
        "provenance": "synthetic",
        "scenario": "session_start_new",
        "event_type": HookEventType.SESSION_START.value,
        "collaborator_script": [{"collaborator": collaborator, "method": "lookup", "result": None}],
        "expect": {"response": {}, "collaborator_calls": []},
    }


def _manifest_offender(offender: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Return ``(manifest, cases, needle)``: a corpus whose one defect names ``needle``."""
    manifest: dict[str, Any] = {
        "schema_version": pipeline_corpus.SCHEMA_VERSION,
        "collaborators": ["task_manager"],
        "case_kinds": list(CASE_KINDS),
    }
    if offender == "unknown_collaborator":
        case = _synthetic_case("unknown-collaborator", "event", "no_such_collaborator")
        return manifest, [case], "no_such_collaborator"
    if offender == "stale_schema_version":
        return {**manifest, "schema_version": 0}, [], "schema_version"
    case = _synthetic_case("unknown-kind", "no-such-kind", "task_manager")
    return manifest, [case], "no-such-kind"


def _carries_item(case: dict[str, Any], axis: str, item: str) -> bool:
    if axis == "handler_event":
        return case.get("event_type") == item
    if axis == "scenario":
        return case.get("scenario") == item
    return case.get("kind") == "collaborator_failure" and case.get("failing_collaborator") == item


def _drop_items(corpus: Path, axis: str, item: str) -> None:
    for path in sorted((corpus / "cases").glob("*.json")):
        document = json.loads(path.read_text())
        document["cases"] = [
            case for case in document["cases"] if not _carries_item(case, axis, item)
        ]
        path.write_text(json.dumps(document))


@pytest.mark.parametrize(
    "offender", ("unknown_collaborator", "stale_schema_version", "unknown_case_kind")
)
def test_pipeline_manifest_contract(tmp_path: Path, offender: str) -> None:
    manifest, cases, needle = _manifest_offender(offender)
    corpus = _write_corpus(tmp_path, manifest, cases)
    with pytest.raises(ValueError, match=re.escape(needle)):
        pipeline_corpus.load_corpus(corpus)


def test_pipeline_corpus_has_required_cases() -> None:
    manifest, cases = pipeline_corpus.load_corpus(CORPUS_DIR)
    assert len(cases) >= MIN_CASES, (
        f"pipeline corpus holds {len(cases)} cases; at least {MIN_CASES} required"
    )
    gaps: list[str] = []
    registry = set(manifest.get("collaborators", []))
    if registry != set(COLLABORATOR_KINDS):
        gaps.append(f"manifest collaborators differ: {sorted(registry ^ set(COLLABORATOR_KINDS))}")
    unresolved = sorted(set(COLLABORATOR_KINDS) - _collaborator_attributes())
    gaps += [f"collaborator on no real attribute: {name}" for name in unresolved]
    covered = {case.get("event_type") for case in cases}
    member_events = {member.value for member in HookEventType}
    gaps += [
        f"no case for handler-map event {event}"
        for event in sorted(_handler_map_events() - covered)
    ]
    gaps += [f"no case for event type {event}" for event in sorted(member_events - covered)]
    scenarios = {case.get("scenario") for case in cases}
    gaps += [f"no case for scenario {name}" for name in REQUIRED_SCENARIOS if name not in scenarios]
    failing = {case.get("failing_collaborator") for case in cases}
    gaps += [
        f"no failing-collaborator case for {name}"
        for name in COLLABORATOR_KINDS
        if name not in failing
    ]
    assert not gaps, f"pipeline corpus gaps: {gaps}"


def test_unread_collaborators_stay_unread() -> None:
    manifest, _cases = pipeline_corpus.load_corpus(CORPUS_DIR)
    assert set(manifest.get("unread_collaborators", {})) == set(UNREAD_COLLABORATORS)
    assert not set(UNREAD_COLLABORATORS) & set(COLLABORATOR_KINDS)
    sources = sorted(HOOKS_SOURCE.rglob("*.py"))
    assert sources, f"no hook sources under {HOOKS_SOURCE}"
    reads: list[str] = []
    for path in sources:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            for kind, attribute in UNREAD_COLLABORATORS.items():
                stripped = line.strip()
                stored = stripped == f"self.{attribute} = {kind}"
                declared = stripped.startswith(f"{attribute}:")
                if re.search(rf"\b{attribute}\b", line) and not (stored or declared):
                    reads.append(f"{path.relative_to(HOOKS_SOURCE)}:{number}")
    assert not reads, f"handlers now read an unread collaborator; give it a failing case: {reads}"


def test_rerecord_committed_corpus() -> None:
    """Re-record the committed expect blocks from the live pipeline when GOBBY_CORPUS_RECORD=1."""
    pipeline_corpus.record_corpus(CORPUS_DIR)
    cases = _committed_cases()
    assert len(cases) >= MIN_CASES, (
        f"pipeline corpus holds {len(cases)} cases after re-record; at least {MIN_CASES} required"
    )


def test_python_pipeline_replays_corpus() -> None:
    cases = _committed_cases()
    assert len(cases) >= MIN_CASES, (
        f"pipeline corpus holds {len(cases)} cases; at least {MIN_CASES} required"
    )
    mismatched = [
        case["id"] for case in cases if pipeline_corpus.replay_case(case) != case["expect"]
    ]
    assert not mismatched, f"pipeline cases that do not replay: {mismatched}"


def test_failing_collaborator_yields_recorded_response() -> None:
    failing = [case for case in _committed_cases() if case.get("kind") == "collaborator_failure"]
    assert len(failing) >= len(COLLABORATOR_KINDS), (
        f"{len(failing)} failing-collaborator cases; at least {len(COLLABORATOR_KINDS)} required"
    )
    mismatched = [
        case["id"]
        for case in failing
        if pipeline_corpus.replay_case(case).get("response") != case["expect"]["response"]
    ]
    assert not mismatched, f"failing-collaborator cases with other responses: {mismatched}"


def test_failing_collaborator_changes_the_healthy_outcome() -> None:
    failing = [case for case in _committed_cases() if case.get("kind") == "collaborator_failure"]
    assert len(failing) >= len(COLLABORATOR_KINDS), (
        f"{len(failing)} failing-collaborator cases; at least {len(COLLABORATOR_KINDS)} required"
    )
    scripted_failures = [
        case["id"]
        for case in failing
        if any(FAILURE_KEY in entry for entry in case.get("collaborator_script", []))
    ]
    assert not scripted_failures, (
        f"failing-collaborator cases that inject a failure in the script: {scripted_failures}"
    )
    unreached: list[str] = []
    for case in failing:
        healthy = {key: value for key, value in case.items() if key not in FAILURE_INJECTION_KEYS}
        if _outcome({**healthy, "kind": "event"}) == case["expect"]:
            unreached.append(f"{case['id']}: {case.get('failing_collaborator')}")
    assert not unreached, f"collaborator failures that never reach the pipeline: {unreached}"


def test_pipeline_cases_change_outcome_when_their_path_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [case for case in _committed_cases() if case.get("kind") == "event"]
    assert len(cases) >= MIN_CASES, (
        f"pipeline corpus holds {len(cases)} event cases; at least {MIN_CASES} required"
    )
    unreached: list[str] = []
    for case in cases:
        handler = _handler_names().get(str(case.get("event_type")))
        if handler is None:
            unreached.append(f"{case['id']}: event type has no handler")
            continue
        with monkeypatch.context() as patch:
            patch.setattr(EventHandlers, handler, _broken)
            handler_changed = _outcome(case) != case["expect"]
        scenario = str(case.get("scenario"))
        if scenario in HANDLER_BYPASSED and handler_changed:
            unreached.append(f"{case['id']}: {scenario} did not decide before {handler}")
        if scenario not in HANDLER_BYPASSED and not handler_changed:
            unreached.append(f"{case['id']}: unchanged with {handler} broken")
        if scenario not in SCENARIO_PATHS:
            continue
        owner, attribute = SCENARIO_PATHS[scenario]
        with monkeypatch.context() as patch:
            patch.setattr(owner, attribute, _broken)
            if _outcome(case) == case["expect"]:
                unreached.append(f"{case['id']}: unchanged with {attribute} broken")
    assert not unreached, f"pipeline cases that never reach their named path: {unreached}"


def test_pipeline_rules_are_synthetic_definitions() -> None:
    cases = _committed_cases()
    assert len(cases) >= MIN_CASES, (
        f"pipeline corpus holds {len(cases)} cases; at least {MIN_CASES} required"
    )
    short_circuit = [case for case in cases if case.get("scenario") == RULE_SHORT_CIRCUIT]
    assert short_circuit, f"no case for scenario {RULE_SHORT_CIRCUIT}"
    without_rule = [case["id"] for case in short_circuit if not case.get("rules")]
    assert not without_rule, f"rule short-circuit cases that define no rule: {without_rule}"
    not_synthetic = [
        f"{case['id']}: {rule.get('name')!r}"
        for case in cases
        for rule in case.get("rules", [])
        if not str(rule.get("name", "")).startswith(SYNTHETIC_RULE_PREFIX)
    ]
    assert not not_synthetic, f"pipeline rules that are not synthetic: {not_synthetic}"


def _first_item(axis: str) -> str:
    if axis == "handler_event":
        return sorted(_handler_map_events())[0]
    if axis == "scenario":
        return REQUIRED_SCENARIOS[0]
    return COLLABORATOR_KINDS[0]


# Every HookEventType member is a handler-map key today, so the event-type axis is covered by
# the handler-event axis.
@pytest.mark.parametrize("axis", ("handler_event", "scenario", "collaborator_kind"))
def test_pipeline_coverage_negative(tmp_path: Path, axis: str) -> None:
    cases = _committed_cases()
    assert len(cases) >= MIN_CASES, (
        f"pipeline corpus holds {len(cases)} cases; at least {MIN_CASES} required"
    )
    item = _first_item(axis)
    corpus = tmp_path / "pipeline"
    shutil.copytree(CORPUS_DIR, corpus)
    _drop_items(corpus, axis, item)
    with pytest.raises(ValueError, match=re.escape(item)):
        pipeline_corpus.check_coverage(corpus, axis)

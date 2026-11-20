"""Engine-path contracts for the rule-pass corpus.

A case that names a production engine path must change its recorded outcome when that path
is replaced by a constant or removed. ``ENGINE_PATH_MUTATIONS`` lists each path with the
cases that reach it and both outcomes, so editing case JSON cannot relax it. The other
tests pin how the skipped-group evidence, the progressive-discovery partners, the sort key,
the POSIX marker, the run_command budget cases, the deadline handoff and the seed loader
behave. Every definition is synthetic.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from gobby.hooks import effect_deadline
from gobby.hooks.events import HookEventType, HookResponse
from gobby.workflows.engine import run_command_effects
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.enforcement_checks import EnforcementCheckMixin
from gobby.workflows.engine.event_utils import _resolve_rule_events
from gobby.workflows.engine.run_command import execute_run_command
from gobby.workflows.engine.templating import TemplatingMixin
from gobby.workflows.evaluation_context import mutable_evaluation_result
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.contracts import (
    rules_corpus,
    rules_engine_seams,
    rules_mutation,
    rules_recorder,
    rules_seed,
)

CORPUS_DIR = Path(__file__).resolve().parent / "rules"
CALLER_KIND = rules_recorder.CALLER_KIND
RULE_PASS_KINDS = ("rule_pass", "handler_pass")
INLINE_CALLER = "inline_run_command"
BACKGROUND_CALLER = "background_delivery"
CLI_INTERNAL_IDS = ("S-CLI-INTERNAL-BEFORE", "S-CLI-INTERNAL-AFTER")
CLI_INTERNAL_KEY = "_cli_internal_request"
SKIPPING_EVENT_TYPES = ("before_tool", "after_tool")
ORDER_TRIGGER_ID = "S-ORDER-TRIGGER-INDEX"
ORDER_PRIORITY_ID = "S-ORDER-PRIORITY"
DEFAULT_PRIORITY = 100
MIN_BOUNDARY_CASES = 4
ABSENT = "absent"
PRESENT = "present"

Spec = dict[str, Any]
Target = tuple[object, str]
StubFactory = Callable[[Any], Any]


def _spec(decision: str, **fields: Any) -> Spec:
    return {"decision": decision, **fields}


# --- stubs: each replaces one production path with a working constant -----------------


def _returns_false(original: Any) -> Any:
    async def stub(self: Any, *args: Any, **kwargs: Any) -> bool:
        return False

    return stub


def _recheck_removed(original: Any) -> Any:
    async def stub(self: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("block_effects_only"):
            return []
        return await original(self, *args, **kwargs)

    return stub


def _recheck_applies_every_effect(original: Any) -> Any:
    async def stub(self: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("block_effects_only"):
            kwargs = {**kwargs, "block_effects_only": False}
        return await original(self, *args, **kwargs)

    return stub


def _returns_none(original: Any) -> Any:
    def stub(self: Any, *args: Any, **kwargs: Any) -> None:
        return None

    return stub


def _async_returns_none(original: Any) -> Any:
    async def stub(self: Any, *args: Any, **kwargs: Any) -> None:
        return None

    return stub


def _constant_step_block(original: Any) -> Any:
    def stub(self: Any, *args: Any, **kwargs: Any) -> HookResponse:
        return HookResponse(decision="block", reason="synthetic constant step block")

    return stub


def _returns_rules(original: Any) -> Any:
    def stub(self: Any, *args: Any, **kwargs: Any) -> Any:
        return args[0] if args else kwargs["rules"]

    return stub


def _resorted(key: Callable[[Any, list[Any]], Any]) -> StubFactory:
    def make(original: Any) -> Any:
        def stub(self: Any, *args: Any, **kwargs: Any) -> Any:
            loaded = original(self, *args, **kwargs)
            rule_events = list(args[0] if args else kwargs["rule_events"])
            return sorted(loaded, key=lambda item: key(item, rule_events))

        return stub

    return make


def _first_call_only(original: Any) -> Any:
    """Call the original on an event's first check; later checks of that event pass."""
    checked: dict[int, Any] = {}

    def stub(self: Any, *args: Any, **kwargs: Any) -> Any:
        event = args[0] if args else kwargs["event"]
        if id(event) in checked:
            return None
        # The event is kept so its id cannot be reused while the stub is installed.
        checked[id(event)] = event
        return original(self, *args, **kwargs)

    return stub


def _unresolved_step_policy(original: Any) -> Any:
    def stub(self: Any, session_id: str) -> Any:
        return self.get_for_session(session_id)

    return stub


def _always_matches(original: Any) -> Any:
    return staticmethod(lambda mcp_key, patterns: True)


def _every_handler_is_rtk(original: Any) -> Any:
    async def stub(self: Any, invocations: list[Any], event: Any, **kwargs: Any) -> Any:
        rerouted = [
            dataclasses.replace(
                invocation, effect=invocation.effect.model_copy(update={"handler": "rtk"})
            )
            for invocation in invocations
        ]
        return await original(self, rerouted, event, **kwargs)

    return stub


def _deadline_ignored(original: Any) -> Any:
    def stub(deadline: Any, *, maximum: float) -> float:
        return maximum

    return stub


def _zero_floor(original: Any) -> Any:
    return 0.0


def _deadline_dropped(original: Any) -> Any:
    def stub(
        self: Any, event: Any, variables: Any, extra_context: dict[str, Any] | None = None
    ) -> Any:
        kept = {k: v for k, v in (extra_context or {}).items() if k != "_blocking_deadline"}
        return original(self, event, variables, kept)

    return stub


# --- the cases each path reaches, with (expected, expected under the mutation) --------

RECHECK_BLOCK = "synthetic-f4-recheck-block"
RECHECK_PROXY_BLOCK = "synthetic-f4-recheck-proxy-block"
RECHECK_ONCE = "synthetic-f4-recheck-once"
PROXY_RTK = "synthetic-proxy-rtk"
PROXY_RTK_DENY = "synthetic-proxy-rtk-deny"
RECHECK_AGENT = "synthetic-recheck-agent-reserved-write"
UNKNOWN_HANDLER = "synthetic-effect-proxy-hook"
STEP_ALLOW = "synthetic-step-tool-allow"
STEP_BLOCK = "synthetic-step-tool-block"
STEP_MCP_BLOCK = "synthetic-step-mcp-tool-block"
STEP_RECHECK = "synthetic-step-recheck-handler-block"
AGENT_SCOPE = "synthetic-agent-scope"
ACTIVE_SELECTED = "synthetic-active-name-selected"
ACTIVE_BASELINE = "synthetic-active-baseline"
ORDER_PRIORITY = "synthetic-order-priority"
ORDER_ACROSS_TRIGGERS = "synthetic-order-priority-across-triggers"
ORDER_TRIGGER_INDEX = "synthetic-order-trigger-index"
INLINE_SUCCESS = "synthetic-run-command-inline-success"
FLOOR = "synthetic-deadline-run-command-floor"
OVERRUN = "synthetic-deadline-run-command-overrun"
WITHIN_BUDGET = "synthetic-deadline-run-command-within-budget"
REMAINING_BUDGET = "synthetic-deadline-run-command-remaining-budget"
DEADLINE_HANDOFF = "synthetic-deadline-handed-to-effects"

STEP_REASON = "step-enforcement:synthetic-step-agent/synthetic-gather"
UNRESOLVED_REASON = "unresolved-agent-definition"
DETECTOR_RESULT = "synthetic detector result"
SUCCESS_ROWS = [["effect", "success"]]
TIMEOUT_ROWS = [["effect", "timeout"]]

R1_BLOCK = _spec("block", reason_contains=("rewritten input denied",))
R2_BLOCK = _spec("block", reason_contains=("proxy rewrite denied",))
R3_BLOCK = _spec(
    "block",
    reason_contains=("rewritten input denied after one observation",),
    effect_counts={"observe": 1, "rewrite_input": 1},
)
R4_ALLOW = _spec("allow", modified_input={"command": "rtk echo rewritten"})
R5_BLOCK = _spec("block", reason_contains=("synthetic rtk denial",))
R6_BLOCK = _spec(
    "block",
    reason_contains=("Variable 'enforce_synthetic_flag' is managed by the workflow runtime.",),
)
S1_ALLOW = _spec("allow", eager_delta_has="synthetic_step_seen")
S1_UNENFORCED = _spec("allow", eager_delta_lacks="synthetic_step_seen")
S2_BLOCK = _spec(
    "block",
    reason_contains=(STEP_REASON, "Tool 'Bash' is not allowed in the 'synthetic-gather' step."),
)
S3_BLOCK = _spec(
    "block",
    reason_contains=(
        STEP_REASON,
        "MCP tool 'synthetic-server:synthetic-forbidden-tool' is not allowed in the "
        "'synthetic-gather' step.",
    ),
)
S4_BLOCK = _spec(
    "block", reason_contains=(STEP_REASON, "synthetic step handler denied the rewritten call")
)
F1_ALLOW = _spec(
    "allow", context_contains=("in scope",), context_lacks=("out of scope", "excluded by name")
)
F3_ALLOW = _spec(
    "allow",
    context_contains=("explicit", "mandatory", "selected"),
    context_lacks=("excluded", "baseline only"),
)
F4_ALLOW = _spec("allow", context_contains=("default rule",), context_lacks=("untagged rule",))
UNRESOLVED_BLOCK = _spec("block", reason_contains=(UNRESOLVED_REASON,))
O1_ALLOW = _spec("allow", context_order=("early", "late"))
O1_NAME_ORDER = _spec("allow", context_order=("late", "early"))
O2_ALLOW = _spec("allow", context_order=("before agent early", "turn start late"))
O2_TRIGGER_FIRST = _spec("allow", context_order=("turn start late", "before agent early"))
O3_ALLOW = _spec("allow", context_order=("beta", "alpha"))
O3_NAME_ORDER = _spec("allow", context_order=("alpha", "beta"))
E1_SUCCESS = _spec(
    "allow", context_contains=("synthetic detector saw: echo hi",), audit_rows=SUCCESS_ROWS
)
NOT_RUN = _spec("allow", context_absent=True, audit_rows=[], command_timeouts=[])
E1_NOT_RUN = _spec("allow", context_absent=True, audit_rows=[])


def _budget(timeouts: list[float], *, succeeded: bool) -> Spec:
    if succeeded:
        return _spec(
            "allow",
            context_contains=(DETECTOR_RESULT,),
            audit_rows=SUCCESS_ROWS,
            command_timeouts=timeouts,
        )
    return _spec("allow", context_absent=True, audit_rows=TIMEOUT_ROWS, command_timeouts=timeouts)


ENGINE_PATH_MUTATIONS: tuple[tuple[str, Target, StubFactory, dict[str, tuple[Spec, Spec]]], ...] = (
    (
        "proxy handlers removed",
        (RuleEngine, "_run_proxy_hooks"),
        _returns_false,
        {
            PROXY_RTK: (R4_ALLOW, _spec("allow", modified_input={"command": "echo rewritten"})),
            RECHECK_PROXY_BLOCK: (R2_BLOCK, _spec("allow", modified_input=ABSENT)),
            PROXY_RTK_DENY: (R5_BLOCK, _spec("allow", modified_input=ABSENT)),
        },
    ),
    (
        "rewrite recheck removed",
        (RuleEngine, "_run_rule_loop"),
        _recheck_removed,
        {
            RECHECK_BLOCK: (R1_BLOCK, _spec("allow", modified_input={"command": "echo rewritten"})),
            RECHECK_PROXY_BLOCK: (
                R2_BLOCK,
                _spec("allow", modified_input={"command": "rtk echo hi"}),
            ),
            RECHECK_ONCE: (
                R3_BLOCK,
                _spec(
                    "allow",
                    modified_input={"command": "echo rewritten"},
                    effect_counts={"observe": 1},
                ),
            ),
        },
    ),
    (
        "rewrite recheck applies every effect",
        (RuleEngine, "_run_rule_loop"),
        _recheck_applies_every_effect,
        {
            RECHECK_ONCE: (
                R3_BLOCK,
                _spec(
                    "block",
                    reason_contains=("rewritten input denied after one observation",),
                    effect_counts={"observe": 2, "rewrite_input": 2},
                ),
            ),
        },
    ),
    (
        "step enforcement removed",
        (RuleEngine, "_check_step_tool_enforcement"),
        _returns_none,
        {
            STEP_ALLOW: (S1_ALLOW, S1_UNENFORCED),
            STEP_BLOCK: (S2_BLOCK, _spec("allow")),
            STEP_MCP_BLOCK: (S3_BLOCK, _spec("allow")),
        },
    ),
    (
        "step enforcement constant block",
        (RuleEngine, "_check_step_tool_enforcement"),
        _constant_step_block,
        {
            STEP_ALLOW: (
                S1_ALLOW,
                _spec("block", reason_contains=("synthetic constant step block",)),
            )
        },
    ),
    (
        "agent scope filter removed",
        (RuleEngine, "_filter_by_agent_scope"),
        _returns_rules,
        {
            AGENT_SCOPE: (
                F1_ALLOW,
                _spec("allow", context_contains=("in scope", "out of scope", "excluded by name")),
            ),
        },
    ),
    (
        "active rule filter removed",
        (RuleEngine, "_filter_by_active_rules"),
        _returns_rules,
        {
            ACTIVE_SELECTED: (
                F3_ALLOW,
                _spec("allow", context_contains=("excluded", "baseline only")),
            ),
            ACTIVE_BASELINE: (F4_ALLOW, _spec("allow", context_contains=("untagged rule",))),
        },
    ),
    (
        "agent definition unresolved",
        (RuleEngine, "_load_active_agent_definition"),
        _returns_none,
        {
            ACTIVE_SELECTED: (F3_ALLOW, UNRESOLVED_BLOCK),
            AGENT_SCOPE: (F1_ALLOW, UNRESOLVED_BLOCK),
        },
    ),
    (
        "sort key without trigger index",
        (RuleEngine, "_load_rules"),
        _resorted(lambda item, events: (item[0].priority, item[0].name)),
        {ORDER_TRIGGER_INDEX: (O3_ALLOW, O3_NAME_ORDER)},
    ),
    (
        "sort key without priority",
        (RuleEngine, "_load_rules"),
        _resorted(lambda item, events: item[0].name),
        {
            ORDER_PRIORITY: (O1_ALLOW, O1_NAME_ORDER),
            ORDER_ACROSS_TRIGGERS: (O2_ALLOW, O2_TRIGGER_FIRST),
        },
    ),
    (
        "trigger first sort order",
        (RuleEngine, "_load_rules"),
        _resorted(
            lambda item, events: (events.index(item[1].event), item[0].priority, item[0].name)
        ),
        {ORDER_ACROSS_TRIGGERS: (O2_ALLOW, O2_TRIGGER_FIRST)},
    ),
    (
        "run command effect removed",
        (RuleEngine, "_apply_run_command"),
        _async_returns_none,
        {
            INLINE_SUCCESS: (E1_SUCCESS, E1_NOT_RUN),
            FLOOR: (_budget([1.0], succeeded=True), NOT_RUN),
            OVERRUN: (_budget([1.0], succeeded=False), NOT_RUN),
            WITHIN_BUDGET: (_budget([5.0], succeeded=True), NOT_RUN),
            REMAINING_BUDGET: (_budget([2.5], succeeded=True), NOT_RUN),
        },
    ),
    (
        "blocking deadline ignored",
        (run_command_effects, "remaining_blocking_effect_seconds"),
        _deadline_ignored,
        {
            OVERRUN: (_budget([1.0], succeeded=False), _budget([5.0], succeeded=True)),
            REMAINING_BUDGET: (_budget([2.5], succeeded=True), _budget([5.0], succeeded=True)),
        },
    ),
    (
        "inline floor removed",
        (run_command_effects, "_RUN_COMMAND_INLINE_FLOOR_SECONDS"),
        _zero_floor,
        {FLOOR: (_budget([1.0], succeeded=True), _budget([0.0], succeeded=False))},
    ),
    (
        "recheck agent check removed",
        (RuleEngine, "_check_agent_tool_enforcement"),
        _first_call_only,
        {RECHECK_AGENT: (R6_BLOCK, _spec("allow", modified_input=PRESENT))},
    ),
    (
        "installed step policy not resolved",
        (AgentStepInstanceManager, "get_active_for_session"),
        _unresolved_step_policy,
        {
            STEP_ALLOW: (S1_ALLOW, S1_UNENFORCED),
            STEP_BLOCK: (S2_BLOCK, _spec("allow")),
            STEP_MCP_BLOCK: (S3_BLOCK, _spec("allow")),
            STEP_RECHECK: (S4_BLOCK, _spec("allow")),
        },
    ),
    (
        "step mcp allow list always matches",
        (EnforcementCheckMixin, "_mcp_tool_matches"),
        _always_matches,
        {STEP_MCP_BLOCK: (S3_BLOCK, S1_UNENFORCED)},
    ),
    (
        "recheck step check removed",
        (RuleEngine, "_check_step_tool_enforcement"),
        _first_call_only,
        {STEP_RECHECK: (S4_BLOCK, _spec("allow", modified_input=PRESENT))},
    ),
    (
        "proxy handler name gate removed",
        (RuleEngine, "_run_proxy_hooks"),
        _every_handler_is_rtk,
        {
            UNKNOWN_HANDLER: (
                _spec("allow", modified_input=ABSENT),
                _spec("allow", modified_input={"command": "rtk echo hi"}),
            ),
        },
    ),
    (
        "deadline not handed to effects",
        (TemplatingMixin, "_build_eval_context"),
        _deadline_dropped,
        {
            DEADLINE_HANDOFF: (
                _spec("allow", log_contains={"run_command_deadline": "expired"}),
                _spec("allow", log_contains={"run_command_deadline": "none"}),
            ),
        },
    ),
)


# --- reading an outcome spec from an expect block --------------------------------------


def _committed_cases() -> list[dict[str, Any]]:
    _manifest, cases = rules_corpus.load_corpus(CORPUS_DIR)
    return cases


def _returned(expect: Mapping[str, Any]) -> Mapping[str, Any]:
    returned = expect.get("returned")
    return returned if isinstance(returned, Mapping) else {}


def _context_text(case: Mapping[str, Any], expect: Mapping[str, Any]) -> str:
    """A seam caller's context is its returned parts; a pass's is the response context."""
    if case.get("kind") == CALLER_KIND:
        parts = _returned(expect).get("context_parts") or []
        return "\n\n".join(str(part[1]) for part in parts if isinstance(part, list) and part)
    response = expect.get("response")
    context = response.get("context") if isinstance(response, Mapping) else None
    return str(context) if context else ""


def _decision(case: Mapping[str, Any], expect: Mapping[str, Any]) -> Any:
    if case.get("kind") == CALLER_KIND and expect.get("outcome_class") != "error":
        return "allow"
    return expect.get("outcome_class")


def _response(expect: Mapping[str, Any]) -> Mapping[str, Any]:
    response = expect.get("response")
    return response if isinstance(response, Mapping) else {}


def _effect_log(expect: Mapping[str, Any]) -> list[Any]:
    log = expect.get("effect_log")
    return log if isinstance(log, list) else []


def _spec_mismatches(spec: Spec, case: Mapping[str, Any], expect: Mapping[str, Any]) -> list[str]:
    """Return one line per spec field the expect block does not meet."""
    found: list[str] = []
    decision = _decision(case, expect)
    if decision != spec["decision"]:
        found.append(f"decision {decision!r}, want {spec['decision']!r}")
    reason = str(_response(expect).get("reason") or "")
    for text in spec.get("reason_contains", ()):
        if text not in reason:
            found.append(f"reason {reason!r} lacks {text!r}")
    context = _context_text(case, expect)
    for text in spec.get("context_contains", ()):
        if text not in context:
            found.append(f"context {context!r} lacks {text!r}")
    for text in spec.get("context_lacks", ()):
        if text in context:
            found.append(f"context {context!r} holds {text!r}")
    if spec.get("context_absent") and context:
        found.append(f"context {context!r} is not empty")
    order = spec.get("context_order")
    if order is not None:
        positions = [context.find(text) for text in order]
        if -1 in positions or positions != sorted(positions):
            found.append(f"context {context!r} is not in the order {list(order)}")
    if "modified_input" in spec:
        want = spec["modified_input"]
        have = _response(expect).get("modified_input", ABSENT)
        if want == PRESENT:
            if have == ABSENT or not have:
                found.append("modified_input is absent")
        elif have != want:
            found.append(f"modified_input {have!r}, want {want!r}")
    counts = spec.get("effect_counts")
    if counts is not None:
        log = _effect_log(expect)
        have_counts = {
            name: sum(
                1 for entry in log if isinstance(entry, Mapping) and entry.get("effect") == name
            )
            for name in counts
        }
        if have_counts != counts:
            found.append(f"effect counts {have_counts}, want {counts}")
    if "audit_rows" in spec:
        rows = _returned(expect).get("audit_rows")
        if rows != spec["audit_rows"]:
            found.append(f"audit_rows {rows!r}, want {spec['audit_rows']!r}")
    if "command_timeouts" in spec:
        timeouts = _returned(expect).get("command_timeouts")
        if timeouts != spec["command_timeouts"]:
            found.append(f"command_timeouts {timeouts!r}, want {spec['command_timeouts']!r}")
    delta = expect.get("eager_variable_delta")
    delta = delta if isinstance(delta, Mapping) else {}
    if spec.get("eager_delta_has") is not None and spec["eager_delta_has"] not in delta:
        found.append(f"variable delta {dict(delta)} lacks {spec['eager_delta_has']!r}")
    if spec.get("eager_delta_lacks") is not None and spec["eager_delta_lacks"] in delta:
        found.append(f"variable delta {dict(delta)} holds {spec['eager_delta_lacks']!r}")
    entry = spec.get("log_contains")
    if entry is not None and entry not in _effect_log(expect):
        found.append(f"effect_log lacks {entry!r}")
    return found


def _skipped_here(case: Mapping[str, Any]) -> bool:
    return sys.platform == "win32" and case.get("posix_only") is True


@pytest.mark.parametrize(
    ("target", "make_stub", "cases"),
    [pytest.param(*entry[1:], id=entry[0]) for entry in ENGINE_PATH_MUTATIONS],
)
def test_named_engine_path_mutation_changes_each_case(
    monkeypatch: pytest.MonkeyPatch,
    target: Target,
    make_stub: StubFactory,
    cases: dict[str, tuple[Spec, Spec]],
) -> None:
    owner, attr = target
    by_id = {case["id"]: case for case in _committed_cases()}
    missing = sorted(case_id for case_id in cases if case_id not in by_id)
    assert not missing, f"no committed case for {attr}: {missing}"
    for case_id, (expected, _mutated) in cases.items():
        case = by_id[case_id]
        committed = _spec_mismatches(expected, case, case["expect"])
        assert not committed, f"{case_id}: the committed expect does not meet its spec: {committed}"
    original = getattr(owner, attr)
    for case_id, (expected, expected_mutated) in cases.items():
        case = by_id[case_id]
        if _skipped_here(case):
            continue
        plain = rules_corpus.replay_case(copy.deepcopy(case))
        unmutated = _spec_mismatches(expected, case, plain)
        assert not unmutated, f"{case_id}: the unmutated replay does not meet its spec: {unmutated}"
        with monkeypatch.context() as patch:
            patch.setattr(owner, attr, make_stub(original))
            stubbed = rules_corpus.replay_case(copy.deepcopy(case))
        rules_mutation.assert_clean_stubbed_replay(stubbed, f"{case_id} with {attr} stubbed")
        mutated = _spec_mismatches(expected_mutated, case, stubbed)
        assert not mutated, f"{case_id}: with {attr} stubbed the replay misses its spec: {mutated}"
        assert stubbed != case["expect"], f"{case_id}: stubbing {attr} leaves the expect unchanged"


# --- skipped groups ---------------------------------------------------------------------


def _conditional_group_case() -> dict[str, Any]:
    return {
        "id": "synthetic-conditional-group",
        "kind": "rule_pass",
        "provenance": "synthetic",
        "scenario_ids": [],
        "family": None,
        "observer": None,
        "transition": None,
        "rules": [
            {
                "name": "synthetic-conditional-member",
                "event": "before_tool",
                "group": "synthetic-conditional-group",
                "when": "False",
                "effects": [{"type": "observe", "message": "never applied"}],
            },
            {
                "name": "synthetic-ungrouped-observer",
                "event": "before_tool",
                "effects": [{"type": "observe", "message": "ungrouped"}],
            },
        ],
        "event": {
            "type": "before_tool",
            "source": "claude",
            "data": {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
            "metadata": {},
        },
        "seam_failures": [],
    }


def test_skipped_groups_reports_only_groups_with_no_reached_member() -> None:
    one: list[Mapping[str, Any]] = [{"name": "synthetic-a", "group": "g"}]
    assert rules_recorder.skipped_groups(one, {"synthetic-a"}) == [], "a reached member's group"
    two_groups: list[Mapping[str, Any]] = [
        {"name": "synthetic-a", "group": "g"},
        {"name": "synthetic-b", "group": "h"},
    ]
    assert rules_recorder.skipped_groups(two_groups, {"synthetic-b"}) == ["g"]
    shared: list[Mapping[str, Any]] = [
        {"name": "synthetic-a", "group": "g"},
        {"name": "synthetic-b", "group": "g"},
    ]
    assert rules_recorder.skipped_groups(shared, {"synthetic-b"}) == [], "one reached member"
    expect = rules_corpus.replay_case(_conditional_group_case())
    assert "skipped_groups" not in expect, (
        f"a group whose member reached the engine and failed its condition is reported "
        f"skipped: {expect.get('skipped_groups')}"
    )


# --- progressive-discovery partners ------------------------------------------------------


def _expected_skips(case: Mapping[str, Any]) -> list[str]:
    """The groups the engine must skip, derived from the case input alone."""
    event = case["event"]
    label = case.get("engine_skip_group")
    internal = event.get("metadata", {}).get(CLI_INTERNAL_KEY) is True
    if not internal or event.get("type") not in SKIPPING_EVENT_TYPES:
        return []
    return sorted({rule["group"] for rule in case["rules"] if rule.get("group") == label})


def _audited_rules(case: Mapping[str, Any]) -> set[str]:
    return {record["rule"] for record in case["expect"].get("audit_records", [])}


def test_cli_internal_cases_have_absent_and_false_partners() -> None:
    cases = _committed_cases()
    mains = [case for case in cases if set(case["scenario_ids"]) & set(CLI_INTERNAL_IDS)]
    assert len(mains) >= len(CLI_INTERNAL_IDS), f"cli-internal cases: {[c['id'] for c in mains]}"
    for main in mains:
        partners = [case for case in cases if case.get("pairs_with") == main["id"]]
        forms = {
            "absent"
            if CLI_INTERNAL_KEY not in partner["event"].get("metadata", {})
            else repr(partner["event"]["metadata"][CLI_INTERNAL_KEY])
            for partner in partners
        }
        assert {"absent", "False"} <= forms, f"{main['id']}: partner metadata forms {forms}"
        for partner in partners:
            assert partner["rules"] == main["rules"], f"{partner['id']}: rules differ"
            assert partner["event"]["type"] == main["event"]["type"], partner["id"]
            assert "skipped_groups" not in partner["expect"], partner["id"]
            unaudited = {rule["name"] for rule in partner["rules"]} - _audited_rules(partner)
            assert not unaudited, f"{partner['id']}: rules without an audit record: {unaudited}"
        label = main.get("engine_skip_group")
        assert isinstance(label, str) and label, f"{main['id']}: no engine_skip_group"
        others = {rule["name"] for rule in main["rules"] if rule.get("group", label) != label}
        assert others, f"{main['id']}: no rule in another group"
        unaudited = others - _audited_rules(main)
        assert not unaudited, f"{main['id']}: other-group rules without an audit: {unaudited}"
    labelled = [case for case in cases if "engine_skip_group" in case]
    assert labelled, "no case carries engine_skip_group"
    wrong = [
        (case["id"], case["expect"].get("skipped_groups", []), _expected_skips(case))
        for case in labelled
        if case["expect"].get("skipped_groups", []) != _expected_skips(case)
    ]
    assert not wrong, f"recorded skipped groups differ from the input's: {wrong}"


# --- ordering -------------------------------------------------------------------------------


def _full_key(case: Mapping[str, Any], rule: Mapping[str, Any]) -> tuple[int, int, str]:
    event = case["event"]
    triggers = [
        trigger.value
        for trigger in _resolve_rule_events(
            HookEventType(event["type"]), source=event.get("source", "claude")
        )
    ]
    assert rule["event"] in triggers, f"{case['id']}: {rule['name']} is on no resolved trigger"
    return (
        int(rule.get("priority", DEFAULT_PRIORITY)),
        triggers.index(rule["event"]),
        rule["name"],
    )


def _applied_order(case: Mapping[str, Any]) -> list[str]:
    order: list[str] = []
    for entry in case["expect"]["effect_log"]:
        name = entry.get("rule") if "effect" in entry else None
        if name is not None and name not in order:
            order.append(name)
    return order


def test_ordering_cases_distinguish_each_sort_key() -> None:
    cases = _committed_cases()
    trigger_cases = [case for case in cases if ORDER_TRIGGER_ID in case["scenario_ids"]]
    priority_cases = [case for case in cases if ORDER_PRIORITY_ID in case["scenario_ids"]]
    assert trigger_cases and priority_cases, "an ordering scenario has no case"
    for case in [*trigger_cases, *priority_cases]:
        rules = case["rules"]
        full = [rule["name"] for rule in sorted(rules, key=lambda rule: _full_key(case, rule))]
        assert _applied_order(case) == full, f"{case['id']}: applied order differs from the key"
        by_name = sorted(rule["name"] for rule in rules)
        if case in trigger_cases:
            without_trigger = [
                rule["name"]
                for rule in sorted(
                    rules, key=lambda rule: (rule.get("priority", DEFAULT_PRIORITY), rule["name"])
                )
            ]
            assert full != without_trigger, f"{case['id']}: the trigger index decides nothing"
        if case in priority_cases:
            assert full != by_name, f"{case['id']}: name order equals the priority order"


# --- the POSIX marker -----------------------------------------------------------------------


def _has_run_command(case: Mapping[str, Any]) -> bool:
    return any(
        effect.get("type") == "run_command"
        for rule in case.get("rules", [])
        for effect in rule.get("effects", [])
    )


def _needs_posix_marker(case: Mapping[str, Any]) -> bool:
    """A case needs the marker when it can spawn a process or needs a POSIX path."""
    caller = case.get("caller") if case.get("kind") == CALLER_KIND else None
    raw_input = case.get("input")
    spec: Mapping[str, Any] = raw_input if isinstance(raw_input, Mapping) else {}
    return (
        (case.get("kind") in RULE_PASS_KINDS and _has_run_command(case))
        or caller == BACKGROUND_CALLER
        or (caller == INLINE_CALLER and "command_boundary" not in spec)
        or "proxy" in case
    )


def test_every_spawning_case_is_marked_posix_only() -> None:
    cases = _committed_cases()
    unmarked = [
        c["id"] for c in cases if _needs_posix_marker(c) and c.get("posix_only") is not True
    ]
    assert not unmarked, f"cases that spawn a process without posix_only: {unmarked}"
    extra = [c["id"] for c in cases if not _needs_posix_marker(c) and "posix_only" in c]
    assert not extra, f"cases marked posix_only that spawn nothing: {extra}"


# --- the run_command budget cases ----------------------------------------------------------


class _ProcessStarted(BaseException):
    """Raised when a budget case starts a process; no recorder except may swallow it."""


def _no_process(*args: Any, **kwargs: Any) -> Any:
    raise _ProcessStarted("a budget case started a process")


def _without_process(caller: Any) -> Any:
    """Run the seam caller with process creation raising the sentinel.

    The sentinel covers the caller only: the replay harness builds each case's schema with
    the native schema tool, a process that is not the case's.
    """

    async def guarded(db: Any, spec: Any) -> Any:
        with (
            mock.patch.object(asyncio, "create_subprocess_exec", _no_process),
            mock.patch.object(subprocess, "Popen", _no_process),
        ):
            return await caller(db, spec)

    return guarded


def _inline_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cases if c.get("kind") == CALLER_KIND and c.get("caller") == INLINE_CALLER]


def test_budget_cases_replay_without_starting_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each command-boundary case replays its expect with process creation refused.

    The seam caller fixes the clock for these cases; this test checks the process side only.
    """
    cases = _committed_cases()
    boundary = [
        case
        for case in _inline_cases(cases)
        if isinstance(case.get("input"), Mapping) and "command_boundary" in case["input"]
    ]
    assert len(boundary) >= MIN_BOUNDARY_CASES, (
        f"{len(boundary)} committed cases carry input.command_boundary; "
        f"at least {MIN_BOUNDARY_CASES} are required"
    )
    with monkeypatch.context() as patch:
        caller = rules_recorder.CALLERS[INLINE_CALLER]
        patch.setitem(rules_recorder.CALLERS, INLINE_CALLER, _without_process(caller))
        for case in boundary:
            replay = rules_corpus.replay_case(copy.deepcopy(case))
            assert replay == case["expect"], f"{case['id']}: replay without a process differs"
            timeouts = _returned(replay).get("command_timeouts")
            assert isinstance(timeouts, list) and timeouts, f"{case['id']}: {timeouts!r}"
            assert all(isinstance(value, float) for value in timeouts), f"{case['id']}: {timeouts}"
    real = [c for c in _inline_cases(cases) if "command_boundary" not in c.get("input", {})]
    assert len(real) == 1, f"inline cases with a real process: {[c['id'] for c in real]}"
    effect = real[0]["rules"][0]["effects"][0]
    budget = effect_deadline.BLOCKING_EFFECT_BUDGET_SECONDS
    assert effect.get("timeout_seconds") == budget, f"{real[0]['id']}: {effect!r}"


# --- the deadline handed to run_command -----------------------------------------------------

DETECTOR_SCRIPT = (
    "import json, sys, time\n"
    "event = json.load(sys.stdin)\n"
    "time.sleep(float(sys.argv[1]))\n"
    'print(json.dumps({"additionalContext": "synthetic detector saw: " '
    '+ event["tool_input"]["command"]}))\n'
)


def _handoff_case(case_id: str, *, expired: bool) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": case_id,
        "kind": "rule_pass",
        "provenance": "synthetic",
        "scenario_ids": [],
        "family": None,
        "observer": None,
        "transition": None,
        "rules": [
            {
                "name": "synthetic-deadline-handoff",
                "event": "before_tool",
                "effects": [
                    {
                        "type": "run_command",
                        "command": ["{python}", "-c", DETECTOR_SCRIPT, "0"],
                        "inject_result": True,
                        "timeout_seconds": 20,
                    }
                ],
            }
        ],
        "event": {
            "type": "before_tool",
            "source": "claude",
            "data": {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
            "metadata": {},
        },
        "seam_failures": [],
    }
    if expired:
        case["deadline"] = "expired"
    return case


@pytest.mark.skipif(sys.platform == "win32", reason="run_command spawns through /bin/sh")
def test_recorder_logs_the_deadline_each_run_command_receives() -> None:
    expired = rules_corpus.record_case(_handoff_case("synthetic-handoff-expired", expired=True))
    assert {"run_command_deadline": "expired"} in expired, f"expired deadline log: {expired}"
    none = rules_corpus.record_case(_handoff_case("synthetic-handoff-none", expired=False))
    assert {"run_command_deadline": "none"} in none, f"no-deadline log: {none}"


def test_recorder_labels_the_deadline_by_its_state() -> None:
    recorder = rules_recorder._Recorder(set())

    async def effect(owner: Any, effect: Any, row: Any, ctx: Any, context_parts: Any) -> None:
        return None

    wrapped = recorder.run_command_wrapper(effect)
    now = time.monotonic()
    contexts: dict[str, dict[str, Any]] = {
        "none": {},
        "live": {"_blocking_deadline": effect_deadline.BlockingEffectDeadline(now + 600.0)},
        "expired": {"_blocking_deadline": effect_deadline.BlockingEffectDeadline(now - 1.0)},
    }
    for ctx in contexts.values():
        asyncio.run(wrapped(None, None, None, ctx, []))
    labels = [entry.get("run_command_deadline") for entry in recorder.effect_log]
    assert labels == list(contexts), f"deadline labels {labels}, want {list(contexts)}"


def _handoff_spawn_timeouts(
    monkeypatch: pytest.MonkeyPatch, *, thaw_event: bool, deadline_dropped: bool
) -> tuple[int, list[float]]:
    """Replay the committed handoff case; return the effect calls and each spawn's timeout.

    With ``thaw_event`` the effect is called with a mutable copy of the frozen event and the
    engine's own effect context and deadline object; without it the effect gets exactly what
    the rule pass hands it. ``deadline_dropped`` removes the blocking deadline from the
    effect context.
    """
    case = {c["id"]: c for c in _committed_cases()}[DEADLINE_HANDOFF]
    assert case.get("deadline") == "expired", f"{DEADLINE_HANDOFF}: {case.get('deadline')!r}"
    original_apply = run_command_effects.RunCommandEffectsMixin._apply_run_command
    applied: list[Any] = []
    timeouts: list[float] = []

    async def observed_apply(
        self: Any, effect: Any, row: Any, ctx: dict[str, Any], context_parts: Any
    ) -> None:
        event = ctx["event"]
        applied.append(event)
        if thaw_event:
            event = dataclasses.replace(
                event,
                data=mutable_evaluation_result(event.data),
                metadata=mutable_evaluation_result(event.metadata),
            )
        await original_apply(self, effect, row, {**ctx, "event": event}, context_parts)

    async def observed_spawn(command: list[str], **kwargs: Any) -> Any:
        timeouts.append(kwargs["timeout_seconds"])
        return await execute_run_command(command, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(run_command_effects, "execute_run_command", observed_spawn)
        patch.setattr(
            run_command_effects.RunCommandEffectsMixin, "_apply_run_command", observed_apply
        )
        if deadline_dropped:
            original_build = TemplatingMixin._build_eval_context
            patch.setattr(TemplatingMixin, "_build_eval_context", _deadline_dropped(original_build))
        rules_corpus.replay_case(copy.deepcopy(case))
    return len(applied), timeouts


@pytest.mark.skipif(sys.platform == "win32", reason="run_command spawns through /bin/sh")
def test_deadline_handoff_case_fails_open_before_the_spawn_on_the_frozen_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python's rule pass starts no detector process for the committed handoff case.

    The rule pass hands the effect a frozen event, the payload builder raises writing into
    it, and the effect fails open before the spawn. This pins a recorded Python flaw, not
    the correct behaviour: the correct effect spawns with the handed deadline. The test
    fails once the payload is built from a detached copy; the committed case then reaches
    the spawn itself and the thawed-event test below is no longer needed.
    """
    applied, spawned = _handoff_spawn_timeouts(
        monkeypatch, thaw_event=False, deadline_dropped=False
    )
    assert applied == 1, f"{DEADLINE_HANDOFF}: run_command effect called {applied} times, want 1"
    assert spawned == [], (
        f"{DEADLINE_HANDOFF}: the frozen event reached the detector spawn with timeouts "
        f"{spawned}; the payload build no longer fails open"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="run_command spawns through /bin/sh")
def test_deadline_handoff_with_a_thawed_event_reaches_the_detector_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With only the event thawed, the handed deadline sets the detector spawn's budget.

    The committed case cannot reach the spawn in Python (the frozen-event test above), so
    this test swaps the frozen event for a mutable copy and keeps the effect context and the
    deadline object the engine handed over. The spawn it observes is the one the pass's
    deadline arithmetic gives; with the deadline dropped from the effect context, the same
    spawn gets the full budget.
    """
    floor = run_command_effects._RUN_COMMAND_INLINE_FLOOR_SECONDS
    _, handed = _handoff_spawn_timeouts(monkeypatch, thaw_event=True, deadline_dropped=False)
    assert handed == [floor], (
        f"{DEADLINE_HANDOFF}: detector spawn timeouts {handed}, want one spawn at the "
        f"{floor}s floor an expired deadline leaves"
    )
    _, dropped = _handoff_spawn_timeouts(monkeypatch, thaw_event=True, deadline_dropped=True)
    assert len(dropped) == 1 and dropped[0] > floor, (
        f"{DEADLINE_HANDOFF}: without the handed deadline the spawn timeouts are {dropped}, "
        f"want one spawn above the {floor}s floor"
    )


# --- the step-enforcement prefix ----------------------------------------------------------

STEP_PREFIX_SPECS: dict[str, Spec] = {
    STEP_BLOCK: S2_BLOCK,
    STEP_MCP_BLOCK: S3_BLOCK,
    STEP_RECHECK: S4_BLOCK,
}


def _step_prefix_dropped(original: Any) -> Any:
    """Return the step check's block with its step-enforcement header line removed."""

    def stub(self: Any, *args: Any, **kwargs: Any) -> Any:
        response = original(self, *args, **kwargs)
        if not isinstance(response, HookResponse) or not response.reason:
            return response
        header, _, rest = response.reason.partition("\n")
        if STEP_REASON not in header:
            return response
        return dataclasses.replace(response, reason=rest)

    return stub


def test_step_block_specs_require_the_step_enforcement_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_id = {case["id"]: case for case in _committed_cases()}
    accepted: list[str] = []
    with monkeypatch.context() as patch:
        original = RuleEngine._check_step_tool_enforcement
        patch.setattr(RuleEngine, "_check_step_tool_enforcement", _step_prefix_dropped(original))
        for case_id, spec in STEP_PREFIX_SPECS.items():
            case = by_id[case_id]
            stubbed = rules_corpus.replay_case(copy.deepcopy(case))
            rules_mutation.assert_clean_stubbed_replay(stubbed, f"{case_id} without the prefix")
            reason = str(_response(stubbed).get("reason") or "")
            assert _decision(case, stubbed) == "block", f"{case_id}: {stubbed.get('outcome_class')}"
            assert STEP_REASON not in reason, f"{case_id}: the stub kept the prefix: {reason!r}"
            if not _spec_mismatches(spec, case, stubbed):
                accepted.append(case_id)
    assert not accepted, (
        f"step block specs that accept a reason without the step-enforcement prefix: {accepted}"
    )


# --- loader refusals --------------------------------------------------------------------------


def _loader_case(case_id: str, **overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": case_id,
        "kind": "rule_pass",
        "provenance": "synthetic",
        "scenario_ids": [],
        "family": None,
        "observer": None,
        "transition": None,
        "rules": [
            {
                "name": "synthetic-loader-rule",
                "event": "before_tool",
                "effects": [{"type": "observe", "message": "loaded"}],
            }
        ],
        "event": {"type": "before_tool", "metadata": {}},
        "seam_failures": [],
        "expect": {
            "outcome_class": "allow",
            "eager_variable_delta": {},
            "on_receipt_payload": None,
            "effect_log": [],
            "audit_records": [],
            "response": {},
        },
    }
    case.update(overrides)
    return case


def _inline_case(case_id: str, spec: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return _loader_case(
        case_id,
        kind=CALLER_KIND,
        caller=INLINE_CALLER,
        rules=[
            {
                "name": "synthetic-loader-detector",
                "event": "before_tool",
                "effects": [{"type": "run_command", "command": ["synthetic-detector"]}],
            }
        ],
        event={},
        input={"rule_name": "synthetic-loader-detector", **spec},
        **{"seed": {"session": True}, **overrides},
    )


def _write_loader_corpus(root: Path, cases: list[dict[str, Any]]) -> Path:
    (root / "cases").mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": rules_corpus.SCHEMA_VERSION,
        "case_kinds": ["rule_pass", CALLER_KIND],
        "seams": list(rules_corpus.seam_registry()),
        "delivery_classes": list(rules_corpus.DELIVERY_CLASSES),
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "cases" / "synthetic.json").write_text(json.dumps({"cases": cases}))
    return root


def _assert_refused(
    root: Path, cases: list[dict[str, Any]], case_id: str, needle: str, member: str
) -> None:
    """The corpus must be refused with a message naming the case and the key."""
    corpus = _write_loader_corpus(root, cases)
    with pytest.raises(ValueError) as refused:
        rules_corpus.load_corpus(corpus)
    message = str(refused.value)
    assert case_id in message, f"{member}: the refusal does not name {case_id!r}: {message!r}"
    assert needle in message, f"{member}: the refusal does not name {needle!r}: {message!r}"


STEP_SEED_DEFINITION: dict[str, Any] = {
    "name": "synthetic-step-agent",
    "body": {"prompts": {"agent": "Synthetic corpus agent."}},
    "step_workflow": {
        "steps": [
            {"name": "synthetic-gather", "allowed_tools": "all"},
            {"name": "synthetic-finish", "allowed_tools": "all"},
        ]
    },
}
STEP_SEED: dict[str, Any] = {
    "session": True,
    "agent_definitions": [STEP_SEED_DEFINITION],
    "step_instance": {"agent": "synthetic-step-agent", "current_step": "synthetic-gather"},
}


def _without(mapping: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {k: copy.deepcopy(v) for k, v in mapping.items() if k != key}


def _malformed_seeds() -> list[tuple[str, dict[str, Any], str]]:
    """(member, seed, the key or name the refusal must name)."""
    no_steps = copy.deepcopy(STEP_SEED)
    no_steps["agent_definitions"][0]["step_workflow"] = {"steps": []}
    no_current = copy.deepcopy(STEP_SEED)
    no_current["step_instance"]["current_step"] = "synthetic-missing-step"
    bad_snapshot = copy.deepcopy(STEP_SEED)
    bad_snapshot["step_instance"]["snapshot_step_workflow"] = {
        "steps": [{"name": "synthetic-finish", "allowed_tools": "all"}]
    }
    no_agent = copy.deepcopy(STEP_SEED)
    no_agent["step_instance"]["agent"] = "synthetic-missing-agent"
    return [
        ("unknown key", {"synthetic-unknown": True}, "synthetic-unknown"),
        ("step instance without session", _without(STEP_SEED, "session"), "session"),
        (
            "step instance without definitions",
            _without(STEP_SEED, "agent_definitions"),
            "agent_definitions",
        ),
        ("step instance agent not seeded", no_agent, "synthetic-missing-agent"),
        ("definition without steps", no_steps, "current_step"),
        ("definition without the current step", no_current, "current_step"),
        ("snapshot without the current step", bad_snapshot, "current_step"),
    ]


def test_loader_rejects_malformed_seeds(tmp_path: Path) -> None:
    assert tuple(STEP_SEED) == rules_seed.SEED_ORDER, "the well-formed seed uses every seeder"
    well_formed = [
        _loader_case("synthetic-seeded", seed=copy.deepcopy(STEP_SEED)),
        _inline_case("synthetic-seeded-inline", {"event": {}, "deadline": None}),
    ]
    _manifest, loaded = rules_corpus.load_corpus(_write_loader_corpus(tmp_path / "ok", well_formed))
    assert [case["id"] for case in loaded] == ["synthetic-seeded", "synthetic-seeded-inline"]
    for index, (member, seed, needle) in enumerate(_malformed_seeds()):
        case = _loader_case("synthetic-seeded", seed=seed)
        _assert_refused(tmp_path / f"seed-{index}", [case], "synthetic-seeded", needle, member)
    unseeded = _inline_case("synthetic-unseeded-inline", {"event": {}, "deadline": None}, seed={})
    _assert_refused(
        tmp_path / "inline",
        [unseeded],
        "synthetic-unseeded-inline",
        "session",
        "inline caller without a session seed",
    )


def _enabled_seed_case(enabled: object) -> dict[str, Any]:
    definition = {**copy.deepcopy(STEP_SEED_DEFINITION), "enabled": enabled}
    return _loader_case(
        "synthetic-seeded", seed={"session": True, "agent_definitions": [definition]}
    )


def test_loader_refuses_a_non_boolean_enabled_flag(tmp_path: Path) -> None:
    for flag in (True, False):
        root = tmp_path / f"boolean-{flag}"
        _manifest, loaded = rules_corpus.load_corpus(
            _write_loader_corpus(root, [_enabled_seed_case(flag)])
        )
        assert [case["id"] for case in loaded] == ["synthetic-seeded"], f"enabled={flag}"
    for index, coerced in enumerate(("false", 0, 1, None)):
        _assert_refused(
            tmp_path / f"coerced-{index}",
            [_enabled_seed_case(coerced)],
            "synthetic-seeded",
            "enabled",
            f"agent definition enabled={coerced!r}",
        )


BOUNDARY = {"duration": 0.5, "context": "synthetic detector result"}


def _engine_path_corpus() -> list[dict[str, Any]]:
    return [
        _loader_case(
            "synthetic-loader-main",
            posix_only=True,
            proxy={"rtk": "echo-prefix"},
            engine_skip_group="synthetic-loader-group",
        ),
        _loader_case(
            "synthetic-loader-partner",
            pairs_with="synthetic-loader-main",
            engine_skip_group="synthetic-loader-group",
        ),
        _inline_case(
            "synthetic-loader-boundary",
            {"event": {}, "command_boundary": dict(BOUNDARY), "deadline_remaining": -1.0},
        ),
        _inline_case("synthetic-loader-real", {"event": {}, "deadline": None}, posix_only=True),
    ]


def _replaced(cases: list[dict[str, Any]], case: dict[str, Any]) -> list[dict[str, Any]]:
    return [case if existing["id"] == case["id"] else existing for existing in cases]


def _malformed_engine_path_members() -> list[tuple[str, dict[str, Any], str]]:
    """(member, the replacing case, the key the refusal must name)."""
    background = _loader_case(
        "synthetic-loader-real",
        kind=CALLER_KIND,
        caller=BACKGROUND_CALLER,
        rules=[],
        event={},
        input={"command": ["synthetic-detector"], "command_boundary": dict(BOUNDARY)},
        posix_only=True,
    )
    return [
        (
            "posix_only not a bool",
            _loader_case("synthetic-loader-main", posix_only="yes"),
            "posix_only",
        ),
        (
            "proxy not the fake",
            _loader_case("synthetic-loader-main", proxy={"rtk": "synthetic-other"}),
            "proxy",
        ),
        (
            "pairs_with names no case",
            _loader_case("synthetic-loader-partner", pairs_with="synthetic-no-such-case"),
            "pairs_with",
        ),
        (
            "empty skip group",
            _loader_case("synthetic-loader-partner", engine_skip_group=""),
            "engine_skip_group",
        ),
        (
            "skip group not a string",
            _loader_case("synthetic-loader-partner", engine_skip_group=3),
            "engine_skip_group",
        ),
        (
            "negative boundary duration",
            _inline_case(
                "synthetic-loader-boundary",
                {"event": {}, "command_boundary": {"duration": -1, "context": "x"}},
            ),
            "command_boundary",
        ),
        (
            "boundary without context",
            _inline_case(
                "synthetic-loader-boundary", {"event": {}, "command_boundary": {"duration": 1}}
            ),
            "command_boundary",
        ),
        (
            "boundary with empty context",
            _inline_case(
                "synthetic-loader-boundary",
                {"event": {}, "command_boundary": {"duration": 1, "context": ""}},
            ),
            "command_boundary",
        ),
        ("boundary on another caller", background, "command_boundary"),
        (
            "deadline and remaining budget",
            _inline_case(
                "synthetic-loader-boundary",
                {
                    "event": {},
                    "command_boundary": dict(BOUNDARY),
                    "deadline": "expired",
                    "deadline_remaining": -1.0,
                },
            ),
            "deadline_remaining",
        ),
        (
            "remaining budget without a boundary",
            _inline_case(
                "synthetic-loader-real",
                {"event": {}, "deadline_remaining": -1.0},
                posix_only=True,
            ),
            "deadline_remaining",
        ),
    ]


def test_loader_rejects_malformed_engine_path_keys(tmp_path: Path) -> None:
    corpus = _engine_path_corpus()
    _manifest, loaded = rules_corpus.load_corpus(_write_loader_corpus(tmp_path / "ok", corpus))
    assert [case["id"] for case in loaded] == [case["id"] for case in corpus]
    for index, (member, case, needle) in enumerate(_malformed_engine_path_members()):
        cases = _replaced(corpus, case)
        _assert_refused(tmp_path / f"key-{index}", cases, case["id"], needle, member)


def _boundary_case(**spec: Any) -> dict[str, Any]:
    return _inline_case("synthetic-loader-boundary", {"event": {}, **spec})


# A remaining budget is a finite JSON number or null; a boundary duration is a finite number.
LOOSE_DEADLINE_INPUTS: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "remaining budget true",
        _boundary_case(command_boundary=dict(BOUNDARY), deadline_remaining=True),
        "deadline_remaining",
    ),
    (
        "remaining budget false",
        _boundary_case(command_boundary=dict(BOUNDARY), deadline_remaining=False),
        "deadline_remaining",
    ),
    (
        "remaining budget numeric string",
        _boundary_case(command_boundary=dict(BOUNDARY), deadline_remaining="1.5"),
        "deadline_remaining",
    ),
    (
        "remaining budget NaN",
        _boundary_case(command_boundary=dict(BOUNDARY), deadline_remaining=float("nan")),
        "deadline_remaining",
    ),
    (
        "remaining budget infinite",
        _boundary_case(command_boundary=dict(BOUNDARY), deadline_remaining=float("inf")),
        "deadline_remaining",
    ),
    (
        "remaining budget negative infinite",
        _boundary_case(command_boundary=dict(BOUNDARY), deadline_remaining=float("-inf")),
        "deadline_remaining",
    ),
    (
        "boundary duration NaN",
        _boundary_case(command_boundary={"duration": float("nan"), "context": "x"}),
        "command_boundary",
    ),
    (
        "boundary duration infinite",
        _boundary_case(command_boundary={"duration": float("inf"), "context": "x"}),
        "command_boundary",
    ),
)
ACCEPTED_REMAINING_BUDGETS: tuple[float | int | None, ...] = (None, -1.0, 0, 2.5)


def test_loader_accepts_only_a_finite_remaining_budget_and_duration(tmp_path: Path) -> None:
    corpus = _engine_path_corpus()
    for index, remaining in enumerate(ACCEPTED_REMAINING_BUDGETS):
        case = _boundary_case(command_boundary=dict(BOUNDARY), deadline_remaining=remaining)
        root = _write_loader_corpus(tmp_path / f"ok-{index}", _replaced(corpus, case))
        _manifest, loaded = rules_corpus.load_corpus(root)
        assert len(loaded) == len(corpus), f"remaining budget {remaining!r} did not load"
    accepted: list[str] = []
    for index, (member, case, needle) in enumerate(LOOSE_DEADLINE_INPUTS):
        root = _write_loader_corpus(tmp_path / f"loose-{index}", _replaced(corpus, case))
        try:
            rules_corpus.load_corpus(root)
        except ValueError as refused:
            message = str(refused)
            if case["id"] not in message or needle not in message:
                accepted.append(f"{member} (refused without naming {needle}: {message})")
        else:
            accepted.append(member)
    assert not accepted, f"the loader accepts these deadline inputs: {accepted}"


# Over the kernel's shebang length, and holding a space the kernel splits a shebang at.
LONG_INTERPRETER_DIRECTORY_BYTES = 160
FAKE_RTK_EXPECTED_STDOUT = "rtk echo hi\n"


def _interpreter_link(directory: Path) -> Path:
    directory.mkdir(parents=True)
    link = directory / "python"
    link.symlink_to(Path(sys.executable).resolve())
    return link


def _fake_rtk_outcome(directory: Path, interpreter: Path) -> str:
    """Run the fake RTK written while ``interpreter`` is the running one; stdout or the error."""
    with mock.patch.object(sys, "executable", str(interpreter)):
        try:
            probe = rules_engine_seams._fake_rtk_probe(directory)
        except RuntimeError as refused:
            return f"probe refused: {refused}"
    try:
        completed = subprocess.run(
            [str(probe.path), "rewrite", "--", "echo hi"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError as failed:
        return f"exec failed: {failed}"
    if completed.returncode != 0:
        return f"exit {completed.returncode}: {completed.stderr.strip()}"
    return completed.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="the fake RTK is a POSIX script")
def test_fake_rtk_runs_with_a_long_or_spaced_interpreter_path(tmp_path: Path) -> None:
    # The spaced path sits under the short system /tmp so it stays inside the shebang limit
    # and only the space is under test; the pytest base directory is often longer than it.
    with tempfile.TemporaryDirectory(prefix="rtk-", dir="/tmp") as short_base:
        spaced = _interpreter_link(Path(short_base) / "synthetic dir")
        assert len(f"#!{spaced}".encode()) <= rules_engine_seams.SHEBANG_LIMIT_BYTES
        _assert_fake_rtk_runs_with(tmp_path, spaced, tmp_path / "synthetic-long")


def _assert_fake_rtk_runs_with(tmp_path: Path, spaced: Path, long_base: Path) -> None:
    long_link = _interpreter_link(long_base / ("x" * LONG_INTERPRETER_DIRECTORY_BYTES))
    assert len(str(long_link).encode()) > rules_engine_seams.SHEBANG_LIMIT_BYTES
    outcomes: dict[str, str] = {}
    for label, link in (("spaced", spaced), ("long", long_link)):
        control = subprocess.run(
            [str(link), "-c", "print('ok')"], capture_output=True, text=True, check=False
        )
        assert control.stdout == "ok\n", f"the {label} interpreter link does not run: {control}"
        rtk_dir = tmp_path / f"rtk-{label}"
        rtk_dir.mkdir()
        outcomes[label] = _fake_rtk_outcome(rtk_dir, link)
    expected = {"spaced": FAKE_RTK_EXPECTED_STDOUT, "long": FAKE_RTK_EXPECTED_STDOUT}
    assert outcomes == expected, f"the fake RTK breaks on the interpreter path: {outcomes}"

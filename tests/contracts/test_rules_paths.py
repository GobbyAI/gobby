"""Path contracts for the family, observer, transition and hardcoded cases.

A case that names a production path must change its own produced outputs when that path is
replaced by its registry stub (``reaches``), or be the unchanged control twin of a case that
does (``control_for``), or pin an inline branch by a twin or by two of its own passes. Fault
cases prove their own fault branch. Every pass of a rule-pass or handler-pass case is
recorded. The registry lives in ``rules_paths``; the produced-output projection in
``rules_mutation``. Every definition a case uses is synthetic.
"""

from __future__ import annotations

import copy
import inspect
import json
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.contracts import rules_corpus, rules_mutation, rules_paths
from tests.contracts import test_rules_corpus as corpus_tests

pytestmark = pytest.mark.slow

CORPUS_DIR = Path(__file__).resolve().parent / "rules"
README_PATH = CORPUS_DIR / "README.md"
CALLER_KIND = "seam_caller"
PASS_KINDS = ("rule_pass", "handler_pass")
HANDLER_ENTRY = "handler"
# Scenario-id prefix -> registry kind; the handler entry is required for the first three.
PATH_PREFIXES = {
    "S-FAM-": "family",
    "S-OBS-": "observer",
    "S-TRANS-": "transition",
    "S-HARDCODED-": "hardcoded",
}
HANDLER_KINDS = ("family", "observer", "transition")
LABEL_OUTCOMES = ("ALLOW", "BLOCK", "SEAMERR", "OBSERVED", "NOTOBSERVED")
# The one hardcoded id whose production function was removed; its case pins the absence and
# is the only path case allowed an empty reaches and control_for. Kept here, not in JSON, so
# editing case data cannot widen the exemption.
REMOVED_PATH_IDS = ("S-HARDCODED-BOUND-COMMIT-REPLAY",)
# Every domain fault kind a committed case must use, written out so the guard needs no
# manifest key and no fault module.
REQUIRED_FAULT_KINDS = (
    "git",
    "git_command",
    "raise_at",
    "session_variables",
    "session_read",
    "task_read",
    "hub_query",
)
PRODUCED_VALUE_GUARDS = ("fact", "fact_nonempty", "variable", "step")
PATH_ID = re.compile(r"^(family|observer|transition|hardcoded)\.[a-z_]+(?:\.[a-z_]+)?$")
PROXY_DISPATCH_CALLER = "proxy_self_call_dispatch"
PROXY_SELF_CALL_ID = "S-HARDCODED-AUTO-DISCOVER-PROXY-SELF-CALL"
DISPATCH_CASE_ID = "synthetic-hardcoded-proxy-self-call-dispatch"
DISPATCH_PATH = "hardcoded.auto_discover_proxy_self_call.dispatch"
PROXY_ROUTE_PATH = "hardcoded.auto_discover_proxy_self_call.proxy_route"
PROXY_SPY_KEY = "proxy_calls"
DISPATCH_RESULTS_KEY = "dispatch_results"
DISPATCH_ENTRY_KEYS = ("server", "tool", "inject_result", "block_on_failure", "block_on_success")
CLAIM_TRANSITION_ID = "S-TRANS-CLAIM"
CLAIM_VARIABLE = "task_claimed"
# The case keys the recorder reads to run a case; a twin may differ from its base only in
# these, and only at its differs_at paths.
INPUT_KEYS = (
    "kind",
    "entry",
    "caller",
    "rules",
    "event",
    "sequence",
    "seam_failures",
    "seed",
    "domain",
    "faults",
    "config",
    "eval_context",
    "input",
    "proxy",
    "observe",
)


def _corpus() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return rules_corpus.load_corpus(CORPUS_DIR)


def _cases() -> list[dict[str, Any]]:
    return _corpus()[1]


def _skipped_here(case: Mapping[str, Any]) -> bool:
    return sys.platform == "win32" and case.get("posix_only") is True


def _path_name(scenario_id: str) -> str | None:
    """Return the registry ``<kind>.<name>`` a scenario id names, or None."""
    for prefix, kind in PATH_PREFIXES.items():
        if not scenario_id.startswith(prefix):
            continue
        rest = scenario_id[len(prefix) :]
        if kind in ("family", "observer"):
            head, _, outcome = rest.rpartition("-")
            if outcome not in LABEL_OUTCOMES or not head:
                return None
            rest = head
        return f"{kind}.{rest.lower().replace('-', '_')}"
    return None


def _labelled_ids(case: Mapping[str, Any]) -> list[str]:
    return [sid for sid in case.get("scenario_ids", []) if _path_name(sid) is not None]


def _required_path_names() -> dict[str, str]:
    """Every required scenario id that names a path, keyed by its ``<kind>.<name>``."""
    scenario_ids = (
        corpus_tests.FAMILY_IDS
        + corpus_tests.OBSERVER_IDS
        + corpus_tests.HARDCODED_CASE_IDS
        + corpus_tests.FIXED_SCENARIO_IDS
    )
    names: dict[str, str] = {}
    for sid in scenario_ids:
        name = _path_name(sid)
        if name is not None:
            names.setdefault(name, sid)
    return names


def _callable_ids() -> list[str]:
    return [
        pid for pid, path in rules_paths.PATHS.items() if isinstance(path, rules_paths.CallablePath)
    ]


def _branch_ids() -> list[str]:
    return [
        pid for pid, path in rules_paths.PATHS.items() if isinstance(path, rules_paths.BranchPath)
    ]


def _is_fault_branch(path_id: str) -> bool:
    path = rules_paths.PATHS.get(path_id)
    return isinstance(path, rules_paths.BranchPath) and path.fault


def _probe_logs(expect: Mapping[str, Any]) -> list[list[str]]:
    """Per pass, the probe log and the between-operations probe log; then the top-level one."""
    passes = expect.get("passes", [])
    logs = [
        [*entry.get("reached", []), *entry.get("between_reached", [])]
        for entry in (passes if isinstance(passes, list) else [])
        if isinstance(entry, Mapping)
    ]
    return [*logs, list(expect.get("reached", []))]


def _guards(case: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """The case's ``requires`` guards as (name, value), from a mapping or a list of mappings."""
    requires = case.get("requires")
    entries: Iterable[Any] = [requires] if isinstance(requires, Mapping) else requires or []
    return [
        (name, value)
        for entry in entries
        if isinstance(entry, Mapping)
        for name, value in entry.items()
    ]


def _replay(case: Mapping[str, Any]) -> dict[str, Any]:
    return rules_corpus.replay_case(copy.deepcopy(dict(case)))


def _changed(case: Mapping[str, Any], replayed: Mapping[str, Any]) -> bool:
    produced = rules_mutation.produced_outputs(case, case["expect"])
    return rules_mutation.produced_outputs(case, replayed) != produced


# --- registry -------------------------------------------------------------------------------


def test_every_path_scenario_id_has_a_registry_path() -> None:
    required = _required_path_names()
    removed = {name for name, sid in required.items() if sid in REMOVED_PATH_IDS}
    assert removed, f"no required id is one of {REMOVED_PATH_IDS}"
    malformed = sorted(pid for pid in rules_paths.PATHS if not PATH_ID.match(pid))
    assert not malformed, f"registry ids not '<kind>.<name>' or '<kind>.<name>.<sub>': {malformed}"
    roots = {".".join(pid.split(".")[:2]) for pid in rules_paths.PATHS}
    missing = sorted(set(required) - removed - roots)
    assert not missing, f"required path ids with no registry path: {missing}"
    stray = sorted(roots - set(required))
    assert not stray, f"registry paths whose '<kind>.<name>' is no required id: {stray}"
    removed_listed = sorted(roots & removed)
    assert not removed_listed, f"removed paths must have no registry id: {removed_listed}"


def test_registry_binding_sites_and_stub_kinds_are_complete() -> None:
    problems: list[str] = []
    for pid in _callable_ids():
        path = rules_paths.PATHS[pid]
        assert isinstance(path, rules_paths.CallablePath)
        function = rules_paths.resolve(path.canonical)
        if path.canonical not in path.targets:
            problems.append(f"{pid}: the canonical {path.canonical} is not a target")
        stale = [t for t in path.targets if rules_paths.resolve(t) is not function]
        if stale:
            problems.append(f"{pid}: targets that do not bind the canonical: {stale}")
        unlisted = sorted(rules_paths.binding_sites(path.canonical) - set(path.targets))
        if unlisted:
            problems.append(f"{pid}: binding sites the registry misses: {unlisted}")
        stub = path.stub_for_target()
        stub_function = stub.__func__ if isinstance(stub, staticmethod) else stub
        is_async = inspect.iscoroutinefunction(function)
        if inspect.iscoroutinefunction(stub_function) != is_async:
            problems.append(f"{pid}: the stub's async kind differs from the canonical's")
        owner, attr = rules_paths.owner_and_attr(path.canonical)
        is_static = isinstance(inspect.getattr_static(owner, attr), staticmethod)
        if isinstance(stub, staticmethod) != is_static:
            problems.append(f"{pid}: the stub's staticmethod wrapping differs from the canonical's")
    assert not problems, "registry paths are incomplete:\n" + "\n".join(problems)


# --- what each case declares ------------------------------------------------------------------


def test_every_path_case_declares_what_it_reaches() -> None:
    cases = _cases()
    labelled = [case for case in cases if _labelled_ids(case)]
    assert labelled, "no committed case carries a family, observer, transition or hardcoded id"
    gaps: list[str] = []
    for case in labelled:
        sids = _labelled_ids(case)
        kinds = {str(_path_name(sid)).split(".")[0] for sid in sids}
        reasons: list[str] = []
        if not case.get("requires"):
            reasons.append("no requires")
        if kinds & set(HANDLER_KINDS) and case.get("entry") != HANDLER_ENTRY:
            reasons.append(f"entry is {case.get('entry')!r}, not {HANDLER_ENTRY!r}")
        removed = all(sid in REMOVED_PATH_IDS for sid in sids)
        if not removed and not case.get("reaches") and not case.get("control_for"):
            reasons.append("empty reaches and control_for")
        if reasons:
            gaps.append(f"{case['id']}: {', '.join(reasons)}")
    assert not gaps, f"{len(gaps)} of {len(labelled)} path cases declare nothing:\n" + "\n".join(
        gaps
    )


def test_each_case_calls_the_paths_it_names_and_every_path_is_named() -> None:
    cases = _cases()
    named = {pid for case in cases for pid in case.get("reaches", [])}
    unnamed = [pid for pid in _callable_ids() if pid not in named]
    assert not unnamed, f"no case lists these callable paths under reaches: {unnamed}"
    problems: list[str] = []
    for case in cases:
        listed = [
            pid
            for pid in [*case.get("reaches", []), *case.get("control_for", [])]
            if isinstance(rules_paths.PATHS.get(pid), rules_paths.CallablePath)
        ]
        if not listed or _skipped_here(case):
            continue
        recorded = _probe_logs(case["expect"])
        live = _probe_logs(_replay(case))
        if live != recorded:
            problems.append(f"{case['id']}: probe logs {live} differ from the recorded {recorded}")
        hit = {pid for log in live for pid in log}
        absent = [pid for pid in listed if pid not in hit]
        if absent:
            problems.append(f"{case['id']}: names paths the replay never called: {absent}")
    assert not problems, "\n".join(problems)


# --- mutation, helper and branch coverage ---------------------------------------------------


def _stubbed_replays(path_id: str, members: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    path = rules_paths.PATHS[path_id]
    assert isinstance(path, rules_paths.CallablePath)
    replays: dict[str, dict[str, Any]] = {}
    with rules_paths.stubbed(path):
        for case in members:
            try:
                replays[case["id"]] = _replay(case)
            except Exception as exc:
                raise AssertionError(
                    f"{case['id']} under {path_id}: the replay raised {exc!r}"
                ) from exc
    for case_id, replayed in replays.items():
        rules_mutation.assert_clean_stubbed_replay(replayed, f"{case_id} with {path_id} stubbed")
    return replays


@pytest.mark.timeout(3600)
def test_stubbing_a_path_changes_a_produced_output() -> None:
    cases = _cases()
    by_id = {case["id"]: case for case in cases}
    callables = _callable_ids()
    reaching = {pid: [c for c in cases if pid in c.get("reaches", [])] for pid in callables}
    uncovered = [pid for pid in callables if not reaching[pid]]
    assert not uncovered, f"callable paths with no (path, case) pair to check: {uncovered}"
    controls = {pid: [c for c in cases if pid in c.get("control_for", [])] for pid in callables}
    mutation: list[tuple[str, str]] = []
    helper: list[tuple[str, str]] = []
    failures: list[str] = []
    for pid in callables:
        members = [c for c in reaching[pid] + controls[pid] if not _skipped_here(c)]
        replays = _stubbed_replays(pid, members)
        for case in reaching[pid]:
            if case["id"] not in replays:
                continue
            if _changed(case, replays[case["id"]]):
                mutation.append((case["id"], pid))
            else:
                failures.append(f"{case['id']}: unchanged with {pid} stubbed")
        for case in controls[pid]:
            if case["id"] not in replays:
                continue
            partner = by_id.get(str(case.get("twin_of")))
            if _changed(case, replays[case["id"]]):
                failures.append(f"{case['id']}: a control for {pid} changes under its stub")
            elif partner is None or pid not in partner.get("reaches", []):
                failures.append(f"{case['id']}: its twin does not list {pid} under reaches")
            else:
                helper.append((case["id"], pid))
    assert not failures, "\n".join(failures)
    for path_id in (DISPATCH_PATH, PROXY_ROUTE_PATH):
        assert (DISPATCH_CASE_ID, path_id) in mutation, (
            f"no mutation pair ({DISPATCH_CASE_ID}, {path_id})"
        )
    branch = [
        (case["id"], pid)
        for case in cases
        for pid in case.get("reaches", [])
        if isinstance(rules_paths.PATHS.get(pid), rules_paths.BranchPath)
    ]
    lists = {"mutation": set(mutation), "helper": set(helper), "branch": set(branch)}
    for left, right in (("mutation", "helper"), ("mutation", "branch"), ("helper", "branch")):
        shared = sorted(lists[left] & lists[right])
        assert not shared, f"pairs in both the {left} and the {right} list: {shared}"
    runnable = [pid for pid in callables if any(not _skipped_here(c) for c in reaching[pid])]
    without = sorted(set(runnable) - {pid for _case, pid in mutation})
    assert not without, f"callable paths with no mutation pair: {without}"
    orphans = [
        (case_id, pid)
        for case_id, pid in helper
        if (str(by_id[case_id].get("twin_of")), pid) not in lists["mutation"]
    ]
    assert not orphans, f"controls whose twin is not in the mutation list: {orphans}"


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Map each leaf of a JSON value to its dotted path; empty containers are leaves."""
    if isinstance(value, Mapping) and value:
        flat: dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}{key}."))
        return flat
    if isinstance(value, list) and value:
        flat = {}
        for index, item in enumerate(value):
            flat.update(_flatten(item, f"{prefix}{index}."))
        return flat
    return {prefix.rstrip("."): value}


def _inputs(case: Mapping[str, Any]) -> dict[str, Any]:
    inputs = {key: case[key] for key in INPUT_KEYS if key in case}
    inputs.setdefault("faults", [])
    return _flatten(inputs)


def _under(path: str, roots: Iterable[str]) -> bool:
    return any(path == root or path.startswith(root + ".") for root in roots)


def _twin_problems(twin: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]) -> list[str]:
    base = by_id.get(str(twin.get("twin_of")))
    if base is None:
        return [f"{twin['id']}: twin_of names no case: {twin.get('twin_of')!r}"]
    differs_at = twin.get("differs_at")
    if not isinstance(differs_at, list) or not differs_at:
        return [f"{twin['id']}: a twin needs a non-empty differs_at"]
    left, right = _inputs(twin), _inputs(base)
    differing = sorted(p for p in set(left) | set(right) if left.get(p, ...) != right.get(p, ...))
    outside = [p for p in differing if not _under(p, differs_at)]
    problems = []
    if outside:
        problems.append(
            f"{twin['id']}: inputs differ from {base['id']} outside differs_at: {outside}"
        )
    if rules_mutation.produced_outputs(twin, twin["expect"]) == rules_mutation.produced_outputs(
        base, base["expect"]
    ):
        problems.append(f"{twin['id']}: produces the same outputs as its twin {base['id']}")
    return problems


def _pass_pair_problems(case: Mapping[str, Any], path_id: str) -> list[str]:
    pair = case.get("branch_passes", {}).get(path_id)
    passes = case["expect"].get("passes")
    if not isinstance(pair, list) or len(pair) != 2 or not isinstance(passes, list):
        return [f"{case['id']}: branch_passes[{path_id!r}] is not a pass pair of recorded passes"]
    first, second = pair
    if first == second or not all(isinstance(i, int) and 0 <= i < len(passes) for i in pair):
        return [f"{case['id']}: branch_passes[{path_id!r}] = {pair} is not two of its passes"]
    if rules_mutation.produced_pass(passes[first]) == rules_mutation.produced_pass(passes[second]):
        return [f"{case['id']}: passes {first} and {second} produce the same outputs"]
    return []


def test_branch_cases_differ_from_their_twin() -> None:
    cases = _cases()
    listed = {pid for case in cases for pid in case.get("reaches", [])}
    unlisted = [pid for pid in _branch_ids() if pid not in listed]
    assert not unlisted, f"branch paths no case lists under reaches: {unlisted}"
    by_id = {case["id"]: case for case in cases}
    named_by: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        if "twin_of" in case:
            named_by.setdefault(str(case["twin_of"]), []).append(case)
    problems: list[str] = []
    for case in cases:
        for pid in case.get("reaches", []):
            if not isinstance(rules_paths.PATHS.get(pid), rules_paths.BranchPath):
                continue
            needs_own_twin = _is_fault_branch(pid) or "faults" in case or "seam_of" in case
            if needs_own_twin and "twin_of" not in case:
                problems.append(f"{case['id']}: {pid} must be proved by its own twin_of")
            elif "twin_of" in case:
                problems.extend(_twin_problems(case, by_id))
            elif pid in case.get("branch_passes", {}):
                problems.extend(_pass_pair_problems(case, pid))
            elif case["id"] in named_by:
                for twin in named_by[case["id"]]:
                    problems.extend(_twin_problems(twin, by_id))
            else:
                problems.append(f"{case['id']}: {pid} has no twin pair and no branch_passes entry")
    assert not problems, "\n".join(sorted(set(problems)))


def _hit_kinds(expect: Mapping[str, Any]) -> set[str]:
    passes = expect.get("passes", [])
    logs = [entry.get("faults", []) for entry in passes if isinstance(entry, Mapping)]
    returned = expect.get("returned")
    if isinstance(returned, Mapping):
        logs.append(returned.get("faults", []))
    logs.append(expect.get("faults", []))
    return {str(hit.get("fault")) for log in logs for hit in log if isinstance(hit, Mapping)}


def test_fault_cases_prove_their_own_fault_branch() -> None:
    manifest, cases = _corpus()
    used = {
        fault.get("kind")
        for case in cases
        for fault in case.get("faults", [])
        if isinstance(fault, Mapping)
    }
    unused = [kind for kind in REQUIRED_FAULT_KINDS if kind not in used]
    assert not unused, f"no committed case has a fault of these kinds: {unused}"
    selected = [
        case
        for case in cases
        if "faults" in case
        or "seam_of" in case
        or any(_is_fault_branch(pid) for pid in case.get("reaches", []))
    ]
    problems: list[str] = []
    for case in selected:
        faults = case.get("faults")
        if not isinstance(faults, list) or not faults:
            problems.append(f"{case['id']}: names a fault branch with no faults entry")
            continue
        names = [name for name, _value in _guards(case)]
        if "fault_hit" not in names:
            problems.append(f"{case['id']}: no fault_hit guard")
        if not any(name in PRODUCED_VALUE_GUARDS for name in names):
            problems.append(f"{case['id']}: no guard on the degraded produced value")
        hits = _hit_kinds(case["expect"])
        unhit = [fault.get("kind") for fault in faults if fault.get("kind") not in hits]
        if unhit:
            problems.append(f"{case['id']}: the recorded hit log has no hit for {unhit}")
    assert not problems, "\n".join(problems)
    assert sorted(REQUIRED_FAULT_KINDS) == sorted(manifest.get("fault_kinds", []))


# --- passes ---------------------------------------------------------------------------------


def test_transition_cases_cross_a_state_boundary() -> None:
    cases = _cases()
    transitions = [
        case
        for case in cases
        if any(sid.startswith("S-TRANS-") for sid in case.get("scenario_ids", []))
    ]
    assert transitions, "no committed case carries a transition id"
    problems: list[str] = []
    for case in transitions:
        passes = case["expect"].get("passes")
        if not (isinstance(passes, list) and len(passes) >= 2):
            problems.append(f"{case['id']}: needs at least two recorded passes, has {passes!r}")
            continue
        outcomes = [entry.get("outcome_class") for entry in passes]
        if all(left == right for left, right in zip(outcomes, outcomes[1:], strict=False)):
            problems.append(f"{case['id']}: no adjacent passes differ in outcome: {outcomes}")
    assert not problems, "\n".join(problems)


def _blocks(entry: Mapping[str, Any]) -> list[Any]:
    return [record for record in entry["audit_records"] if record.get("result") == "block"]


def test_every_pass_is_recorded() -> None:
    cases = [case for case in _cases() if case.get("kind") in PASS_KINDS]
    assert cases, "no rule_pass or handler_pass case is committed"
    for case in cases:
        expect = case["expect"]
        passes = expect.get("passes")
        assert isinstance(passes, list), f"{case['id']}: expect.passes is {passes!r}, not a list"
        assert len(passes) == 1 + len(case.get("sequence", [])), f"{case['id']}: pass count"
        no_entries: list[Any] = []
        effect_log = sum((entry["effect_log"] for entry in passes), no_entries)
        assert effect_log == expect["effect_log"], f"{case['id']}: pass effect logs"
        audit_records = sum((entry["audit_records"] for entry in passes), no_entries)
        assert audit_records == expect["audit_records"], f"{case['id']}: pass audit records"
        assert passes[-1]["response"] == expect["response"], f"{case['id']}: last response"
        assert passes[-1]["outcome_class"] == expect["outcome_class"], f"{case['id']}: outcome"
    multi = [
        case["id"]
        for case in cases
        if sum(1 for entry in case["expect"]["passes"] if entry["effect_log"]) > 1
    ]
    assert multi, "no case logs effects in more than one pass"
    claims = [case for case in cases if CLAIM_TRANSITION_ID in case.get("scenario_ids", [])]
    assert len(claims) == 1, f"one {CLAIM_TRANSITION_ID} case is required, found {len(claims)}"
    claim = claims[0]
    passes = claim["expect"]["passes"]
    assert len(passes) == 3, f"{claim['id']}: {len(passes)} passes, not three"
    assert [entry["outcome_class"] for entry in passes] == ["block", "allow", "allow"]
    rule = claim["rules"][0]["name"]
    assert _blocks(passes[0]) == [{"rule": rule, "result": "block", "event": "before_tool"}]
    assert _blocks(passes[1]) == _blocks(passes[2]) == []
    assert passes[1]["eager_variable_delta"].get(CLAIM_VARIABLE) is True
    assert CLAIM_VARIABLE not in passes[0]["eager_variable_delta"]
    assert CLAIM_VARIABLE not in passes[2]["eager_variable_delta"]


# --- the synthetic namespace ----------------------------------------------------------------


def test_case_definitions_stay_in_the_synthetic_namespace() -> None:
    paths = [*sorted((CORPUS_DIR / "cases").glob("*.json")), README_PATH]
    assert len(paths) > 1, "no rules case file is committed"
    findings = rules_corpus.find_definition_references(paths)
    assert not findings, "definition references outside the synthetic namespace:\n" + "\n".join(
        findings
    )


def _scan_one(tmp_path: Path, name: str, content: Any) -> list[str]:
    path = tmp_path / name
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text)
    return rules_corpus.find_definition_references([path])


def _reported_members() -> dict[str, Any]:
    return {
        "rule name": {"cases": [{"rules": [{"name": "unprefixed-rule"}]}]},
        "agent name": {"seed": {"agent_definitions": [{"name": "unprefixed-agent"}]}},
        "workflows.rules member": {"body": {"workflows": {"rules": ["unprefixed-rule"]}}},
        "agent_scope member": {
            "rules": [{"name": "synthetic-x", "agent_scope": ["unprefixed-agent"]}]
        },
        "negated agent_scope member": {
            "rules": [{"name": "synthetic-x", "agent_scope": ["!unprefixed-agent"]}]
        },
        "_agent_type value": {"seed": {"variables": {"_agent_type": "unprefixed-agent"}}},
        "name selector": {"workflows": {"rule_selectors": {"include": ["name:unprefixed-rule"]}}},
        "bare selector": {"workflows": {"rule_selectors": {"exclude": ["unprefixed-rule"]}}},
        "file below the shared root": {"note": "src/gobby/install/shared/synthetic-dir/x.yaml"},
        "file below the workflows root": {"note": ".gobby/workflows/synthetic.yaml"},
        "file below the scanned rules directory": {"note": ".gobby/workflows/rules/synthetic.yaml"},
        "unscanned directory": {"note": ".gobby/workflows/unscanned"},
        "markdown path": "See .gobby/workflows/rules/synthetic.yaml for the source.\n",
    }


def _clean_members() -> dict[str, Any]:
    names = sorted(corpus_tests._bundled_definition_names())
    members: dict[str, Any] = {
        "wildcard selector and scope": {
            "rules": [{"name": "synthetic-x", "agent_scope": ["*"]}],
            "workflows": {"rule_selectors": {"include": ["*"]}},
        },
        "synthetic scope": {
            "rules": [{"name": "synthetic-x", "agent_scope": ["synthetic-x", "!synthetic-x"]}]
        },
        "synthetic name selector": {"rule_selectors": {"exclude": ["name:synthetic-x-*"]}},
        "tag selectors": {"rule_selectors": {"include": ["tag:default", "tag:unprefixed-label"]}},
        "prose names": {
            "rules": [
                {"name": "synthetic-x", "effects": [{"type": "block", "reason": " ".join(names)}]}
            ],
            "prompt": " ".join(names),
        },
        "readme prose": "Prose may mention " + ", ".join(names) + ".\n",
    }
    for index, directory in enumerate(rules_corpus.SCANNED_DIRECTORIES):
        members[f"scanned directory {index}"] = {"note": directory}
        members[f"scanned directory {index} glob"] = {"note": f"{directory}/*.yaml"}
        members[f"scanned directory {index} deep glob"] = {"note": f"{directory}/**/*.toml"}
    return members


def test_definition_reference_scan_checks_positions_not_words(tmp_path: Path) -> None:
    assert corpus_tests._bundled_definition_names(), "the bundled-name probe found no name"
    missed: list[str] = []
    for index, (member, content) in enumerate(_reported_members().items()):
        suffix = ".md" if isinstance(content, str) else ".json"
        if not _scan_one(tmp_path, f"reported-{index}{suffix}", content):
            missed.append(member)
    assert not missed, f"the scan does not report: {missed}"
    flagged: dict[str, list[str]] = {}
    for index, (member, content) in enumerate(_clean_members().items()):
        suffix = ".md" if isinstance(content, str) else ".json"
        findings = _scan_one(tmp_path, f"clean-{index}{suffix}", content)
        if findings:
            flagged[member] = findings
    assert not flagged, f"the scan reports values that are not references: {flagged}"


# --- loader refusals --------------------------------------------------------------------------


def _path_case(case_id: str, **overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": case_id,
        "kind": "handler_pass",
        "entry": HANDLER_ENTRY,
        "provenance": "synthetic",
        "scenario_ids": [],
        "family": None,
        "observer": None,
        "transition": None,
        "rules": [
            {
                "name": "synthetic-path-rule",
                "event": "before_tool",
                "effects": [{"type": "block", "reason": "synthetic"}],
            }
        ],
        "event": {
            "type": "before_tool",
            "source": "claude",
            "data": {"tool_name": "Bash", "tool_input": {"command": "git commit -m synthetic"}},
            "metadata": {},
        },
        "seam_failures": [],
        "expect": {
            "outcome_class": "block",
            "eager_variable_delta": {},
            "on_receipt_payload": None,
            "effect_log": [],
            "audit_records": [],
            "response": {},
        },
    }
    case.update(overrides)
    return case


def _event_with(command: str) -> dict[str, Any]:
    return {
        "type": "before_tool",
        "source": "claude",
        "data": {"tool_name": "Bash", "tool_input": {"command": command}},
        "metadata": {},
    }


DOMAIN_AGENT = "synthetic-domain-agent"


def _domain(agent: str = DOMAIN_AGENT, step: str = "synthetic-domain-step") -> dict[str, Any]:
    return {
        "agents": {
            agent: {
                "name": agent,
                "body": {"prompts": {"agent": "Synthetic corpus agent."}},
                "step_workflow": {"steps": [{"name": step, "allowed_tools": "all"}]},
                "enabled": True,
            }
        }
    }


GIT_FAULT = {"kind": "git"}
OPEN_FAULT = {
    "kind": "raise_at",
    "target": "pathlib:Path.open",
    "raises": "OSError",
    "in_stack": "gobby.sessions.transcript_interrupt:_tail_records",
}


def _well_formed_cases() -> dict[str, dict[str, Any]]:
    """One well-formed member per key the loader validates, keyed by case id."""
    cases = [
        _path_case(
            "synthetic-path-base",
            reaches=["family.commit_guard"],
            requires={"reached": "family.commit_guard"},
        ),
        _path_case(
            "synthetic-path-control",
            event=_event_with("git status"),
            twin_of="synthetic-path-base",
            differs_at=["event.data.tool_input.command"],
            control_for=["family.commit_guard"],
            requires={"reached": "family.commit_guard"},
        ),
        _path_case(
            "synthetic-path-seam",
            faults=[dict(GIT_FAULT)],
            twin_of="synthetic-path-base",
            differs_at=["faults"],
            seam_of="family.ledger_reconcile",
            reaches=["family.ledger_reconcile.status_unavailable"],
            requires={"fault_hit": True, "variable": {"name": "synthetic_x", "equals": True}},
        ),
        _path_case(
            "synthetic-path-raise",
            faults=[dict(OPEN_FAULT)],
            twin_of="synthetic-path-base",
            differs_at=["faults"],
            reaches=["observer.turn_interrupt.unreadable_transcript"],
            requires={"fault_hit": True, "variable": {"name": "synthetic_x", "equals": False}},
        ),
        _path_case(
            "synthetic-path-passes",
            kind="rule_pass",
            entry="engine",
            sequence=[_event_with("echo again")],
            reaches=["hardcoded.auto_discover_servers"],
            branch_passes={"hardcoded.auto_discover_servers": [0, 1]},
            requires={"variable": {"name": "servers_listed", "equals": True}},
        ),
        _path_case(
            "synthetic-path-branch",
            kind="rule_pass",
            entry="engine",
            config={"rules.enforcement_enabled": False},
            reaches=["hardcoded.enforcement_off"],
            requires={"variable": {"name": "synthetic_x", "equals": True}},
        ),
        _path_case(
            "synthetic-path-branch-twin",
            kind="rule_pass",
            entry="engine",
            config={"rules.enforcement_enabled": True},
            twin_of="synthetic-path-branch",
            differs_at=["config"],
            reaches=["hardcoded.enforcement_off"],
            requires={"variable": {"name": "synthetic_x", "equals": True}},
        ),
        _path_case("synthetic-path-domain", domain=_domain()),
        _path_case(
            "synthetic-path-caller",
            kind=CALLER_KIND,
            caller=PROXY_DISPATCH_CALLER,
            rules=[],
            event={},
            input={},
            seed={"session": True},
            returned_spy_keys=["calls"],
            expect={**_path_case("x")["expect"], "returned": {"calls": [], "results": []}},
        ),
    ]
    return {case["id"]: case for case in cases}


UNPAIRED_BRANCH_ID = "synthetic-path-unpaired"
UNPAIRED_BRANCH = "hardcoded.aggregate_off"


def _branch_case(case_id: str, *, aggregate: bool, **overrides: Any) -> dict[str, Any]:
    return _path_case(
        case_id,
        kind="rule_pass",
        entry="engine",
        config={"rules.aggregate_blocks": aggregate},
        reaches=[UNPAIRED_BRANCH],
        requires={"variable": {"name": "synthetic_x", "equals": True}},
        **overrides,
    )


def _with(cases: dict[str, dict[str, Any]], case_id: str, **changes: Any) -> dict[str, Any]:
    changed = copy.deepcopy(cases[case_id])
    for key, value in changes.items():
        if value is None:
            changed.pop(key, None)
        else:
            changed[key] = value
    return changed


def _malformed_path_members() -> list[tuple[str, list[dict[str, Any]], str, str]]:
    """(member, the corpus with one key malformed, the offending case id, the key it names)."""
    ok = _well_formed_cases()

    def replaced(case: dict[str, Any]) -> list[dict[str, Any]]:
        return [case if case_id == case["id"] else copy.deepcopy(c) for case_id, c in ok.items()]

    def member(
        label: str, case_id: str, needle: str, **changes: Any
    ) -> tuple[str, list[dict[str, Any]], str, str]:
        return label, replaced(_with(ok, case_id, **changes)), case_id, needle

    seam = "synthetic-path-seam"
    control = "synthetic-path-control"
    passes = "synthetic-path-passes"
    caller = "synthetic-path-caller"
    unpaired = _branch_case(UNPAIRED_BRANCH_ID, aggregate=False)
    return [
        member(
            "unknown reaches id",
            "synthetic-path-base",
            "reaches",
            reaches=["family.synthetic_unknown"],
        ),
        member(
            "unknown control_for id",
            control,
            "control_for",
            control_for=["family.synthetic_unknown"],
        ),
        member("unknown seam_of id", seam, "seam_of", seam_of="family.synthetic_unknown"),
        member("seam_of without twin_of", seam, "twin_of", twin_of=None, differs_at=None),
        member(
            "seam_of under reaches",
            seam,
            "seam_of",
            reaches=["family.ledger_reconcile.status_unavailable", "family.ledger_reconcile"],
        ),
        member(
            "seam_of under control_for", seam, "seam_of", control_for=["family.ledger_reconcile"]
        ),
        member("control_for without twin_of", control, "twin_of", twin_of=None, differs_at=None),
        member(
            "control_for a branch path",
            control,
            "control_for",
            control_for=["hardcoded.enforcement_off"],
        ),
        member(
            "an id under reaches and control_for",
            control,
            "control_for",
            reaches=["family.commit_guard"],
        ),
        member(
            "faults without twin_of",
            "synthetic-path-raise",
            "twin_of",
            twin_of=None,
            differs_at=None,
        ),
        member(
            "a fault branch without faults",
            "synthetic-path-raise",
            "faults",
            faults=None,
            event=_event_with("git log"),
            differs_at=["event.data.tool_input.command"],
        ),
        member(
            "seam_of without faults",
            seam,
            "faults",
            faults=None,
            differs_at=["event"],
            event=_event_with("git log"),
        ),
        (
            "a branch path in no twin pair",
            [*replaced(ok[seam]), unpaired],
            UNPAIRED_BRANCH_ID,
            UNPAIRED_BRANCH,
        ),
        member(
            "branch_passes on a fault branch",
            seam,
            "branch_passes",
            sequence=[_event_with("echo again")],
            branch_passes={"family.ledger_reconcile.status_unavailable": [0, 1]},
        ),
        member(
            "branch_passes on a callable path",
            passes,
            "branch_passes",
            reaches=["hardcoded.auto_discover_servers", "family.commit_guard"],
            branch_passes={
                "hardcoded.auto_discover_servers": [0, 1],
                "family.commit_guard": [0, 1],
            },
        ),
        member(
            "branch_passes not in reaches",
            passes,
            "branch_passes",
            branch_passes={
                "hardcoded.auto_discover_servers": [0, 1],
                "hardcoded.grok_post_compact": [0, 1],
            },
        ),
        member(
            "branch_passes equal indexes",
            passes,
            "branch_passes",
            branch_passes={"hardcoded.auto_discover_servers": [1, 1]},
        ),
        member(
            "branch_passes index outside",
            passes,
            "branch_passes",
            branch_passes={"hardcoded.auto_discover_servers": [0, 2]},
        ),
        member(
            "raise_at outside the map",
            "synthetic-path-raise",
            "raises",
            faults=[{**OPEN_FAULT, "raises": "synthetic-unknown-error"}],
        ),
        member("twin_of names no case", control, "twin_of", twin_of="synthetic-no-such-case"),
        member(
            "differs_at absent from a member",
            control,
            "differs_at",
            differs_at=["event.data.synthetic_absent"],
        ),
        member(
            "unknown fault kind",
            seam,
            "synthetic-unknown-fault",
            faults=[{"kind": "synthetic-unknown-fault"}],
        ),
        member(
            "unknown fault filter",
            seam,
            "synthetic_filter",
            faults=[{**GIT_FAULT, "synthetic_filter": "x"}],
        ),
        member(
            "unprefixed domain agent",
            "synthetic-path-domain",
            "unprefixed-agent",
            domain=_domain(agent="unprefixed-agent"),
        ),
        member(
            "unprefixed domain step",
            "synthetic-path-domain",
            "unprefixed-step",
            domain=_domain(step="unprefixed-step"),
        ),
        member(
            "handler_pass without the handler entry", "synthetic-path-base", "entry", entry="engine"
        ),
        member("seed and domain", "synthetic-path-domain", "seed", seed={"session": True}),
        member(
            "returned_spy_keys on a rule_pass case",
            passes,
            "returned_spy_keys",
            returned_spy_keys=["calls"],
        ),
        member(
            "returned_spy_keys not a list", caller, "returned_spy_keys", returned_spy_keys="calls"
        ),
        member("returned_spy_keys empty", caller, "returned_spy_keys", returned_spy_keys=[]),
        member(
            "returned_spy_keys repeated",
            caller,
            "returned_spy_keys",
            returned_spy_keys=["calls", "calls"],
        ),
        member(
            "returned_spy_keys empty string", caller, "returned_spy_keys", returned_spy_keys=[""]
        ),
        member(
            "returned_spy_keys not returned",
            caller,
            "returned_spy_keys",
            returned_spy_keys=["synthetic-absent"],
        ),
        member(
            "returned_spy_keys every key",
            caller,
            "returned_spy_keys",
            returned_spy_keys=["calls", "results"],
        ),
    ]


def _write_path_corpus(root: Path, cases: list[dict[str, Any]]) -> Path:
    (root / "cases").mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": rules_corpus.SCHEMA_VERSION,
        "case_kinds": [*PASS_KINDS, CALLER_KIND],
        "seams": list(rules_corpus.seam_registry()),
        "delivery_classes": list(rules_corpus.DELIVERY_CLASSES),
        "fault_kinds": list(REQUIRED_FAULT_KINDS),
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "cases" / "synthetic.json").write_text(json.dumps({"cases": cases}))
    return root


def test_loader_rejects_malformed_path_keys(tmp_path: Path) -> None:
    ok = list(_well_formed_cases().values())
    _manifest, loaded = rules_corpus.load_corpus(_write_path_corpus(tmp_path / "ok", ok))
    assert [case["id"] for case in loaded] == [case["id"] for case in ok]
    paired = [
        *copy.deepcopy(ok),
        _branch_case(UNPAIRED_BRANCH_ID, aggregate=False),
        _branch_case(
            "synthetic-path-unpaired-twin",
            aggregate=True,
            twin_of=UNPAIRED_BRANCH_ID,
            differs_at=["config"],
        ),
    ]
    _manifest, loaded = rules_corpus.load_corpus(_write_path_corpus(tmp_path / "paired", paired))
    assert len(loaded) == len(paired), "a branch case with a twin naming it must load"
    for index, (member, cases, case_id, needle) in enumerate(_malformed_path_members()):
        corpus = _write_path_corpus(tmp_path / f"key-{index}", cases)
        with pytest.raises(ValueError) as refused:
            rules_corpus.load_corpus(corpus)
        message = str(refused.value)
        assert case_id in message, f"{member}: the refusal does not name {case_id!r}: {message!r}"
        assert needle in message, f"{member}: the refusal does not name {needle!r}: {message!r}"


# --- the proxy self-call dispatch ---------------------------------------------------------


def _dispatch_entries(expect: Mapping[str, Any]) -> list[Any]:
    returned = expect.get("returned")
    return list(returned.get(DISPATCH_RESULTS_KEY, [])) if isinstance(returned, Mapping) else []


def test_proxy_self_call_dispatch_replays_the_engine_entry() -> None:
    cases = _cases()
    dispatch = [
        case
        for case in cases
        if case.get("kind") == CALLER_KIND and case.get("caller") == PROXY_DISPATCH_CALLER
    ]
    assert len(dispatch) == 1, (
        f"one {PROXY_DISPATCH_CALLER} case is required, found {len(dispatch)}"
    )
    case = dispatch[0]
    engine = [
        other
        for other in cases
        if other.get("kind") in PASS_KINDS
        and PROXY_SELF_CALL_ID in other.get("scenario_ids", [])
        and other["expect"].get("response", {}).get("metadata", {}).get("mcp_calls")
    ]
    assert len(engine) == 1, f"one engine case must emit the self-call, found {len(engine)}"
    emitted = engine[0]["expect"]["response"]["metadata"]["mcp_calls"]
    assert case["input"]["mcp_calls"] == emitted, "the dispatch input drifted from the engine entry"
    recorded = case["expect"]
    assert recorded["returned"][PROXY_SPY_KEY] == [["list_servers", "gobby-*"]]
    entries = _dispatch_entries(recorded)
    assert len(entries) == 1 and entries[0]["success"] is True, f"recorded dispatch: {entries}"
    plain = _replay(case)
    assert plain == recorded, "the unmutated replay differs from the recorded expect"
    assert {DISPATCH_PATH, PROXY_ROUTE_PATH} <= set(plain.get("reached", []))

    produced = rules_mutation.produced_outputs(case, recorded)
    with rules_paths.stubbed(_callable(DISPATCH_PATH)):
        no_dispatch = _replay(case)
    rules_mutation.assert_clean_stubbed_replay(no_dispatch, f"{case['id']} with {DISPATCH_PATH}")
    assert no_dispatch["returned"] == {PROXY_SPY_KEY: [], DISPATCH_RESULTS_KEY: []}
    assert _dispatch_entries(no_dispatch) != entries
    assert rules_mutation.produced_outputs(case, no_dispatch) != produced

    with rules_paths.stubbed(_callable(PROXY_ROUTE_PATH)):
        no_route = _replay(case)
    rules_mutation.assert_clean_stubbed_replay(no_route, f"{case['id']} with {PROXY_ROUTE_PATH}")
    assert no_route["returned"][PROXY_SPY_KEY] == []
    routed = _dispatch_entries(no_route)
    assert len(routed) == 1, f"stubbed route dispatch: {routed}"
    assert routed[0]["success"] is False
    assert routed[0]["result"] == {"success": False, "error": "stubbed"}
    assert {k: routed[0][k] for k in DISPATCH_ENTRY_KEYS} == {
        k: entries[0][k] for k in DISPATCH_ENTRY_KEYS
    }
    assert routed != entries
    assert rules_mutation.produced_outputs(case, no_route) != produced

    top = produced["top"]
    assert "returned" in top and "effect_log" not in top and "reached" not in top
    assert case["returned_spy_keys"] == [PROXY_SPY_KEY]
    assert top["returned"] == {DISPATCH_RESULTS_KEY: entries}
    spy_only = copy.deepcopy(recorded)
    spy_only["returned"][PROXY_SPY_KEY] = []
    assert rules_mutation.produced_outputs(case, spy_only) == produced


def _callable(path_id: str) -> rules_paths.CallablePath:
    path = rules_paths.PATHS[path_id]
    assert isinstance(path, rules_paths.CallablePath), f"{path_id} is not a callable path"
    return path

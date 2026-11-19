"""Rule-pass corpus: case schema, coverage, replay, normalizer vectors and name hygiene.

Each case file under ``rules/cases/`` holds ``{"cases": [...]}``. A case carries
``id``, ``kind``, ``provenance``, ``scenario_ids``, optional ``family``, ``observer``
and ``transition`` labels, the synthetic ``rules`` it defines, the ``event`` it fires,
``seam_failures`` and an ``expect`` block. A rule may declare a ``group`` label.
Rule ids start with ``synthetic-``, and no case asserts the behavior of a bundled rule.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookResponse
from tests.contracts import rules_corpus, rules_vectors

CONTRACTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = CONTRACTS_DIR.parents[1]
SRC_DIR = REPO_ROOT / "src"
CORPUS_DIR = CONTRACTS_DIR / "rules"
VECTORS_PATH = CORPUS_DIR / "normalize_vectors.json"
MODULES_PATH = CORPUS_DIR / "normalize_modules.json"
README_PATH = CORPUS_DIR / "README.md"
BUNDLED_WORKFLOWS_DIR = SRC_DIR / "gobby" / "install" / "shared" / "workflows"
RECORD_ENV = "GOBBY_CORPUS_RECORD"
RECORD_COMMAND = f"{RECORD_ENV}=1"
REPLAY_TARGET = "tests/contracts/test_rules_corpus.py"

EFFECT_TYPES = (
    "block",
    "set_variable",
    "inject_context",
    "set_display_content",
    "mcp_call",
    "observe",
    "rewrite_input",
    "set_permission_response",
    "set_retry",
    "set_watch_paths",
    "set_worktree_path",
    "set_elicitation",
    "load_skill",
    "run_command",
    "proxy_hook",
)
DELIVERY_CLASSES = ("eager", "on_receipt")
SEAM_KINDS = (
    "rule_definitions",
    "workflow_audit",
    "metrics_events",
    "coordination_waits",
    "completion_subscribers",
    "inter_session_messages",
    "tool_results",
    "rule_allow_audit",
)
FAMILIES = (
    "commit_guard",
    "review_scope",
    "ledger_reconcile",
    "dirty_state",
    "claim",
    "binding",
    "dirty_grant",
    "edit_ledger",
    "found_work",
    "validation",
    "monolith",
    "memory_review",
    "session_target",
)
OBSERVERS = ("plan_mode", "context_usage", "mcp", "commits", "turn_interrupt")
# An observer that performs an external read (git, transcript or session store) needs a
# seam-error case. The tuple is written out by hand; nothing else derives it.
SEAM_READING_OBSERVERS = ("plan_mode", "context_usage", "commits", "turn_interrupt")
RESPONSE_FIELDS = (
    "decision",
    "reason",
    "context",
    "context_parts",
    "system_message",
    "display_content",
    "modified_input",
    "modify_args",
    "auto_approve",
    "permission_decision",
    "updated_permissions",
    "retry",
    "watch_paths",
    "worktree_path",
    "elicitation_action",
    "elicitation_content",
    "elicitation_error",
    "trigger_action",
    "metadata",
)
# system_message, modify_args and trigger_action are set by the adapters and the route
# handlers, outside RuleEngine.evaluate, so the rule-pass layer cannot set them. The
# adapter and pipeline layers cover them.
ENGINE_RESPONSE_FIELDS = tuple(
    field
    for field in RESPONSE_FIELDS
    if field not in ("system_message", "modify_args", "trigger_action")
)


def _response_defaults() -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for item in dataclasses.fields(HookResponse):
        if item.name not in RESPONSE_FIELDS:
            continue
        if item.default_factory is not dataclasses.MISSING:
            defaults[item.name] = item.default_factory()
        else:
            defaults[item.name] = item.default
    return defaults


RESPONSE_DEFAULTS = _response_defaults()
EXPECT_KEYS = (
    "outcome_class",
    "eager_variable_delta",
    "on_receipt_payload",
    "effect_log",
    "audit_records",
)
# Optional expect key, present only on a case whose event skips a declared rule group.
SKIPPED_GROUPS_KEY = "skipped_groups"
OPTIONAL_EXPECT_KEYS = (SKIPPED_GROUPS_KEY,)
LABEL_AXES = (("family", FAMILIES, "S-FAM-"), ("observer", OBSERVERS, "S-OBS-"))


def _upper_id(name: str) -> str:
    return name.upper().replace("_", "-")


FIXED_SCENARIO_IDS = (
    "S-ORDER-PRIORITY",
    "S-ORDER-TRIGGER-INDEX",
    "S-AGG-BLOCK",
    "S-SETVAR-READ-LATER",
    "S-MCPCALL-READ-LATER",
    "S-AGENT-SCOPE",
    "S-AUDIENCE",
    "S-ACTIVE-NAME",
    "S-PROJECT-TWO-ROWS",
    "S-PROJECT-NONE",
    "S-PARSE-SKIP",
    "S-COND-ERR-BLOCK",
    "S-COND-ERR-NONBLOCK",
    "S-DEADLINE",
    "S-ONRECEIPT-STAGE",
    "S-ACK-EAGER",
    "S-ACK-RECEIPT",
    "S-STEP-TOOL-ALLOW",
    "S-STEP-TOOL-BLOCK",
    "S-PROXY-RTK",
    "S-F4-RECHECK-BLOCK",
    "S-F4-RECHECK-ONCE",
    "S-HANDOFF-PENDING-ALLOW",
    "S-HARDCODED-NO-RULE",
    "S-AFTER-NO-BEFORE",
    "S-DUP-BEFORE",
    "S-TRANS-CLAIM",
    "S-TRANS-REVIEW-GATE",
    "S-TRANS-PLANMODE",
    "S-CLI-INTERNAL-BEFORE",
    "S-CLI-INTERNAL-AFTER",
)
SEAM_FAIL_IDS = tuple(f"S-SEAM-FAIL-{_upper_id(kind)}" for kind in SEAM_KINDS)
FAMILY_IDS = tuple(
    f"S-FAM-{_upper_id(name)}-{outcome}"
    for name in FAMILIES
    for outcome in ("ALLOW", "BLOCK", "SEAMERR")
)
OBSERVER_IDS = tuple(
    f"S-OBS-{_upper_id(name)}-{outcome}"
    for name in OBSERVERS
    for outcome in ("OBSERVED", "NOTOBSERVED")
) + tuple(f"S-OBS-{_upper_id(name)}-SEAMERR" for name in SEAM_READING_OBSERVERS)
REQUIRED_SCENARIO_IDS = FIXED_SCENARIO_IDS + SEAM_FAIL_IDS + FAMILY_IDS + OBSERVER_IDS
MIN_RULE_CASES = len(REQUIRED_SCENARIO_IDS)

HARDCODED_CASE_IDS = (
    "S-HARDCODED-ENFORCEMENT-OFF",
    "S-HARDCODED-AGGREGATE-OFF",
    "S-HARDCODED-SURVEY-INJECT",
    "S-HARDCODED-GROK-POST-COMPACT",
    "S-HARDCODED-COMPACT-TURN-BYPASS",
    "S-HARDCODED-COMMIT-REVIEW-DEFAULT",
    "S-HARDCODED-BLOCKED-TOOL-RETRY",
    "S-HARDCODED-AFTER-TOOL-RECOVERY",
    "S-HARDCODED-AFTER-TOOL-STEP",
    "S-HARDCODED-BATCH-RESET",
    "S-HARDCODED-AUTO-DISCOVER-SERVERS",
    "S-HARDCODED-AUTO-DISCOVER-PROXY-SELF-CALL",
    "S-HARDCODED-TURN-END-ATTEMPTS",
    "S-HARDCODED-STOP-GATES",
    "S-HARDCODED-LATE-MCP-INJECTION",
    "S-HARDCODED-TERMINAL-DENIAL",
    "S-HARDCODED-BLOCK-RESPONSE",
    "S-HARDCODED-AGENT-TOOL-ENFORCEMENT",
    "S-HARDCODED-LOST-BINDING-REWIND",
    "S-HARDCODED-BOUND-COMMIT-REPLAY",
    "S-HARDCODED-STEP-TRANSITION-NOTICE",
)
ORDERING_AND_FILTER_IDS = (
    "S-ORDER-PRIORITY",
    "S-ORDER-TRIGGER-INDEX",
    "S-AGG-BLOCK",
    "S-AGENT-SCOPE",
    "S-AUDIENCE",
    "S-ACTIVE-NAME",
    "S-PROJECT-TWO-ROWS",
    "S-PROJECT-NONE",
)
FAILURE_MODE_IDS = (
    "S-PARSE-SKIP",
    "S-COND-ERR-BLOCK",
    "S-COND-ERR-NONBLOCK",
    "S-DEADLINE",
) + SEAM_FAIL_IDS
CLI_INTERNAL_IDS = ("S-CLI-INTERNAL-BEFORE", "S-CLI-INTERNAL-AFTER")
CLI_INTERNAL_EVENTS = {
    "S-CLI-INTERNAL-BEFORE": "before_tool",
    "S-CLI-INTERNAL-AFTER": "after_tool",
}

NORMALIZER_ENTRY_POINTS = (
    "tokenize_shell_command",
    "scan_shell_command",
    "extract_heredoc_bodies",
    "decode_ansi_c_escape",
    "apply_path_scope_metadata",
    "normalize_tool_fields",
    "classify_git_mutation",
    "executable_command_subjects",
    "command_patterns_match",
    "blocks_direct_provider_launch",
    "rule_matches_agent",
)
MIN_NORMALIZE_VECTORS = len(NORMALIZER_ENTRY_POINTS)
NORMALIZE_FAMILY_GLOBS = (
    "hooks/checkout_reads.py",
    "hooks/_normalization_*.py",
    "hooks/normalization.py",
    "hooks/_ansi_c.py",
    "hooks/_path_scope.py",
    "hooks/_shell_syntax.py",
    "hooks/_provider_shell_syntax.py",
    "hooks/_case_syntax.py",
    "hooks/_shell_execution_words.py",
    "hooks/tool_outcomes.py",
    "hooks/provider_launch_guard.py",
    "hooks/code_navigation.py",
    "hooks/code_navigation_recovery.py",
    "workflows/engine/command_matching*.py",
    "workflows/engine/command_environment.py",
    "workflows/selectors.py",
    "utils/python_source.py",
    "mcp_proxy/_call_tool_wrapper.py",
)
NEGATIVE_TEST_FILES = (
    "tests/workflows/test_command_matching.py",
    "tests/hooks/test_code_navigation_recovery.py",
)
NEGATIVE_WORDS = ("negative", "reject", "refuse", "block")


def _load_committed_cases() -> list[dict[str, Any]]:
    _manifest, cases = rules_corpus.load_corpus(CORPUS_DIR)
    return cases


def _assert_enough(cases: list[dict[str, Any]]) -> None:
    assert len(cases) >= MIN_RULE_CASES, (
        f"{len(cases)} rule-pass cases committed; at least {MIN_RULE_CASES} are required"
    )


def _assert_scenarios_present(cases: list[dict[str, Any]], required: Iterable[str]) -> None:
    carried = {sid for case in cases for sid in case.get("scenario_ids", [])}
    missing = [sid for sid in required if sid not in carried]
    assert not missing, f"no case carries these scenario IDs: {missing}"


def _effect_types(case: dict[str, Any]) -> set[str]:
    types = {effect["type"] for rule in case["rules"] for effect in rule["effects"]}
    types.update(entry["type"] for entry in case["expect"]["effect_log"] if "type" in entry)
    return types


def _deliveries(case: dict[str, Any]) -> set[str]:
    deliveries = {
        effect.get("delivery", "eager") for rule in case["rules"] for effect in rule["effects"]
    }
    deliveries.update(
        entry["delivery"] for entry in case["expect"]["effect_log"] if "delivery" in entry
    )
    return deliveries


def _carries(case: dict[str, Any], axis: str, item: str) -> bool:
    if axis == "scenario":
        return item in case.get("scenario_ids", [])
    if axis == "effect_type":
        return item in _effect_types(case)
    if axis == "delivery":
        return item in _deliveries(case)
    if axis == "response_field":
        response = case["expect"].get("response", {})
        return item in response and _response_set(item, response[item])
    return case.get(axis) == item


def _response_set(field: str, value: Any) -> bool:
    """A response field counts as set when its value differs from the HookResponse default."""
    return bool(value != RESPONSE_DEFAULTS[field])


def _label_gaps(case: dict[str, Any]) -> list[str]:
    """Return the label axes the case's scenario IDs call for but the case does not carry."""
    scenario_ids = case.get("scenario_ids", [])
    gaps: set[str] = set()
    for axis, names, prefix in LABEL_AXES:
        for name in names:
            tag = f"{prefix}{_upper_id(name)}-"
            if any(sid.startswith(tag) for sid in scenario_ids) and case.get(axis) != name:
                gaps.add(axis)
    return sorted(gaps)


def _synthetic_case(**overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": "synthetic-case",
        "kind": "rule_pass",
        "provenance": "synthetic",
        "scenario_ids": ["S-ORDER-PRIORITY"],
        "family": None,
        "observer": None,
        "transition": None,
        "rules": [
            {
                "name": "synthetic-rule",
                "event": "before_tool",
                "effects": [{"type": "block", "reason": "synthetic", "delivery": "eager"}],
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


def _write_corpus(
    root: Path, *, schema_version: int = 1, cases: list[dict[str, Any]] | None = None
) -> None:
    (root / "cases").mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": schema_version,
        "case_kinds": ["rule_pass"],
        "seams": list(SEAM_KINDS),
        "delivery_classes": list(DELIVERY_CLASSES),
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    if cases is not None:
        (root / "cases" / "synthetic.json").write_text(json.dumps({"cases": cases}))


def _bundled_definition_names() -> set[str]:
    names: set[str] = set()
    for path in (BUNDLED_WORKFLOWS_DIR / "rules").rglob("*.yaml"):
        body = yaml.safe_load(path.read_text())
        if isinstance(body, dict) and isinstance(body.get("rules"), dict):
            names.update(body["rules"])
    for subdir in ("agents", "pipelines"):
        for path in (BUNDLED_WORKFLOWS_DIR / subdir).rglob("*.yaml"):
            body = yaml.safe_load(path.read_text())
            if isinstance(body, dict) and isinstance(body.get("name"), str):
                names.add(body["name"])
    names.discard("default")
    return names


def _dotted(path: Path) -> str:
    return ".".join(path.relative_to(SRC_DIR).with_suffix("").parts)


def _module_functions(module: str) -> set[str]:
    path = SRC_DIR / (module.replace(".", "/") + ".py")
    tree = ast.parse(path.read_text(), filename=str(path))
    return {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _resolve_import(importer: str, is_package: bool, node: ast.ImportFrom) -> str | None:
    """Return the absolute dotted module an ``ImportFrom`` names, or None past the root."""
    if node.level == 0:
        return node.module
    parts = importer.split(".")
    package = parts if is_package else parts[:-1]
    keep = len(package) - (node.level - 1)
    if keep <= 0:
        return None
    return ".".join([*package[:keep], *([node.module] if node.module else [])])


def _gobby_imported_names() -> dict[str, set[str]]:
    imported: dict[str, set[str]] = {}
    for path in (SRC_DIR / "gobby").rglob("*.py"):
        is_package = path.name == "__init__.py"
        importer = _dotted(path)
        if is_package:
            importer = importer.rsplit(".", 1)[0]
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = _resolve_import(importer, is_package, node)
            if not module or module == importer or not module.startswith("gobby."):
                continue
            imported.setdefault(module, set()).update(alias.name for alias in node.names)
    return imported


def _negative_test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    relative = path.relative_to(REPO_ROOT).as_posix()
    return {
        f"{relative}::{node.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and any(word in node.name.lower() for word in NEGATIVE_WORDS)
    }


def test_loader_rejects_stale_schema_version(tmp_path: Path) -> None:
    _write_corpus(tmp_path, schema_version=0)
    with pytest.raises(ValueError, match="schema_version"):
        rules_corpus.load_corpus(tmp_path)


def test_corpus_covers_effect_types_and_delivery() -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    types = set().union(*(_effect_types(case) for case in cases))
    deliveries = set().union(*(_deliveries(case) for case in cases))
    missing_types = [name for name in EFFECT_TYPES if name not in types]
    missing_deliveries = [name for name in DELIVERY_CLASSES if name not in deliveries]
    assert not missing_types, f"effect types no case produces: {missing_types}"
    assert not missing_deliveries, f"delivery classes no case uses: {missing_deliveries}"


def test_corpus_has_required_scenarios() -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    _assert_scenarios_present(cases, REQUIRED_SCENARIO_IDS)


def test_python_engine_replays_corpus() -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    mismatched = [case["id"] for case in cases if rules_corpus.replay_case(case) != case["expect"]]
    assert not mismatched, f"engine output differs from the expect block for: {mismatched}"


def test_corpus_covers_hardcoded_behavior_and_response_fields() -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    _assert_scenarios_present(cases, HARDCODED_CASE_IDS)
    set_fields = {
        field
        for case in cases
        for field, value in case["expect"].get("response", {}).items()
        if _response_set(field, value)
    }
    missing = [field for field in ENGINE_RESPONSE_FIELDS if field not in set_fields]
    assert not missing, f"response fields no case sets: {missing}"


def test_corpus_covers_ordering_and_filters() -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    _assert_scenarios_present(cases, ORDERING_AND_FILTER_IDS)


def test_corpus_covers_failure_modes() -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    _assert_scenarios_present(cases, FAILURE_MODE_IDS)


def test_case_schema_requires_every_expect_section() -> None:
    for key in EXPECT_KEYS:
        case = _synthetic_case()
        del case["expect"][key]
        with pytest.raises(ValueError, match=re.escape(key)):
            rules_corpus.validate_case(case)
    mismatched_family = _synthetic_case(scenario_ids=[FAMILY_IDS[0]], family="review_scope")
    with pytest.raises(ValueError, match="family"):
        rules_corpus.validate_case(mismatched_family)
    unlabelled_observer = _synthetic_case(scenario_ids=[OBSERVER_IDS[0]], observer=None)
    with pytest.raises(ValueError, match="observer"):
        rules_corpus.validate_case(unlabelled_observer)
    cases = _load_committed_cases()
    _assert_enough(cases)
    incomplete = [
        (case["id"], key) for case in cases for key in EXPECT_KEYS if key not in case["expect"]
    ]
    assert not incomplete, f"committed cases missing expect sections: {incomplete}"
    unlabelled = [(case["id"], axis) for case in cases for axis in _label_gaps(case)]
    assert not unlabelled, f"committed cases missing or mismatching labels: {unlabelled}"


def test_optional_expect_keys_load_with_or_without_the_key(tmp_path: Path) -> None:
    with_key = _synthetic_case(id="synthetic-skipped-group")
    with_key["rules"][0]["group"] = "synthetic-group"
    with_key["expect"][SKIPPED_GROUPS_KEY] = ["synthetic-group"]
    without_key = _synthetic_case(id="synthetic-no-skipped-group")
    _write_corpus(tmp_path, cases=[with_key, without_key])
    _manifest, loaded = rules_corpus.load_corpus(tmp_path)
    by_id = {case["id"]: case for case in loaded}
    assert set(by_id) == {with_key["id"], without_key["id"]}, f"loaded cases: {sorted(by_id)}"
    assert by_id[with_key["id"]]["expect"][SKIPPED_GROUPS_KEY] == ["synthetic-group"]
    bare = by_id[without_key["id"]]["expect"]
    present = [key for key in OPTIONAL_EXPECT_KEYS if key in bare]
    assert not present, f"optional expect keys appear on a case that omits them: {present}"


def test_cli_internal_request_skips_declared_group() -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    _assert_scenarios_present(cases, CLI_INTERNAL_IDS)
    for scenario_id, event_type in CLI_INTERNAL_EVENTS.items():
        case = next(case for case in cases if scenario_id in case["scenario_ids"])
        assert case["event"]["type"] == event_type, case["id"]
        assert case["event"]["metadata"].get("_cli_internal_request") is True, case["id"]
        skipped = set(case["expect"].get(SKIPPED_GROUPS_KEY, []))
        declared = {rule["group"] for rule in case["rules"] if "group" in rule}
        assert skipped and skipped <= declared, f"{case['id']} skips no group its rules declare"
        assert any(rule.get("group") not in skipped for rule in case["rules"]), case["id"]
        assert rules_corpus.replay_case(case) == case["expect"], case["id"]


def test_exporter_records_through_recorder() -> None:
    logged = rules_corpus.record_case(_synthetic_case())
    assert logged, "the recorder logs no seam call for a synthetic rule-pass case"
    registry = rules_corpus.seam_registry()
    missing_kinds = [kind for kind in SEAM_KINDS if kind not in registry]
    assert not missing_kinds, f"seam registry lacks these kinds: {missing_kinds}"
    unwrapped = [kind for kind in SEAM_KINDS if not hasattr(registry[kind][0], registry[kind][1])]
    assert not unwrapped, f"registry names attributes that do not exist: {unwrapped}"
    cases = _load_committed_cases()
    _assert_enough(cases)
    empty_log = [case["id"] for case in cases if not case["expect"].get("effect_log")]
    assert not empty_log, f"cases with an empty effect log: {empty_log}"


def test_seam_registry_matches_required_kinds() -> None:
    registry_kinds = set(rules_corpus.seam_registry())
    required_kinds = set(SEAM_KINDS)
    registry_only = sorted(registry_kinds - required_kinds)
    list_only = sorted(required_kinds - registry_kinds)
    assert not registry_only and not list_only, (
        f"registry-only seam kinds: {registry_only}; kinds in SEAM_KINDS only: {list_only}"
    )
    failure_ids = {f"S-SEAM-FAIL-{_upper_id(kind)}" for kind in registry_kinds}
    unrequired = sorted(failure_ids - set(SEAM_FAIL_IDS))
    unregistered = sorted(set(SEAM_FAIL_IDS) - failure_ids)
    assert not unrequired and not unregistered, (
        f"registry kinds with no required failure ID: {unrequired}; "
        f"required failure IDs with no registry kind: {unregistered}"
    )


def test_record_branch_writes_case_through_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RECORD_ENV, "1")
    case = _synthetic_case()
    _write_corpus(tmp_path, cases=[case])
    rules_corpus.record_corpus(tmp_path)
    written = json.loads((tmp_path / "cases" / "synthetic.json").read_text())["cases"]
    logged = rules_corpus.record_case(case)
    assert logged, "the recorder logs no seam call for the synthetic case"
    assert written[0]["expect"]["effect_log"] == logged, (
        "the record branch did not persist the recorder's effect log"
    )


def test_python_normalizer_replays_vectors() -> None:
    assert VECTORS_PATH.is_file(), f"missing {VECTORS_PATH.name}"
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    assert len(vectors) >= MIN_NORMALIZE_VECTORS, (
        f"{len(vectors)} normalizer vectors; at least {MIN_NORMALIZE_VECTORS} are required"
    )
    entries = {vector["entry"].rsplit(".", 1)[-1] for vector in vectors}
    unexercised = [name for name in NORMALIZER_ENTRY_POINTS if name not in entries]
    assert not unexercised, f"entry points with no vector: {unexercised}"
    mismatched = [
        vector["id"]
        for vector in vectors
        if rules_corpus.replay_normalizer_vector(vector) != vector["expect"]
    ]
    assert not mismatched, f"normalizer output differs for vectors: {mismatched}"


def test_vectors_cover_every_cross_module_function() -> None:
    assert MODULES_PATH.is_file(), f"missing {MODULES_PATH.name}"
    assert VECTORS_PATH.is_file(), f"missing {VECTORS_PATH.name}"
    listed = {entry["module"] for entry in json.loads(MODULES_PATH.read_text())["modules"]}
    globbed = {
        _dotted(path)
        for pattern in NORMALIZE_FAMILY_GLOBS
        for path in (SRC_DIR / "gobby").glob(pattern)
    }
    unlisted = sorted(globbed - listed)
    assert not unlisted, f"normalizer modules missing from the manifest: {unlisted}"
    imported = _gobby_imported_names()
    required = {
        f"{module}.{name}"
        for module in listed
        for name in _module_functions(module) & imported.get(module, set())
    }
    assert required, "no cross-module functions found in the listed modules"
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    covered = {vector["entry"] for vector in vectors}
    uncovered = sorted(required - covered)
    assert not uncovered, f"cross-module functions with no vector: {uncovered}"


def test_python_negative_tests_replayed() -> None:
    expected: set[str] = set()
    for relative in NEGATIVE_TEST_FILES:
        expected |= _negative_test_names(REPO_ROOT / relative)
    assert expected, "no negative test functions found in the named test files"
    assert VECTORS_PATH.is_file(), f"missing {VECTORS_PATH.name}"
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    replayed = {vector["negative_from"] for vector in vectors if vector.get("negative_from")}
    missing = sorted(expected - replayed)
    assert not missing, f"negative tests with no replayed vector: {missing}"


def test_python_sources_name_no_bundled_definition(tmp_path: Path) -> None:
    names = sorted(_bundled_definition_names())
    assert names, "no bundled definition names were found"
    found = rules_corpus.find_bundled_name_mentions(sorted(CONTRACTS_DIR.rglob("*.py")), names)
    assert found == [], f"Python sources name bundled definitions: {found}"
    negative = tmp_path / "negative_fixture.py"
    negative.write_text(f"# fixture naming {names[0]}\n")
    flagged = rules_corpus.find_bundled_name_mentions([negative], names)
    assert flagged, "the checker did not flag a Python file that names a bundled definition"


def test_bundled_rules_load_through_engine_loader(tmp_path: Path) -> None:
    files = sorted((BUNDLED_WORKFLOWS_DIR / "rules").rglob("*.yaml"))
    assert files, "no bundled rule YAML files were found"
    bodies = rules_corpus.load_bundled_rules()
    assert len(bodies) == len(files), (
        f"loaded {len(bodies)} bodies for {len(files)} bundled rule files"
    )
    unknown = tmp_path / "unknown-field.yaml"
    unknown.write_text(
        yaml.safe_dump(
            {
                "rules": {
                    "synthetic-rule": {
                        "event": "before_tool",
                        "effects": [{"type": "block", "bogus_field": "x"}],
                    }
                }
            }
        )
    )
    with pytest.raises(ValueError, match="bogus_field"):
        rules_corpus.load_rule_body(unknown)


def test_readme_names_record_replay_and_rerecord() -> None:
    assert README_PATH.is_file(), f"missing {README_PATH.name}"
    text = README_PATH.read_text()
    for marker in (RECORD_COMMAND, REPLAY_TARGET, "synthetic-", "re-record"):
        assert marker in text, f"the README does not name {marker!r}"


def test_loader_rejects_unknown_case_kind_and_seam(tmp_path: Path) -> None:
    kind_root = tmp_path / "kind"
    _write_corpus(kind_root, cases=[_synthetic_case(kind="no_such_kind")])
    with pytest.raises(ValueError, match="no_such_kind"):
        rules_corpus.load_corpus(kind_root)
    seam_root = tmp_path / "seam"
    _write_corpus(seam_root, cases=[_synthetic_case(seam_failures=["no_such_seam"])])
    with pytest.raises(ValueError, match="no_such_seam"):
        rules_corpus.load_corpus(seam_root)


@pytest.mark.parametrize(
    ("axis", "item"),
    [
        pytest.param("scenario", "S-ORDER-PRIORITY", id="scenario"),
        pytest.param("effect_type", "block", id="effect-type"),
        pytest.param("delivery", "on_receipt", id="delivery"),
        pytest.param("response_field", "modified_input", id="response-field"),
        pytest.param("response_field", "retry", id="response-field-retry"),
        pytest.param("family", "found_work", id="helper-family"),
        pytest.param("observer", "plan_mode", id="observer"),
        pytest.param("transition", "claim", id="transition"),
    ],
)
def test_coverage_negative_cases(tmp_path: Path, axis: str, item: str) -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    corpus_copy = tmp_path / "rules"
    shutil.copytree(CORPUS_DIR, corpus_copy)
    for case_file in sorted((corpus_copy / "cases").glob("*.json")):
        document = json.loads(case_file.read_text())
        document["cases"] = [case for case in document["cases"] if not _carries(case, axis, item)]
        case_file.write_text(json.dumps(document))
    with pytest.raises(ValueError, match=re.escape(item)):
        rules_corpus.check_coverage(corpus_copy, axis)


@pytest.mark.skipif(os.environ.get(RECORD_ENV) != "1", reason=f"set {RECORD_ENV}=1 to re-record")
def test_rerecord_committed_corpus() -> None:
    """Re-record the committed case and vector expect blocks from the live engine."""
    rules_corpus.record_corpus(CORPUS_DIR)
    rules_vectors.record_vectors(VECTORS_PATH)
    _manifest, cases = rules_corpus.load_corpus(CORPUS_DIR)
    _assert_enough(cases)
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    assert len(vectors) >= MIN_NORMALIZE_VECTORS, (
        f"{len(vectors)} normalizer vectors after re-record; at least {MIN_NORMALIZE_VECTORS} required"
    )

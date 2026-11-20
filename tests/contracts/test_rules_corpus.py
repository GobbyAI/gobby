"""Rule-pass corpus: case schema, coverage, replay, normalizer vectors and name hygiene.

Each case file under ``rules/cases/`` holds ``{"cases": [...]}``. A case carries
``id``, ``kind``, ``provenance``, ``scenario_ids``, optional ``family``, ``observer``
and ``transition`` labels, the synthetic ``rules`` it defines, the ``event`` it fires,
``seam_failures`` and an ``expect`` block. A rule may declare a ``group`` label.
Rule ids start with ``synthetic-``, and no case asserts the behavior of a bundled rule.
"""

from __future__ import annotations

import ast
import collections
import contextlib
import dataclasses
import inspect
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import typing
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

import gobby.utils.machine_id as machine_id
import gobby.utils.spawn as spawn
from gobby.hooks.events import HookResponse
from gobby.storage.tool_results import ToolResultStore
from tests.contracts import rules_corpus, rules_recorder, rules_vectors

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
MANIFEST_PATH = CORPUS_DIR / "manifest.json"
RESPONSE_FIELD_OWNER_KEY = "response_field_owners"
RESPONSE_OWNER_LAYERS = ("adapters", "pipeline", "unrouted", "none")
# The three response fields no rule-pass case can set, each with the layer that covers it.
# This constant is the contract: the manifest repeats it, so a JSON-only edit cannot move a
# field into the rule-pass layer. "none" means no production code writes the field;
# "unrouted" means a production writer exists that no routed adapter reaches.
RESPONSE_FIELD_OWNERS: dict[str, str] = {
    "system_message": "adapters",
    "modify_args": "unrouted",
    "trigger_action": "none",
}
ENGINE_RESPONSE_FIELDS = tuple(
    field for field in RESPONSE_FIELDS if field not in RESPONSE_FIELD_OWNERS
)
# A field whose owner is "none" or "unrouted" has no reachable producer in Python, so no src
# code may store it on a response or pass it to one.
FIELDS_WITHOUT_RESPONSE_WRITER = tuple(
    field for field, owner in RESPONSE_FIELD_OWNERS.items() if owner in ("none", "unrouted")
)
# Write and read forms are templates over the field name, so every field without a response
# writer runs each of them. Each write form names the scan rule that detects it:
# - "attribute_store": an attribute of that name in a store or delete position;
# - "keyword": a keyword argument of that name in any call;
# - "call_literal": the name as a key of a dict literal unpacked into a call, or as the literal
#   name argument of setattr or __setattr__;
# - "string_literal": the name as any other string constant. This rule applies only to a field
#   whose owner is "none": an unrouted field's production capability tables hold its name as a
#   string without writing it.
FIELD_TOKEN = "FIELD_NAME"
STRING_LITERAL_RULE = "string_literal"
UNUSED_FIELD_WRITE_FORMS: dict[str, tuple[str, str]] = {
    "attribute_assign": ("attribute_store", 'resp.FIELD_NAME = "synthetic"\n'),
    "keyword_argument": ("keyword", 'HookResponse(FIELD_NAME="synthetic")\n'),
    "augmented_assign": ("attribute_store", 'resp.FIELD_NAME += "synthetic"\n'),
    "setattr_call": ("call_literal", 'setattr(resp, "FIELD_NAME", "synthetic")\n'),
    "unpacked_keywords": ("call_literal", 'HookResponse(**{"FIELD_NAME": "synthetic"})\n'),
    "tuple_target": ("attribute_store", 'resp.FIELD_NAME, other = "synthetic", 1\n'),
    "for_target": ("attribute_store", 'for resp.FIELD_NAME in ["synthetic"]:\n    pass\n'),
    "indirect_unpacked_keywords": (
        STRING_LITERAL_RULE,
        'kw = {"FIELD_NAME": "synthetic"}\nHookResponse(**kw)\n',
    ),
    "subscript_then_unpack": (
        STRING_LITERAL_RULE,
        'kw = {}\nkw["FIELD_NAME"] = "synthetic"\nHookResponse(**kw)\n',
    ),
    "setattr_bound_name": (
        STRING_LITERAL_RULE,
        'name = "FIELD_NAME"\nsetattr(resp, name, "synthetic")\n',
    ),
    "object_setattr_bound_name": (
        STRING_LITERAL_RULE,
        'name = "FIELD_NAME"\nobject.__setattr__(resp, name, "synthetic")\n',
    ),
    "vars_subscript": (STRING_LITERAL_RULE, 'vars(resp)["FIELD_NAME"] = "synthetic"\n'),
    "tuple_unpack_setattr": (
        STRING_LITERAL_RULE,
        'name, value = "FIELD_NAME", "synthetic"\nsetattr(resp, name, value)\n',
    ),
    "dict_of_pairs": (
        STRING_LITERAL_RULE,
        'HookResponse(**dict([("FIELD_NAME", "synthetic")]))\n',
    ),
    "for_literal_tuple_setattr": (
        STRING_LITERAL_RULE,
        'for name in ("FIELD_NAME",):\n    setattr(resp, name, "synthetic")\n',
    ),
}
UNUSED_FIELD_READS = {
    "attribute_read": "value = resp.FIELD_NAME\n",
    "getattr_literal": 'value = getattr(resp, "FIELD_NAME")\n',
    "name_listing": 'FIELDS = frozenset({"FIELD_NAME"})\n',
    "docstring": 'def f():\n    """Mentions FIELD_NAME."""\n',
}
RELAXING_EDITS = ("move-owned-field-to-required", "relabel-owner-layer")
BUILTIN_SCALAR_NAMES = frozenset({"str", "int", "bool", "bytes"})
BUILTIN_CONTAINER_NAMES = frozenset({"list", "tuple", "dict", "Optional"})


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
# Optional expect keys: skipped_groups is present only on a case whose event skips a declared
# rule group, and eager_variable_deletions only on a case whose pass deletes a variable.
SKIPPED_GROUPS_KEY = "skipped_groups"
VARIABLE_DELETIONS_KEY = "eager_variable_deletions"
OPTIONAL_EXPECT_KEYS = (SKIPPED_GROUPS_KEY, VARIABLE_DELETIONS_KEY)
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
    "S-VAR-SET-NONE",
    "S-VAR-DELETE",
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
    types.update(entry["effect"] for entry in case["expect"]["effect_log"] if "effect" in entry)
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


def _negative_test_functions(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(), filename=str(path))
    relative = path.relative_to(REPO_ROOT).as_posix()
    return {
        f"{relative}::{node.name}": node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and any(word in node.name.lower() for word in NEGATIVE_WORDS)
    }


def _negative_test_names(path: Path) -> set[str]:
    return set(_negative_test_functions(path))


def _parametrize_combinations(function: ast.FunctionDef) -> int:
    """Return how many cases the function's own ``parametrize`` marks generate."""
    combinations = 1
    for decorator in function.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func) == "pytest.mark.parametrize"
        ):
            values = decorator.args[1]
            if not isinstance(values, ast.List | ast.Tuple):
                raise AssertionError(f"{function.name}: parametrize values are not a literal list")
            combinations *= len(values.elts)
    return combinations


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def _callee_name(node: ast.Call) -> str:
    return node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")


def _names_field_in_call(node: ast.Call, field: str) -> bool:
    """True when a call passes ``field`` as a ``**`` dict-literal key or as a setattr name."""
    for keyword in node.keywords:
        if (
            keyword.arg is None
            and isinstance(keyword.value, ast.Dict)
            and any(
                isinstance(key, ast.Constant) and key.value == field for key in keyword.value.keys
            )
        ):
            return True
    return _callee_name(node) in ("setattr", "__setattr__") and any(
        isinstance(arg, ast.Constant) and arg.value == field for arg in node.args
    )


def _exempt_literal_ids(tree: ast.AST) -> set[int]:
    """Ids of string constants that name a field without writing it.

    These are docstrings, elements of a set display that no ``for`` statement or
    comprehension iterates, and the name argument of ``getattr`` or ``hasattr``.
    """
    exempt: set[int] = set()
    iterated: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            iterated.add(id(node.iter))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                exempt.add(id(first.value))
        elif isinstance(node, ast.Set) and id(node) not in iterated:
            exempt.update(id(element) for element in node.elts)
        elif (
            isinstance(node, ast.Call)
            and _callee_name(node) in ("getattr", "hasattr")
            and len(node.args) > 1
        ):
            exempt.add(id(node.args[1]))
    return exempt


def _writers_of(field: str, paths: Iterable[Path] | None = None, *, literals: bool) -> list[str]:
    """Return ``file:line`` for each place that may store ``field`` on an object.

    ``paths`` defaults to every module under ``src/gobby``. Four rules, in one pass per file:
    an attribute named ``field`` in a store or delete position (any target, including tuple,
    ``for`` and ``with`` targets); a keyword argument named ``field`` in any call; ``field`` as
    a key of a dict literal unpacked into a call or as the literal name argument of setattr
    or ``__setattr__``; and, with ``literals``, ``field`` as any other string constant outside
    a docstring, a set display that is not iterated, or a getattr or hasattr name. The last
    rule catches dict keys, subscript stores, a variable or tuple binding of the literal and a
    loop over a literal listing. The scan is static: a name built at run time, a listing held
    in a variable or ``dataclasses.fields`` feeding setattr, and a constructor unpacking
    external data are not seen.
    """
    found: list[str] = []
    sources = sorted((SRC_DIR / "gobby").rglob("*.py")) if paths is None else list(paths)
    for path in sources:
        tree = ast.parse(path.read_text(), filename=str(path))
        location = _display_path(path)
        exempt = _exempt_literal_ids(tree) if literals else set()
        for node in ast.walk(tree):
            if (
                (
                    isinstance(node, ast.Attribute)
                    and node.attr == field
                    and isinstance(node.ctx, ast.Store | ast.Del)
                )
                or (isinstance(node, ast.keyword) and node.arg == field)
                or (isinstance(node, ast.Call) and _names_field_in_call(node, field))
                or (
                    literals
                    and isinstance(node, ast.Constant)
                    and node.value == field
                    and id(node) not in exempt
                )
            ):
                found.append(f"{location}:{getattr(node, 'lineno', 0)}")
    return found


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


def test_effect_type_helper_reads_the_recorder_effect_key() -> None:
    case = _synthetic_case()
    case["expect"]["effect_log"] = [
        {"effect": "run_command", "delivery": "eager", "rule": "synthetic-rule"}
    ]
    types = _effect_types(case)
    assert types == {"block", "run_command"}, f"effect types read from the log: {sorted(types)}"


def test_corpus_has_required_scenarios() -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    _assert_scenarios_present(cases, REQUIRED_SCENARIO_IDS)


def _needs_posix_shell(case: dict[str, Any]) -> bool:
    """True for a case marked ``posix_only``, or a seam caller whose command is a POSIX shell."""
    if case.get("posix_only") is True:
        return True
    command = case.get("input", {}).get("command")
    return (
        case.get("kind") == rules_recorder.CALLER_KIND
        and isinstance(command, list)
        and bool(command)
        and str(command[0]).startswith("/bin/")
    )


def test_python_engine_replays_corpus() -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    if sys.platform == "win32":
        cases = [case for case in cases if not _needs_posix_shell(case)]
    mismatched = [case["id"] for case in cases if rules_corpus.replay_case(case) != case["expect"]]
    assert not mismatched, f"engine output differs from the expect block for: {mismatched}"


@pytest.mark.skipif(
    sys.platform == "win32", reason="the seam caller runs /bin/sh, which Windows lacks"
)
def test_posix_shell_seam_callers_replay() -> None:
    cases = [case for case in _load_committed_cases() if _needs_posix_shell(case)]
    assert cases, "no case is marked posix_only"
    mismatched = [case["id"] for case in cases if rules_corpus.replay_case(case) != case["expect"]]
    assert not mismatched, f"shell seam callers differ from the expect block for: {mismatched}"


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


def _variable_set_none_case() -> dict[str, Any]:
    """Two rules write None: one to a key the session lacks, one over an existing value."""
    return {
        "id": "synthetic-variable-set-none",
        "kind": "rule_pass",
        "provenance": "synthetic",
        "scenario_ids": ["S-VAR-SET-NONE"],
        "family": None,
        "observer": None,
        "transition": None,
        "rules": [
            {
                "name": "synthetic-clear-new",
                "event": "before_tool",
                "priority": 10,
                "effects": [
                    {"type": "set_variable", "variable": "synthetic_cleared", "value": None}
                ],
            },
            {
                "name": "synthetic-clear-existing",
                "event": "before_tool",
                "priority": 20,
                "effects": [
                    {"type": "set_variable", "variable": "synthetic_overwritten", "value": None}
                ],
            },
        ],
        "variables": {"synthetic_overwritten": "before", "synthetic_kept": None},
        "event": {
            "type": "before_tool",
            "data": {"tool_name": "Bash", "tool_input": {"command": "true"}},
            "metadata": {},
        },
        "seam_failures": [],
    }


def _variable_delete_case() -> dict[str, Any]:
    """A turn start with the blocked-turn-end flag set: the engine pops the flag."""
    return {
        "id": "synthetic-variable-delete",
        "kind": "rule_pass",
        "provenance": "synthetic",
        "scenario_ids": ["S-VAR-DELETE"],
        "family": None,
        "observer": None,
        "transition": None,
        "rules": [
            {
                "name": "synthetic-turn-start-observer",
                "event": "before_agent",
                "effects": [{"type": "observe", "category": "synthetic", "message": "turn start"}],
            }
        ],
        "variables": {"_blocked_turn_end_pending": True, "stop_attempts": 2},
        "event": {"type": "before_agent", "data": {"prompt": "synthetic prompt"}, "metadata": {}},
        "seam_failures": [],
    }


def test_variable_delta_records_none_written_to_a_new_key() -> None:
    expect = rules_corpus.replay_case(_variable_set_none_case())
    delta = expect["eager_variable_delta"]
    assert "synthetic_cleared" in delta, f"a None write to a new key is missing from {delta}"
    assert delta["synthetic_cleared"] is None, f"recorded value: {delta['synthetic_cleared']!r}"
    assert delta.get("synthetic_overwritten", "absent") is None, f"overwrite to None: {delta}"
    assert "synthetic_kept" not in delta, f"an unwritten key appears in the delta: {delta}"


def test_variable_delta_records_a_deleted_variable() -> None:
    expect = rules_corpus.replay_case(_variable_delete_case())
    deletions = expect.get(VARIABLE_DELETIONS_KEY)
    assert deletions == ["_blocked_turn_end_pending"], f"recorded deletions: {deletions!r}"
    delta = expect["eager_variable_delta"]
    assert "_blocked_turn_end_pending" not in delta, f"a deleted key appears in the delta: {delta}"
    assert "stop_attempts" not in delta, f"stop_attempts was reset with the flag set: {delta}"


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
    mismatched = _replay_mismatches([vector for vector in vectors if not vector.get("setup")])
    assert not mismatched, f"normalizer output differs for vectors: {mismatched}"


@pytest.mark.skipif(
    sys.platform == "win32", reason="fixture repositories run git init and use POSIX checkouts"
)
def test_python_normalizer_replays_fixture_vectors() -> None:
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    fixtures = [vector for vector in vectors if vector.get("setup")]
    assert fixtures, "no normalizer vector builds a fixture repository"
    mismatched = _replay_mismatches(fixtures)
    assert not mismatched, f"normalizer output differs for fixture vectors: {mismatched}"


def _replay_mismatches(vectors: list[dict[str, Any]]) -> list[str]:
    return [
        vector["id"]
        for vector in vectors
        if rules_corpus.replay_normalizer_vector(vector) != vector["expect"]
    ]


def _required_entries() -> set[str]:
    """Return ``module.function`` for each listed module function another gobby module imports."""
    listed = {entry["module"] for entry in json.loads(MODULES_PATH.read_text())["modules"]}
    imported = _gobby_imported_names()
    return {
        f"{module}.{name}"
        for module in listed
        for name in _module_functions(module) & imported.get(module, set())
    }


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
    required = _required_entries()
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


def _owner_findings(manifest: dict[str, Any]) -> list[str]:
    """Return what the response-field owner check finds wrong with a manifest."""
    owners = manifest.get(RESPONSE_FIELD_OWNER_KEY)
    if not owners:
        return [f"the manifest names no {RESPONSE_FIELD_OWNER_KEY}"]
    findings: list[str] = []
    if owners != RESPONSE_FIELD_OWNERS:
        findings.append(f"manifest owners {owners} differ from the owner constant")
    required = set(manifest["required"]["response_field"])
    if required != set(ENGINE_RESPONSE_FIELDS):
        findings.append(f"required fields differ from the rule-pass set: {sorted(required)}")
    overlap = sorted(required & set(owners))
    if overlap:
        findings.append(f"fields both required and owned: {overlap}")
    if required | set(owners) != set(RESPONSE_FIELDS):
        unowned = sorted(set(RESPONSE_FIELDS) - required - set(owners))
        findings.append(f"fields with no owner: {unowned}")
    unknown = {
        field: owner for field, owner in owners.items() if owner not in RESPONSE_OWNER_LAYERS
    }
    if unknown:
        findings.append(f"unknown owner layers: {unknown}")
    return findings


def test_response_field_owners_cover_every_field_the_rules_cannot_set() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    findings = _owner_findings(manifest)
    assert not findings, "; ".join(findings)
    assert set(RESPONSE_FIELDS) == {field.name for field in dataclasses.fields(HookResponse)}


def _relaxed_manifest(edit: str) -> dict[str, Any]:
    """Return the manifest after a JSON-only edit that moves a field without touching the pins."""
    manifest: dict[str, Any] = json.loads(MANIFEST_PATH.read_text())
    owners = manifest[RESPONSE_FIELD_OWNER_KEY]
    if edit == "move-owned-field-to-required":
        owners.pop("trigger_action")
        manifest["required"]["response_field"].append("trigger_action")
    elif edit == "relabel-owner-layer":
        owners["modify_args"] = "adapters"
    else:
        raise ValueError(f"unknown edit {edit!r}")
    return manifest


@pytest.mark.parametrize("edit", RELAXING_EDITS)
def test_owner_check_rejects_a_json_only_relaxation(edit: str) -> None:
    findings = _owner_findings(_relaxed_manifest(edit))
    assert findings, f"the owner check accepts the relaxed manifest ({edit}) with no finding"


@pytest.mark.parametrize("field", FIELDS_WITHOUT_RESPONSE_WRITER)
def test_fields_without_a_rule_producer_have_no_src_writer(field: str) -> None:
    writers = _writers_of(field, literals=_string_literal_rule_applies(field))
    assert not writers, f"{field} is now written in src at {writers}; give it an owner layer"


def _string_literal_rule_applies(field: str) -> bool:
    """True for a field no production code names as a string, so any string literal counts."""
    return RESPONSE_FIELD_OWNERS[field] == "none"


def test_unused_field_guard_flags_every_write_form(tmp_path: Path) -> None:
    unflagged: list[str] = []
    flagged_reads: list[str] = []
    for field in FIELDS_WITHOUT_RESPONSE_WRITER:
        literals = _string_literal_rule_applies(field)
        for name, (rule, template) in UNUSED_FIELD_WRITE_FORMS.items():
            if rule == STRING_LITERAL_RULE and not literals:
                continue
            fixture = tmp_path / f"write-{field}-{name}.py"
            fixture.write_text(template.replace(FIELD_TOKEN, field))
            if not _writers_of(field, [fixture], literals=literals):
                unflagged.append(f"{field}:{name}")
        for name, template in UNUSED_FIELD_READS.items():
            fixture = tmp_path / f"read-{field}-{name}.py"
            fixture.write_text(template.replace(FIELD_TOKEN, field))
            if _writers_of(field, [fixture], literals=literals):
                flagged_reads.append(f"{field}:{name}")
    assert not unflagged, f"the guard misses these (field, write form) pairs: {unflagged}"
    assert not flagged_reads, f"the guard flags these (field, read form) pairs: {flagged_reads}"


def test_negative_vectors_cover_every_parametrize_combination() -> None:
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    counts: dict[str, int] = {}
    for vector in vectors:
        if vector.get("negative_from"):
            counts[vector["negative_from"]] = counts.get(vector["negative_from"], 0) + 1
    short: dict[str, tuple[int, int]] = {}
    for relative in NEGATIVE_TEST_FILES:
        for key, function in _negative_test_functions(REPO_ROOT / relative).items():
            expected = _parametrize_combinations(function)
            if counts.get(key, 0) != expected:
                short[key] = (counts.get(key, 0), expected)
    assert not short, f"replayed (vectors, combinations) per negative test: {short}"


def test_nested_wrapper_vectors_record_the_scan_budget() -> None:
    key = (
        "tests/workflows/test_command_matching.py::"
        "test_nested_wrappers_with_many_functions_match_blocks"
    )
    vectors = [
        vector
        for vector in json.loads(VECTORS_PATH.read_text())["vectors"]
        if vector.get("negative_from") == key
    ]
    assert vectors, "no nested-wrapper negative vectors"
    for vector in vectors:
        assert vector.get("scan_budget", {}).get("factor") == 96, (
            f"{vector['id']} carries no 96x scan budget"
        )
        assert vector["expect"] == {
            "result": True,
            "scan_within_budget": True,
            "scan_observed": True,
        }, f"{vector['id']} does not record the budget outcome"


def _failed_seam_entry(seam: str, entry: dict[str, Any]) -> bool:
    label = rules_recorder.WRITE_LABELS.get(seam)
    expected = {"write": label} if label else {"seam": seam}
    return entry.get("outcome") == "failed" and all(
        entry.get(key) == value for key, value in expected.items()
    )


def test_every_seam_failure_case_records_a_failed_seam_call() -> None:
    cases = _load_committed_cases()
    _assert_enough(cases)
    unfired = [
        f"{case['id']}:{seam}"
        for case in cases
        for seam in case.get("seam_failures", [])
        if not any(_failed_seam_entry(seam, entry) for entry in case["expect"]["effect_log"])
    ]
    assert not unfired, f"injected seam failures that never fire: {unfired}"


def test_loader_rejects_manifest_seam_without_a_real_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_corpus(tmp_path, cases=[_synthetic_case()])
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["seams"] = [*manifest["seams"], "no_such_seam"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="no_such_seam"):
        rules_corpus.load_corpus(tmp_path)
    attribute_root = tmp_path / "attribute"
    _write_corpus(attribute_root, cases=[_synthetic_case()])
    real_registry = rules_recorder.seam_registry

    def registry_with_missing_attribute() -> dict[str, tuple[object, str]]:
        return {
            **real_registry(),
            "tool_results": (ToolResultStore, "no_such_method"),
        }

    monkeypatch.setattr(rules_recorder, "seam_registry", registry_with_missing_attribute)
    with pytest.raises(ValueError, match="no_such_method"):
        rules_corpus.load_corpus(attribute_root)


def test_bundled_loader_accepts_file_level_group_and_audience(tmp_path: Path) -> None:
    grouped = tmp_path / "grouped.yaml"
    grouped.write_text(
        yaml.safe_dump(
            {
                "group": "synthetic-group",
                "audience": "synthetic-audience",
                "sources": ["claude"],
                "rules": {
                    "synthetic-rule": {
                        "event": "before_tool",
                        "effects": [{"type": "block", "reason": "synthetic"}],
                    }
                },
            }
        )
    )
    body = rules_corpus.load_rule_body(grouped)["rules"]["synthetic-rule"]["body"]
    assert body.get("group") == "synthetic-group", f"file-level group lost: {body}"
    assert body.get("audience") == "synthetic-audience", f"file-level audience lost: {body}"
    stray = tmp_path / "stray.yaml"
    stray.write_text(yaml.safe_dump({"bogus_top_level": 1, "rules": {"synthetic-rule": {}}}))
    with pytest.raises(ValueError, match="bogus_top_level"):
        rules_corpus.load_rule_body(stray)


def test_recorder_scrubs_windows_checkout_paths() -> None:
    text = rules_recorder._scrub_text(r"opened C:\Users\runner\work\repo\app.py now")
    assert "runner" not in text and "<path>" in text, text


_SCRUB_MARKERS = frozenset({"<uuid>", "<timestamp>", "<token>", "<path>", "<volatile>"})


def _string_leaves(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item)


def test_expects_name_no_bare_type_placeholder() -> None:
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    recorded = [(vector["id"], vector["expect"]) for vector in vectors]
    recorded += [(case["id"], case["expect"]) for case in _load_committed_cases()]
    bare = sorted(
        f"{name}: {leaf}"
        for name, expect in recorded
        for leaf in _string_leaves(expect)
        if re.fullmatch(r"<[A-Za-z_]\w*>", leaf) and leaf not in _SCRUB_MARKERS
    )
    assert not bare, f"expects that pin a bare type name instead of their output: {bare}"


def test_normalizer_replay_fails_loudly_on_a_missing_entry_point() -> None:
    vector = {
        "id": "probe-missing-entry",
        "entry": "gobby.hooks.checkout_reads.no_such_function_for_test",
        "input": {"args": [], "kwargs": {}},
    }
    with pytest.raises(AttributeError):
        rules_vectors.replay_vector(vector)


def _is_builtin_annotation(node: ast.expr | None) -> bool:
    """True for str, int, bool and bytes, list, tuple, dict or Optional of those, and X | None."""
    if isinstance(node, ast.Name):
        return node.id in BUILTIN_SCALAR_NAMES
    if isinstance(node, ast.Constant):
        return node.value is None or node.value is Ellipsis
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_builtin_annotation(node.left) and _is_builtin_annotation(node.right)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id not in BUILTIN_CONTAINER_NAMES:
            return False
        inner = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        return all(_is_builtin_annotation(item) for item in inner)
    return False


def _builtin_signature_entries() -> list[str]:
    """Return ``module.function`` for each listed module-level function with builtin parameters.

    A function with no parameters is left out: it takes no input that could be non-empty.
    """
    entries: list[str] = []
    for listed in json.loads(MODULES_PATH.read_text())["modules"]:
        module = listed["module"]
        path = SRC_DIR / (module.replace(".", "/") + ".py")
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            params = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            params = [param for param in params if param.arg not in ("self", "cls")]
            if params and all(_is_builtin_annotation(param.annotation) for param in params):
                entries.append(f"{module}.{node.name}")
    return entries


# Keys a vector's expect block may carry beside "result" when replay records more than the
# return value: argument mutations, a scan budget outcome, node requests and values read
# inside an entered context.
ENVELOPE_MARKERS = frozenset(
    {"mutated_args", "scan_within_budget", "scan_observed", "node_requests", "within"}
)


def _is_envelope(expect: Any) -> bool:
    return (
        isinstance(expect, dict)
        and "result" in expect
        and bool(set(expect) & ENVELOPE_MARKERS)
        and set(expect) <= {"result"} | ENVELOPE_MARKERS
    )


def _vector_result(expect: Any) -> Any:
    """Return the recorded result of a vector: the envelope's ``result``, else the expect."""
    return expect["result"] if _is_envelope(expect) else expect


def _vector_mutations(expect: Any) -> dict[str, Any]:
    """Return the envelope's ``mutated_args`` (argument index or keyword to value), else {}."""
    if not _is_envelope(expect):
        return {}
    mutated = expect.get("mutated_args") or {}
    return dict(mutated) if isinstance(mutated, dict) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _vector_inputs(vector: dict[str, Any]) -> list[Any]:
    arguments = vector.get("input", {})
    return [*arguments.get("args", []), *arguments.get("kwargs", {}).values()]


def _is_raised(expect: Any) -> bool:
    return isinstance(expect, dict) and "raised" in expect


def _result_differs_from_inputs(vector: dict[str, Any]) -> bool:
    result = _canonical(_vector_result(vector.get("expect")))
    return all(result != _canonical(value) for value in _vector_inputs(vector))


def _result_argument_changed(vector: dict[str, Any]) -> bool:
    """True when a ``result_from_arg`` vector records that argument changed by the call."""
    index = vector.get("result_from_arg")
    args = vector.get("input", {}).get("args", [])
    if not isinstance(index, int) or not 0 <= index < len(args):
        return False
    return _canonical(_vector_result(vector.get("expect"))) != _canonical(args[index])


def _is_substantive(vector: dict[str, Any]) -> bool:
    """A vector counts when its input is non-empty, it did not raise, and it shows the call.

    The call shows when the recorded result differs from every input argument, an argument
    mutation is recorded, or a ``result_from_arg`` argument differs from its input.
    """
    expect = vector.get("expect")
    if not any(bool(value) for value in _vector_inputs(vector)) or _is_raised(expect):
        return False
    return (
        _result_differs_from_inputs(vector)
        or bool(_vector_mutations(expect))
        or _result_argument_changed(vector)
    )


def test_every_builtin_signature_function_has_a_substantive_vector() -> None:
    entries = _builtin_signature_entries()
    assert entries, "no listed normalizer function has an all-builtin signature"
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    substantive = {vector["entry"] for vector in vectors if _is_substantive(vector)}
    missing = sorted(entry for entry in entries if entry not in substantive)
    assert not missing, f"all-builtin functions with no substantive vector: {missing}"


def test_no_two_combinations_of_one_test_replay_the_same_input() -> None:
    """Two vectors of one negative test may not replay the same input.

    Two different tests may share an input, since each needs its own vector for coverage.
    """
    vectors = json.loads(VECTORS_PATH.read_text())["vectors"]
    groups: dict[str, list[str]] = {}
    for vector in vectors:
        key = json.dumps(
            [
                vector.get("negative_from"),
                vector["entry"],
                vector.get("input"),
                vector.get("setup"),
                vector.get("construct"),
                vector.get("prepare"),
                vector.get("scope"),
                vector.get("within"),
                vector.get("result_from_arg"),
            ],
            sort_keys=True,
        )
        groups.setdefault(key, []).append(vector["id"])
    repeated = [ids for ids in groups.values() if len(ids) > 1]
    assert not repeated, f"combinations that replay the same input: {repeated}"


NORMALIZE_TOOL_FIELDS_ENTRY = "gobby.hooks._normalization_tools.normalize_tool_fields"
NAVIGATION_GATE_ENTRY = "gobby.hooks.code_navigation_recovery.navigation_requires_index"
REFUSED_OUTLINE_GATE_ID = "gate-refused-outline-broad-read"
REFUSED_OUTLINE_TEST = (
    "tests/hooks/test_code_navigation_recovery.py::test_refused_outline_opens_only_requested_read"
)
OTHER_PATH_READ_COMMAND = "cat src/other.py"
LIVE_TASK_REFERENCE = re.compile(r"(?:gobby-)?#\d{3,}")
CONSTRUCT_SOURCES = ("data", "args", "items", "from")
DERIVATION_KEYS = frozenset({"entry", "args", "kwargs", "pick"})
EXEMPTION_REASON_CODES = frozenset({"identity-keyed-argument", "argument-independent-result"})
# Entries the discrimination rules skip, each with a reason code and a cite. The set is closed:
# an entry keyed by token identity cannot carry its argument in JSON and keeps a caller vector
# through normalize_tool_fields; an argument-independent entry keeps its own committed vector.
UNDISCRIMINATED_ENTRIES: dict[str, str] = {
    "gobby.hooks._normalization_shell_stdin.shell_stdin_arguments": (
        "identity-keyed-argument: raw is keyed by id() of each token "
        "(_normalization_shell_stdin.py:67, :153)"
    ),
    "gobby.hooks._normalization_compound.case_arm_tokens": (
        "identity-keyed-argument: spans are keyed by id() of each token "
        "(_normalization_canonical.py:238)"
    ),
    "gobby.hooks._normalization_compound.case_pattern_process_bodies": (
        "identity-keyed-argument: spans are keyed by id() of each token "
        "(_normalization_canonical.py:238)"
    ),
    "gobby.hooks._normalization_compound.process_substitution_bodies": (
        "identity-keyed-argument: spans are keyed by id() of each token "
        "(_normalization_canonical.py:238)"
    ),
    "gobby.hooks._normalization_metadata._unknown_write_metadata": (
        "argument-independent-result: takes no parameters (_normalization_metadata.py:122)"
    ),
    "gobby.workflows.engine.command_matching_variables.initial_names": (
        "argument-independent-result: takes no parameters (command_matching_variables.py:40)"
    ),
    "gobby.hooks.code_navigation.search_navigation_metadata": (
        "argument-independent-result: ignores its paths argument (code_navigation.py:245)"
    ),
}
# A writer that returns None and records its change in the first argument.
MUTATING_WRITER_VECTOR: dict[str, Any] = {
    "id": "synthetic-mutating-writer",
    "entry": "gobby.hooks._normalization_tool_input.mark_tool_input_unavailable",
    "input": {
        "args": [{"tool_name": "Bash", "tool_input": "{not json"}, "tool_input", "invalid_json"],
        "kwargs": {},
    },
}


def _committed_vectors() -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = json.loads(VECTORS_PATH.read_text())["vectors"]
    return vectors


def _returns_none(function: Callable[..., Any]) -> bool:
    """True when the evaluated return annotation is None.

    Modules with postponed annotations store the annotation as text, so the hints are evaluated;
    a module whose hints do not evaluate falls back to the signature's text.
    """
    try:
        return typing.get_type_hints(function).get("return") is type(None)
    except (NameError, TypeError, AttributeError):
        annotation = inspect.signature(function).return_annotation
        return annotation is None or annotation == "None"


def _discrimination_findings(
    vectors: list[dict[str, Any]], entries: Iterable[str]
) -> dict[str, str]:
    """Return, per entry, why a constant, identity or no-op stub would replay its vectors.

    An entry is discriminating when its non-raised vectors record two distinct outcomes, one of
    them differs from every input argument or records a mutation, and an entry whose return is
    None records the change it makes to an argument.
    """
    by_entry: dict[str, list[dict[str, Any]]] = {}
    for vector in vectors:
        by_entry.setdefault(vector["entry"], []).append(vector)
    findings: dict[str, str] = {}
    for entry in sorted(entries):
        if entry in UNDISCRIMINATED_ENTRIES:
            continue
        recorded = [
            vector for vector in by_entry.get(entry, []) if not _is_raised(vector["expect"])
        ]
        reasons: list[str] = []
        if len({_canonical(vector["expect"]) for vector in recorded}) < 2:
            reasons.append("fewer than two distinct outcomes")
        mutated = any(_vector_mutations(vector["expect"]) for vector in recorded)
        if not mutated and not any(_result_differs_from_inputs(vector) for vector in recorded):
            reasons.append("every outcome equals an input argument")
        if _returns_none(rules_vectors.resolve_entry(entry)) and not any(
            _vector_mutations(vector["expect"]) or _result_argument_changed(vector)
            for vector in recorded
        ):
            reasons.append("returns None and records no argument change")
        if reasons:
            findings[entry] = "; ".join(reasons)
    return findings


def test_vector_replay_records_a_mutated_argument() -> None:
    replayed = rules_vectors.replay_vector(MUTATING_WRITER_VECTOR)
    mutated = _vector_mutations(replayed).get("0")
    assert isinstance(mutated, dict), f"replay records no change to argument 0: {replayed!r}"
    assert "tool_input" not in mutated, f"the unusable tool_input is kept: {mutated}"
    assert "tool_input_error" in mutated, f"no tool_input_error marker recorded: {mutated}"


def test_every_vectored_entry_is_discriminating() -> None:
    vectors = _committed_vectors()
    entries = _required_entries() | {vector["entry"] for vector in vectors}
    findings = _discrimination_findings(vectors, entries)
    assert not findings, (
        f"{len(findings)} of {len(entries)} vectored entries would replay unchanged under a "
        f"constant, identity or no-op stub: {findings}"
    )


def _setup_less_vectors() -> list[dict[str, Any]]:
    return [
        vector
        for vector in _committed_vectors()
        if not vector.get("setup") and not vector.get("scope")
    ]


class _CountingEnviron(collections.UserDict[str, str]):
    """A process environment stand-in that logs every key read (``get`` reads through both)."""

    def __init__(self, data: Mapping[str, str], hits: list[str]) -> None:
        super().__init__(data)
        self.hits = hits

    def __getitem__(self, key: str) -> str:
        self.hits.append(f"environ[{key}]")
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self.hits.append(f"environ contains {key}")
        return super().__contains__(key)


HOST_READ_TARGETS: tuple[tuple[object, str], ...] = (
    (pathlib.Path, "resolve"),
    (pathlib.Path, "exists"),
    (pathlib.Path, "is_dir"),
    (pathlib.Path, "is_file"),
    (pathlib.Path, "stat"),
    (pathlib.Path, "read_text"),
    (pathlib.Path, "read_bytes"),
    (pathlib.Path, "cwd"),
    (pathlib.Path, "home"),
    (pathlib.Path, "expanduser"),
    (os.path, "realpath"),
    (os.path, "exists"),
    (os.path, "isdir"),
    (os.path, "isfile"),
    (os.path, "expanduser"),
    (os, "getcwd"),
    (subprocess, "run"),
    (subprocess, "Popen"),
    (spawn, "run"),
    (machine_id, "get_machine_id"),
    (tempfile, "gettempdir"),
)


def _spy(label: str, original: Callable[..., Any], hits: list[str]) -> Callable[..., Any]:
    def spy(*args: Any, **kwargs: Any) -> Any:
        hits.append(label)
        return original(*args, **kwargs)

    return spy


def _install_host_spies(stack: contextlib.ExitStack, hits: list[str]) -> None:
    """Wrap each host read with a call-through spy, including every by-name module binding."""
    loaded = [
        module
        for name, module in list(sys.modules.items())
        if name.startswith("gobby") and module is not None
    ]
    for owner, attribute in HOST_READ_TARGETS:
        label = f"{getattr(owner, '__name__', owner)}.{attribute}"
        if isinstance(owner, type):
            static = inspect.getattr_static(owner, attribute)
            if isinstance(static, classmethod):
                bound = getattr(owner, attribute)
                replacement: Any = staticmethod(_spy(label, bound, hits))
            else:
                replacement = _spy(label, static, hits)
            stack.enter_context(mock.patch.object(owner, attribute, replacement))
            continue
        original = getattr(owner, attribute)
        spy = _spy(label, original, hits)
        stack.enter_context(mock.patch.object(owner, attribute, spy))
        for module in loaded:
            for name, value in list(vars(module).items()):
                if value is original:
                    stack.enter_context(mock.patch.object(module, name, spy))


def _import_vector_names(vectors: list[dict[str, Any]]) -> None:
    """Import every entry, prepare step and constructed class, so imports read nothing later."""
    for vector in vectors:
        rules_vectors.resolve_entry(vector["entry"])
        for name in vector.get("prepare", []):
            rules_vectors.resolve_entry(name)
        for spec in vector.get("construct", []):
            rules_vectors.resolve_entry(spec["class"])


def _host_read_summary(hits: list[str]) -> str:
    """Name each spied read once; environment reads are counted, not listed by key."""
    reads = sorted({hit for hit in hits if not hit.startswith("environ")})
    environment = {hit for hit in hits if hit.startswith("environ")}
    if environment:
        reads.append(f"environ: {len(environment)} keys")
    return ", ".join(reads)


def test_setup_less_vectors_replay_without_host_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vectors = _setup_less_vectors()
    assert vectors, "no setup-less normalizer vector"
    _import_vector_names(vectors)
    home = tmp_path / "home"
    home.mkdir()
    empty_cwd = tmp_path / "cwd"
    empty_cwd.mkdir()
    monkeypatch.chdir(empty_cwd)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    offenders: list[str] = []
    for vector in vectors:
        machine_id.clear_cache()
        hits: list[str] = []
        with contextlib.ExitStack() as stack:
            _install_host_spies(stack, hits)
            stack.enter_context(
                mock.patch.object(os, "environ", _CountingEnviron(dict(os.environ), hits))
            )
            replayed = rules_vectors.replay_vector(vector)
        if hits or replayed != vector["expect"]:
            differs = "" if replayed == vector["expect"] else ", output differs"
            offenders.append(f"{vector['id']} ({_host_read_summary(hits)}{differs})")
    assert not offenders, f"{len(offenders)} setup-less vectors read the host: {offenders}"


def _fixture_vectors() -> list[dict[str, Any]]:
    return [vector for vector in _committed_vectors() if vector.get("setup")]


@pytest.mark.skipif(
    sys.platform == "win32", reason="fixture repositories run git init and use POSIX checkouts"
)
def test_fixture_vectors_record_paths_relative_to_the_fixture() -> None:
    vectors = _fixture_vectors()
    assert vectors, "no normalizer vector builds a fixture repository"
    opaque = sorted(
        {
            vector["id"]
            for vector in vectors
            for leaf in _string_leaves(vector["expect"])
            if "<path>" in leaf
        }
    )
    assert not opaque, f"fixture vectors that record an opaque <path> marker: {opaque}"


def _gate_variables(vector: dict[str, Any]) -> Any:
    args = vector.get("input", {}).get("args", [])
    return args[1] if len(args) > 1 else None


def _gate_command(vector: dict[str, Any]) -> Any:
    args = vector.get("input", {}).get("args", [])
    event = args[0] if args else None
    tool_input = event.get("tool_input") if isinstance(event, dict) else None
    return tool_input.get("command") if isinstance(tool_input, dict) else None


def test_refused_outline_other_path_read_is_replayed() -> None:
    vectors = _committed_vectors()
    gate = next((vector for vector in vectors if vector["id"] == REFUSED_OUTLINE_GATE_ID), None)
    assert gate is not None, f"no {REFUSED_OUTLINE_GATE_ID} vector"
    variables = _gate_variables(gate)
    replayed = [
        vector["id"]
        for vector in vectors
        if vector["entry"] == NAVIGATION_GATE_ENTRY
        and _gate_command(vector) == OTHER_PATH_READ_COMMAND
        and _gate_variables(vector) == variables
        and _vector_result(vector["expect"]) is True
    ]
    assert replayed, (
        f"no vector replays {OTHER_PATH_READ_COMMAND!r} through the refused-outline gate state"
    )


def _tracker_state(refusal: dict[str, Any]) -> dict[str, Any] | None:
    """The gate variables a refusal vector's recorded event implies."""
    recorded = _vector_result(refusal["expect"])
    if not isinstance(recorded, dict):
        return None
    return {
        "code_index_attempts": recorded.get("canonical_code_index_attempts"),
        "code_index_verified": recorded.get("canonical_code_index_verified", []),
        "code_index_recoveries": recorded.get("canonical_code_index_recovery", []),
    }


@pytest.mark.skipif(
    sys.platform == "win32", reason="fixture repositories run git init and use POSIX checkouts"
)
def test_gate_vectors_carry_the_refusal_tracker_state() -> None:
    vectors = _committed_vectors()
    refusals = [vector for vector in vectors if vector.get("negative_from") == REFUSED_OUTLINE_TEST]
    gates = [vector for vector in vectors if vector["id"].startswith("gate-")]
    assert refusals and gates, f"refusal vectors {len(refusals)}, gate vectors {len(gates)}"
    mismatched = [
        f"{gate['id']} against {refusal['id']}: gate {_gate_variables(gate)}, "
        f"recorded {_tracker_state(refusal)}"
        for gate in gates
        for refusal in refusals
        if _gate_variables(gate) != _tracker_state(refusal)
    ]
    assert not mismatched, f"gate state differs from the refusal record: {mismatched}"


def test_committed_corpus_names_no_live_task_reference() -> None:
    documents = [VECTORS_PATH, *sorted((CORPUS_DIR / "cases").glob("*.json"))]
    found = sorted(
        f"{path.name}: {leaf}"
        for path in documents
        for leaf in _string_leaves(json.loads(path.read_text()))
        if LIVE_TASK_REFERENCE.search(leaf)
    )
    assert not found, f"committed corpus strings that name a live task: {found}"


def _derivation_trees(value: Any) -> Iterable[Any]:
    """Yield every ``{"from": ...}`` tree in an input value, at any depth."""
    if isinstance(value, dict):
        if list(value) == ["from"]:
            yield value["from"]
            return
        for item in value.values():
            yield from _derivation_trees(item)
    elif isinstance(value, list):
        for item in value:
            yield from _derivation_trees(item)


def _derivation_findings(label: str, tree: Any) -> list[str]:
    """Check a derivation tree's shape and entry names without calling any of them."""
    if not isinstance(tree, dict):
        return [f"{label}: derivation {tree!r} is not a mapping"]
    findings: list[str] = []
    extra = sorted(set(tree) - DERIVATION_KEYS)
    if extra:
        findings.append(f"{label}: derivation keys outside {sorted(DERIVATION_KEYS)}: {extra}")
    entry = tree.get("entry")
    try:
        rules_vectors.resolve_entry(str(entry))
    except (ImportError, AttributeError, ValueError):
        findings.append(f"{label}: derivation entry {entry!r} does not resolve")
    for item in [*tree.get("args", []), *tree.get("kwargs", {}).values()]:
        for nested in _derivation_trees(item):
            findings.extend(_derivation_findings(label, nested))
    return findings


def test_domain_vectors_reach_their_function() -> None:
    findings: list[str] = []
    for vector in _committed_vectors():
        label = vector["id"]
        for spec in vector.get("construct", []):
            sources = [key for key in CONSTRUCT_SOURCES if spec.get(key)]
            if len(sources) != 1:
                findings.append(f"{label}: construct {spec.get('index')} has sources {sources}")
            if "from" in spec:
                findings.extend(_derivation_findings(label, spec["from"]))
        if _is_raised(vector["expect"]) and not vector.get("metadata", {}).get("precondition"):
            findings.append(f"{label}: records {vector['expect']} with no metadata.precondition")
        for tree in _derivation_trees(vector.get("input", {})):
            findings.extend(_derivation_findings(label, tree))
    assert not findings, f"{len(findings)} vector rows do not reach their function: {findings}"


def test_vector_exemption_set_is_closed_with_caller_vectors() -> None:
    assert set(UNDISCRIMINATED_ENTRIES) == {
        "gobby.hooks._normalization_shell_stdin.shell_stdin_arguments",
        "gobby.hooks._normalization_compound.case_arm_tokens",
        "gobby.hooks._normalization_compound.case_pattern_process_bodies",
        "gobby.hooks._normalization_compound.process_substitution_bodies",
        "gobby.hooks._normalization_metadata._unknown_write_metadata",
        "gobby.workflows.engine.command_matching_variables.initial_names",
        "gobby.hooks.code_navigation.search_navigation_metadata",
    }, f"the exemption set changed: {sorted(UNDISCRIMINATED_ENTRIES)}"
    malformed = sorted(
        entry
        for entry, reason in UNDISCRIMINATED_ENTRIES.items()
        if reason.partition(": ")[0] not in EXEMPTION_REASON_CODES or not reason.partition(": ")[2]
    )
    assert not malformed, f"exemption reasons without a known code and a cite: {malformed}"
    vectors = _committed_vectors()
    by_id = {vector["id"]: vector for vector in vectors}
    normalize = rules_vectors.resolve_entry(NORMALIZE_TOOL_FIELDS_ENTRY)
    uncovered: list[str] = []
    for entry, reason in sorted(UNDISCRIMINATED_ENTRIES.items()):
        if reason.startswith("identity-keyed-argument: "):
            caller = by_id.get(f"caller-{entry.rsplit('.', 1)[-1]}")
            if (
                caller is None
                or not caller.get("setup")
                or rules_vectors.resolve_entry(caller["entry"]) is not normalize
            ):
                uncovered.append(f"{entry}: no caller fixture vector through normalize_tool_fields")
        elif not any(vector["entry"] == entry for vector in vectors):
            uncovered.append(f"{entry}: no vector")
    assert not uncovered, f"exempt entries without the vector their reason requires: {uncovered}"

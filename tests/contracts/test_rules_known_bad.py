"""Known-bad registry: Python engine flaws the Rust port must not reproduce.

Each row of ``rules_known_bad.json`` names an engine entry, an input on which Python gives a
wrong value (``python_observed``), the value Rust must give (``correct``), and a control input
on which Python is right. The tests reproduce every row and control in Python, tie each
corpus case and normalizer vector that reaches a row's flaw to the row (the ``known_bad``
tag and its ``known_bad_expect`` declarations), and keep the registry's scope to engine
entries. Every definition the tests seed is synthetic.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import functools
import hashlib
import json
import os
import re
import tempfile
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.services.result_handling import apply_before_tool_enforcement
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.sessions import SessionManager
from gobby.utils import project_context
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_context import snapshot_evaluation_context
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.safe_evaluator import build_condition_helpers
from tests.contracts import (
    adapters_corpus,
    pipeline_corpus,
    rules_corpus,
    rules_paths,
    rules_recorder,
    rules_seed,
    rules_vectors,
)

CONTRACTS_DIR = Path(__file__).resolve().parent
REPO = CONTRACTS_DIR.parents[1]
REGISTRY_PATH = CONTRACTS_DIR / "rules_known_bad.json"
VECTORS_PATH = CONTRACTS_DIR / "rules" / "normalize_vectors.json"
VECTOR_MODULES_PATH = CONTRACTS_DIR / "rules" / "normalize_modules.json"
S2_11_PATH = REPO / ".gobby" / "plans" / "s2-11-hub-hooks-rules.md"
RECORD_ENV = "GOBBY_CORPUS_RECORD"
SCHEMA_VERSION = 1

REQUIRED_ROW_KEYS = (
    "id",
    "ref",
    "entry",
    "input",
    "field",
    "python_observed",
    "correct",
    "control",
    "dependent_paths",
)
ENGINE_ENTRY_PREFIXES = ("gobby.hooks.", "gobby.workflows.")
ENGINE_ENTRY_EXCEPTIONS: Mapping[str, str] = {
    "gobby.mcp_proxy.services.result_handling.build_before_tool_event": (
        "builds the before_tool event the rule engine evaluates for a direct MCP call; "
        "addendum 2026-11-19 15:32"
    ),
}
PROXY_ROUTE_FORMS = ("admitted_by_hook", "no_prior_admission")
ROW_ACCEPTS = frozenset({"not_reevaluated"})
PROXY_ROW_RULE: Mapping[str, Any] = {
    "name": "synthetic-kb-parent-only",
    "event": "before_tool",
    "when": "not event.data.get('agent_id')",
    "effects": [{"type": "block", "reason": "synthetic parent-only refusal"}],
}
PROXY_FIELD_KEYS = ("duplicate", "agent_id_present", "agent_id", "decision")
S2_11_PROXY_PHRASES = (
    "unfiled:proxy-duplicate-before-tool-agent-identity",
    "inherits the agent identity of the admitted call or is not evaluated a second time",
    "the pending tool context keeps the agent id of the admitted call",
)
KNOWN_BAD_FIXTURE_SETUP: Mapping[str, Any] = {
    "git_repo": True,
    "files": {"src/fixture.py": {"repeat": "value = 1\n", "count": 60}, "sub/.keep": ""},
}
CLASSIFICATIONS = frozenset({"contains", "extends", "unpinned", "unchanged"})
TOP_LEVEL_LOG_PATHS = ("effect_log", "audit_records")
TOP_LEVEL_LAST_PASS_PATHS = ("response", "outcome_class")
RAISED_FIELD = "raised"
FROZEN_FORM = "frozen_event"
LIVE_FORM = "live_event"
VECTOR_REASON = "replays a known-bad row input; the expect is the Python value"

PROXY_SERVER = "synthetic-server"
PROXY_TOOL = "synthetic-tool"
PROXY_ARGUMENTS: Mapping[str, Any] = {"value": 1}
PROXY_EXTERNAL_ID = "synthetic-external-1"
FIXED_TIMESTAMP = datetime(2026, 11, 19, tzinfo=UTC)

# Layers whose committed cases are replayed to find the cases a row's flaw reaches.
CASE_LAYERS: Mapping[str, tuple[Path, Callable[[dict[str, Any]], Any]]] = {
    "rules": (rules_corpus.CORPUS_DIR, rules_corpus.replay_case),
    "adapters": (adapters_corpus.CORPUS_DIR, adapters_corpus.replay_case),
    "pipeline": (pipeline_corpus.CORPUS_DIR, pipeline_corpus.replay_case),
}


# ---------------------------------------------------------------------------------------------
# Registry access
# ---------------------------------------------------------------------------------------------


def _registry() -> dict[str, Any]:
    assert REGISTRY_PATH.is_file(), f"{REGISTRY_PATH.name} is missing"
    document = json.loads(REGISTRY_PATH.read_text())
    assert isinstance(document, dict), f"{REGISTRY_PATH.name} is not a JSON object"
    return document


def _rows() -> list[dict[str, Any]]:
    rows = _registry().get("rows")
    assert isinstance(rows, list) and rows, f"{REGISTRY_PATH.name} holds no rows"
    return rows


def _row_id(row: Mapping[str, Any]) -> str:
    """The stable tag value of a row: a digest of entry, input form, input and field."""
    parts = [row["entry"], row.get("input_form", ""), row["input"], row["field"]]
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:12]
    return "known-bad-" + digest


def _control_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The row as its control runs it: the control's input and input form."""
    control = row["control"]
    merged = {key: value for key, value in row.items() if key != "input_form"}
    merged["input"] = control["input"]
    if "input_form" in control:
        merged["input_form"] = control["input_form"]
    return merged


def _resolve(entry: str) -> Any:
    return rules_vectors.resolve_entry(entry)


def _is_condition_helper(entry: str) -> bool:
    function = _resolve(entry)
    return any(helper is function for helper in build_condition_helpers().values())


def _vector_modules() -> set[str]:
    document = json.loads(VECTOR_MODULES_PATH.read_text())
    return {item["module"] for item in document["modules"]}


def _vector_selected(row: Mapping[str, Any]) -> bool:
    """A row gets a known-bad vector when its entry's module is vectored and it has no form."""
    module = row["entry"].rpartition(".")[0]
    return "input_form" not in row and module in _vector_modules()


def _vector_id(row: Mapping[str, Any]) -> str:
    return "known-bad-vector-" + _row_id(row).removeprefix("known-bad-")


# ---------------------------------------------------------------------------------------------
# Row values
# ---------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _fixture_repo() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="gobby-known-bad-") as scratch:
        root = Path(scratch).resolve() / "repo"
        root.mkdir()
        rules_vectors.materialize(dict(KNOWN_BAD_FIXTURE_SETUP), root)
        yield root


def _raised_value(entry: str, text: str, form: str | None) -> str | None:
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=rules_seed.SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=FIXED_TIMESTAMP,
        data={"tool_name": "Bash", "tool_input": {"command": text}},
        metadata={"_platform_session_id": rules_seed.SESSION_ID},
    )
    if form == FROZEN_FORM:
        event = snapshot_evaluation_context({"event": event})["event"]
    try:
        _resolve(entry)(event)
    except Exception as exc:
        return type(exc).__name__
    return None


def _field_value(field: str, result: Any, first_argument: Any) -> Any:
    if field == "result":
        return result
    if field == "count":
        return len(result)
    if isinstance(first_argument, Mapping):
        return first_argument.get(field)
    return None


def _row_value(row: Mapping[str, Any], text: str) -> object:
    """Python's value of the row's ``field`` for ``text``, read as the row's entry gives it."""
    form = row.get("input_form")
    if form in PROXY_ROUTE_FORMS:
        admitted = text if form == "admitted_by_hook" else None
        observation = _proxy_before_tool_observation(admitted)
        return {key: observation[key] for key in PROXY_FIELD_KEYS}
    entry = row["entry"]
    field = row["field"]
    if field == RAISED_FIELD:
        return _raised_value(entry, text, form)
    function = _resolve(entry)
    if entry.endswith(".normalize_tool_fields"):
        with _fixture_repo() as root:
            data = {
                "tool_name": "Bash",
                "tool_input": {"command": text},
                "cwd": str(root),
                "project_path": str(root),
            }
            result = function(data)
            return _field_value(field, result, data)
    if entry.endswith(".blocks_direct_provider_launch"):
        return _field_value(field, function("Bash", {"command": text}), None)
    return _field_value(field, function(text), None)


# ---------------------------------------------------------------------------------------------
# The proxy's duplicate before_tool route
# ---------------------------------------------------------------------------------------------


def _route_decision(error: Mapping[str, Any] | None, outcome: str | None) -> str | None:
    if outcome == "policy_denied":
        return "block"
    if error is None:
        return "allow"
    return outcome


async def _observe_proxy_route(
    admitted_agent_id: str | None, session_source: str
) -> dict[str, object]:
    with rules_recorder._hub_database() as db, tempfile.TemporaryDirectory() as root:
        rules_recorder._seed_rules(db, [dict(PROXY_ROW_RULE)])
        rules_seed.seed_identity(db)
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (rules_seed.SYNTHETIC_PROJECT_ID, rules_seed.SYNTHETIC_PROJECT_NAME),
        )
        sessions = SessionManager(db)
        session_id = sessions.register_session(
            external_id=PROXY_EXTERNAL_ID,
            machine_id=rules_seed.SYNTHETIC_MACHINE_ID,
            source=session_source,
            project_id=rules_seed.SYNTHETIC_PROJECT_ID,
        )
        LocalProjectCheckoutManager(db).register(
            rules_seed.SYNTHETIC_MACHINE_ID, rules_seed.SYNTHETIC_PROJECT_ID, root
        )
        token = project_context.set_project_context(
            {"id": rules_seed.SYNTHETIC_PROJECT_ID, "project_path": root}
        )
        runtime = WorkflowEvaluationRuntime()
        try:
            handler = WorkflowHookHandler(
                timeout=0,
                enabled=True,
                rule_engine=RuleEngine(db),
                evaluation_runtime=runtime,
                config=None,
                terminal_manager=None,
            )
            real_evaluate = handler.evaluate
            seen: list[HookEvent] = []

            def recording_evaluate(event: HookEvent, *args: Any, **kwargs: Any) -> Any:
                seen.append(event)
                return real_evaluate(event, *args, **kwargs)

            admission_event: HookEvent | None = None
            admission: str | None = None
            if admitted_agent_id is not None:
                admission_event = HookEvent(
                    event_type=HookEventType.BEFORE_TOOL,
                    session_id=session_id,
                    source=SessionSource.CLAUDE,
                    timestamp=FIXED_TIMESTAMP,
                    data={
                        "tool_name": "mcp__gobby__call_tool",
                        "tool_input": {
                            "server_name": PROXY_SERVER,
                            "tool_name": PROXY_TOOL,
                            "arguments": dict(PROXY_ARGUMENTS),
                        },
                        "agent_id": admitted_agent_id,
                    },
                    metadata={"_platform_session_id": session_id},
                )
                response = await asyncio.to_thread(real_evaluate, admission_event)
                admission = response.decision
            hook_manager = types.SimpleNamespace(
                _workflow_handler=handler, _session_manager=sessions
            )
            client_manager: Any = types.SimpleNamespace()
            resolver: Callable[[], Any] = lambda: hook_manager  # noqa: E731
            service = ToolProxyService(client_manager, hook_manager_resolver=resolver)
            with mock.patch.object(handler, "evaluate", new=recording_evaluate):
                _, _, _, error, outcome = await apply_before_tool_enforcement(
                    service, PROXY_SERVER, PROXY_TOOL, dict(PROXY_ARGUMENTS), session_id
                )
        finally:
            runtime.shutdown()
            project_context.reset_project_context(token)
    assert seen, "the route evaluated no event"
    route_event = seen[-1]
    return {
        "duplicate": route_event.metadata.get("_mcp_proxy_duplicate_before_tool") is True,
        "agent_id_present": "agent_id" in route_event.data,
        "agent_id": route_event.data.get("agent_id"),
        "decision": _route_decision(error, outcome),
        "route_error": None if error is None else error.get("error"),
        "admission": admission,
        "admitted_agent_id": (
            None if admission_event is None else admission_event.data.get("agent_id")
        ),
        "sources": [
            None if admission_event is None else admission_event.source.value,
            route_event.source.value,
        ],
        "tool_inputs": [
            None if admission_event is None else admission_event.data["tool_input"],
            route_event.data["tool_input"],
        ],
        "route_tool_name": route_event.data["tool_name"],
        "route_external_id": route_event.metadata.get("external_id"),
        "route_project_id": route_event.project_id,
        "route_evaluations": len(seen),
    }


def _proxy_before_tool_observation(
    admitted_agent_id: str | None, *, session_source: str = "claude"
) -> dict[str, object]:
    """Drive the proxy's before_tool route once and report what it built and decided.

    With an agent id, a hook event for the same call is evaluated first (the admission); the
    route then builds its own event through the production service and resolver, and the
    wrapped handler records the event the route hands it.
    """
    with mock.patch("gobby.utils.machine_id._cached_machine_id", rules_seed.SYNTHETIC_MACHINE_ID):
        return asyncio.run(_observe_proxy_route(admitted_agent_id, session_source))


# ---------------------------------------------------------------------------------------------
# Demonstrations
# ---------------------------------------------------------------------------------------------


def _demonstration_cases(
    row: Mapping[str, Any], index: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two rule-pass cases a condition-helper row's demonstration runs: input, control."""
    demonstration = row["demonstration"]

    def built(side: str, text: str) -> dict[str, Any]:
        event = copy.deepcopy(demonstration["event"])
        event["data"]["tool_input"]["command"] = text
        return {
            "id": f"synthetic-kb-{index}-{side}",
            "kind": "rule_pass",
            "provenance": "synthetic",
            "seam_failures": [],
            "rules": copy.deepcopy(demonstration["rules"]),
            "event": event,
        }

    return built("input", row["input"]), built("control", row["control"]["input"])


def _demonstration_outcomes(row: Mapping[str, Any], index: int) -> dict[str, str]:
    input_case, control_case = _demonstration_cases(row, index)
    return {
        "input": rules_corpus.replay_case(input_case)["outcome_class"],
        "control": rules_corpus.replay_case(control_case)["outcome_class"],
    }


# ---------------------------------------------------------------------------------------------
# Affected cases: a replay-time spy on every row's entry
# ---------------------------------------------------------------------------------------------


def _argument_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _argument_strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _argument_strings(item)
    elif isinstance(value, HookEvent):
        yield from _argument_strings(value.data)


def _spy(row: Mapping[str, Any], original: Callable[..., Any], log: list[Any]) -> Any:
    field = row["field"]

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        strings = list(_argument_strings((args, kwargs)))
        try:
            result = original(*args, **kwargs)
        except Exception as exc:
            log.append((strings, type(exc).__name__ if field == RAISED_FIELD else None))
            raise
        if field == RAISED_FIELD:
            log.append((strings, None))
        else:
            first = args[0] if args else None
            log.append((strings, _field_value(field, result, first)))
        return result

    return wrapper


@contextlib.contextmanager
def _spied(rows: Sequence[Mapping[str, Any]]) -> Iterator[dict[str, list[Any]]]:
    logs: dict[str, list[Any]] = {_row_id(row): [] for row in rows}
    with contextlib.ExitStack() as stack:
        for row in rows:
            canonical = row["entry"].rpartition(".")
            reference = f"{canonical[0]}:{canonical[2]}"
            original = rules_paths.resolve(reference)
            for site in sorted(rules_paths.binding_sites(reference)):
                owner, attribute = rules_paths.owner_and_attr(site)
                stack.enter_context(
                    mock.patch.object(owner, attribute, _spy(row, original, logs[_row_id(row)]))
                )
        yield logs


def _reached(row: Mapping[str, Any], log: Sequence[Any]) -> bool:
    if row.get("input_form") in PROXY_ROUTE_FORMS:
        return bool(log)
    return any(
        any(row["input"] in text for text in strings) and value == row["python_observed"]
        for strings, value in log
    )


def _replay_quietly(replay: Callable[[], Any]) -> None:
    with contextlib.suppress(Exception):
        replay()


def _committed_units() -> Iterator[tuple[str, Callable[[], Any]]]:
    """Every committed case of the three layers and every vector, as (key, replay)."""
    for layer, (corpus_dir, replay) in CASE_LAYERS.items():
        for path in sorted((corpus_dir / "cases").glob("*.json")):
            for case in json.loads(path.read_text())["cases"]:
                yield f"{layer}:{case['id']}", functools.partial(replay, case)
    for vector in json.loads(VECTORS_PATH.read_text())["vectors"]:
        yield f"vectors:{vector['id']}", functools.partial(rules_vectors.replay_vector, vector)


def _affected_cases(rows: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Map each case or vector key (``<layer>:<id>``) to the ids of the rows it reaches."""
    affected: dict[str, set[str]] = {}
    with _spied(rows) as logs:
        for key, replay in _committed_units():
            for log in logs.values():
                log.clear()
            _replay_quietly(replay)
            for row in rows:
                if _reached(row, logs[_row_id(row)]):
                    affected.setdefault(key, set()).add(_row_id(row))
    return affected


def _committed_tags() -> dict[str, list[str]]:
    tags: dict[str, list[str]] = {}
    for key, unit in _committed_documents():
        if "known_bad" in unit:
            tags[key] = unit["known_bad"]
    return tags


def _committed_documents() -> Iterator[tuple[str, dict[str, Any]]]:
    for layer, (corpus_dir, _) in CASE_LAYERS.items():
        for path in sorted((corpus_dir / "cases").glob("*.json")):
            for case in json.loads(path.read_text())["cases"]:
                yield f"{layer}:{case['id']}", case
    for vector in json.loads(VECTORS_PATH.read_text())["vectors"]:
        yield f"vectors:{vector['id']}", vector


# ---------------------------------------------------------------------------------------------
# Dependent-field declarations
# ---------------------------------------------------------------------------------------------

_MISSING = object()


def _path_parts(path: str) -> list[str]:
    return [part for part in path.split(".") if part]


def _lookup(value: Any, path: str) -> Any:
    for part in _path_parts(path):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def _resolves(expect: Any, path: str) -> bool:
    if _lookup(expect, path) is not _MISSING:
        return True
    parent = ".".join(_path_parts(path)[:-1])
    return bool(parent) and _lookup(expect, parent) is not _MISSING


def _under(path: str, roots: Sequence[str]) -> bool:
    parts = _path_parts(path)
    return any(parts[: len(_path_parts(root))] == _path_parts(root) for root in roots)


def _from_value(source: Mapping[str, Any], units: Mapping[str, dict[str, Any]]) -> Any:
    if "row" in source:
        rows = {_row_id(row): row for row in _rows()}
        row = rows.get(source["row"])
        return _MISSING if row is None else row.get(source.get("field", ""), _MISSING)
    for kind, prefixes in (("case", ("rules", "adapters", "pipeline")), ("vector", ("vectors",))):
        if kind in source:
            for prefix in prefixes:
                unit = units.get(f"{prefix}:{source[kind]}")
                if unit is not None:
                    return _lookup(unit.get("expect"), source.get("path", ""))
            return _MISSING
    return _MISSING


def _declaration_findings(
    key: str,
    unit: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
    units: Mapping[str, dict[str, Any]],
) -> list[str]:
    findings: list[str] = []
    tag = unit.get("known_bad", [])
    expect = unit.get("expect")
    entries = unit.get("known_bad_expect")
    if not isinstance(entries, list):
        return [f"{key}: known_bad_expect is not a list"]
    for row_id in tag:
        if not any(entry.get("row") == row_id for entry in entries):
            findings.append(f"{key}: no known_bad_expect entry for row {row_id}")
    for entry in entries:
        row = rows.get(entry.get("row"))
        if entry.get("row") not in tag or row is None:
            findings.append(f"{key}: entry names row {entry.get('row')!r} outside its tag")
            continue
        path = entry.get("path", "")
        if not _resolves(expect, path):
            findings.append(f"{key}: path {path!r} does not resolve in the recorded expect")
        if not _under(path, row["dependent_paths"]) and not key.startswith("vectors:"):
            findings.append(f"{key}: path {path!r} is not under the row's dependent_paths")
        has_correct = "correct" in entry
        has_class = "classification" in entry
        if has_correct == has_class:
            findings.append(
                f"{key}: entry at {path!r} needs exactly one of correct, classification"
            )
            continue
        recorded = _lookup(expect, path)
        if has_correct:
            source = entry.get("from")
            if not isinstance(source, Mapping):
                findings.append(f"{key}: correct at {path!r} has no from")
                continue
            if entry["correct"] == recorded:
                findings.append(f"{key}: correct at {path!r} equals the recorded Python value")
            if _from_value(source, units) != entry["correct"]:
                findings.append(f"{key}: correct at {path!r} differs from its from value")
            continue
        classification = entry["classification"]
        if classification not in CLASSIFICATIONS:
            findings.append(f"{key}: classification {classification!r} is not in the closed set")
        elif classification in ("unpinned", "unchanged") and not entry.get("reason"):
            findings.append(f"{key}: {classification} at {path!r} has no reason")
        elif classification == "contains":
            source = entry.get("from")
            tied = _MISSING if not isinstance(source, Mapping) else _from_value(source, units)
            if "value" not in entry or tied != entry["value"]:
                findings.append(f"{key}: contains at {path!r} is not tied to its from value")
            elif isinstance(recorded, str | list) and entry["value"] in recorded:
                findings.append(f"{key}: contains value at {path!r} is already recorded")
        elif classification == "extends" and not isinstance(recorded, list):
            findings.append(f"{key}: extends at {path!r} is not a recorded list")
    findings.extend(_mirror_findings(key, entries, expect))
    return findings


def _entry_form(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        entry.get("row"),
        entry.get("classification"),
        json.dumps(entry.get("correct", None), sort_keys=True),
        json.dumps(entry.get("value", None), sort_keys=True),
        json.dumps(entry.get("from", None), sort_keys=True),
        entry.get("reason"),
    )


def _mirror_findings(key: str, entries: Sequence[Mapping[str, Any]], expect: Any) -> list[str]:
    """A pass case's top-level declarations and their per-pass mirrors agree in both directions."""
    passes = expect.get("passes") if isinstance(expect, Mapping) else None
    if not isinstance(passes, list) or not passes:
        return []
    last = len(passes) - 1
    findings: list[str] = []
    per_pass = [entry for entry in entries if _path_parts(entry.get("path", ""))[:1] == ["passes"]]
    for entry in entries:
        parts = _path_parts(entry.get("path", ""))
        if not parts or parts[0] == "passes":
            continue
        if parts[0] in TOP_LEVEL_LAST_PASS_PATHS:
            mirrors = [f"passes.{last}." + ".".join(parts)]
        elif parts[0] in TOP_LEVEL_LOG_PATHS:
            mirrors = [f"passes.{index}." + ".".join(parts) for index in range(len(passes))]
        else:
            continue
        found = [
            other
            for other in per_pass
            if other.get("path") in mirrors and _entry_form(other) == _entry_form(entry)
        ]
        if not found:
            findings.append(f"{key}: top-level entry at {entry.get('path')!r} has no mirror")
    for entry in per_pass:
        parts = _path_parts(entry.get("path", ""))
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        rest = parts[2:]
        has_copy = rest[0] in TOP_LEVEL_LOG_PATHS or (
            rest[0] in TOP_LEVEL_LAST_PASS_PATHS and int(parts[1]) == last
        )
        if not has_copy:
            continue
        top = ".".join(rest)
        if not any(
            other.get("path") == top and _entry_form(other) == _entry_form(entry)
            for other in entries
        ):
            findings.append(f"{key}: per-pass entry at {entry.get('path')!r} has no top-level copy")
    return findings


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


def test_rows_have_required_fields() -> None:
    document = _registry()
    assert document.get("schema_version") == SCHEMA_VERSION, "schema_version is not 1"
    rows = _rows()
    ids = []
    for row in rows:
        missing = [key for key in REQUIRED_ROW_KEYS if key not in row]
        assert missing == [], f"row {row.get('entry')!r} lacks {missing}"
        assert isinstance(row["dependent_paths"], list), f"{row['id']}: dependent_paths"
        control = row["control"]
        assert isinstance(control, Mapping), f"{row['id']}: control is not an object"
        assert {"input", "python_observed"} <= set(control), f"{row['id']}: control keys"
        assert row["id"] == _row_id(row), f"{row['id']}: id is not the digest of the row"
        if _is_condition_helper(row["entry"]):
            assert "demonstration" in row, f"{row['id']}: a condition helper row needs one"
        if "accepts" in row:
            assert row.get("input_form") in PROXY_ROUTE_FORMS, f"{row['id']}: accepts on a call row"
            assert isinstance(row["accepts"], list), f"{row['id']}: accepts is not a list"
            assert set(row["accepts"]) <= ROW_ACCEPTS, f"{row['id']}: accepts outside the set"
        ids.append(row["id"])
    assert len(ids) == len(set(ids)), f"row ids repeat: {ids}"


def test_each_row_reproduces_in_python() -> None:
    for row in _rows():
        observed = _row_value(row, row["input"])
        assert observed == row["python_observed"], (
            f"{row['id']} ({row['ref']}): Python gives {observed!r}, "
            f"the row records {row['python_observed']!r}"
        )


def test_each_row_has_a_differing_control() -> None:
    rows = _rows()
    for row in rows:
        control = row["control"]
        assert "correct" not in control, f"{row['id']}: a control carries no correct value"
        assert "accepts" not in control, f"{row['id']}: a control carries no accepts"
        assert control["python_observed"] != row["python_observed"], (
            f"{row['id']}: the control's value equals the row's"
        )
        observed = _row_value(_control_row(row), control["input"])
        assert observed == control["python_observed"], (
            f"{row['id']}: the control gives {observed!r}, recorded {control['python_observed']!r}"
        )


def test_affected_corpus_cases_marked_known_bad() -> None:
    rows = _rows()
    row_ids = {row["id"] for row in rows}
    committed = _committed_tags()
    for key, tag in committed.items():
        unknown = sorted(set(tag) - row_ids)
        assert unknown == [], f"{key}: known_bad names no registry row: {unknown}"
        assert tag == sorted(tag), f"{key}: known_bad is not sorted"
    computed = {key: sorted(ids) for key, ids in _affected_cases(rows).items()}
    assert computed, "no corpus case or vector reaches any row's flaw"
    assert {key: sorted(tag) for key, tag in committed.items()} == computed, (
        "the committed known_bad tags differ from the cases the rows' flaws reach"
    )


def test_known_bad_vectors_tagged() -> None:
    rows = _rows()
    by_id = {row["id"]: row for row in rows}
    vectors = {vector["id"]: vector for vector in json.loads(VECTORS_PATH.read_text())["vectors"]}
    for row in rows:
        if _vector_selected(row):
            assert _vector_id(row) in vectors, f"{row['id']}: no {_vector_id(row)} vector"
    tagged = [vector for vector in vectors.values() if vector.get("known_bad")]
    assert tagged, "no normalizer vector is tagged known_bad"
    for vector in tagged:
        for row_id in vector["known_bad"]:
            assert row_id in by_id, f"{vector['id']}: known_bad names no row {row_id}"
            row = by_id[row_id]
            result = _vector_field(row, vector.get("expect"))
            assert result == row["python_observed"], (
                f"{vector['id']}: the recorded {row['field']} is {result!r}, "
                f"the row records {row['python_observed']!r}"
            )
            assert "correct" in row, f"{row_id}: the row holds no correct value"
            assert row["correct"] != row["python_observed"], f"{row_id}: correct equals Python"


def _vector_result(expect: Any) -> Any:
    if isinstance(expect, Mapping) and "result" in expect:
        return expect["result"]
    return expect


def _vector_field(row: Mapping[str, Any], expect: Any) -> Any:
    result = _vector_result(expect)
    field = row["field"]
    if field == "result":
        return result
    if field == "count":
        return len(result)
    if isinstance(result, Mapping):
        return result.get(field)
    mutated = expect.get("mutated_args", {}) if isinstance(expect, Mapping) else {}
    first = mutated.get("0") if isinstance(mutated, Mapping) else None
    return first.get(field) if isinstance(first, Mapping) else None


def test_s2_11_pins_known_bad_item() -> None:
    assert S2_11_PATH.is_file(), f"{S2_11_PATH.name} is missing"
    plan_text = S2_11_PATH.read_text()
    section = plan_text.split("\n### 1.1 ", 1)[-1].split("\n### 1.2 ", 1)[0]
    assert re.search(r"known[-_ ]bad", section), "the 1.1 narrative does not tag known-bad rows"
    item = re.search(r"^- 1\.1\.8 - (.+)$", section, re.MULTILINE)
    assert item is not None, "section 1.1 has no item 1.1.8"
    assert re.search(r"known[-_ ]bad", item.group(1)), "item 1.1.8 does not name known-bad rows"
    text = " ".join(plan_text.split())
    missing = [phrase for phrase in S2_11_PROXY_PHRASES if phrase not in text]
    assert missing == [], f"the plan lacks the proxy row's statements: {missing}"


def _scope_finding(row: Mapping[str, Any]) -> str | None:
    entry = row["entry"]
    if not (entry.startswith(ENGINE_ENTRY_PREFIXES) or entry in ENGINE_ENTRY_EXCEPTIONS):
        return f"{entry} is outside the engine packages"
    return None


def test_registry_rows_are_engine_entries() -> None:
    rows = _rows()
    for row in rows:
        finding = _scope_finding(row)
        assert finding is None, f"{row['id']}: {finding}"
        assert callable(_resolve(row["entry"])), f"{row['id']}: the entry is not callable"
    entries = {row["entry"] for row in rows}
    for exception, reason in ENGINE_ENTRY_EXCEPTIONS.items():
        assert reason.strip(), f"{exception}: an exception needs a reason"
        assert exception in entries, f"{exception}: no row uses this exception"
    server_row = {"entry": "gobby.servers.websocket.session_control.synthetic_entry"}
    assert _scope_finding(server_row) is not None, "a server entry was admitted"
    sibling_row = {"entry": "gobby.mcp_proxy.services.result_handling.build_after_tool_event"}
    assert _scope_finding(sibling_row) is not None, "the exception widened to its module"


def _proxy_row() -> dict[str, Any]:
    exception = next(iter(ENGINE_ENTRY_EXCEPTIONS))
    found = [
        row
        for row in _rows()
        if row["entry"] == exception and row.get("input_form") == "admitted_by_hook"
    ]
    assert len(found) == 1, f"expected one proxy duplicate-event row, found {len(found)}"
    return found[0]


def test_proxy_duplicate_before_tool_row_drives_the_real_route() -> None:
    row = _proxy_row()
    observation = _proxy_before_tool_observation(row["input"])
    assert _row_value(row, row["input"]) == row["python_observed"], "the row does not reproduce"
    assert observation["agent_id_present"] is False, "the route event carries an agent_id key"
    assert observation["agent_id"] is None, "the route event carries an agent id"
    assert observation["duplicate"] is True, "the route did not mark the event a duplicate"
    assert observation["decision"] == "block", f"decision is {observation['decision']!r}"
    assert observation["admission"] == "allow", "the prior admission was not allowed"
    assert observation["admitted_agent_id"] == row["input"], "the admission lost its agent id"
    control_row = _control_row(row)
    control = _proxy_before_tool_observation(None)
    assert control_row["input_form"] == "no_prior_admission", "the control form"
    assert _row_value(control_row, control_row["input"]) == row["control"]["python_observed"]
    assert control["admission"] is None, "the control was admitted"
    assert {key: control[key] for key in PROXY_FIELD_KEYS} == {
        "duplicate": False,
        "agent_id_present": False,
        "agent_id": None,
        "decision": "block",
    }, f"control observation {control}"
    for label, seen in (("row", observation), ("control", control)):
        error = str(seen["route_error"])
        assert PROXY_ROW_RULE["name"] in error, f"{label}: the block is not the synthetic rule's"
        assert PROXY_ROW_RULE["effects"][0]["reason"] in error, f"{label}: the block's reason"
    expected_correct = dict(row["python_observed"])
    expected_correct.update(
        {"agent_id_present": True, "agent_id": row["input"], "decision": "allow"}
    )
    assert row["correct"] == expected_correct, f"correct is {row['correct']!r}"
    assert set(row.get("accepts", [])) <= ROW_ACCEPTS, "accepts outside the closed set"
    assert "correct" not in row["control"] and "accepts" not in row["control"]
    assert row["dependent_paths"] == [], "the proxy row tags no case"
    assert not any(row["id"] in ids for ids in _affected_cases([row]).values()), (
        "a committed case reaches the proxy route"
    )


def test_proxy_route_helper_drives_the_production_route() -> None:
    agent_id = "synthetic-subagent-a1"
    admitted = _proxy_before_tool_observation(agent_id)
    assert admitted["admission"] == "allow", f"admission {admitted['admission']!r}"
    assert admitted["admitted_agent_id"] == agent_id
    assert admitted["route_evaluations"] == 1, "the route evaluates exactly one event"
    assert admitted["duplicate"] is True, "an admitted call is a duplicate on the route"
    assert admitted["sources"] == ["claude", "claude"], f"sources {admitted['sources']}"
    tool_inputs = admitted["tool_inputs"]
    assert isinstance(tool_inputs, list) and tool_inputs[0] == tool_inputs[1], (
        f"the fingerprint inputs differ: {tool_inputs}"
    )
    fresh = _proxy_before_tool_observation(None)
    assert fresh["admission"] is None
    assert fresh["route_evaluations"] == 1
    assert fresh["duplicate"] is False, "a call no hook admitted is not a duplicate"
    other_source = _proxy_before_tool_observation(agent_id, session_source="codex")
    assert other_source["admission"] == "allow"
    assert other_source["sources"] == ["claude", "codex"], "the route's source follows the row"
    assert other_source["duplicate"] is False, "a different source has no pending context"
    for seen in (admitted, fresh, other_source):
        assert seen["route_tool_name"] == "mcp__gobby__call_tool"
        assert seen["route_external_id"] == PROXY_EXTERNAL_ID
        assert seen["route_project_id"] == rules_seed.SYNTHETIC_PROJECT_ID


def test_tagged_cases_declare_their_dependent_fields() -> None:
    rows = {row["id"]: row for row in _rows()}
    units = dict(_committed_documents())
    tagged_rows: dict[str, set[str]] = {}
    findings: list[str] = []
    for key, unit in units.items():
        tagged = bool(unit.get("known_bad"))
        if tagged != ("known_bad_expect" in unit):
            findings.append(f"{key}: known_bad and known_bad_expect must appear together")
            continue
        if not tagged:
            continue
        findings.extend(_declaration_findings(key, unit, rows, units))
        expect = unit.get("expect")
        for row_id in unit["known_bad"]:
            tagged_rows.setdefault(row_id, set()).add(key)
            row = rows.get(row_id)
            has_passes = isinstance(expect, Mapping) and "passes" in expect
            if row is not None and has_passes and not _under("passes", row["dependent_paths"]):
                findings.append(f"{key}: row {row_id} tags a pass case without listing passes")
    for row_id, row in rows.items():
        case_keys = {
            key for key in tagged_rows.get(row_id, set()) if not key.startswith("vectors:")
        }
        if bool(case_keys) == (row["dependent_paths"] == []):
            findings.append(f"{row_id}: dependent_paths is empty exactly when the row tags no case")
    for row in _rows():
        demonstration = row.get("demonstration")
        if demonstration is None:
            continue
        expect = demonstration.get("expect", {})
        correct_input = demonstration.get("correct_input")
        if correct_input is None or correct_input == expect.get("input"):
            findings.append(f"{row['id']}: correct_input must differ from expect.input")
        if row["correct"] == row["control"]["python_observed"] and (
            correct_input != expect.get("control")
        ):
            findings.append(f"{row['id']}: correct_input must equal expect.control")
    assert findings == [], "\n".join(findings)
    probe_entries: list[dict[str, Any]] = [
        {"row": "known-bad-000000000000", "path": "effect_log", "classification": "extends"}
    ]
    probe_expect: dict[str, Any] = {"effect_log": [], "passes": [{"effect_log": []}]}
    assert _mirror_findings("synthetic", probe_entries, probe_expect), (
        "a top-level extends with no per-pass mirror was accepted"
    )


def test_condition_helper_rows_reproduce_through_the_engine() -> None:
    rows = _rows()
    helpers = build_condition_helpers().values()
    selected = [
        (index, row)
        for index, row in enumerate(rows)
        if any(helper is _resolve(row["entry"]) for helper in helpers)
    ]
    assert selected, "no registry row's entry is a condition helper"
    for index, row in selected:
        demonstration = row.get("demonstration")
        assert isinstance(demonstration, Mapping), f"{row['id']}: no demonstration"
        outcomes = _demonstration_outcomes(row, index)
        assert outcomes == demonstration["expect"], (
            f"{row['id']}: the engine gives {outcomes}, recorded {demonstration['expect']}"
        )
        assert outcomes["input"] != outcomes["control"], f"{row['id']}: the sides agree"


def test_registry_names_no_bundled_definition() -> None:
    assert REGISTRY_PATH.is_file(), f"{REGISTRY_PATH.name} is missing"
    findings = rules_corpus.find_definition_references([REGISTRY_PATH])
    assert findings == [], f"the registry names bundled definitions: {findings}"


# ---------------------------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------------------------


def _dump(path: Path, document: Any) -> None:
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def _rerecord_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-reproduce every row and control; a row Python now gets right is retired."""
    kept: list[dict[str, Any]] = []
    exception = next(iter(ENGINE_ENTRY_EXCEPTIONS))
    for row in document["rows"]:
        assert _scope_finding(row) is None, f"{row.get('entry')}: outside the registry's scope"
        observed = _row_value(row, row["input"])
        control = _row_value(_control_row(row), row["control"]["input"])
        if row["entry"] == exception:
            assert observed == row["python_observed"], f"the proxy row changed: {observed!r}"
            assert control == row["control"]["python_observed"], f"its control: {control!r}"
        if observed == row["correct"]:
            continue
        row["python_observed"] = observed
        row["control"]["python_observed"] = control
        row["id"] = _row_id(row)
        kept.append(row)
    return kept


def _known_bad_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    vector: dict[str, Any] = {"id": _vector_id(row), "entry": row["entry"]}
    if row["entry"].endswith(".normalize_tool_fields"):
        vector["input"] = {
            "args": [
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": row["input"]},
                    "cwd": rules_vectors.REPO_TOKEN,
                    "project_path": rules_vectors.REPO_TOKEN,
                }
            ]
        }
        vector["result_from_arg"] = 0
        vector["setup"] = copy.deepcopy(dict(KNOWN_BAD_FIXTURE_SETUP))
    else:
        vector["input"] = {"args": ["Bash", {"command": row["input"]}]}
    vector["metadata"] = {"reason": VECTOR_REASON}
    return vector


def _write_tags(affected: Mapping[str, set[str]], rows: Sequence[Mapping[str, Any]]) -> None:
    by_id = {row["id"]: row for row in rows}
    for layer, (corpus_dir, _) in CASE_LAYERS.items():
        for path in sorted((corpus_dir / "cases").glob("*.json")):
            document = json.loads(path.read_text())
            for case in document["cases"]:
                tag = sorted(affected.get(f"{layer}:{case['id']}", set()))
                if tag:
                    case["known_bad"] = tag
                else:
                    case.pop("known_bad", None)
                    case.pop("known_bad_expect", None)
            _dump(path, document)
    document = json.loads(VECTORS_PATH.read_text())
    for vector in document["vectors"]:
        tag = sorted(affected.get(f"vectors:{vector['id']}", set()))
        if not tag:
            vector.pop("known_bad", None)
            vector.pop("known_bad_expect", None)
            continue
        vector["known_bad"] = tag
        vector["known_bad_expect"] = [
            {
                "row": row_id,
                "path": "expect",
                "correct": by_id[row_id]["correct"],
                "from": {"row": row_id, "field": "correct"},
            }
            for row_id in tag
        ]
    _dump(VECTORS_PATH, document)


def test_rerecord_known_bad_registry() -> None:
    if os.environ.get(RECORD_ENV) != "1":
        pytest.skip("record mode only: set GOBBY_CORPUS_RECORD=1")
    document = _registry()
    rows = _rerecord_rows(document)
    for index, row in enumerate(rows):
        if "demonstration" in row:
            row["demonstration"]["expect"] = _demonstration_outcomes(row, index)
    document["rows"] = rows
    _dump(REGISTRY_PATH, document)
    vectors = json.loads(VECTORS_PATH.read_text())
    present = {vector["id"] for vector in vectors["vectors"]}
    for row in rows:
        if _vector_selected(row) and _vector_id(row) not in present:
            vectors["vectors"].append(_known_bad_vector(row))
    _dump(VECTORS_PATH, vectors)
    rules_vectors.record_vectors(VECTORS_PATH)
    _write_tags(_affected_cases(rows), rows)
    assert REGISTRY_PATH.is_file(), "the registry was not written"

"""Exporter for the rule-pass corpus.

Each case runs ``RuleEngine.evaluate`` on an isolated test-hub schema, with its synthetic
rules seeded through ``RuleDefinitionManager``. While the engine runs, each seam method
and the rule-evaluation audit call are wrapped with ``unittest.mock.patch.object`` and
the effect dispatcher is wrapped too, so the ordered effect log is observed rather than
written by hand. ``GOBBY_CORPUS_RECORD=1`` writes the observed blocks back into the case
files. A default run replays and compares the observed blocks with the committed ones.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import copy
import dataclasses
import json
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from uuid import UUID

from gobby.config.features import ToolResultOffloadConfig
from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.mcp_proxy.metrics_events import MetricsEventStore
from gobby.mcp_proxy.services.result_offload import ToolResultOffloader
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from gobby.storage.tool_results import ToolResultStore
from gobby.storage.workflow_audit import WorkflowAuditManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine import evaluation as evaluation_module
from gobby.workflows.engine import proxy_hooks
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.effects import EffectsMixin
from tests.fixtures.hub_guard import hub_database_url
from tests.fixtures.postgres import isolated_test_schema

RECORD_ENV = "GOBBY_CORPUS_RECORD"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
FIXED_TIMESTAMP = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
ROW_KEYS = frozenset({"name", "project_id", "priority", "enabled", "sources", "tags"})
STAGED_PAYLOAD_KEY = "_gobby_staged_effects"
VOLATILE_KEYS = frozenset({"latency_ms", "timestamp", "created_at", "updated_at", "duration_ms"})
# Seams whose writes the effect log names as writes rather than seam calls.
WRITE_LABELS = {"workflow_audit": "workflow_audit_log", "rule_allow_audit": "rule_eval"}
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?")
_ABS_PATH = re.compile(r"/(?:Users|home|private|tmp|var)/\S*")
# A drive-letter checkout path, as a Windows runner prints it (C:\Users\...).
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\\S*")
# A random 16-hex token, such as the heredoc delimiter secrets.token_hex(8) draws per call.
_RANDOM_TOKEN = re.compile(r"\b[0-9a-f]{16}\b")


class SeamFailure(RuntimeError):
    """The injected failure a case's seam script asks for."""


def seam_registry() -> dict[str, tuple[object, str]]:
    """Map each seam kind to the owner (class or module) and attribute the recorder wraps."""
    return {
        "rule_definitions": (RuleDefinitionManager, "list_by_event"),
        "workflow_audit": (WorkflowAuditManager, "log"),
        "metrics_events": (MetricsEventStore, "record_events"),
        "coordination_waits": (CoordinationWaitManager, "count_active_waits"),
        "completion_subscribers": (CompletionSubscriberManager, "has_active_agent_wait"),
        "inter_session_messages": (InterSessionMessageManager, "create_message"),
        "tool_results": (ToolResultStore, "save"),
        "rule_allow_audit": (evaluation_module, "record_rule_evaluation"),
    }


def _scrub_text(text: str) -> str:
    text = _UUID.sub("<uuid>", text)
    text = _ISO_TIME.sub("<timestamp>", text)
    text = _RANDOM_TOKEN.sub("<token>", text)
    return _WINDOWS_PATH.sub("<path>", _ABS_PATH.sub("<path>", text))


def _jsonable(value: Any) -> Any:
    """Return a JSON-native, scrubbed copy of ``value`` (the form the corpus stores)."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, bytes):
        return _scrub_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, UUID):
        return "<uuid>"
    if isinstance(value, datetime):
        return "<timestamp>"
    if isinstance(value, Path):
        return _scrub_text(str(value))
    if isinstance(value, Mapping):
        return {
            str(key): ("<volatile>" if str(key) in VOLATILE_KEYS else _jsonable(item))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_jsonable(item) for item in value), key=repr)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, subprocess.CompletedProcess):
        return {
            "returncode": value.returncode,
            "stdout": _jsonable(value.stdout),
            "stderr": _jsonable(value.stderr),
        }
    if isinstance(value, ast.AST):
        return _scrub_text(ast.dump(value, include_attributes=False))
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    return f"<{type(value).__name__}>"


def _decision(response: HookResponse | None) -> str:
    """The decision of a completed pass; a missing response with no error is a harness fault."""
    if response is None:
        raise RuntimeError("RuleEngine.evaluate returned neither a response nor an error")
    return str(response.decision)


def response_defaults() -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for item in dataclasses.fields(HookResponse):
        if item.default_factory is not dataclasses.MISSING:
            defaults[item.name] = item.default_factory()
        else:
            defaults[item.name] = item.default
    return defaults


def _event_from(spec: Mapping[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType(spec["type"]),
        session_id=SESSION_ID,
        source=SessionSource(spec.get("source", "claude")),
        timestamp=FIXED_TIMESTAMP,
        data=copy.deepcopy(dict(spec.get("data", {}))),
        metadata=copy.deepcopy(dict(spec.get("metadata", {}))),
    )


@contextlib.contextmanager
def _hub_database() -> Iterator[PostgresHubDatabase]:
    """Create a migrated, empty schema on the isolated test hub and drop it on exit."""
    from gobby.storage.schema_contract import apply_schema

    base = hub_database_url()
    with isolated_test_schema(base, "rules") as schema:
        apply_schema(base, schema=schema)
        db = PostgresHubDatabase(base + f"?options=-csearch_path%3D{schema}")
        try:
            yield db
        finally:
            db.close()


def _seed_rules(db: PostgresHubDatabase, rules: list[Mapping[str, Any]]) -> None:
    manager = RuleDefinitionManager(db)
    for rule in rules:
        body = {
            key: value for key, value in rule.items() if key not in ROW_KEYS and key != "corrupt"
        }
        definition = RuleDefinitionBody.model_validate(body).model_dump()
        if rule.get("project_id") is not None:
            # Rule rows reference projects; the project row only needs an id and a name.
            db.execute(
                "INSERT INTO projects (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (rule["project_id"], f"synthetic-project-{rule['project_id'][:8]}"),
            )
        manager.create(
            name=rule["name"],
            definition_json=definition,
            project_id=rule.get("project_id"),
            priority=rule.get("priority", 100),
            enabled=rule.get("enabled", True),
            sources=rule.get("sources"),
            tags=rule.get("tags", ["default"]),
        )
        if "corrupt" in rule:
            # A stored row that no longer parses as a rule body: the engine must skip it.
            db.execute(
                "UPDATE rule_definitions SET definition_json = %s::jsonb WHERE name = %s",
                (json.dumps(rule["corrupt"]), rule["name"]),
            )


def _config_runtime(values: Mapping[str, Any] | None) -> Any | None:
    """A read-only stand-in for the config runtime, holding only the case's overrides."""
    if values is None:
        return None
    return SimpleNamespace(snapshot=SimpleNamespace(active_values=dict(values)))


class _Recorder:
    """Ordered effect log and audit records for one case run."""

    def __init__(self, failing: set[str]) -> None:
        self.failing = failing
        self.effect_log: list[dict[str, Any]] = []
        self.audit_records: list[dict[str, Any]] = []

    def seam_wrapper(
        self, kind: str, attr: str, original: Callable[..., Any]
    ) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            label = WRITE_LABELS.get(kind)
            entry_key = ("write", label) if label else ("seam", kind)
            if kind in self.failing:
                self.effect_log.append(
                    {entry_key[0]: entry_key[1], "call": attr, "outcome": "failed"}
                )
                raise SeamFailure(kind)
            result = original(*args, **kwargs)
            self.effect_log.append({entry_key[0]: entry_key[1], "call": attr, "outcome": "ok"})
            return result

        return wrapper

    def record_rule_evaluation(self, *args: Any, **kwargs: Any) -> None:
        rule_name = kwargs["rule_name"]
        result = kwargs["result"]
        event = kwargs["event"]
        if "rule_allow_audit" in self.failing:
            self.effect_log.append(
                {
                    "write": WRITE_LABELS["rule_allow_audit"],
                    "call": "record_rule_evaluation",
                    "outcome": "failed",
                }
            )
            raise SeamFailure("rule_allow_audit")
        self.audit_records.append({"rule": rule_name, "result": result, "event": event})
        self.effect_log.append(
            {
                "write": WRITE_LABELS["rule_allow_audit"],
                "call": "record_rule_evaluation",
                "outcome": "ok",
            }
        )

    def apply_effect_wrapper(self, original: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(owner: Any, effect: Any, row: Any, *args: Any, **kwargs: Any) -> Any:
            self.effect_log.append(
                {"effect": effect.type, "delivery": effect.delivery, "rule": row.name}
            )
            return await original(owner, effect, row, *args, **kwargs)

        return wrapper


def _patched_recorder(recorder: _Recorder, stack: contextlib.ExitStack) -> None:
    for kind, (owner, attr) in seam_registry().items():
        if kind == "rule_allow_audit":
            continue
        original = getattr(owner, attr)
        stack.enter_context(
            mock.patch.object(owner, attr, recorder.seam_wrapper(kind, attr, original))
        )
    stack.enter_context(
        mock.patch.object(
            evaluation_module,
            "record_rule_evaluation",
            recorder.record_rule_evaluation,
        )
    )
    stack.enter_context(
        mock.patch.object(
            EffectsMixin,
            "_apply_effect",
            recorder.apply_effect_wrapper(EffectsMixin._apply_effect),
        )
    )
    # The rtk binary is machine-specific. Pinning it unavailable keeps the corpus portable.
    stack.enter_context(mock.patch.object(proxy_hooks, "resolve_rtk", return_value=None))


async def _fake_mcp_dispatcher(
    server: str, tool: str, arguments: Any, event: Any
) -> dict[str, Any]:
    """A fixed MCP answer: every captured call succeeds with one text result."""
    return {"success": True, "result": f"{server}/{tool} synthetic result"}


async def _run_events(
    db: PostgresHubDatabase, case: Mapping[str, Any], recorder: _Recorder
) -> tuple[HookResponse | None, dict[str, Any], dict[str, Any], str | None]:
    engine = RuleEngine(
        db,
        metrics_event_store=MetricsEventStore(db),
        mcp_dispatcher=_fake_mcp_dispatcher if case.get("mcp") else None,
        config_runtime=_config_runtime(case.get("config")),
    )
    initial = copy.deepcopy(dict(case.get("variables", {})))
    variables = copy.deepcopy(initial)
    specs = [case["event"], *case.get("sequence", [])]
    response: HookResponse | None = None
    error: str | None = None
    deadline = None
    if case.get("deadline") is not None:
        # A deadline that expired before the pass starts: every blocking effect is out of budget.
        deadline = BlockingEffectDeadline(expires_at=time.monotonic() - 1.0)
    try:
        for spec in specs:
            # Every dispatched event is an entry, so a pass that matches nothing still logs.
            recorder.effect_log.append({"event": spec["type"]})
            response = await engine.evaluate(
                _event_from(spec), SESSION_ID, variables, blocking_deadline=deadline
            )
    except Exception as exc:
        error = type(exc).__name__
    return response, initial, variables, error


CALLER_KIND = "seam_caller"


async def _run_background_delivery(db: PostgresHubDatabase, spec: Mapping[str, Any]) -> Any:
    """Drive the background run_command delivery that hands its output to the next turn."""
    engine = RuleEngine(db, metrics_event_store=MetricsEventStore(db))
    await engine._run_command_then_deliver(
        [str(part) for part in spec["command"]],
        str(spec["cwd"]),
        b"{}",
        float(spec["timeout_seconds"]),
        project_id=None,
        skill=None,
        script=None,
        rule_name=str(spec["rule_name"]),
        rule_id=str(spec["rule_id"]),
        platform_session_id=str(spec["platform_session_id"]),
    )
    return None


async def _run_tool_result_offload(db: PostgresHubDatabase, spec: Mapping[str, Any]) -> Any:
    """Drive the MCP proxy's offload of an oversized successful tool result."""
    config = ToolResultOffloadConfig(
        threshold_chars=int(spec["threshold_chars"]),
        max_envelope_chars=int(spec["max_envelope_chars"]),
    )
    offloader = ToolResultOffloader(ToolResultStore(db, config), db, config, lambda: None)
    return await offloader.maybe_offload(
        server_name=str(spec["server_name"]),
        tool_name=str(spec["tool_name"]),
        result=str(spec["result"]) * int(spec.get("repeat", 1)),
        session_id=str(spec["session_id"]),
        intent=None,
        project_id=str(spec["project_id"]),
    )


CALLERS: dict[str, Callable[[PostgresHubDatabase, Mapping[str, Any]], Awaitable[Any]]] = {
    "background_delivery": _run_background_delivery,
    "tool_result_offload": _run_tool_result_offload,
}


async def _guarded_call(
    caller: Callable[[PostgresHubDatabase, Mapping[str, Any]], Awaitable[Any]],
    db: PostgresHubDatabase,
    spec: Mapping[str, Any],
) -> tuple[Any, str | None]:
    try:
        return await caller(db, spec), None
    except Exception as exc:
        return None, type(exc).__name__


def _observe_caller(case: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run a ``seam_caller`` case: one real caller with its seams wrapped, not the rule engine."""
    recorder = _Recorder(set(case.get("seam_failures", [])))
    caller = CALLERS[str(case["caller"])]
    with _hub_database() as db:
        with contextlib.ExitStack() as stack:
            _patched_recorder(recorder, stack)
            returned, error = asyncio.run(_guarded_call(caller, db, case["input"]))
    if error is not None:
        recorder.effect_log.append({"raised": error})
    expect: dict[str, Any] = {
        "outcome_class": "error" if error is not None else "allow",
        "eager_variable_delta": {},
        "on_receipt_payload": None,
        "effect_log": recorder.effect_log,
        "audit_records": recorder.audit_records,
        "response": {},
        "returned": _jsonable(returned),
    }
    return expect, recorder.effect_log


def _observe(case: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one case and return ``(expect block, effect log)``."""
    if case.get("kind") == CALLER_KIND:
        return _observe_caller(case)
    recorder = _Recorder(set(case.get("seam_failures", [])))
    with _hub_database() as db:
        _seed_rules(db, case["rules"])
        with contextlib.ExitStack() as stack:
            _patched_recorder(recorder, stack)
            response, initial, variables, error = asyncio.run(_run_events(db, case, recorder))
    if error is not None:
        recorder.effect_log.append({"raised": error})
    defaults = response_defaults()
    response_block: dict[str, Any] = {}
    if response is not None:
        for name, default in defaults.items():
            value = getattr(response, name)
            if value != default:
                response_block[name] = _jsonable(value)
    staged = response.metadata.get(STAGED_PAYLOAD_KEY) if response is not None else None
    delta = {key: _jsonable(value) for key, value in variables.items() if initial.get(key) != value}
    expect: dict[str, Any] = {
        "outcome_class": "error" if error is not None else _decision(response),
        "eager_variable_delta": delta,
        "on_receipt_payload": _jsonable(staged),
        "effect_log": recorder.effect_log,
        "audit_records": recorder.audit_records,
        "response": response_block,
    }
    evaluated = {record["rule"] for record in recorder.audit_records}
    skipped = sorted({rule["group"] for rule in case["rules"] if "group" in rule} - evaluated)
    if skipped:
        expect["skipped_groups"] = skipped
    return expect, recorder.effect_log


def record_case(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one case with every seam wrapped and return the ordered seam call log."""
    return _observe(case)[1]


def replay_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the Python rule engine and return the observed ``expect`` block."""
    return _observe(case)[0]


def record_corpus(corpus_dir: Path) -> None:
    """Fill each case's ``expect`` block with the recorded engine output.

    Runs only when the ``GOBBY_CORPUS_RECORD`` environment variable is 1; otherwise the
    corpus files are left unchanged, so a default run replays.
    """
    if os.environ.get(RECORD_ENV) != "1":
        return
    for path in sorted((corpus_dir / "cases").glob("*.json")):
        document = json.loads(path.read_text())
        for case in document["cases"]:
            case["expect"] = replay_case(case)
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")

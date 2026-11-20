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
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePath
from types import SimpleNamespace
from typing import Any
from unittest import mock
from uuid import UUID

from gobby.config.features import ToolResultOffloadConfig
from gobby.hooks import effect_deadline
from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.integrations.rtk import RtkProbe
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
from gobby.workflows.engine.evaluation import EvaluationMixin
from gobby.workflows.engine.run_command_effects import RunCommandEffectsMixin
from tests.contracts import rules_engine_seams, rules_seed
from tests.fixtures.hub_guard import hub_database_url
from tests.fixtures.postgres import isolated_test_schema

RECORD_ENV = "GOBBY_CORPUS_RECORD"
SESSION_ID = rules_seed.SESSION_ID
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


def _scrub_text(text: str, *, repo_root: str | None = None) -> str:
    """Scrub volatile values; a path under ``repo_root`` is written relative to ``{repo}``."""
    if repo_root:
        text = text.replace(repo_root, "{repo}")
    text = _UUID.sub("<uuid>", text)
    text = _ISO_TIME.sub("<timestamp>", text)
    text = _RANDOM_TOKEN.sub("<token>", text)
    return _WINDOWS_PATH.sub("<path>", _ABS_PATH.sub("<path>", text))


def _jsonable(value: Any, *, repo_root: str | None = None) -> Any:
    """Return a JSON-native, scrubbed copy of ``value`` (the form the corpus stores).

    ``repo_root`` names a fixture checkout whose paths are recorded relative to ``{repo}``;
    case recording passes none. A path is rendered in POSIX form on every platform.
    """
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, str):
        return _scrub_text(value, repo_root=repo_root)
    if isinstance(value, bytes):
        return _scrub_text(value.decode("utf-8", errors="replace"), repo_root=repo_root)
    if isinstance(value, Enum):
        return _jsonable(value.value, repo_root=repo_root)
    if isinstance(value, UUID):
        return "<uuid>"
    if isinstance(value, datetime):
        return "<timestamp>"
    if isinstance(value, PurePath):
        return _scrub_text(value.as_posix(), repo_root=repo_root)
    if isinstance(value, Mapping):
        return {
            str(key): (
                "<volatile>" if str(key) in VOLATILE_KEYS else _jsonable(item, repo_root=repo_root)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_jsonable(item, repo_root=repo_root) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_jsonable(item, repo_root=repo_root) for item in value), key=repr)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value), repo_root=repo_root)
    if isinstance(value, subprocess.CompletedProcess):
        return {
            "returncode": value.returncode,
            "stdout": _jsonable(value.stdout, repo_root=repo_root),
            "stderr": _jsonable(value.stderr, repo_root=repo_root),
        }
    if isinstance(value, ast.AST):
        return _scrub_text(ast.dump(value, include_attributes=False), repo_root=repo_root)
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"), repo_root=repo_root)
    return f"<{type(value).__name__}>"


EAGER_VARIABLE_DELETIONS_KEY = "eager_variable_deletions"


def variable_delta(
    initial: Mapping[str, Any], final: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Return ``(changed, deleted)`` between two variable snapshots.

    A key counts as changed when it is new (a new key written to None included) or its value
    differs as a JSON value (see ``_json_equal``); a key present before and absent after is
    listed as deleted.
    """
    changed = {
        key: _jsonable(value)
        for key, value in final.items()
        if key not in initial or not _json_equal(initial[key], value)
    }
    return changed, sorted(key for key in initial if key not in final)


def _json_equal(left: Any, right: Any) -> bool:
    """Equality of the raw values as JSON values, compared before scrubbing.

    Python's ``==`` treats ``True == 1`` and ``False == 0``; JSON booleans and numbers are
    different types, so a boolean equals only a boolean, at every depth of a mapping or list.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list | tuple) and isinstance(right, list | tuple):
        return len(left) == len(right) and all(
            _json_equal(item, other) for item, other in zip(left, right, strict=True)
        )
    return bool(left == right)


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


def _event_from(
    spec: Mapping[str, Any],
    *,
    session_id: str | None = None,
    project_id: str | None = None,
    cwd: str | None = None,
) -> HookEvent:
    """Build a case event; with no keyword the event carries no platform session or cwd.

    ``session_id`` is written as ``_platform_session_id`` metadata, the key the effects read
    the platform session from; ``project_id`` and ``cwd`` fill the event fields.
    """
    metadata = copy.deepcopy(dict(spec.get("metadata", {})))
    if session_id is not None:
        metadata["_platform_session_id"] = session_id
    return HookEvent(
        event_type=HookEventType(spec["type"]),
        session_id=SESSION_ID,
        source=SessionSource(spec.get("source", "claude")),
        timestamp=FIXED_TIMESTAMP,
        data=copy.deepcopy(dict(spec.get("data", {}))),
        metadata=metadata,
        cwd=cwd,
        project_id=project_id,
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
    for raw_rule in rules:
        rule = rules_engine_seams._expand_placeholders(raw_rule)
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
        # Names of the rules whose effects reached the event-match check, so a rule the
        # group check skipped is told apart from one whose condition was false.
        self.rules_reached: set[str] = set()
        self._effect_owner: dict[int, str] = {}

    def rule_loop_pass_wrapper(self, original: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(
            owner: Any,
            rules: Any,
            evaluation: Any,
            bridge: Any,
            *,
            aggregate_blocks: bool,
            block_effects_only: bool,
        ) -> Any:
            self._effect_owner = {
                id(effect): row.name for row, body in rules for effect in body.resolved_effects
            }
            return await original(
                owner,
                rules,
                evaluation,
                bridge,
                aggregate_blocks=aggregate_blocks,
                block_effects_only=block_effects_only,
            )

        return wrapper

    def effect_matches_wrapper(self, original: Callable[..., bool]) -> Callable[..., bool]:
        def wrapper(owner: Any, effect: Any, event: Any) -> bool:
            name = self._effect_owner.get(id(effect))
            if name is not None:
                self.rules_reached.add(name)
            return original(owner, effect, event)

        return wrapper

    def run_command_wrapper(self, original: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(
            owner: Any, effect: Any, row: Any, ctx: dict[str, Any], context_parts: Any
        ) -> Any:
            # The effect-seam contract: a run_command effect receives the pass's deadline. The
            # label is the deadline's real state, read through the effect module's own clock.
            deadline = ctx.get("_blocking_deadline")
            if not isinstance(deadline, BlockingEffectDeadline):
                label = "none"
            elif effect_deadline.blocking_budget_overrun(deadline):
                label = "expired"
            else:
                label = "live"
            self.effect_log.append({"run_command_deadline": label})
            return await original(owner, effect, row, ctx, context_parts)

        return wrapper

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


def _patched_recorder(
    recorder: _Recorder, stack: contextlib.ExitStack, *, rtk_probe: RtkProbe | None = None
) -> None:
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
    stack.enter_context(
        mock.patch.object(
            EvaluationMixin,
            "_run_rule_loop_pass",
            recorder.rule_loop_pass_wrapper(EvaluationMixin._run_rule_loop_pass),
        )
    )
    stack.enter_context(
        mock.patch.object(
            EffectsMixin,
            "_effect_matches_event",
            recorder.effect_matches_wrapper(EffectsMixin._effect_matches_event),
        )
    )
    stack.enter_context(
        mock.patch.object(
            RunCommandEffectsMixin,
            "_apply_run_command",
            recorder.run_command_wrapper(RunCommandEffectsMixin._apply_run_command),
        )
    )
    # The rtk binary is machine-specific. A case pins it unavailable, or opts into the fake.
    stack.enter_context(mock.patch.object(proxy_hooks, "resolve_rtk", return_value=rtk_probe))


def _rtk_probe(case: Mapping[str, Any], stack: contextlib.ExitStack) -> RtkProbe | None:
    """The fake RTK probe a case opts into with ``proxy``, written to a temp directory."""
    proxy = case.get("proxy")
    if proxy is None:
        return None
    if proxy != {"rtk": rules_engine_seams.RTK_FAKE}:
        raise ValueError(f"{case.get('id')}: unknown proxy {proxy!r}")
    directory = stack.enter_context(tempfile.TemporaryDirectory())
    return rules_engine_seams._fake_rtk_probe(Path(directory))


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


async def _run_inline_command(db: PostgresHubDatabase, spec: Mapping[str, Any]) -> Any:
    """Drive one seeded run_command effect with a live event (rules_engine_seams)."""
    return await rules_engine_seams._run_inline_command(db, spec, event_from=_event_from)


CALLERS: dict[str, Callable[[PostgresHubDatabase, Mapping[str, Any]], Awaitable[Any]]] = {
    "background_delivery": _run_background_delivery,
    "tool_result_offload": _run_tool_result_offload,
    "inline_run_command": _run_inline_command,
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
        rules_seed.apply_case_seed(db, case.get("seed"))
        if case.get("rules"):
            _seed_rules(db, case["rules"])
        with contextlib.ExitStack() as stack:
            _patched_recorder(recorder, stack, rtk_probe=_rtk_probe(case, stack))
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


def skipped_groups(rules: list[Mapping[str, Any]], reached: set[str]) -> list[str]:
    """Return the sorted groups the engine skipped, given the names of the rules it reached.

    A group is skipped when it has a member and no member reached the event-match check.
    """
    members: dict[str, set[str]] = {}
    for rule in rules:
        if "group" in rule:
            members.setdefault(str(rule["group"]), set()).add(str(rule["name"]))
    return sorted(group for group, names in members.items() if not names & reached)


def _observe(case: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one case and return ``(expect block, effect log)``."""
    if case.get("kind") == CALLER_KIND:
        return _observe_caller(case)
    recorder = _Recorder(set(case.get("seam_failures", [])))
    with _hub_database() as db:
        rules_seed.apply_case_seed(db, case.get("seed"))
        _seed_rules(db, case["rules"])
        with contextlib.ExitStack() as stack:
            _patched_recorder(recorder, stack, rtk_probe=_rtk_probe(case, stack))
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
    delta, deletions = variable_delta(initial, variables)
    expect: dict[str, Any] = {
        "outcome_class": "error" if error is not None else _decision(response),
        "eager_variable_delta": delta,
        "on_receipt_payload": _jsonable(staged),
        "effect_log": recorder.effect_log,
        "audit_records": recorder.audit_records,
        "response": response_block,
    }
    if deletions:
        expect[EAGER_VARIABLE_DELETIONS_KEY] = deletions
    skipped = skipped_groups(case["rules"], recorder.rules_reached)
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

"""Exporter for the hook pipeline corpus.

Each case runs the route's adapter step, ``adapter.handle_native``, inside the delivery and
staging scopes the route opens, on a worker thread beside a running event loop, against a real
``HookManager`` built on an isolated test-hub schema. The input is the provider's native hook
payload, so the real adapter translation runs before ``HookManager.handle``. A case may carry
setup deliveries (run first, not recorded) and synthetic rule rows seeded through
``RuleDefinitionManager``. A setup delivery inherits the case's other keys but never its
``payload`` or ``event`` (``pipeline_corpus.setup_delivery``).

Every collaborator kind the manifest names is wrapped in a recording proxy where it is injected:
the ``EventHandlers`` keyword arguments, and the manager's memory manager, webhook dispatcher and
broadcaster, agent-run manager and session coordinator; the manager's machine-id and project
resolution methods are wrapped where they are defined. The proxy logs each attribute the pipeline
reads from a collaborator, in order, and delegates to the real collaborator. A collaborator that
leaves the process or runs background work (the commit auto-link worker, the code-index trigger,
the terminal runtime registry and the attention store) has no real object here; when a case
names it, the proxy stands in for it and answers ``None`` unless ``collaborator_script`` lists a
result for that attribute.
A ``collaborator_failure`` case names the failing collaborator and ``failure`` (``method`` and
``error``), and reading that attribute raises ``CollaboratorFailure``. Blocking webhooks go
through the real dispatcher; only its HTTP transport is replaced by the case's ``webhook``
reply.

``GOBBY_CORPUS_RECORD=1`` writes the observed blocks back into the case files. A default run
replays and compares.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import functools
import inspect
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.git import GitAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.config.app import DaemonConfig
from gobby.config.extensions import WebhookEndpointConfig, WebhooksConfig
from gobby.hooks.broadcaster import HookEventBroadcaster
from gobby.hooks.checkout_reads import delivery_scope
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.receipt_effects import worker_staging_scope
from gobby.hooks.webhooks import WebhookDispatcher
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils import machine_id as machine_id_module
from tests.contracts.pipeline_corpus import setup_delivery
from tests.contracts.rules_recorder import (
    FIXED_TIMESTAMP,
    _hub_database,
    _jsonable,
    _seed_rules,
    response_defaults,
)

RECORD_ENV = "GOBBY_CORPUS_RECORD"
CORPUS_DIR = Path(__file__).resolve().parent / "pipeline"
PROJECT_ID = "5d0c0e4e-1f2a-4b3c-8d4e-5f6a7b8c9d0e"
PROJECT_NAME = "synthetic-app"
MACHINE_ID = "7a1e2b3c-4d5e-4f60-8172-839405162738"
USER_ID = "8b1e2b3c-4d5e-4f60-8172-839405162738"
# Placeholders a payload may carry. The replay swaps in the fixture checkout and the hub's
# machine id, which differ per run, so the committed JSON holds no machine-specific value.
PROJECT_PLACEHOLDER = "<project>"
MACHINE_PLACEHOLDER = "<machine>"
ADAPTERS: dict[str, Any] = {
    "claude": ClaudeCodeAdapter,
    "codex": CodexHooksAdapter,
    "droid": DroidAdapter,
    "grok": GrokAdapter,
    "agy": AgyAdapter,
    "git": GitAdapter,
}
# Collaborators with no real object in this harness: each one leaves the process or starts
# background work, so the recording proxy stands in for it.
STAND_IN_COLLABORATORS = frozenset(
    {
        "session_end_auto_link_worker",
        "code_index_trigger",
        "terminal_runtime_registry",
        "attention_metadata_store",
    }
)
# Collaborators the manager itself holds and reads, by the manager attribute that holds them.
MANAGER_COLLABORATORS = {
    "memory_manager": "_memory_manager",
    "webhook_dispatcher": "_webhook_dispatcher",
    "broadcaster": "broadcaster",
    "agent_run_manager": "_agent_run_manager",
    "session_coordinator": "_session_coordinator",
}
# Callables the manager owns and injects into the handlers; the manager method is wrapped, so
# the manager's own calls and the handlers' calls are both recorded.
MANAGER_CALLABLES = {
    "get_machine_id": "get_machine_id",
    "resolve_project_id": "_resolve_project_id",
}
HANDLER_PARAMETERS = frozenset(inspect.signature(EventHandlers.__init__).parameters)
# The handler attribute of a kind whose attribute name is not the kind with a leading underscore.
HANDLER_ATTRIBUTES = {"terminal_manager": "terminal_manager"}
# A failure whose method is this marker fails the handler's read of the injected attribute
# itself. It covers a collaborator the handler only hands on to a helper.
ATTRIBUTE_READ = "(attribute read)"
# Seconds the replay waits for the adapter worker; a case that hangs fails instead of blocking.
WORKER_TIMEOUT_SECONDS = 60.0


@functools.cache
def collaborator_kinds() -> frozenset[str]:
    """The collaborator kinds the committed manifest names; only these are wrapped."""
    manifest = json.loads((CORPUS_DIR / "manifest.json").read_text())
    return frozenset(str(name) for name in manifest.get("collaborators", []))


class CollaboratorFailure(RuntimeError):
    """The failure a ``collaborator_failure`` case injects into one collaborator."""


class _CallLog:
    """Ordered collaborator reads for one delivery, plus the injected failure."""

    def __init__(self, case: Mapping[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.recording = False
        self.script: dict[tuple[str, str], Any] = {
            (str(entry["collaborator"]), str(entry["method"])): entry.get("result")
            for entry in case.get("collaborator_script", [])
        }
        failure = case.get("failure") or {}
        self.failing: tuple[str, str] | None = None
        if case.get("failing_collaborator") is not None:
            self.failing = (str(case["failing_collaborator"]), str(failure.get("method", "")))
        self.error = str(failure.get("error", "injected collaborator failure"))
        self._lock = threading.Lock()

    def note(self, collaborator: str, method: str, outcome: str) -> None:
        if not self.recording:
            return
        with self._lock:
            self.calls.append({"collaborator": collaborator, "method": method, "outcome": outcome})

    def scripted(self, collaborator: str, method: str) -> tuple[bool, Any]:
        key = (collaborator, method)
        return (key in self.script, copy.deepcopy(self.script.get(key)))

    def fails(self, collaborator: str, method: str) -> bool:
        """Whether this read fails; failures apply to the recorded delivery only."""
        return self.recording and self.failing == (collaborator, method)

    def check(self, collaborator: str, method: str) -> None:
        """Log one read, raising the injected failure when this read is the failing one."""
        if self.fails(collaborator, method):
            self.note(collaborator, method, "failed")
            raise CollaboratorFailure(f"{collaborator}.{method}: {self.error}")
        self.note(collaborator, method, "ok")


class _Recorded:
    """A recording proxy for one collaborator kind."""

    def __init__(self, kind: str, real: Any, log: _CallLog) -> None:
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_log", log)

    def _read(self, attribute: str) -> Any:
        kind: str = object.__getattribute__(self, "_kind")
        real = object.__getattribute__(self, "_real")
        log: _CallLog = object.__getattribute__(self, "_log")
        has_script, result = log.scripted(kind, attribute)
        if has_script and not log.fails(kind, attribute):
            log.note(kind, attribute, "scripted")
        else:
            log.check(kind, attribute)
        if has_script:
            return _answer(real, attribute, result)
        if real is None:
            return _answer(None, attribute, None)
        return getattr(real, attribute)

    def __getattr__(self, attribute: str) -> Any:
        if attribute.startswith("__"):
            raise AttributeError(attribute)
        return object.__getattribute__(self, "_read")(attribute)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        target = object.__getattribute__(self, "_read")("__call__")
        return target(*args, **kwargs)


def _answer(real: Any, attribute: str, result: Any) -> Any:
    """A callable returning ``result``, async when the real attribute is a coroutine function."""
    original = getattr(real, attribute, None) if real is not None else None
    if original is not None and asyncio.iscoroutinefunction(original):

        async def answer_async(*_args: Any, **_kwargs: Any) -> Any:
            return result

        return answer_async

    def answer(*_args: Any, **_kwargs: Any) -> Any:
        return result

    if attribute == "__call__" or original is None or callable(original):
        return answer
    return result


def _fill(value: Any, project: str) -> Any:
    """Replace the payload placeholders with this run's checkout path and machine id."""
    if isinstance(value, str):
        return value.replace(PROJECT_PLACEHOLDER, project).replace(MACHINE_PLACEHOLDER, MACHINE_ID)
    if isinstance(value, list):
        return [_fill(item, project) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, project) for key, item in value.items()}
    return value


def _seed_identity(db: PostgresHubDatabase) -> None:
    db.execute(
        "INSERT INTO users (id, email, name, password_hash) VALUES (%s, %s, %s, %s)",
        (USER_ID, "corpus@example.invalid", "synthetic-corpus", "unused"),
    )
    db.execute(
        "INSERT INTO machines (id, owner_user_id) VALUES (%s, %s)",
        (MACHINE_ID, USER_ID),
    )


def _seed_node_checkout(db: PostgresHubDatabase, spec: Mapping[str, Any] | None) -> None:
    """Register a node machine and its checkout of the synthetic project, as a node pairing does."""
    if spec is None:
        return
    db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (PROJECT_ID, PROJECT_NAME),
    )
    db.execute(
        "INSERT INTO machines (id, owner_user_id) VALUES (%s, %s)",
        (spec["machine_id"], USER_ID),
    )
    db.execute(
        "INSERT INTO project_checkouts (machine_id, project_id, root_path) VALUES (%s, %s, %s)",
        (spec["machine_id"], PROJECT_ID, spec["root_path"]),
    )


@contextlib.contextmanager
def _checkout() -> Iterator[str]:
    """A temporary checkout carrying the synthetic project's marker file."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve() / "app"
        (root / ".gobby").mkdir(parents=True)
        (root / ".gobby" / "project.json").write_text(
            json.dumps({"id": PROJECT_ID, "name": PROJECT_NAME})
        )
        yield str(root)


def _config(case: Mapping[str, Any]) -> DaemonConfig:
    config = DaemonConfig()
    webhook = case.get("webhook")
    if webhook is not None:
        config.hook_extensions.webhooks = WebhooksConfig(
            enabled=True,
            endpoints=[WebhookEndpointConfig.model_validate(webhook["endpoint"])],
        )
    return config


def _mock_transport(webhook: Mapping[str, Any] | None) -> httpx.MockTransport:
    reply = dict((webhook or {}).get("reply") or {"status": 200, "json": {}})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(int(reply.get("status", 200)), json=reply.get("json", {}))

    return httpx.MockTransport(handler)


def _recorded_method(kind: str, original: Callable[..., Any], log: _CallLog) -> Callable[..., Any]:
    """Wrap a manager-owned callable so each call is logged under its collaborator kind."""

    @functools.wraps(original)
    def recorded(owner: Any, *args: Any, **kwargs: Any) -> Any:
        log.check(kind, "__call__")
        return original(owner, *args, **kwargs)

    return recorded


def _failing_attribute(kind: str, attribute: str, log: _CallLog) -> property:
    """An injected attribute whose read fails while the recorded delivery runs."""
    storage = f"_corpus_{attribute}"

    def read(owner: Any) -> Any:
        log.check(kind, ATTRIBUTE_READ)
        return owner.__dict__.get(storage)

    def write(owner: Any, value: Any) -> None:
        owner.__dict__[storage] = value

    return property(read, write)


def _patched_harness(case: Mapping[str, Any], log: _CallLog, stack: contextlib.ExitStack) -> None:
    named = {str(entry["collaborator"]) for entry in case.get("collaborator_script", [])}
    if case.get("failing_collaborator") is not None:
        named.add(str(case["failing_collaborator"]))
    original_init = EventHandlers.__init__

    def init(handlers: EventHandlers, **kwargs: Any) -> None:
        # A named stand-in the factory does not pass (the attention store) is injected here.
        kwargs.update({kind: None for kind in named & HANDLER_PARAMETERS if kind not in kwargs})
        for kind, real in list(kwargs.items()):
            if kind not in collaborator_kinds() or kind in MANAGER_CALLABLES:
                continue
            if kind in STAND_IN_COLLABORATORS:
                real = None
            if real is None and kind not in named:
                kwargs[kind] = None
                continue
            kwargs[kind] = _Recorded(kind, real, log)
        original_init(handlers, **kwargs)

    stack.enter_context(mock.patch.object(EventHandlers, "__init__", init))
    for kind, method in MANAGER_CALLABLES.items():
        stack.enter_context(
            mock.patch.object(
                HookManager, method, _recorded_method(kind, getattr(HookManager, method), log)
            )
        )
    failing = log.failing
    if failing is not None and failing[1] == ATTRIBUTE_READ:
        kind = failing[0]
        # A kind both objects hold fails where the handlers read it.
        on_handlers = kind in HANDLER_PARAMETERS
        owner: type = EventHandlers if on_handlers else HookManager
        attribute = (
            HANDLER_ATTRIBUTES.get(kind, f"_{kind}") if on_handlers else MANAGER_COLLABORATORS[kind]
        )
        stack.enter_context(
            mock.patch.object(
                owner, attribute, _failing_attribute(kind, attribute, log), create=True
            )
        )
    stack.enter_context(mock.patch.object(machine_id_module, "_cached_machine_id", MACHINE_ID))
    monitor = stack.enter_context(mock.patch("gobby.hooks.factory.HealthMonitor", autospec=True))
    monitor.return_value.get_cached_status.return_value = (True, None, "running", None)
    monitor.return_value.check_now.return_value = True
    transport = _mock_transport(case.get("webhook"))

    def new_client(dispatcher: WebhookDispatcher) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=dispatcher.config.default_timeout)

    stack.enter_context(mock.patch.object(WebhookDispatcher, "_new_client", new_client))


def _build_manager(db: PostgresHubDatabase, case: Mapping[str, Any], log: _CallLog) -> HookManager:
    manager = HookManager(
        database=db,
        session_manager=SessionManager(db),
        config=_config(case),
        broadcaster=HookEventBroadcaster(None, DaemonConfig()),
    )
    for kind, attribute in MANAGER_COLLABORATORS.items():
        setattr(manager, attribute, _Recorded(kind, getattr(manager, attribute), log))
    return manager


def _direct_event(spec: Mapping[str, Any], project: str) -> HookEvent:
    """The event for a type no adapter produces, carried as the handler-map input itself."""
    data = _fill(copy.deepcopy(dict(spec.get("data", {}))), project)
    return HookEvent(
        event_type=HookEventType(spec["type"]),
        session_id=str(spec["session_id"]),
        source=SessionSource(spec.get("source", "claude")),
        timestamp=FIXED_TIMESTAMP,
        data=data,
        machine_id=MACHINE_ID,
        cwd=data.get("cwd"),
    )


class _Delivery:
    """One delivery's observed outcome: the manager's response and the adapter's output."""

    def __init__(self) -> None:
        self.response: HookResponse | None = None
        self.event_type: str | None = None
        self.native: Any = None
        self.raised: str | None = None


def _deliver(
    manager: HookManager,
    delivery: Mapping[str, Any],
    project: str,
    loop: asyncio.AbstractEventLoop,
) -> _Delivery:
    """Run one delivery the way the route's adapter worker does."""
    observed = _Delivery()
    original_handle = manager.handle

    def handle(event: HookEvent) -> HookResponse:
        observed.event_type = event.event_type.value
        observed.response = original_handle(event)
        return observed.response

    try:
        with mock.patch.object(manager, "handle", handle):
            if "event" in delivery:
                handle(_direct_event(delivery["event"], project))
                return observed
            payload = _fill(copy.deepcopy(dict(delivery["payload"])), project)
            source = str(delivery.get("source", "claude"))
            envelope = {"hook_type": delivery["hook_type"], "input_data": payload, "source": source}
            adapter = ADAPTERS[source](hook_manager=manager)
            with (
                worker_staging_scope(),
                delivery_scope(payload.get("machine_id"), loop, payload.get("cwd") or ""),
            ):
                observed.native = adapter.handle_native(envelope, manager)
    except Exception as exc:
        observed.raised = type(exc).__name__
    return observed


def _response_block(response: HookResponse | None) -> dict[str, Any]:
    if response is None:
        return {}
    block: dict[str, Any] = {}
    for name, default in response_defaults().items():
        value = getattr(response, name)
        if value != default:
            block[name] = _jsonable(value)
    return block


async def _run(db: PostgresHubDatabase, case: Mapping[str, Any], project: str) -> dict[str, Any]:
    log = _CallLog(case)
    loop = asyncio.get_running_loop()
    with contextlib.ExitStack() as stack:
        _patched_harness(case, log, stack)
        manager = _build_manager(db, case, log)
        try:
            for setup in case.get("setup", []):
                await asyncio.wait_for(
                    asyncio.to_thread(
                        _deliver, manager, setup_delivery(case, setup), project, loop
                    ),
                    WORKER_TIMEOUT_SECONDS,
                )
            log.recording = True
            target = {
                key: case[key] for key in ("source", "hook_type", "payload", "event") if key in case
            }
            observed = await asyncio.wait_for(
                asyncio.to_thread(_deliver, manager, target, project, loop),
                WORKER_TIMEOUT_SECONDS,
            )
            # Broadcasts are scheduled on this loop; let them run before the manager closes.
            for _ in range(3):
                await asyncio.sleep(0)
            log.recording = False
        finally:
            await manager.shutdown_async()
    expect: dict[str, Any] = {
        "event_type": observed.event_type,
        "response": _response_block(observed.response),
        "native_output": _jsonable(observed.native),
        "collaborator_calls": log.calls,
    }
    if observed.raised is not None:
        expect["raised"] = observed.raised
    return expect


def _observe(case: Mapping[str, Any]) -> dict[str, Any]:
    with _hub_database() as db, _checkout() as project:
        _seed_identity(db)
        _seed_node_checkout(db, case.get("node_checkout"))
        _seed_rules(db, list(case.get("rules", [])))
        return asyncio.run(_run(db, case, project))


def replay_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the Python pipeline and return the observed ``expect`` block."""
    return _observe(case)


def record_corpus(corpus_dir: Path, writer: Callable[[Path, dict[str, Any]], None]) -> None:
    """Fill each case's ``expect`` block with the recorded pipeline output.

    Runs only when ``GOBBY_CORPUS_RECORD`` is 1; otherwise the corpus files are left unchanged.
    """
    if os.environ.get(RECORD_ENV) != "1":
        return
    for path in sorted((corpus_dir / "cases").glob("*.json")):
        document = json.loads(path.read_text())
        for case in document["cases"]:
            case["expect"] = replay_case(case)
        writer(path, document)

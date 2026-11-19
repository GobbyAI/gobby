"""Facade tests for initialize, invoke and the ordering rules (WP-2, R2a red).

Every test drives WorkerFacade.handle with bytes, as the Rust client does. The runner
is a fake create coroutine that runs the five phase scopes of design section 5.7 and
records what happened, so no real GobbyRunner is started.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gobby.hub.caller_scope import current_caller, require_machine_owned
from gobby.runner_init import StartRefusal
from gobby.utils.session_context import is_daemon_execution
from gobby.worker.decode import Int, IsoDatetime, Str
from gobby.worker.facade import WorkerFacade
from gobby.worker.gate import _ACTIVE, phase_scope
from gobby.worker.ops import OPS, DomainOp
from gobby.worker.phase_ids import InitPhase
from gobby.worker.protocol import (
    ErrorBody,
    ErrorCode,
    InitializeResult,
    Response,
    encode_response,
)

CREDENTIAL = "corpus-credential"
GENERATION = 7
PHASES: tuple[str, ...] = (
    "storage_and_config",
    "content_refusal",
    "startup_content",
    "config_runtime",
    "runtime_services",
)
_TESTS_DIR = Path(__file__).resolve().parents[1]
CORPUS_PATH = _TESTS_DIR / "contracts" / "worker_protocol.json"
INVENTORY_PATH = _TESTS_DIR / "contracts" / "runner_lifecycle.json"

Step = Callable[[], Awaitable[None]]
Create = Callable[[], Awaitable[object]]

# Synthetic op state. Events are created inside each test, on the test's loop.
_EVENTS: dict[str, asyncio.Event] = {}
_PENDING: dict[str, asyncio.Future[str]] = {}
_FUTURE_LOOPS: list[asyncio.AbstractEventLoop] = []
_RECEIVED: list[object] = []


class OpStop(BaseException):
    """A BaseException raised by an op: the facade must propagate it with no response."""


class Trace:
    """What the fake runner did, in order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.unusable_at_rollback: list[bool | None] = []
        self.rollback_done = False


def make_create(
    trace: Trace,
    *,
    steps: Mapping[InitPhase, Step] | None = None,
    rollback: Step | None = None,
) -> Create:
    """A stand-in for GobbyRunner.create with the same phase-scope shape as section 5.8."""

    async def create() -> object:
        owner = object()
        try:
            for phase in InitPhase:
                async with phase_scope(phase, owner):
                    trace.events.append(phase.value)
                    step = None if steps is None else steps.get(phase)
                    if step is not None:
                        await step()
            trace.events.append("returned")
            return owner
        except BaseException:
            gate = _ACTIVE.get()
            trace.unusable_at_rollback.append(None if gate is None else gate.unusable)
            trace.events.append("rollback")
            if rollback is not None:
                await rollback()
            trace.rollback_done = True
            raise

    return create


def envelope(
    command: str, body: object, *, generation: int = GENERATION, operation_id: str | None = None
) -> bytes:
    obj = {
        "protocol_version": 1,
        "generation": generation,
        "operation_id": operation_id or f"{generation}:1",
        "command": command,
        "body": body,
    }
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def handshake_bytes(generation: int = GENERATION) -> bytes:
    return envelope("handshake", {"credential": CREDENTIAL}, generation=generation)


def initialize_bytes(phase: str, generation: int = GENERATION) -> bytes:
    return envelope("initialize", {"phase": phase}, generation=generation)


def invoke_bytes(
    op: str, params: Mapping[str, object] | None = None, *, generation: int = GENERATION
) -> bytes:
    return envelope("invoke", {"op": op, "params": dict(params or {})}, generation=generation)


def domain_op(name: str, function: str, params: Mapping[str, Any] | None = None) -> DomainOp:
    return DomainOp(name=name, func=f"{__name__}:{function}", params=dict(params or {}))


async def send(facade: WorkerFacade, raw: bytes) -> dict[str, Any]:
    reply: dict[str, Any] = json.loads(await facade.handle(raw))
    return reply


def error_code(reply: Mapping[str, Any]) -> str:
    return str(reply["error"]["code"])


async def bind(facade: WorkerFacade, generation: int = GENERATION) -> None:
    reply = await send(facade, handshake_bytes(generation))
    assert "result" in reply, reply


async def initialize_through(facade: WorkerFacade, phases: Sequence[str]) -> None:
    for phase in phases:
        reply = await send(facade, initialize_bytes(phase))
        assert reply["result"]["status"] == "ready", reply


async def ready_facade(
    *,
    ops: Mapping[str, DomainOp] | None = None,
    steps: Mapping[InitPhase, Step] | None = None,
) -> tuple[WorkerFacade, Trace]:
    trace = Trace()
    facade = WorkerFacade(
        credential=CREDENTIAL,
        ops=OPS if ops is None else ops,
        create=make_create(trace, steps=steps),
    )
    await bind(facade)
    await initialize_through(facade, PHASES)
    return facade, trace


async def refuse_content() -> None:
    raise StartRefusal("bundled content is refused")


async def fail_startup() -> None:
    raise RuntimeError("boom")


async def hold_op() -> str:
    _EVENTS["hold_started"].set()
    await _EVENTS["hold_release"].wait()
    return "slow-done"


async def quick_op() -> str:
    return "fast-done"


async def await_pending_op() -> str:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    _PENDING["future"] = future
    _FUTURE_LOOPS.append(future.get_loop())
    _EVENTS["pending_started"].set()
    return await future


async def resolve_pending_op() -> str:
    _PENDING["future"].set_result("resolved")
    return "sent"


def echo_op(n: int) -> int:
    return n


def boom_op() -> None:
    raise ValueError("bad value")


def stop_op() -> None:
    raise OpStop("stop")


def caller_probe() -> dict[str, bool]:
    require_machine_owned(None)
    return {
        "caller_bound": current_caller() is not None,
        "daemon_execution": is_daemon_execution(),
    }


def return_unencodable(kind: str) -> object:
    values: dict[str, object] = {
        "object": object(),
        "bytes": b"raw",
        "nan": float("nan"),
        "module": json,
        "set_unencodable": {object()},
        "dict_int_key": {1: "x"},
    }
    return values[kind]


def record_when(at: datetime) -> str:
    _RECEIVED.append(at)
    return at.isoformat()


async def test_initialize_runs_phases_without_run_daemon() -> None:
    trace = Trace()
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=make_create(trace))
    await bind(facade)
    for index, phase in enumerate(PHASES):
        following = PHASES[index + 1] if index + 1 < len(PHASES) else None
        reply = await send(facade, initialize_bytes(phase))
        assert reply["result"] == {
            "phase": phase,
            "status": "ready",
            "next_phase": following,
            "message": None,
            "error_type": None,
        }
    # The create coroutine returns after the last phase; no serve step runs in between.
    assert trace.events == [*PHASES, "returned"]
    assert trace.rollback_done is False


async def test_out_of_order_phase_is_refused() -> None:
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=make_create(Trace()))
    await bind(facade)
    reply = await send(facade, initialize_bytes("config_runtime"))
    assert error_code(reply) == "phase_out_of_order"
    reply = await send(facade, initialize_bytes("storage_and_config"))
    assert reply["result"]["status"] == "ready"
    repeat = await send(facade, initialize_bytes("storage_and_config"))
    assert error_code(repeat) == "phase_out_of_order"
    skipped = await send(facade, initialize_bytes("startup_content"))
    assert error_code(skipped) == "phase_out_of_order"
    # A refused order check changed nothing: the next phase still runs.
    reply = await send(facade, initialize_bytes("content_refusal"))
    assert reply["result"]["status"] == "ready"


async def test_refused_phase_rolls_back() -> None:
    trace = Trace()
    facade = WorkerFacade(
        credential=CREDENTIAL,
        ops=OPS,
        create=make_create(trace, steps={InitPhase.CONTENT_REFUSAL: refuse_content}),
    )
    await bind(facade)
    await initialize_through(facade, ["storage_and_config"])
    reply = await send(facade, initialize_bytes("content_refusal"))
    assert reply["result"] == {
        "phase": "content_refusal",
        "status": "refused",
        "next_phase": None,
        "message": "bundled content is refused",
        "error_type": None,
    }
    # The reply came after the rollback finished.
    assert trace.rollback_done
    assert "startup_content" not in trace.events
    after = await send(facade, initialize_bytes("startup_content"))
    assert error_code(after) == "worker_unusable"


async def test_failed_phase_rolls_back() -> None:
    trace = Trace()
    facade = WorkerFacade(
        credential=CREDENTIAL,
        ops=OPS,
        create=make_create(trace, steps={InitPhase.STARTUP_CONTENT: fail_startup}),
    )
    await bind(facade)
    await initialize_through(facade, ["storage_and_config", "content_refusal"])
    reply = await send(facade, initialize_bytes("startup_content"))
    assert reply["result"] == {
        "phase": "startup_content",
        "status": "failed",
        "next_phase": None,
        "message": "boom",
        "error_type": "RuntimeError",
    }
    assert trace.rollback_done
    after = await send(facade, initialize_bytes("config_runtime"))
    assert error_code(after) == "worker_unusable"


async def test_cancelled_phase_rolls_back_and_propagates() -> None:
    entered = asyncio.Event()
    never = asyncio.Event()

    async def block() -> None:
        entered.set()
        await never.wait()

    trace = Trace()
    facade = WorkerFacade(
        credential=CREDENTIAL,
        ops=OPS,
        create=make_create(trace, steps={InitPhase.CONTENT_REFUSAL: block}),
    )
    await bind(facade)
    await initialize_through(facade, ["storage_and_config"])
    waiter = asyncio.create_task(facade.handle(initialize_bytes("content_refusal")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert trace.rollback_done
    after = await send(facade, initialize_bytes("startup_content"))
    assert error_code(after) == "worker_unusable"


async def test_init_phases_match_inventory() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    construct = [entry["id"] for entry in inventory["phases"] if entry.get("stage") == "construct"]
    assert tuple(construct) == PHASES
    trace = Trace()
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=make_create(trace))
    await bind(facade)
    for phase_id in construct:
        reply = await send(facade, initialize_bytes(phase_id))
        assert reply["result"]["status"] == "ready", reply
        assert reply["result"]["phase"] == phase_id
    assert trace.events == [*construct, "returned"]


def test_response_vectors_round_trip() -> None:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    for version in corpus["versions"]:
        for vector in version["responses"]:
            raw = vector["response_bytes"].encode("utf-8")
            response = Response.model_validate_json(raw)
            assert encode_response(response) == raw, vector["name"]
            body = json.loads(raw)
            if "result" in body:
                InitializeResult.model_validate(body["result"])
            else:
                ErrorBody.model_validate(body["error"])
                assert ErrorCode(body["error"]["code"]).value == body["error"]["code"]
    # A response carries exactly one of result or error.
    both = {
        "protocol_version": 1,
        "generation": GENERATION,
        "operation_id": "7:1",
        "result": 1,
        "error": {"code": "not_ready", "message": "worker is not ready"},
    }
    with pytest.raises(ValidationError):
        Response.model_validate(both)
    neither = {"protocol_version": 1, "generation": GENERATION, "operation_id": "7:1"}
    with pytest.raises(ValidationError):
        Response.model_validate(neither)


async def test_concurrent_duplicate_initialize_runs_once() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    trace = Trace()
    facade = WorkerFacade(
        credential=CREDENTIAL,
        ops=OPS,
        create=make_create(trace, steps={InitPhase.STORAGE_AND_CONFIG: hold}),
    )
    await bind(facade)
    first = asyncio.create_task(facade.handle(initialize_bytes("storage_and_config")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    duplicate = asyncio.create_task(facade.handle(initialize_bytes("storage_and_config")))
    for _ in range(3):
        await asyncio.sleep(0)
    release.set()
    replies = [json.loads(raw) for raw in await asyncio.gather(first, duplicate)]
    results = [reply for reply in replies if "result" in reply]
    errors = [reply for reply in replies if "error" in reply]
    assert len(results) == 1
    assert results[0]["result"]["status"] == "ready"
    assert [error_code(reply) for reply in errors] == ["phase_out_of_order"]
    assert trace.events.count("storage_and_config") == 1


async def test_rollback_failure_keeps_facade_unusable() -> None:
    # A rollback that raises: the phase reports failed, and the facade stays unusable.
    async def broken_rollback() -> None:
        raise RuntimeError("rollback broke")

    facade = WorkerFacade(
        credential=CREDENTIAL,
        ops=OPS,
        create=make_create(
            Trace(),
            steps={InitPhase.CONTENT_REFUSAL: refuse_content},
            rollback=broken_rollback,
        ),
    )
    await bind(facade)
    await initialize_through(facade, ["storage_and_config"])
    reply = await send(facade, initialize_bytes("content_refusal"))
    assert reply["result"] == {
        "phase": "content_refusal",
        "status": "failed",
        "next_phase": None,
        "message": "rollback failed: rollback broke",
        "error_type": "RuntimeError",
    }
    later = await send(facade, initialize_bytes("startup_content"))
    assert error_code(later) == "worker_unusable"

    # A rollback that is cancelled: the cancellation propagates, with no response.
    async def cancelled_rollback() -> None:
        raise asyncio.CancelledError()

    cancelled = WorkerFacade(
        credential=CREDENTIAL,
        ops=OPS,
        create=make_create(
            Trace(),
            steps={InitPhase.CONTENT_REFUSAL: refuse_content},
            rollback=cancelled_rollback,
        ),
    )
    await bind(cancelled)
    await initialize_through(cancelled, ["storage_and_config"])
    with pytest.raises(asyncio.CancelledError):
        await cancelled.handle(initialize_bytes("content_refusal"))
    after = await send(cancelled, initialize_bytes("startup_content"))
    assert error_code(after) == "worker_unusable"


async def test_unusable_is_set_before_rollback_starts() -> None:
    trace = Trace()
    facade = WorkerFacade(
        credential=CREDENTIAL,
        ops=OPS,
        create=make_create(trace, steps={InitPhase.CONTENT_REFUSAL: refuse_content}),
    )
    await bind(facade)
    await initialize_through(facade, ["storage_and_config"])
    reply = await send(facade, initialize_bytes("content_refusal"))
    assert reply["result"]["status"] == "refused"
    # The gate was already unusable when the rollback began (R5).
    assert trace.unusable_at_rollback == [True]
    assert trace.rollback_done


async def test_invoke_async_op_does_not_block_other_invoke() -> None:
    _EVENTS["hold_started"] = asyncio.Event()
    _EVENTS["hold_release"] = asyncio.Event()
    ops = {
        "hold": domain_op("hold", "hold_op"),
        "quick": domain_op("quick", "quick_op"),
    }
    facade, _ = await ready_facade(ops=ops)
    slow = asyncio.create_task(facade.handle(invoke_bytes("hold")))
    await asyncio.wait_for(_EVENTS["hold_started"].wait(), timeout=5)
    fast = json.loads(await asyncio.wait_for(facade.handle(invoke_bytes("quick")), timeout=5))
    assert fast["result"] == "fast-done"
    assert not slow.done()
    _EVENTS["hold_release"].set()
    pending = json.loads(await asyncio.wait_for(slow, timeout=5))
    assert pending["result"] == "slow-done"


async def test_async_op_future_resolves_on_worker_loop() -> None:
    _EVENTS["pending_started"] = asyncio.Event()
    _FUTURE_LOOPS.clear()
    ops = {
        "await_pending": domain_op("await_pending", "await_pending_op"),
        "resolve_pending": domain_op("resolve_pending", "resolve_pending_op"),
    }
    facade, _ = await ready_facade(ops=ops)
    waiter = asyncio.create_task(facade.handle(invoke_bytes("await_pending")))
    await asyncio.wait_for(_EVENTS["pending_started"].wait(), timeout=5)
    sent = json.loads(await asyncio.wait_for(facade.handle(invoke_bytes("resolve_pending")), 5))
    assert sent["result"] == "sent"
    pending = json.loads(await asyncio.wait_for(waiter, timeout=5))
    assert pending["result"] == "resolved"
    assert _FUTURE_LOOPS == [asyncio.get_running_loop()]


async def test_invoke_before_ready_is_not_ready() -> None:
    ops = {"quick": domain_op("quick", "quick_op")}
    facade = WorkerFacade(credential=CREDENTIAL, ops=ops, create=make_create(Trace()))
    await bind(facade)
    assert error_code(await send(facade, invoke_bytes("anything"))) == "not_ready"
    assert error_code(await send(facade, invoke_bytes("quick"))) == "not_ready"
    await initialize_through(facade, PHASES[:-1])
    assert error_code(await send(facade, invoke_bytes("quick"))) == "not_ready"
    await initialize_through(facade, PHASES[-1:])
    reply = await send(facade, invoke_bytes("quick"))
    assert reply["result"] == "fast-done"


async def test_invoke_after_unusable_is_worker_unusable() -> None:
    ops = {"quick": domain_op("quick", "quick_op")}
    facade = WorkerFacade(
        credential=CREDENTIAL,
        ops=ops,
        create=make_create(Trace(), steps={InitPhase.CONTENT_REFUSAL: refuse_content}),
    )
    await bind(facade)
    await initialize_through(facade, ["storage_and_config"])
    refused = await send(facade, initialize_bytes("content_refusal"))
    assert refused["result"]["status"] == "refused"
    assert error_code(await send(facade, invoke_bytes("quick"))) == "worker_unusable"


async def test_request_before_handshake_refused() -> None:
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=make_create(Trace()))
    raws = [
        # Step 7 precedes the body checks, so even a malformed body is not_handshaken.
        envelope("invoke", {"op": 5}),
        invoke_bytes("anything"),
        envelope("frobnicate", {}),
        initialize_bytes("bogus"),
        initialize_bytes("storage_and_config"),
    ]
    for raw in raws:
        assert error_code(await send(facade, raw)) == "not_handshaken"


async def test_stale_generation_refused() -> None:
    ops = {"quick": domain_op("quick", "quick_op")}
    facade, _ = await ready_facade(ops=ops)
    raws = [
        invoke_bytes("quick", generation=8),
        # Generation precedes the command check, so an unknown op is still stale.
        invoke_bytes("no_such_op", generation=8),
        initialize_bytes("content_refusal", generation=8),
        handshake_bytes(8),
    ]
    for raw in raws:
        assert error_code(await send(facade, raw)) == "stale_generation"
    reply = await send(facade, invoke_bytes("quick"))
    assert reply["result"] == "fast-done"


async def test_unknown_command_after_handshake() -> None:
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=make_create(Trace()))
    await bind(facade)
    assert error_code(await send(facade, envelope("frobnicate", {}))) == "unknown_command"
    # Command names are exact: a different case is an unknown command.
    raw = envelope("Invoke", {"op": "quick", "params": {}})
    assert error_code(await send(facade, raw)) == "unknown_command"


async def test_unknown_op_refused() -> None:
    facade, _ = await ready_facade()
    assert error_code(await send(facade, invoke_bytes("no_such_op"))) == "unknown_command"
    # The op lookup precedes the params check.
    raw = invoke_bytes("no_such_op", {"x": 1})
    assert error_code(await send(facade, raw)) == "unknown_command"


async def test_facade_error_ordering() -> None:
    ops = {
        "echo": domain_op("echo", "echo_op", {"n": Int()}),
        "quick": domain_op("quick", "quick_op"),
    }
    # Bound but not ready: the body shape is checked first, and params after readiness.
    bound = WorkerFacade(credential=CREDENTIAL, ops=ops, create=make_create(Trace()))
    await bind(bound)
    extra = envelope("invoke", {"op": "echo", "params": {}, "extra": 1})
    assert error_code(await send(bound, extra)) == "invalid_request"
    assert error_code(await send(bound, invoke_bytes("echo", {"n": "x"}))) == "not_ready"

    # Unusable: the body shape comes first, then worker_unusable, then phase order.
    refusing = WorkerFacade(
        credential=CREDENTIAL,
        ops=ops,
        create=make_create(Trace(), steps={InitPhase.CONTENT_REFUSAL: refuse_content}),
    )
    await bind(refusing)
    await initialize_through(refusing, ["storage_and_config"])
    await send(refusing, initialize_bytes("content_refusal"))
    assert error_code(await send(refusing, initialize_bytes("bogus"))) == "invalid_request"
    assert error_code(await send(refusing, initialize_bytes("config_runtime"))) == (
        "worker_unusable"
    )
    assert error_code(await send(refusing, extra)) == "invalid_request"
    assert error_code(await send(refusing, invoke_bytes("quick"))) == "worker_unusable"

    # Ready: an unknown op wins over bad params, and params are checked exactly.
    ready, _ = await ready_facade(ops=ops)
    assert error_code(await send(ready, invoke_bytes("no_such_op", {"x": 1}))) == (
        "unknown_command"
    )
    assert error_code(await send(ready, invoke_bytes("echo", {"n": "x"}))) == "invalid_request"
    assert error_code(await send(ready, invoke_bytes("echo", {}))) == "invalid_request"
    raw = invoke_bytes("echo", {"n": 1, "extra": 2})
    assert error_code(await send(ready, raw)) == "invalid_request"
    ok = await send(ready, invoke_bytes("echo", {"n": 1}))
    assert ok["result"] == 1

    # Initialize on a healthy facade: a bad phase value is invalid before order is checked.
    fresh = WorkerFacade(credential=CREDENTIAL, ops=ops, create=make_create(Trace()))
    await bind(fresh)
    await initialize_through(fresh, ["storage_and_config"])
    assert error_code(await send(fresh, initialize_bytes("config_runtime"))) == (
        "phase_out_of_order"
    )
    assert error_code(await send(fresh, initialize_bytes("bogus"))) == "invalid_request"


async def test_op_failed_on_exception() -> None:
    ops = {
        "boom": domain_op("boom", "boom_op"),
        "quick": domain_op("quick", "quick_op"),
    }
    facade, _ = await ready_facade(ops=ops)
    reply = await send(facade, invoke_bytes("boom"))
    assert error_code(reply) == "op_failed"
    assert reply["error"]["message"] == "bad value"
    # The op failure does not make the worker unusable.
    ok = await send(facade, invoke_bytes("quick"))
    assert ok["result"] == "fast-done"


async def test_base_exception_propagates() -> None:
    ops = {"stop": domain_op("stop", "stop_op")}
    facade, _ = await ready_facade(ops=ops)
    with pytest.raises(OpStop):
        await facade.handle(invoke_bytes("stop"))


async def test_worker_op_binds_no_caller() -> None:
    ops = {"probe": domain_op("probe", "caller_probe")}
    facade, _ = await ready_facade(ops=ops)
    reply = await send(facade, invoke_bytes("probe"))
    assert reply["result"] == {"caller_bound": False, "daemon_execution": False}


async def test_unencodable_result_is_op_failed() -> None:
    ops = {"ret": domain_op("ret", "return_unencodable", {"kind": Str()})}
    facade, _ = await ready_facade(ops=ops)
    kinds = ("object", "bytes", "nan", "module", "set_unencodable", "dict_int_key")
    for kind in kinds:
        reply = await send(facade, invoke_bytes("ret", {"kind": kind}))
        assert error_code(reply) == "op_failed", kind
        assert "result" not in reply, kind
        assert isinstance(reply["error"]["message"], str), kind


async def test_datetime_param_decoded() -> None:
    ops = {"when": domain_op("when", "record_when", {"at": IsoDatetime()})}
    facade, _ = await ready_facade(ops=ops)
    _RECEIVED.clear()
    reply = await send(facade, invoke_bytes("when", {"at": "2026-11-18T12:00:00+00:00"}))
    assert reply["result"] == "2026-11-18T12:00:00+00:00"
    assert _RECEIVED == [datetime(2026, 11, 18, 12, 0, tzinfo=UTC)]
    assert isinstance(_RECEIVED[0], datetime)
    bad = await send(facade, invoke_bytes("when", {"at": "not-a-date"}))
    assert error_code(bad) == "invalid_request"

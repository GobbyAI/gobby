"""Facade fault tests (R31, R47, R49): lone surrogates, registration faults, results that
cannot be encoded, op exception text, and invoke ordering.

Every test drives WorkerFacade.handle with bytes, as the Rust client does. Each reply must be
canonical UTF-8 JSON, and handle must answer rather than raise. The ops and registrations are
synthetic and defined here, so no bundled definition is named.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, make_dataclass
from typing import Any

import pytest

from gobby.worker.decode import DataclassFields, Int
from gobby.worker.facade import WorkerFacade
from gobby.worker.gate import phase_scope
from gobby.worker.ops import DomainOp
from gobby.worker.phase_ids import InitPhase

CREDENTIAL = "corpus-credential"
GENERATION = 7
OPERATION_ID = "7:1"
LONE_SURROGATE = chr(0xD800)
# How a lone surrogate reads once the facade has escaped it with backslashreplace.
ESCAPED_SURROGATE = "\\ud800"

Create = Callable[[], Coroutine[Any, Any, object]]


@dataclass
class Plain:
    label: str


@dataclass
class OutsideTable:
    ratio: float


# The annotation names a type that exists in no module, so typing.get_type_hints raises NameError.
Unresolvable = make_dataclass(
    "Unresolvable", [("label", "NoSuchTypeForRegistration")], module=__name__
)


def echo_op(n: int) -> int:
    return n


def zero_op() -> int:
    return 0


def label_of(record: Plain) -> str:
    return record.label


def raise_surrogate_op() -> None:
    raise ValueError("bad " + LONE_SURROGATE + " text")


def surrogate_result_op() -> str:
    return "bad " + LONE_SURROGATE


def surrogate_key_result_op() -> dict[str, int]:
    return {LONE_SURROGATE: 1}


def deep_result_op() -> object:
    value: object = []
    for _ in range(100_000):
        value = [value]
    return value


def cyclic_result_op() -> object:
    cycle: list[object] = []
    cycle.append(cycle)
    return cycle


def domain_op(name: str, function: str, params: Mapping[str, Any] | None = None) -> DomainOp:
    return DomainOp(name=name, func=f"{__name__}:{function}", params=dict(params or {}))


_SYNTHETIC_OPS: tuple[DomainOp, ...] = (
    domain_op("echo", "echo_op", {"n": Int()}),
    domain_op("zero", "zero_op"),
    domain_op("label", "label_of", {"record": DataclassFields(f"{__name__}:Plain")}),
    domain_op("raise_surrogate", "raise_surrogate_op"),
    domain_op("surrogate_result", "surrogate_result_op"),
    domain_op("surrogate_key_result", "surrogate_key_result_op"),
    domain_op("deep_result", "deep_result_op"),
    domain_op("cyclic_result", "cyclic_result_op"),
    # Registration faults. Each registration is broken in one way, one per named class.
    DomainOp(name="no_module", func="no_such_fault_module:run", params={}),
    DomainOp(name="no_module_int", func="no_such_fault_module:run", params={"n": Int()}),
    domain_op("no_callable", "no_such_callable"),
    domain_op("undeclared_param", "echo_op", {"missing": Int()}),
    domain_op("undeclared_required", "echo_op"),
    domain_op(
        "dataclass_no_module",
        "label_of",
        {"record": DataclassFields("no_such_fault_module:Plain")},
    ),
    domain_op(
        "dataclass_not_dataclass",
        "label_of",
        {"record": DataclassFields(f"{__name__}:echo_op")},
    ),
    domain_op(
        "dataclass_outside_table",
        "label_of",
        {"record": DataclassFields(f"{__name__}:OutsideTable")},
    ),
    domain_op(
        "dataclass_unresolved",
        "label_of",
        {"record": DataclassFields(f"{__name__}:Unresolvable")},
    ),
)
SYNTHETIC_OPS: dict[str, DomainOp] = {op.name: op for op in _SYNTHETIC_OPS}

# (op, params, fragment of the registration message). Each op is broken in one way.
FAULT_CASES: tuple[tuple[str, dict[str, object], str], ...] = (
    # OpRegistrationError (R31): the module does not import, the path names no callable,
    # a declared parameter is not taken, a required parameter is not declared.
    ("no_module", {}, "op module does not import: no_such_fault_module"),
    ("no_callable", {}, "op path does not name a callable"),
    ("undeclared_param", {"missing": 1}, "declared parameters the op does not take"),
    ("undeclared_required", {}, "required parameters the op does not declare"),
    # ImportError (R31): a dataclass target whose module does not import.
    ("dataclass_no_module", {"record": {"label": "x"}}, "No module named 'no_such_fault_module'"),
    # TypeError (R31): a dataclass target that is not a dataclass, and a field annotation
    # outside the decoder table.
    ("dataclass_not_dataclass", {"record": {}}, "is not a dataclass"),
    ("dataclass_outside_table", {"record": {}}, "<class 'float'>"),
    # NameError (R47): an annotation that typing.get_type_hints cannot resolve.
    ("dataclass_unresolved", {"record": {}}, "NoSuchTypeForRegistration"),
)


def envelope(
    command: str,
    body: object,
    *,
    generation: int = GENERATION,
    operation_id: str = OPERATION_ID,
    protocol_version: int = 1,
) -> bytes:
    obj = {
        "protocol_version": protocol_version,
        "generation": generation,
        "operation_id": operation_id,
        "command": command,
        "body": body,
    }
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def invoke_bytes(op: str, params: Mapping[str, object] | None = None) -> bytes:
    return envelope("invoke", {"op": op, "params": dict(params or {})})


def canonical_reply(raw: bytes) -> dict[str, Any]:
    """Parse one reply and check that it is the canonical UTF-8 text the Rust client reads."""
    assert isinstance(raw, bytes)
    reply: dict[str, Any] = json.loads(raw.decode("utf-8"))
    canonical = json.dumps(reply, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert canonical.encode("utf-8") == raw
    return reply


async def send(facade: WorkerFacade, raw: bytes) -> dict[str, Any]:
    return canonical_reply(await facade.handle(raw))


def error_code(reply: Mapping[str, Any]) -> str:
    return str(reply["error"]["code"])


def fake_create() -> Create:
    async def create() -> object:
        owner = object()
        for phase in InitPhase:
            async with phase_scope(phase, owner):
                pass
        return owner

    return create


async def ready_facade() -> WorkerFacade:
    facade = WorkerFacade(credential=CREDENTIAL, ops=SYNTHETIC_OPS, create=fake_create())
    bound = await send(facade, envelope("handshake", {"credential": CREDENTIAL}))
    assert "result" in bound
    for phase in InitPhase:
        reply = await send(facade, envelope("initialize", {"phase": phase.value}))
        assert reply["result"]["status"] == "ready"
    return facade


async def test_lone_surrogate_credential_is_refused_and_binds_nothing() -> None:
    facade = WorkerFacade(credential=CREDENTIAL, ops=SYNTHETIC_OPS, create=fake_create())
    reply = await send(facade, envelope("handshake", {"credential": LONE_SURROGATE}))
    assert "result" not in reply
    assert error_code(reply) == "invalid_request"
    assert reply["generation"] == GENERATION
    assert reply["operation_id"] == OPERATION_ID
    # Nothing bound: a command that needs a handshake is refused, and a good handshake binds.
    assert error_code(await send(facade, invoke_bytes("zero"))) == "not_handshaken"
    bound = await send(facade, envelope("handshake", {"credential": CREDENTIAL}))
    assert bound["result"]["generation"] == GENERATION


async def test_lone_surrogate_operation_id_is_refused_and_not_echoed() -> None:
    facade = await ready_facade()
    refused = await send(
        facade, envelope("invoke", {"op": "zero", "params": {}}, operation_id=LONE_SURROGATE)
    )
    assert "result" not in refused
    assert error_code(refused) == "invalid_request"
    assert refused["generation"] == GENERATION
    assert refused["operation_id"] is None
    # The refused request changed nothing: the same worker still answers.
    ok = await send(facade, invoke_bytes("zero"))
    assert ok["result"] == 0


async def test_lone_surrogate_operation_id_on_failing_envelope_is_not_echoed() -> None:
    facade = WorkerFacade(credential=CREDENTIAL, ops=SYNTHETIC_OPS, create=fake_create())
    raw = envelope(
        "handshake",
        {"credential": CREDENTIAL},
        operation_id=LONE_SURROGATE,
        protocol_version=0,
    )
    reply = await send(facade, raw)
    assert error_code(reply) == "invalid_request"
    assert reply["generation"] == GENERATION
    assert reply["operation_id"] is None
    assert "result" not in reply


@pytest.mark.parametrize(("op", "params", "fragment"), FAULT_CASES)
async def test_registration_fault_is_op_failed(
    op: str, params: dict[str, object], fragment: str
) -> None:
    facade = await ready_facade()
    reply = await send(facade, invoke_bytes(op, params))
    assert error_code(reply) == "op_failed"
    assert "result" not in reply
    assert reply["generation"] == GENERATION
    message = str(reply["error"]["message"])
    assert message.startswith("op registration fault: ")
    assert fragment in message
    # A registration fault fails that call only: the next healthy op answers.
    healthy = await send(facade, invoke_bytes("echo", {"n": 1}))
    assert healthy["result"] == 1


@pytest.mark.parametrize(
    ("params", "code"),
    [
        pytest.param({"n": "x"}, "invalid_request", id="wrong-type"),
        pytest.param({}, "invalid_request", id="missing-key"),
        pytest.param({"n": 1, "extra": 2}, "invalid_request", id="extra-key"),
        pytest.param({"n": 1}, "op_failed", id="well-formed"),
    ],
)
async def test_broken_registration_with_bad_params_is_invalid_request(
    params: dict[str, object], code: str
) -> None:
    # Params are checked before the op is resolved, so bad params win over a broken registration.
    facade = await ready_facade()
    reply = await send(facade, invoke_bytes("no_module_int", params))
    assert "result" not in reply
    assert error_code(reply) == code
    if code == "op_failed":
        assert str(reply["error"]["message"]).startswith("op registration fault: ")


async def test_unresolved_dataclass_checks_the_param_shape_first() -> None:
    facade = await ready_facade()
    not_object = await send(facade, invoke_bytes("dataclass_unresolved", {"record": 5}))
    assert error_code(not_object) == "invalid_request"
    layout = await send(facade, invoke_bytes("dataclass_unresolved", {"record": {"label": "x"}}))
    assert error_code(layout) == "op_failed"
    assert "NoSuchTypeForRegistration" in str(layout["error"]["message"])


async def test_dataclass_param_decodes_when_registration_is_sound() -> None:
    facade = await ready_facade()
    reply = await send(facade, invoke_bytes("label", {"record": {"label": "x"}}))
    assert reply["result"] == "x"


@pytest.mark.parametrize(
    ("op", "message"),
    [
        pytest.param("surrogate_result", "payload is not canonical JSON", id="surrogate-value"),
        pytest.param("surrogate_key_result", "payload is not canonical JSON", id="surrogate-key"),
        pytest.param("deep_result", "result is nested too deeply or is not JSON", id="deep"),
        pytest.param("cyclic_result", "result is nested too deeply or is not JSON", id="cycle"),
    ],
)
async def test_result_that_cannot_be_encoded_is_op_failed(op: str, message: str) -> None:
    facade = await ready_facade()
    reply = await send(facade, invoke_bytes(op))
    assert error_code(reply) == "op_failed"
    assert "result" not in reply
    assert reply["error"]["message"] == message
    # The failed call leaves the worker ready.
    ok = await send(facade, invoke_bytes("zero"))
    assert ok["result"] == 0


async def test_op_exception_text_with_lone_surrogate_is_escaped() -> None:
    facade = await ready_facade()
    reply = await send(facade, invoke_bytes("raise_surrogate"))
    assert error_code(reply) == "op_failed"
    assert reply["generation"] == GENERATION
    assert reply["error"]["message"] == "bad " + ESCAPED_SURROGATE + " text"
    ok = await send(facade, invoke_bytes("zero"))
    assert ok["result"] == 0

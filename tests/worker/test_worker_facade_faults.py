"""Facade fault tests (R31, R47, R49): lone surrogates, registration faults, results that
cannot be encoded, op exception text, and invoke ordering.

Every test drives WorkerFacade.handle with bytes, as the Rust client does. Each reply must be
canonical UTF-8 JSON, and handle must answer rather than raise. The ops and registrations are
synthetic and defined here, so no bundled definition is named.
"""

from __future__ import annotations

import json
import sys
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


@dataclass
class Keyed:
    key: str

    def __post_init__(self) -> None:
        if self.key == "missing":
            raise KeyError(self.key)


# A module name that no test imports. RaisingFinder makes its import raise, as a module that
# fails while it imports does.
FAULT_MODULE = "synthetic_import_fault_module"


class RaisingFinder:
    """A meta path finder whose import of FAULT_MODULE raises RuntimeError."""

    @staticmethod
    def find_spec(name: str, path: object = None, target: object = None) -> None:
        if name == FAULT_MODULE:
            raise RuntimeError("import-time fault in the registration fixture")
        return None


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


def big_result_op() -> int:
    return 2**64


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
    domain_op("big_result", "big_result_op"),
    domain_op("keyed", "label_of", {"record": DataclassFields(f"{__name__}:Keyed")}),
    DomainOp(name="op_import_fault", func=f"{FAULT_MODULE}:run", params={}),
    domain_op(
        "dataclass_import_fault",
        "label_of",
        {"record": DataclassFields(f"{FAULT_MODULE}:Plain")},
    ),
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
        pytest.param("big_result", "integer outside the wire range", id="integer-past-wire-range"),
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


async def test_constructor_exception_is_invalid_request() -> None:
    # A constructor or __post_init__ that raises any Exception rejects the client's value (R53).
    facade = await ready_facade()
    reply = await send(facade, invoke_bytes("keyed", {"record": {"key": "missing"}}))
    assert error_code(reply) == "invalid_request"
    assert reply["generation"] == GENERATION
    assert "result" not in reply
    ok = await send(facade, invoke_bytes("zero"))
    assert ok["result"] == 0


async def test_import_time_fault_in_op_module_is_op_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    # An op module that raises at import is a registration fault of any Exception class (R53).
    facade = await ready_facade()
    monkeypatch.setattr(sys, "meta_path", [RaisingFinder(), *sys.meta_path])
    reply = await send(facade, invoke_bytes("op_import_fault"))
    assert error_code(reply) == "op_failed"
    assert reply["generation"] == GENERATION
    message = str(reply["error"]["message"])
    assert message.startswith("op registration fault: ")
    assert "import-time fault in the registration fixture" in message
    ok = await send(facade, invoke_bytes("zero"))
    assert ok["result"] == 0


async def test_import_time_fault_in_dataclass_module_is_op_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dataclass module that raises at import is a registration fault of any class (R53).
    facade = await ready_facade()
    monkeypatch.setattr(sys, "meta_path", [RaisingFinder(), *sys.meta_path])
    reply = await send(facade, invoke_bytes("dataclass_import_fault", {"record": {"label": "x"}}))
    assert error_code(reply) == "op_failed"
    assert reply["generation"] == GENERATION
    message = str(reply["error"]["message"])
    assert message.startswith("op registration fault: ")
    assert "import-time fault in the registration fixture" in message


async def test_schema_identity_fault_is_op_failed_and_binds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A handshake whose schema identity cannot be loaded answers op_failed and binds no generation.
    def unreadable() -> object:
        raise OSError("schema identity file is missing")

    monkeypatch.setattr("gobby.worker.facade.expected_schema_identity", unreadable)
    facade = WorkerFacade(credential=CREDENTIAL, ops=SYNTHETIC_OPS, create=fake_create())
    reply = await send(facade, envelope("handshake", {"credential": CREDENTIAL}))
    assert error_code(reply) == "op_failed"
    assert reply["error"]["message"] == "schema identity unavailable"
    assert reply["generation"] == GENERATION
    assert reply["operation_id"] == OPERATION_ID
    later = await send(facade, envelope("initialize", {"phase": InitPhase.STORAGE_AND_CONFIG.value}))
    assert error_code(later) == "not_handshaken"
    monkeypatch.undo()
    bound = await send(facade, envelope("handshake", {"credential": CREDENTIAL}))
    assert bound["result"]["generation"] == GENERATION


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1])
async def test_out_of_range_integer_param_is_invalid_request(value: int) -> None:
    # The wire carries integers in [-2^63, 2^64-1] only (R54).
    facade = await ready_facade()
    reply = await send(facade, invoke_bytes("echo", {"n": value}))
    assert error_code(reply) == "invalid_request"
    assert reply["generation"] == GENERATION
    assert "result" not in reply


@pytest.mark.parametrize("value", [2**64 - 1, -(2**63)])
async def test_wire_bound_integer_param_is_accepted(value: int) -> None:
    facade = await ready_facade()
    reply = await send(facade, invoke_bytes("echo", {"n": value}))
    assert reply["result"] == value

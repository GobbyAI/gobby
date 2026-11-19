"""Protocol tests for the private worker wire format (WP-2, R2a red).

Every request and response is bytes, as the Rust client sends and reads them.
The corpus in tests/contracts/worker_protocol.json is the frozen contract
(design section 5.13). The 16 MiB and 64 KiB bounds are built here, not stored.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any

import pytest

from gobby.runner_init import StartRefusal
from gobby.storage.schema_contract import expected_schema_identity
from gobby.worker.decode import Str
from gobby.worker.facade import WorkerFacade
from gobby.worker.gate import phase_scope
from gobby.worker.ops import OPS, DomainOp
from gobby.worker.phase_ids import InitPhase
from gobby.worker.protocol import (
    ACCEPTED_PROTOCOL_VERSIONS,
    DOMAIN_OP_MAX_REQUEST_BYTES,
    MAX_REQUEST_BYTES,
    MESSAGES,
    WORKER_PROTOCOL_VERSION,
    Command,
    ErrorBody,
    ErrorCode,
    InitializeResult,
    PhaseStatus,
    ProtocolError,
    Response,
    decode_request,
    encode_response,
)

CREDENTIAL = "corpus-credential"
WRONG_CREDENTIAL = "not-the-credential"
GENERATION = 7
MIB = 1024 * 1024
PHASES: tuple[str, ...] = (
    "storage_and_config",
    "content_refusal",
    "startup_content",
    "config_runtime",
    "runtime_services",
)
COMMANDS = {"handshake", "initialize", "invoke"}
ERROR_CODES = {
    "invalid_request",
    "protocol_mismatch",
    "unauthorized",
    "not_handshaken",
    "stale_generation",
    "unknown_command",
    "phase_out_of_order",
    "worker_unusable",
    "not_ready",
    "op_failed",
}
FIXED_MESSAGES = {
    "invalid_request": "invalid request",
    "protocol_mismatch": "protocol version not accepted",
    "unauthorized": "credential refused",
    "not_handshaken": "no handshake for this worker",
    "stale_generation": "generation does not match the bound generation",
    "unknown_command": "unknown command",
    "phase_out_of_order": "phase is not the next phase",
    "worker_unusable": "worker is unusable",
    "not_ready": "worker is not ready",
}

_TESTS_DIR = Path(__file__).resolve().parents[1]
CORPUS_PATH = _TESTS_DIR / "contracts" / "worker_protocol.json"
DOC_PATH = _TESTS_DIR.parent / "docs" / "contracts" / "worker-protocol.md"

Step = Callable[[], Awaitable[None]]
Create = Callable[[], Coroutine[Any, Any, object]]


def load_corpus() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    return loaded


def find_case(corpus: Mapping[str, Any], name: str) -> dict[str, Any]:
    for version in corpus["versions"]:
        for case in version["cases"]:
            if case["name"] == name:
                found: dict[str, Any] = case
                return found
    raise AssertionError(f"corpus has no case {name!r}")


def canonical(value: object) -> str:
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False
    )


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


def handshake_bytes(
    generation: int = GENERATION, *, credential: str = CREDENTIAL, operation_id: str | None = None
) -> bytes:
    return envelope(
        "handshake", {"credential": credential}, generation=generation, operation_id=operation_id
    )


def initialize_bytes(phase: str, generation: int = GENERATION) -> bytes:
    return envelope("initialize", {"phase": phase}, generation=generation)


def invoke_bytes(op: str, params: Mapping[str, object] | None = None) -> bytes:
    return envelope("invoke", {"op": op, "params": dict(params or {})})


def pad(raw: bytes, size: int) -> bytes:
    """Pad with leading JSON whitespace to an exact byte size."""
    assert len(raw) <= size
    return b" " * (size - len(raw)) + raw


async def send(facade: WorkerFacade, raw: bytes) -> dict[str, Any]:
    reply: dict[str, Any] = json.loads(await facade.handle(raw))
    return reply


def error_code(reply: Mapping[str, Any]) -> str:
    return str(reply["error"]["code"])


def fake_create(steps: Mapping[InitPhase, Step] | None = None) -> Create:
    """A stand-in for GobbyRunner.create: five phase scopes, no runner."""

    async def create() -> object:
        owner = object()
        for phase in InitPhase:
            async with phase_scope(phase, owner):
                step = steps.get(phase) if steps else None
                if step is not None:
                    await step()
        return owner

    return create


async def ready_facade(ops: Mapping[str, DomainOp] | None = None) -> WorkerFacade:
    facade = WorkerFacade(
        credential=CREDENTIAL, ops=OPS if ops is None else ops, create=fake_create()
    )
    bound = await send(facade, handshake_bytes())
    assert "result" in bound, bound
    for phase in PHASES:
        reply = await send(facade, initialize_bytes(phase))
        assert reply["result"]["status"] == "ready", reply
    return facade


def placeholder_text() -> str:
    return json.dumps(
        expected_schema_identity(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def expected_bytes(response: str) -> bytes:
    """Substitute the R9 placeholder. A corpus with a literal identity object fails here."""
    assert '"schema_identity":{' not in response, "corpus carries a literal schema identity"
    return response.replace('"$schema_identity"', placeholder_text()).encode("utf-8")


async def replay(corpus: Mapping[str, Any], case: Mapping[str, Any]) -> bytes:
    credential = corpus["credential"] if case["credential_configured"] else None
    facade = WorkerFacade(credential=credential, ops=OPS, create=fake_create())
    generation = case["bound_generation"]
    if generation is not None:
        bound = await send(facade, handshake_bytes(generation, credential=corpus["credential"]))
        assert "result" in bound, bound
    if case["state"] == "ready":
        for phase in PHASES:
            reply = await send(facade, initialize_bytes(phase, generation))
            assert reply["result"]["status"] == "ready", reply
    return await facade.handle(case["request_bytes"].encode("utf-8"))


def _case_params() -> list[Any]:
    corpus = load_corpus()
    return [
        pytest.param(version["protocol_version"], case, id=case["name"])
        for version in corpus["versions"]
        for case in version["cases"]
    ]


def test_protocol_v1_surface() -> None:
    assert WORKER_PROTOCOL_VERSION == 1
    assert ACCEPTED_PROTOCOL_VERSIONS == (1,)
    assert MAX_REQUEST_BYTES == 65_536
    assert DOMAIN_OP_MAX_REQUEST_BYTES == 16 * MIB
    assert {command.value for command in Command} == COMMANDS
    assert {code.value for code in ErrorCode} == ERROR_CODES
    assert {status.value for status in PhaseStatus} == {"ready", "refused", "failed"}
    assert {str(key): text for key, text in MESSAGES.items()} == FIXED_MESSAGES
    assert issubclass(ProtocolError, Exception)
    assert callable(decode_request)
    assert callable(encode_response)
    assert isinstance(Response, type)
    assert isinstance(InitializeResult, type)
    assert isinstance(ErrorBody, type)
    assert tuple(phase.value for phase in InitPhase) == PHASES


async def test_handle_binds_one_generation() -> None:
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    first = await send(facade, handshake_bytes(GENERATION))
    assert first["result"]["generation"] == GENERATION
    again = await send(facade, handshake_bytes(GENERATION, operation_id="7:2"))
    assert again["operation_id"] == "7:2"
    assert again["result"] == first["result"]
    other = await send(facade, handshake_bytes(8))
    assert error_code(other) == "stale_generation"
    ready = await send(facade, initialize_bytes("storage_and_config", GENERATION))
    assert ready["result"]["status"] == "ready"
    stale = await send(facade, initialize_bytes("content_refusal", 8))
    assert error_code(stale) == "stale_generation"
    still = await send(facade, initialize_bytes("content_refusal", GENERATION))
    assert still["result"]["status"] == "ready"


@pytest.mark.parametrize(("protocol_version", "case"), _case_params())
async def test_corpus_cases_replay(protocol_version: int, case: dict[str, Any]) -> None:
    assert protocol_version in ACCEPTED_PROTOCOL_VERSIONS
    actual = await replay(load_corpus(), case)
    assert actual == expected_bytes(case["response_bytes"])


TEXT_CALLS: list[int] = []


def text_len(text: str) -> int:
    TEXT_CALLS.append(len(text))
    return len(text)


async def test_oversized_request_is_refused() -> None:
    TEXT_CALLS.clear()
    ops = {
        "text_len": DomainOp(name="text_len", func=f"{__name__}:text_len", params={"text": Str()})
    }
    facade = await ready_facade(ops=ops)
    base = invoke_bytes("text_len", {"text": ""})

    accepted = await send(facade, pad(base, DOMAIN_OP_MAX_REQUEST_BYTES))
    assert accepted["result"] == 0
    assert TEXT_CALLS == [0]

    refused = await send(facade, pad(base, DOMAIN_OP_MAX_REQUEST_BYTES + 1))
    assert error_code(refused) == "invalid_request"
    assert refused["generation"] is None
    assert refused["operation_id"] is None
    assert TEXT_CALLS == [0]


async def test_construction_over_64k_is_refused() -> None:
    handshake = handshake_bytes(GENERATION)
    accepted = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    reply = await send(accepted, pad(handshake, MAX_REQUEST_BYTES))
    assert reply["result"]["generation"] == GENERATION

    refused = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    reply = await send(refused, pad(handshake, MAX_REQUEST_BYTES + 1))
    assert error_code(reply) == "invalid_request"
    # The refusal still echoes the valid generation and operation id (R43).
    assert reply["generation"] == GENERATION
    assert reply["operation_id"] == "7:1"
    # The refused handshake bound nothing, so a normal one still binds.
    bound = await send(refused, handshake)
    assert bound["result"]["generation"] == GENERATION

    init = initialize_bytes("storage_and_config", GENERATION)
    at_limit = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    await send(at_limit, handshake)
    reply = await send(at_limit, pad(init, MAX_REQUEST_BYTES))
    assert reply["result"]["status"] == "ready"

    over_limit = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    await send(over_limit, handshake)
    reply = await send(over_limit, pad(init, MAX_REQUEST_BYTES + 1))
    assert error_code(reply) == "invalid_request"
    assert reply["generation"] == GENERATION
    assert reply["operation_id"] == "7:1"
    # Nothing ran: the storage phase is still the next phase.
    reply = await send(over_limit, init)
    assert reply["result"]["status"] == "ready"


async def test_handshake_reports_schema_identity() -> None:
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    reply = await send(facade, handshake_bytes(GENERATION))
    identity = reply["result"]["schema_identity"]
    assert identity == expected_schema_identity()
    assert isinstance(identity, dict)
    assert all(isinstance(value, int | str) for value in identity.values())


def test_contract_doc_covers_protocol() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    for value in (*COMMANDS, *ERROR_CODES, *PHASES, "ready", "refused", "failed"):
        assert value in text, value
    for needle in ("protocol_version", "generation", "operation_id", "credential"):
        assert needle in text, needle
    assert "65,536" in text or "65536" in text
    assert "16 MiB" in text or "16777216" in text
    assert "unknown outcome" in text.lower()
    assert "runner_protocol" in text
    assert "require_machine_owned" in text
    assert "is_daemon_execution" in text


def test_placeholder_substitution() -> None:
    corpus = load_corpus()
    response = find_case(corpus, "handshake accepted")["response_bytes"]
    assert response.count('"$schema_identity"') == 1
    assert '"schema_identity":"$schema_identity"' in response
    substituted = json.loads(expected_bytes(response))
    assert substituted["result"]["schema_identity"] == expected_schema_identity()
    literal = response.replace('"$schema_identity"', placeholder_text())
    with pytest.raises(AssertionError):
        expected_bytes(literal)


async def test_unconfigured_facade_refuses_every_handshake() -> None:
    facade = WorkerFacade(credential=None, ops=OPS, create=fake_create())
    for generation, credential in (
        (GENERATION, CREDENTIAL),
        (GENERATION, WRONG_CREDENTIAL),
        (8, CREDENTIAL),
    ):
        reply = await send(facade, handshake_bytes(generation, credential=credential))
        assert error_code(reply) == "unauthorized"
        assert reply["error"]["message"] == "credential refused"
    reply = await send(facade, initialize_bytes("storage_and_config", GENERATION))
    assert error_code(reply) == "not_handshaken"


async def test_wrong_credential_refused() -> None:
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    reply = await send(facade, handshake_bytes(GENERATION, credential=WRONG_CREDENTIAL))
    assert error_code(reply) == "unauthorized"
    assert reply["error"]["message"] == "credential refused"
    # A refused handshake binds nothing.
    reply = await send(facade, initialize_bytes("storage_and_config", GENERATION))
    assert error_code(reply) == "not_handshaken"
    bound = await send(facade, handshake_bytes(GENERATION))
    assert "result" in bound
    # Auth comes before generation: a wrong credential for another generation is not stale.
    reply = await send(facade, handshake_bytes(8, credential=WRONG_CREDENTIAL))
    assert error_code(reply) == "unauthorized"


async def test_missing_or_empty_credential_refused() -> None:
    bodies: list[object] = [
        {},
        {"credential": ""},
        {"credential": 5},
        {"credential": CREDENTIAL, "extra": 1},
    ]
    for body in bodies:
        facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
        reply = await send(facade, envelope("handshake", body))
        assert error_code(reply) == "invalid_request", body
        reply = await send(facade, initialize_bytes("storage_and_config"))
        assert error_code(reply) == "not_handshaken", body


async def test_credential_never_echoed() -> None:
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    raws = [
        handshake_bytes(GENERATION),
        handshake_bytes(GENERATION, credential=WRONG_CREDENTIAL),
        handshake_bytes(8, credential=WRONG_CREDENTIAL),
        envelope("handshake", {"credential": WRONG_CREDENTIAL, "extra": 1}),
        envelope("handshake", {"credential": ""}),
    ]
    for raw in raws:
        reply = await facade.handle(raw)
        assert CREDENTIAL.encode("utf-8") not in reply
        assert WRONG_CREDENTIAL.encode("utf-8") not in reply


def _valid_envelope() -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "generation": GENERATION,
        "operation_id": "7:1",
        "command": "handshake",
        "body": {"credential": CREDENTIAL},
    }


def _without(key: str) -> dict[str, Any]:
    env = _valid_envelope()
    del env[key]
    return env


def _with(key: str, value: object) -> dict[str, Any]:
    env = _valid_envelope()
    env[key] = value
    return env


INVALID_ENVELOPES: dict[str, object] = {
    "missing protocol_version": _without("protocol_version"),
    "missing generation": _without("generation"),
    "missing operation_id": _without("operation_id"),
    "missing command": _without("command"),
    "extra key": {**_valid_envelope(), "extra": True},
    "protocol_version true": _with("protocol_version", True),
    "protocol_version zero": _with("protocol_version", 0),
    "generation true": _with("generation", True),
    "generation string": _with("generation", "7"),
    "generation zero": _with("generation", 0),
    "operation_id empty": _with("operation_id", ""),
    "operation_id too long": _with("operation_id", "x" * 129),
    "command not a string": _with("command", 5),
    "body not an object": _with("body", []),
}


async def test_strict_envelope_rules() -> None:
    for name, obj in INVALID_ENVELOPES.items():
        facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
        reply = await send(facade, json.dumps(obj).encode("utf-8"))
        assert error_code(reply) == "invalid_request", name
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    reply = await send(facade, b"[]")
    assert error_code(reply) == "invalid_request"
    # The body defaults to an empty object, so this reaches the command check.
    bound = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    await send(bound, handshake_bytes(GENERATION))
    reply = await send(
        bound, b'{"protocol_version":1,"generation":7,"operation_id":"7:1","command":"frobnicate"}'
    )
    assert error_code(reply) == "unknown_command"


async def test_generation_zero_refused() -> None:
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    reply = await send(facade, handshake_bytes(0))
    assert error_code(reply) == "invalid_request"
    bound = await send(facade, handshake_bytes(GENERATION))
    assert bound["result"]["generation"] == GENERATION
    reply = await send(facade, initialize_bytes("storage_and_config", 0))
    assert error_code(reply) == "invalid_request"


async def test_envelope_integers_are_wire_bounded() -> None:
    # A generation past the wire range is invalid_request: no generation is echoed, and
    # nothing binds (R54).
    facade = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    too_big = envelope(
        "handshake", {"credential": CREDENTIAL}, generation=2**64, operation_id="7:1"
    )
    reply = await send(facade, too_big)
    assert error_code(reply) == "invalid_request"
    assert reply["generation"] is None
    assert reply["operation_id"] == "7:1"
    bound = await send(facade, handshake_bytes(GENERATION))
    assert bound["result"]["generation"] == GENERATION

    # A protocol_version past the wire range is invalid_request, not protocol_mismatch.
    too_big_version = json.dumps({**_valid_envelope(), "protocol_version": 2**64})
    reply = await send(facade, too_big_version.encode("utf-8"))
    assert error_code(reply) == "invalid_request"

    # The top of the wire range is an ordinary generation.
    top = WorkerFacade(credential=CREDENTIAL, ops=OPS, create=fake_create())
    reply = await send(top, handshake_bytes(2**64 - 1))
    assert reply["result"]["generation"] == 2**64 - 1


def long_failure() -> None:
    raise ValueError("x" * 5000)


async def refuse_long() -> None:
    raise StartRefusal("y" * 5000)


async def fail_long() -> None:
    raise RuntimeError("z" * 5000)


async def test_error_messages_capped() -> None:
    ops = {"long": DomainOp(name="long", func=f"{__name__}:long_failure", params={})}
    facade = await ready_facade(ops=ops)
    reply = await send(facade, invoke_bytes("long"))
    assert error_code(reply) == "op_failed"
    assert len(reply["error"]["message"]) == 2048

    refusing = WorkerFacade(
        credential=CREDENTIAL,
        ops=OPS,
        create=fake_create({InitPhase.CONTENT_REFUSAL: refuse_long}),
    )
    await send(refusing, handshake_bytes(GENERATION))
    await send(refusing, initialize_bytes("storage_and_config"))
    reply = await send(refusing, initialize_bytes("content_refusal"))
    assert reply["result"]["status"] == "refused"
    assert len(reply["result"]["message"]) == 2048

    failing = WorkerFacade(
        credential=CREDENTIAL,
        ops=OPS,
        create=fake_create({InitPhase.STARTUP_CONTENT: fail_long}),
    )
    await send(failing, handshake_bytes(GENERATION))
    await send(failing, initialize_bytes("storage_and_config"))
    await send(failing, initialize_bytes("content_refusal"))
    reply = await send(failing, initialize_bytes("startup_content"))
    assert reply["result"]["status"] == "failed"
    assert len(reply["result"]["message"]) == 2048


def test_corpus_versions_equal_accepted() -> None:
    corpus = load_corpus()
    assert corpus["schema"] == "worker-protocol-corpus"
    assert corpus["credential"] == CREDENTIAL
    assert [version["protocol_version"] for version in corpus["versions"]] == list(
        ACCEPTED_PROTOCOL_VERSIONS
    )
    for version in corpus["versions"]:
        assert version["cases"]
        assert version["responses"]


def test_canonical_response_bytes() -> None:
    corpus = load_corpus()
    for version in corpus["versions"]:
        texts = [case["response_bytes"] for case in version["cases"]]
        texts += [vector["response_bytes"] for vector in version["responses"]]
        for text in texts:
            assert canonical(json.loads(text)) == text, text

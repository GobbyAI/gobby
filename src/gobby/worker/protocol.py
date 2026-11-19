"""Private worker wire protocol, version 1 (R0 design 5.1 to 5.5, 5.13).

Every request and response is UTF-8 JSON bytes. ``decode_request`` reads a request and
checks its envelope (guard steps 1 to 4 of design 5.6). ``encode_response`` writes a
response in canonical form (design 5.3). The facade applies the rest of the guard order.

This module imports ``phase_ids`` and ``encode`` only, so the live runner never loads it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from gobby.worker.encode import canonical_bytes
from gobby.worker.phase_ids import InitPhase, PhaseStatus

__all__ = [
    "ACCEPTED_PROTOCOL_VERSIONS",
    "CONSTRUCTION_COMMANDS",
    "DOMAIN_OP_MAX_REQUEST_BYTES",
    "MAX_MESSAGE_CHARS",
    "MAX_OPERATION_ID_CHARS",
    "MAX_REQUEST_BYTES",
    "MESSAGES",
    "WORKER_PROTOCOL_VERSION",
    "Command",
    "ErrorBody",
    "ErrorCode",
    "HandshakeBody",
    "HandshakeResult",
    "InitializeBody",
    "InitializeResult",
    "InvokeBody",
    "PhaseStatus",
    "ProtocolError",
    "Request",
    "Response",
    "cap_message",
    "decode_request",
    "echo_fields",
    "encode_response",
    "error_response",
    "parse_body",
    "result_response",
]

WORKER_PROTOCOL_VERSION: Final = 1
ACCEPTED_PROTOCOL_VERSIONS: Final[tuple[int, ...]] = (WORKER_PROTOCOL_VERSION,)
MAX_REQUEST_BYTES: Final = 65_536
DOMAIN_OP_MAX_REQUEST_BYTES: Final = 16 * 1024 * 1024
MAX_MESSAGE_CHARS: Final = 2048
MAX_OPERATION_ID_CHARS: Final = 128


class Command(StrEnum):
    """The three commands. ``invoke`` is the domain-op command."""

    HANDSHAKE = "handshake"
    INITIALIZE = "initialize"
    INVOKE = "invoke"


CONSTRUCTION_COMMANDS: Final[tuple[Command, ...]] = (Command.HANDSHAKE, Command.INITIALIZE)


class ErrorCode(StrEnum):
    """The ten error codes. ``op_failed`` is the server-fault code (R31)."""

    INVALID_REQUEST = "invalid_request"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    UNAUTHORIZED = "unauthorized"
    NOT_HANDSHAKEN = "not_handshaken"
    STALE_GENERATION = "stale_generation"
    UNKNOWN_COMMAND = "unknown_command"
    PHASE_OUT_OF_ORDER = "phase_out_of_order"
    WORKER_UNUSABLE = "worker_unusable"
    NOT_READY = "not_ready"
    OP_FAILED = "op_failed"


# Fixed text per code (design 5.13). ``op_failed`` has no entry: it carries the failure text.
MESSAGES: Mapping[ErrorCode, str] = MappingProxyType(
    {
        ErrorCode.INVALID_REQUEST: "invalid request",
        ErrorCode.PROTOCOL_MISMATCH: "protocol version not accepted",
        ErrorCode.UNAUTHORIZED: "credential refused",
        ErrorCode.NOT_HANDSHAKEN: "no handshake for this worker",
        ErrorCode.STALE_GENERATION: "generation does not match the bound generation",
        ErrorCode.UNKNOWN_COMMAND: "unknown command",
        ErrorCode.PHASE_OUT_OF_ORDER: "phase is not the next phase",
        ErrorCode.WORKER_UNUSABLE: "worker is unusable",
        ErrorCode.NOT_READY: "worker is not ready",
    }
)

_PHASE_VALUES: Final = frozenset(phase.value for phase in InitPhase)


def cap_message(text: str) -> str:
    """Truncate a message to ``MAX_MESSAGE_CHARS`` (GF-10)."""
    return text[:MAX_MESSAGE_CHARS]


class ProtocolError(Exception):
    """A request refused before any op or phase ran.

    ``generation`` and ``operation_id`` are the echo fields of the reply. They are set only
    from a parsed object whose value passes its own envelope rule (see ``echo_fields``).
    Every code except ``op_failed`` has fixed text in ``MESSAGES``; ``op_failed`` must be
    given its ``message``.
    """

    def __init__(
        self,
        code: ErrorCode,
        *,
        generation: int | None = None,
        operation_id: str | None = None,
        message: str | None = None,
    ) -> None:
        text = cap_message(MESSAGES[code] if message is None else message)
        super().__init__(text)
        self.code = code
        self.generation = generation
        self.operation_id = operation_id
        self.message = text

    def to_response(self) -> Response:
        return error_response(
            self.code,
            self.message,
            generation=self.generation,
            operation_id=self.operation_id,
        )


class Request(BaseModel):
    """The request envelope (design 5.2). Strict and closed: any other key is refused."""

    model_config = ConfigDict(extra="forbid", strict=True)

    protocol_version: int = Field(ge=1)
    generation: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=MAX_OPERATION_ID_CHARS)
    command: str
    body: dict[str, Any] = Field(default_factory=dict)


class HandshakeBody(BaseModel):
    """Body of ``handshake``: exactly one non-empty credential (design 5.5)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    credential: str = Field(min_length=1)


class InitializeBody(BaseModel):
    """Body of ``initialize``: exactly one known phase value (design 5.6, step 10)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    phase: str

    @field_validator("phase")
    @classmethod
    def _known_phase(cls, value: str) -> str:
        if value not in _PHASE_VALUES:
            raise ValueError("not an init phase")
        return value


class InvokeBody(BaseModel):
    """Body of ``invoke``: an op name and its parameters (design 5.9)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    op: str
    params: dict[str, Any] = Field(default_factory=dict)


class ErrorBody(BaseModel):
    """The ``error`` member of a response: a code and a capped, fixed or dynamic message."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(max_length=MAX_MESSAGE_CHARS)


class InitializeResult(BaseModel):
    """The ``result`` of a phase response (design 5.7). Every key is always present."""

    model_config = ConfigDict(extra="forbid")

    phase: InitPhase
    status: PhaseStatus
    next_phase: InitPhase | None
    message: str | None = Field(max_length=MAX_MESSAGE_CHARS)
    error_type: str | None


class HandshakeResult(BaseModel):
    """The ``result`` of a handshake response (design 5.5)."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: int
    generation: int
    commands: list[str]
    schema_identity: dict[str, int | str]


class Response(BaseModel):
    """A response envelope: exactly one of ``result`` or ``error`` (design 5.3).

    Presence is decided on the input keys, so a ``result`` of ``0`` or ``null`` is valid.
    An ``error`` of ``null`` counts as absent.
    """

    model_config = ConfigDict(extra="forbid")

    protocol_version: int
    generation: int | None
    operation_id: str | None
    result: JsonValue | None = None
    error: ErrorBody | None = None

    @model_validator(mode="before")
    @classmethod
    def _exactly_one_branch(cls, data: object) -> object:
        if isinstance(data, dict):
            has_result = "result" in data
            has_error = data.get("error") is not None
            if has_result == has_error:
                raise ValueError("a response carries exactly one of result or error")
        return data


def result_response(
    result: JsonValue, *, generation: int | None, operation_id: str | None
) -> Response:
    """A success response. ``result`` may be ``0``, ``null`` or any encoded JSON value."""
    return Response(
        protocol_version=WORKER_PROTOCOL_VERSION,
        generation=generation,
        operation_id=operation_id,
        result=result,
    )


def error_response(
    code: ErrorCode, message: str, *, generation: int | None, operation_id: str | None
) -> Response:
    """An error response with its message capped to ``MAX_MESSAGE_CHARS``."""
    return Response(
        protocol_version=WORKER_PROTOCOL_VERSION,
        generation=generation,
        operation_id=operation_id,
        error=ErrorBody(code=code, message=cap_message(message)),
    )


def encode_response(response: Response) -> bytes:
    """Canonical bytes of ``response`` (design 5.3).

    The payload is already JSON-compatible, so it goes through ``canonical_bytes`` only.
    ``encode_value`` is not applied here: it refuses the ``_type`` keys that encoded results
    carry.
    """
    envelope: dict[str, object] = {
        "protocol_version": response.protocol_version,
        "generation": response.generation,
        "operation_id": response.operation_id,
    }
    if response.error is not None:
        envelope["error"] = response.error.model_dump(mode="json")
    else:
        envelope["result"] = response.result
    return canonical_bytes(envelope)


def echo_fields(parsed: Mapping[str, object]) -> tuple[int | None, str | None]:
    """The generation and operation id to echo, each only if it passes its own rule.

    ``bool`` is excluded explicitly, because ``True`` is an ``int`` in Python.
    """
    generation = parsed.get("generation")
    operation_id = parsed.get("operation_id")
    echo_generation = generation if type(generation) is int and generation >= 1 else None
    echo_operation = (
        operation_id
        if isinstance(operation_id, str) and 1 <= len(operation_id) <= MAX_OPERATION_ID_CHARS
        else None
    )
    return echo_generation, echo_operation


def decode_request(raw: bytes) -> Request:
    """Read ``raw`` as a request envelope (design 5.6, steps 1 to 4).

    Raises ``ProtocolError(INVALID_REQUEST)`` when the bytes exceed ``DOMAIN_OP_MAX_REQUEST_BYTES``
    (nothing is parsed, so no echo), when they are not UTF-8 JSON with an object at the top
    level, when a handshake or initialize exceeds ``MAX_REQUEST_BYTES``, or when the envelope
    fails ``Request``. Every raise after parsing echoes the values that pass their own rule.
    Body shape, unknown commands, generation binding and protocol version are the facade's.
    """
    if len(raw) > DOMAIN_OP_MAX_REQUEST_BYTES:
        raise ProtocolError(ErrorCode.INVALID_REQUEST)
    try:
        parsed: object = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(ErrorCode.INVALID_REQUEST) from exc
    if not isinstance(parsed, dict):
        raise ProtocolError(ErrorCode.INVALID_REQUEST)
    generation, operation_id = echo_fields(parsed)
    if parsed.get("command") in CONSTRUCTION_COMMANDS and len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST, generation=generation, operation_id=operation_id
        )
    try:
        return Request.model_validate(parsed)
    except ValidationError as exc:
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST, generation=generation, operation_id=operation_id
        ) from exc


def parse_body[M: BaseModel](
    model: type[M], body: Mapping[str, Any], *, generation: int, operation_id: str
) -> M:
    """Validate a command body against its model, or raise ``invalid_request``."""
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST, generation=generation, operation_id=operation_id
        ) from exc

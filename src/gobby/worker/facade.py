"""Worker facade: one raw request in, one canonical response out (R0 design 5.5 to 5.10, 5.6).

``WorkerFacade.handle`` applies the guard order of design 5.6 to one request. It assumes no
transport: no socket, no signal and no path. The caller moves the bytes.

Construct a facade inside a running event loop. The phase driver creates loop futures, and
the create task runs on the loop that drives the initialize calls (R44).
"""

from __future__ import annotations

import hmac
import inspect
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue, ValidationError

from gobby.storage.schema_contract import expected_schema_identity
from gobby.worker.decode import DecodeError
from gobby.worker.encode import UnencodableValue, canonical_bytes, encode_value
from gobby.worker.ops import OPS, DomainOp, OpRegistrationError, resolve
from gobby.worker.phase_ids import InitPhase
from gobby.worker.phases import UNUSABLE, PhaseDriver, PhaseOutcome, runner_create_factory
from gobby.worker.protocol import (
    ACCEPTED_PROTOCOL_VERSIONS,
    WORKER_PROTOCOL_VERSION,
    Command,
    ErrorCode,
    HandshakeBody,
    HandshakeResult,
    InitializeBody,
    InitializeResult,
    InvokeBody,
    ProtocolError,
    Request,
    Response,
    cap_message,
    decode_request,
    encode_response,
    error_response,
    parse_body,
    result_response,
)

__all__ = ["WorkerFacade"]

Create = Callable[[], Coroutine[Any, Any, object]]


class WorkerFacade:
    """One worker's request surface: a handshake binds a generation, then initialize and invoke.

    ``credential`` is the expected handshake credential. ``None`` (or an empty string) refuses
    every handshake, so there is no allow-when-unconfigured path (ruling R2). ``ops`` is the
    allow-listed table. ``create`` is a zero-argument factory for the runner's create coroutine;
    when it is omitted, the real ``GobbyRunner.create`` runs with ``config_path`` and ``verbose``.
    """

    def __init__(
        self,
        *,
        credential: str | None,
        ops: Mapping[str, DomainOp] = OPS,
        create: Create | None = None,
        config_path: Path | None = None,
        verbose: bool = False,
    ) -> None:
        self._credential = credential
        self._ops = ops
        factory = runner_create_factory(config_path, verbose) if create is None else create
        self._driver = PhaseDriver(factory)
        self._generation: int | None = None

    async def handle(self, raw: bytes) -> bytes:
        """Answer one request. Only ``BaseException`` from an op or a phase escapes (GF-12)."""
        try:
            response = await self._respond(raw)
        except ProtocolError as exc:
            response = _refused_response(exc)
        return encode_response(response)

    async def _respond(self, raw: bytes) -> Response:
        request = decode_request(raw)
        if request.protocol_version not in ACCEPTED_PROTOCOL_VERSIONS:
            raise _refusal(request, ErrorCode.PROTOCOL_MISMATCH)
        if request.command == Command.HANDSHAKE:
            return self._handshake(request)
        if self._generation is None:
            raise _refusal(request, ErrorCode.NOT_HANDSHAKEN)
        if request.generation != self._generation:
            raise _refusal(request, ErrorCode.STALE_GENERATION)
        if request.command == Command.INITIALIZE:
            return await self._initialize(request)
        if request.command == Command.INVOKE:
            return await self._invoke(request)
        raise _refusal(request, ErrorCode.UNKNOWN_COMMAND)

    def _handshake(self, request: Request) -> Response:
        body = parse_body(HandshakeBody, request.body, **_echo_of(request))
        if not self._authorized(body.credential):
            raise _refusal(request, ErrorCode.UNAUTHORIZED)
        if self._generation is not None and self._generation != request.generation:
            raise _refusal(request, ErrorCode.STALE_GENERATION)
        try:
            result = _handshake_result(request.generation)
        except Exception as exc:
            # A schema identity that cannot be loaded is a server fault (R53). The handshake
            # binds nothing, so the next command is not_handshaken.
            raise _refusal(request, ErrorCode.OP_FAILED, "schema identity unavailable") from exc
        self._generation = request.generation
        return result_response(result, **_echo_of(request))

    def _authorized(self, credential: str) -> bool:
        if not self._credential:
            return False
        # Both sides are encoded the same way, so a lone surrogate cannot raise here.
        return hmac.compare_digest(
            credential.encode("utf-8", "surrogatepass"),
            self._credential.encode("utf-8", "surrogatepass"),
        )

    async def _initialize(self, request: Request) -> Response:
        body = parse_body(InitializeBody, request.body, **_echo_of(request))
        phase = InitPhase(body.phase)
        outcome = await self._driver.initialize(phase)
        if not isinstance(outcome, PhaseOutcome):
            if outcome is UNUSABLE:
                raise _refusal(request, ErrorCode.WORKER_UNUSABLE)
            raise _refusal(request, ErrorCode.PHASE_OUT_OF_ORDER)
        result = InitializeResult(
            phase=phase,
            status=outcome.status,
            next_phase=outcome.next_phase,
            message=None if outcome.message is None else _text(outcome.message),
            error_type=outcome.error_type,
        )
        return result_response(result.model_dump(mode="json"), **_echo_of(request))

    async def _invoke(self, request: Request) -> Response:
        body = parse_body(InvokeBody, request.body, **_echo_of(request))
        if self._driver.unusable:
            raise _refusal(request, ErrorCode.WORKER_UNUSABLE)
        if not self._driver.ready:
            raise _refusal(request, ErrorCode.NOT_READY)
        op = self._ops.get(body.op)
        if op is None:
            raise _refusal(request, ErrorCode.UNKNOWN_COMMAND)
        function, decoded = _prepare(request, op, body.params)
        try:
            value = function(**decoded)
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:
            # The op-call boundary: any Exception from the op fails this call only (GF-12).
            raise _refusal(request, ErrorCode.OP_FAILED, _text(str(exc))) from exc
        return _result(request, value)


def _prepare(
    request: Request, op: DomainOp, params: Mapping[str, Any]
) -> tuple[Callable[..., object], dict[str, object]]:
    """Check the params against the declared keys and specs, then resolve the op (design 5.9).

    A value that a decoder rejects is ``invalid_request``. A registration fault, such as an
    unresolvable path, an unbuildable dataclass target or an unusable signature, is
    ``op_failed`` with an explicit message (R31).
    """
    if set(params) != set(op.params):
        raise _refusal(request, ErrorCode.INVALID_REQUEST)
    try:
        decoded = {name: spec.decode(params[name]) for name, spec in op.params.items()}
    except DecodeError as exc:
        raise _refusal(request, ErrorCode.INVALID_REQUEST) from exc
    except _REGISTRATION_FAULTS as exc:
        raise _registration_fault(request, exc) from exc
    try:
        function = resolve(op)
    except _REGISTRATION_FAULTS as exc:
        raise _registration_fault(request, exc) from exc
    return function, decoded


# What resolving or building a registration can raise when the registration is broken.
# NameError comes from typing.get_type_hints on a dataclass whose annotation does not resolve.
_REGISTRATION_FAULTS: tuple[type[Exception], ...] = (
    OpRegistrationError,
    ImportError,
    NameError,
    TypeError,
)


def _result(request: Request, value: object) -> Response:
    """Encode an op's return value, or fail the call as ``op_failed`` (design 5.11)."""
    try:
        encoded = cast(JsonValue, encode_value(value))
        # Probe the canonical writer too: a lone surrogate passes encode_value but cannot be
        # written as UTF-8, and a very deep value fails the recursion limit in the writer.
        canonical_bytes(encoded)
        return result_response(encoded, **_echo_of(request))
    except UnencodableValue as exc:
        raise _refusal(request, ErrorCode.OP_FAILED, _text(str(exc))) from exc
    except (RecursionError, ValidationError) as exc:
        raise _refusal(
            request, ErrorCode.OP_FAILED, "result is nested too deeply or is not JSON"
        ) from exc


def _registration_fault(request: Request, exc: Exception) -> ProtocolError:
    return _refusal(request, ErrorCode.OP_FAILED, _text(f"op registration fault: {exc}"))


def _handshake_result(generation: int) -> JsonValue:
    result = HandshakeResult(
        protocol_version=WORKER_PROTOCOL_VERSION,
        generation=generation,
        commands=sorted(command.value for command in Command),
        schema_identity=expected_schema_identity(),
    )
    return cast(JsonValue, result.model_dump(mode="json"))


def _refusal(request: Request, code: ErrorCode, message: str | None = None) -> ProtocolError:
    """The refusal for ``request``, echoing its generation and operation id (design 5.2)."""
    return ProtocolError(
        code,
        generation=request.generation,
        operation_id=request.operation_id,
        message=message,
    )


def _echo_of(request: Request) -> dict[str, Any]:
    """The keyword arguments that echo a request's generation and operation id."""
    return {"generation": request.generation, "operation_id": request.operation_id}


def _refused_response(exc: ProtocolError) -> Response:
    """The reply for a refused request. An echoed operation id that is not writable is dropped."""
    operation_id = exc.operation_id if _writable(exc.operation_id) else None
    return error_response(
        exc.code, exc.message, generation=exc.generation, operation_id=operation_id
    )


def _writable(text: str | None) -> bool:
    """Whether ``text`` can be written as UTF-8. A lone surrogate from a JSON escape cannot."""
    if text is None:
        return True
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _text(value: str) -> str:
    """A message UTF-8 can write, capped to 2,048 characters (GF-10)."""
    return cap_message(value.encode("utf-8", "backslashreplace").decode("utf-8"))

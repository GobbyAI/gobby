"""Parameter decoders, the op table and lazy resolution of worker ops."""

import asyncio
import inspect
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import pytest

import gobby
from gobby.hub.caller_scope import require_machine_owned
from gobby.storage.terminals import Terminal
from gobby.terminals.write_coordinator import WriteRequest
from gobby.utils.session_context import is_daemon_execution
from gobby.worker import decode, ops
from gobby.worker.decode import DecodeError
from gobby.worker.ops import DomainOp, OpRegistrationError, resolve

NOT_CALLABLE_CONSTANT = 3


@dataclass(frozen=True)
class Window:
    start: datetime
    label: str = "w"


@dataclass(frozen=True)
class Labelled:
    mode: Literal["alpha", "beta"]
    flag: Literal[1, 2]


@dataclass(frozen=True)
class Strict:
    name: str
    count: int = 0


@dataclass(frozen=True)
class Holder:
    items: set[int]


@dataclass(frozen=True)
class Bounded:
    count: int

    def __post_init__(self) -> None:
        if self.count > 10:
            raise ValueError("count is over the bound")


def sync_op(value: str) -> str:
    return value


async def async_op(value: str) -> str:
    return value


def defaults_op(a: str, b: int = 0) -> str:
    return f"{a}{b}"


def needs_two(first: str, second: str) -> str:
    return f"{first}{second}"


def positional_only_op(value: str, /) -> str:
    return value


def test_optional_datetime_null() -> None:
    spec = decode.Optional(decode.IsoDatetime())

    assert spec.decode(None) is None
    assert spec.decode("2026-11-18T12:00:00+00:00") == datetime(2026, 11, 18, 12, tzinfo=UTC)


def test_iso_datetime_bad_text_is_decode_error() -> None:
    with pytest.raises(DecodeError):
        decode.IsoDatetime().decode("not a date")
    with pytest.raises(DecodeError):
        decode.IsoDatetime().decode(12)


def test_scalar_specs_are_strict_about_type() -> None:
    with pytest.raises(DecodeError):
        decode.Int().decode(True)
    with pytest.raises(DecodeError):
        decode.Bool().decode(1)
    with pytest.raises(DecodeError):
        decode.Str().decode(1)
    assert decode.Anything().decode([1, "x"]) == [1, "x"]


def test_json_object_requires_str_keys() -> None:
    assert decode.JsonObject().decode({"a": 1}) == {"a": 1}
    with pytest.raises(DecodeError):
        decode.JsonObject().decode([1])
    with pytest.raises(DecodeError):
        decode.JsonObject().decode({1: "non-str key"})


def test_dataclass_decoder_rejects_unknown_field() -> None:
    spec = decode.DataclassFields(Strict)

    with pytest.raises(DecodeError):
        spec.decode({"name": "a", "extra": 1})
    with pytest.raises(DecodeError):
        spec.decode({"count": 1})  # required field "name" is absent
    assert spec.decode({"name": "a"}) == Strict(name="a", count=0)


def test_dataclass_decoder_decodes_datetime_field() -> None:
    window = decode.DataclassFields(Window).decode({"start": "2026-11-18T12:00:00+00:00"})

    assert window == Window(start=datetime(2026, 11, 18, 12, tzinfo=UTC), label="w")
    assert isinstance(window, Window)
    assert isinstance(window.start, datetime)


def test_dataclass_decoder_validates_literal_field() -> None:
    spec = decode.DataclassFields(Labelled)

    assert spec.decode({"mode": "alpha", "flag": 1}) == Labelled(mode="alpha", flag=1)
    with pytest.raises(DecodeError):
        spec.decode({"mode": "gamma", "flag": 1})
    with pytest.raises(DecodeError):
        spec.decode({"mode": 1, "flag": 1})
    # True equals 1 in Python, but its JSON type is not the literal's type.
    with pytest.raises(DecodeError):
        spec.decode({"mode": "alpha", "flag": True})


def test_write_request_decodes_without_terminal() -> None:
    spec = decode.DataclassFields(WriteRequest)

    request = spec.decode(
        {
            "terminal_id": "t-1",
            "action_key": "a-1",
            "origin": "operator",
            "kind": "text",
            "payload": "ls",
            "submit": True,
        }
    )

    assert request == WriteRequest(
        terminal_id="t-1",
        action_key="a-1",
        origin="operator",
        kind="text",
        payload="ls",
        submit=True,
    )
    assert isinstance(request, WriteRequest)
    assert request.terminal is None
    with pytest.raises(DecodeError):
        spec.decode(
            {
                "terminal_id": "t-1",
                "action_key": "a-1",
                "origin": "robot",
                "kind": "text",
                "payload": "",
            }
        )


def test_write_request_decodes_with_terminal() -> None:
    spec = decode.DataclassFields(WriteRequest)
    terminal_json: dict[str, Any] = {
        "id": "term-1",
        "backend": "tmux",
        "ownership": "daemon",
        "state": "running",
        "machine_id": "machine-1",
        "project_id": "project-1",
        "created_at": "2026-11-18T12:00:00+00:00",
        "updated_at": "2026-11-18T12:01:00+00:00",
        "attempt_generation": 1,
        "attempt_started_at": "2026-11-18T12:00:00+00:00",
        "unresolved_writes": {},
    }

    request = spec.decode(
        {
            "terminal_id": "term-1",
            "action_key": "a-1",
            "origin": "automatic",
            "kind": "key",
            "payload": "Enter",
            "terminal": terminal_json,
        }
    )

    assert isinstance(request, WriteRequest)
    assert isinstance(request.terminal, Terminal)
    assert request.terminal.created_at == datetime(2026, 11, 18, 12, tzinfo=UTC)
    assert request.terminal.unresolved_writes == {}
    assert request.terminal.liveness_at is None


def test_unsupported_annotation_is_registration_error() -> None:
    # The field annotation set[int] is outside the 5.12 table.
    with pytest.raises(TypeError):
        decode.DataclassFields(Holder)
    with pytest.raises(TypeError):
        decode.spec_for(complex)
    with pytest.raises(TypeError):
        decode.spec_for(list[str])


def test_dataclass_constructor_failure_is_decode_error() -> None:
    # The constructor's own validation rejects the value, so it is a decode failure.
    spec = decode.DataclassFields(Bounded)

    assert spec.decode({"count": 3}) == Bounded(count=3)
    with pytest.raises(DecodeError):
        spec.decode({"count": 11})


def test_dataclass_string_target_resolves_lazily() -> None:
    spec = decode.DataclassFields(f"{__name__}:Strict")

    assert spec.decode({"name": "lazy"}) == Strict(name="lazy", count=0)


def test_op_table_empty_in_r0() -> None:
    assert isinstance(ops.OPS, MappingProxyType)
    assert len(ops.OPS) == 0


def test_import_is_lazy() -> None:
    probe = DomainOp(name="r0-probe", func="r0_probe_missing_module:call", params={})

    table = ops._build((probe,))

    assert probe.name in table
    assert "r0_probe_missing_module" not in sys.modules
    with pytest.raises(OpRegistrationError):
        resolve(probe)


def test_importing_op_table_loads_no_other_gobby_module() -> None:
    src = Path(gobby.__file__).resolve().parents[1]
    code = (
        "import json, sys\n"
        "import gobby.worker.ops\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('gobby'))))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(src)}

    done = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )

    loaded = json.loads(done.stdout)
    extra = [m for m in loaded if m != "gobby" and not m.startswith("gobby.worker")]
    assert extra == []


def test_registration_checks() -> None:
    resolved = resolve(DomainOp("ok", f"{__name__}:sync_op", {"value": decode.Str()}))
    assert resolved is sync_op

    defaults = resolve(DomainOp("defaults", f"{__name__}:defaults_op", {"a": decode.Str()}))
    assert defaults is defaults_op

    bad_ops = [
        # A declared name the function does not take.
        DomainOp(
            "undeclared", f"{__name__}:sync_op", {"value": decode.Str(), "extra": decode.Str()}
        ),
        # A required parameter the op does not declare.
        DomainOp("missing", f"{__name__}:needs_two", {"first": decode.Str()}),
        # Keyword calls cannot reach a positional-only parameter.
        DomainOp("positional", f"{__name__}:positional_only_op", {"value": decode.Str()}),
        # No module:qualname separator.
        DomainOp("no-separator", "no_separator_here", {}),
        # The qualname names nothing in the module.
        DomainOp("no-attr", f"{__name__}:no_such_name", {}),
        # The qualname names something that is not callable.
        DomainOp("not-callable", f"{__name__}:NOT_CALLABLE_CONSTANT", {}),
        # The module does not exist.
        DomainOp("no-module", "r0_probe_missing_module:call", {}),
    ]
    for bad in bad_ops:
        with pytest.raises(OpRegistrationError):
            resolve(bad)


def test_duplicate_op_names_are_rejected() -> None:
    first = DomainOp("dup", f"{__name__}:sync_op", {})
    second = DomainOp("dup", f"{__name__}:async_op", {})

    with pytest.raises(OpRegistrationError):
        ops._build((first, second))


def test_sync_and_async_ops() -> None:
    sync_fn = resolve(DomainOp("s", f"{__name__}:sync_op", {"value": decode.Str()}))
    async_fn = resolve(DomainOp("a", f"{__name__}:async_op", {"value": decode.Str()}))

    assert sync_fn is sync_op
    assert not inspect.iscoroutinefunction(sync_fn)
    assert inspect.iscoroutinefunction(async_fn)
    assert asyncio.run(async_fn(value="v")) == "v"


def test_ops_docstring_states_caller_facts() -> None:
    # A worker op binds no caller and no daemon-owned execution.
    # Returning without raising is the fact: no caller is bound, so nothing is refused.
    require_machine_owned("machine-r0")
    assert is_daemon_execution() is False

    doc = ops.__doc__ or ""
    assert "require_machine_owned" in doc
    assert "is_daemon_execution" in doc

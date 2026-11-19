"""Result encoder rules (design-v2 5.11) and the real-type shape test (R10)."""

import enum
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import PurePosixPath, PureWindowsPath
from types import ModuleType

import pytest

from gobby.agents.team_communications import RecipientPolicy
from gobby.mcp_proxy.tools.sessions._terminal_send_keys_gate import SendKeysRefusal
from gobby.terminals import composer_ledger
from gobby.terminals.runtime import (
    AutomaticWriteQuarantined,
    Delivered,
    IndeterminateWrite,
    Suppressed,
)
from gobby.terminals.write_coordinator import WriteRequest
from gobby.worker.decode import DataclassFields, DecodeError, Int
from gobby.worker.encode import UnencodableValue, canonical_bytes, encode_value

TERMINAL_JSON: dict[str, object] = {
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


class Color(enum.StrEnum):
    RED = "red"
    BLUE = "blue"


class Count(enum.IntEnum):
    ONE = 1


class Shape(enum.Enum):
    PAIR = (1, 2)


@dataclass(frozen=True)
class Inner:
    x: int


@dataclass(frozen=True)
class Sample:
    name: str
    when: datetime
    tags: frozenset[str]
    inner: Inner


@dataclass(frozen=True)
class TypeNamedField:
    _type: str = "spoofed"


@dataclass(frozen=True)
class WithTuple:
    pairs: tuple[int, ...]


class Boom(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TypeNamedAttrBoom(Exception):
    def __init__(self) -> None:
        super().__init__("attribute named like the type tag")
        self._type = "spoofed"


class NoInstanceDict(Exception):
    """Hides ``__dict__`` from attribute lookup, so ``vars()`` raises TypeError."""

    def __getattribute__(self, name: str) -> object:
        if name == "__dict__":
            raise AttributeError(name)
        return super().__getattribute__(name)


def test_dataclass_result_has_type_and_fields() -> None:
    sample = Sample(
        name="n",
        when=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
        tags=frozenset({"b", "a"}),
        inner=Inner(x=3),
    )

    assert encode_value(sample) == {
        "_type": "Sample",
        "name": "n",
        "when": "2026-11-18T12:00:00+00:00",
        "tags": ["a", "b"],
        "inner": {"x": 3},
    }


def test_nested_dataclass_has_no_type() -> None:
    assert encode_value(Inner(x=7)) == {"x": 7, "_type": "Inner"}
    outer = Sample(name="n", when=datetime(2026, 1, 1), tags=frozenset(), inner=Inner(x=1))
    assert encode_value(outer)["inner"] == {"x": 1}


def test_dataclass_field_named_type_fails_call() -> None:
    with pytest.raises(UnencodableValue):
        encode_value(TypeNamedField())


def test_result_key_named_type_fails_call() -> None:
    with pytest.raises(UnencodableValue):
        encode_value({"_type": "spoofed"})


def test_exception_answers_type_and_attrs() -> None:
    assert encode_value(Boom("bad-code")) == {"_type": "Boom", "code": "bad-code"}
    assert encode_value(IndeterminateWrite("lost reply")) == {
        "_type": "IndeterminateWrite",
        "detail": "lost reply",
    }


def test_exception_attribute_named_type_fails() -> None:
    with pytest.raises(UnencodableValue):
        encode_value(TypeNamedAttrBoom())


def test_exception_without_dict_encodes_type_only() -> None:
    exc = NoInstanceDict("hidden")
    with pytest.raises(TypeError):
        vars(exc)

    assert encode_value(exc) == {"_type": "NoInstanceDict"}


def test_frozenset_is_sorted_list() -> None:
    assert encode_value(frozenset({"gamma", "alpha", "beta"})) == ["alpha", "beta", "gamma"]
    # Sorted by canonical element text: '"a"' sorts before '1'.
    assert encode_value({1, "a"}) == ["a", 1]


def test_frozenset_with_unencodable_element_fails() -> None:
    with pytest.raises(UnencodableValue):
        encode_value(frozenset({object()}))


def test_enum_answers_value() -> None:
    assert encode_value(Color.RED) == "red"
    count = encode_value(Count.ONE)
    assert count == 1
    assert type(count) is int
    assert encode_value(Shape.PAIR) == [1, 2]


def test_tuple_is_array() -> None:
    assert encode_value((1, ("a", None))) == [1, ["a", None]]
    assert encode_value(WithTuple(pairs=(4, 5))) == {"pairs": [4, 5], "_type": "WithTuple"}


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_float_unencodable(value: float) -> None:
    with pytest.raises(UnencodableValue):
        encode_value(value)
    with pytest.raises(UnencodableValue):
        encode_value([1.5, value])


def test_datetime_and_path_strings() -> None:
    assert encode_value(datetime(2026, 11, 18, 12, 0, tzinfo=UTC)) == "2026-11-18T12:00:00+00:00"
    assert encode_value(datetime(2026, 11, 18, 12, 0)) == "2026-11-18T12:00:00"
    assert encode_value(PurePosixPath("runs/a.log")) == "runs/a.log"
    # A path is str(path) in the host-native form: backslashes separate on Windows (R59).
    assert encode_value(PureWindowsPath("C:/data/gobby")) == "C:\\data\\gobby"
    with pytest.raises(UnencodableValue):
        encode_value(date(2026, 11, 18))


def test_unencodable_values_raise() -> None:
    # The six O5 values of design-v2 4.3.
    values: list[object] = [
        object(),
        b"raw bytes",
        float("nan"),
        json,
        frozenset({object()}),
        {1: "non-str key"},
    ]
    for value in values:
        with pytest.raises(UnencodableValue):
            encode_value(value)


@dataclass
class WideField:
    n: int


class WideAttr(Exception):
    def __init__(self) -> None:
        super().__init__("wide")
        self.n = 2**64


# Out-of-range integers at each depth an encoded value can hold them (R54).
WIDE_NESTED: list[object] = [
    [2**64],
    {"k": 2**64},
    frozenset({2**64}),
    WideField(2**64),
    WideAttr(),
]


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1, *WIDE_NESTED])
def test_out_of_range_integer_is_unencodable(value: object) -> None:
    with pytest.raises(UnencodableValue):
        encode_value(value)


@pytest.mark.parametrize("value", [2**64 - 1, -(2**63), 0, -1])
def test_wire_range_integers_encode_unchanged(value: int) -> None:
    assert encode_value(value) == value
    assert encode_value([value]) == [value]


def test_int_decoder_refuses_values_outside_wire_range() -> None:
    assert Int().decode(2**64 - 1) == 2**64 - 1
    assert Int().decode(-(2**63)) == -(2**63)
    for value in (2**64, -(2**63) - 1):
        with pytest.raises(DecodeError):
            Int().decode(value)


def test_canonical_bytes_is_compact_sorted_utf8() -> None:
    assert canonical_bytes({"b": [True, None], "a": "é"}) == '{"a":"é","b":[true,null]}'.encode()


def test_canonical_bytes_does_not_reencode_encoded_values() -> None:
    # The encoded form carries "_type" keys, which encode_value refuses on a second pass.
    encoded = encode_value(Inner(x=2))
    payload = canonical_bytes({"result": encoded})

    assert payload == b'{"result":{"_type":"Inner","x":2}}'


def test_canonical_bytes_rejects_non_json_payloads() -> None:
    with pytest.raises(UnencodableValue):
        canonical_bytes({"result": object()})
    with pytest.raises(UnencodableValue):
        canonical_bytes("\ud800")


def test_real_types_encode_without_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    # The composer ledger is unbound here, so read_composer answers its unbound read.
    monkeypatch.setattr(composer_ledger, "_bound", None)
    composer = composer_ledger.read_composer("r0-terminal")
    assert encode_value(composer) == {
        "_type": "ComposerRead",
        "state": "unknown",
        "line": None,
        "reason": None,
        "unsubmitted": False,
    }

    refusal = SendKeysRefusal(code="r0_refused", message="refused", recovery=None)
    assert encode_value(refusal) == {
        "_type": "SendKeysRefusal",
        "code": "r0_refused",
        "message": "refused",
        "recovery": None,
    }

    policy = RecipientPolicy(withheld=frozenset({"zeta", "alpha"}))
    assert encode_value(policy) == {"_type": "RecipientPolicy", "withheld": ["alpha", "zeta"]}

    assert encode_value(Delivered()) == {"_type": "Delivered"}
    assert encode_value(Suppressed(action_key="k-1")) == {
        "_type": "Suppressed",
        "action_key": "k-1",
        "reason": "unresolved_write",
    }
    assert encode_value(AutomaticWriteQuarantined(action_key="k-2")) == {
        "_type": "AutomaticWriteQuarantined",
        "action_key": "k-2",
        "reason": "automatic_write_quarantined",
    }
    assert encode_value(IndeterminateWrite("lost reply")) == {
        "_type": "IndeterminateWrite",
        "detail": "lost reply",
    }

    # A real WriteRequest decoded from JSON, without and then with its Terminal row.
    request_json: dict[str, object] = {
        "terminal_id": "t-1",
        "action_key": "a-1",
        "origin": "operator",
        "kind": "text",
        "payload": "ls",
    }
    bare = DataclassFields(WriteRequest).decode(request_json)
    assert encode_value(bare) == {
        "_type": "WriteRequest",
        "terminal_id": "t-1",
        "action_key": "a-1",
        "origin": "operator",
        "kind": "text",
        "payload": "ls",
        "submit": False,
        "attachment_id": None,
        "expected_lease_generation": None,
        "idempotency_key": None,
        "terminal": None,
    }

    with_terminal = DataclassFields(WriteRequest).decode(
        {**request_json, "terminal": TERMINAL_JSON}
    )
    encoded = encode_value(with_terminal)
    assert isinstance(encoded, dict)
    terminal = encoded["terminal"]
    # A nested dataclass encodes as a plain object with no _type (design 5.11, rule 3).
    assert isinstance(terminal, dict)
    assert "_type" not in terminal
    assert terminal["created_at"] == "2026-11-18T12:00:00+00:00"


def test_module_value_is_unencodable() -> None:
    module: ModuleType = json
    with pytest.raises(UnencodableValue):
        encode_value(module)

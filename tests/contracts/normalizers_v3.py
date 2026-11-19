"""Field normalizers for the v3 corpus: a closed set of rules.

A normalization unit is one response document (``normalize_document``), the rows of one db_delta
entry (``normalize_rows``) or the captures of one side-effect entry (``normalize_captures``).
Every unit has its own ``uuid_ordinal`` counter, so ordinals never leak between units.

Addressing. A document rule is an RFC 6901 pointer from the document root. A rule on a row or a
capture is a pointer relative to that item, and a db_delta column name is the one-token pointer
``/<column>``. A rule must match at least one value in its unit, or the unit fails.

JSON form of database values (what every recorder and every Rust reader must reproduce):
text, integers, booleans and null stay as they are; a UUID becomes its lowercase canonical
string; a timestamp becomes its ``isoformat()`` string, which carries a UTC offset; a decimal
becomes its plain string; bytes become ``\\x`` followed by lowercase hex; jsonb is its parsed value.

Normalizers (names are exact):
- ``uuid_ordinal``: a canonical UUID becomes ``@uuid:<n>@``. ``n`` is the first-appearance index
  in unit order (rows in their declared order, then rules by document order within one item).
- ``monotonic``: an integer, or a decimal string, becomes ``@seq@``. The values at the pointer,
  taken across the unit in order, must be strictly increasing. Only sequence units may use it:
  rows declared ``as_recorded`` and captures. A set-ordered table or a document cannot.
- ``timestamp``: an ISO-8601 string with an offset becomes ``@ts@`` when it lies within the run
  window ``[start, end]``. Any other value fails, including one outside the window. The optional
  ``after`` names a sibling column of the same row or capture item; the value must not be earlier
  than that sibling. ``after`` is only legal on db_delta normalizers.
- ``bytes`` and ``none``: identity. ``bytes`` marks terminal, envelope and grant fields.
- ``set`` is not a field normalizer. A db_delta table with ``order: "set"`` is sorted by its key
  (by the JSON text of each key value, in key order) before its unit is normalized.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

FIELD_NORMALIZERS = frozenset({"uuid_ordinal", "monotonic", "timestamp", "bytes", "none"})
ORDER_MODES = frozenset({"set", "as_recorded"})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_INTEGER_TEXT = re.compile(r"-?[0-9]+")


class NormalizationError(ValueError):
    """A value or a rule breaks its normalizer. The message names the pointer or column."""


@dataclass(frozen=True)
class Window:
    """The run window in UTC: from before the request to the end of the capture window."""

    start: datetime
    end: datetime


def json_form(value: Any) -> Any:
    """Return the JSON form of a value read from the database or built in Python."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return "\\x" + bytes(value).hex()
    if isinstance(value, Mapping):
        return {str(key): json_form(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_form(item) for item in value]
    raise NormalizationError(f"no JSON form for a {type(value).__name__} value")


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _leaf_pointers(node: Any, prefix: str = "") -> Iterator[str]:
    """Yield the pointer of every scalar leaf, in document order."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{prefix}/{_escape(str(key))}"
            if isinstance(value, Mapping | list):
                yield from _leaf_pointers(value, child)
            else:
                yield child
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child = f"{prefix}/{index}"
            if isinstance(value, Mapping | list):
                yield from _leaf_pointers(value, child)
            else:
                yield child


def _parent_and_token(document: Any, pointer: str) -> tuple[Any, str] | None:
    """Return the container and the final token of a pointer, or None when it matches nothing."""
    if not pointer.startswith("/"):
        raise NormalizationError(f"pointer {pointer!r} must start with '/'")
    tokens = [raw.replace("~1", "/").replace("~0", "~") for raw in pointer[1:].split("/")]
    node: Any = document
    for token in tokens[:-1]:
        if isinstance(node, Mapping) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return None
    last = tokens[-1]
    if isinstance(node, Mapping) and last in node:
        return node, last
    if isinstance(node, list) and last.isdigit() and int(last) < len(node):
        return node, last
    return None


def _spec(spec: Any) -> tuple[str, str | None]:
    if isinstance(spec, str):
        name, after = spec, None
    elif isinstance(spec, Mapping) and "name" in spec and set(spec) <= {"name", "after"}:
        name, after = spec["name"], spec.get("after")
    else:
        raise NormalizationError(f"normalizer {spec!r} is not a field normalizer")
    if name not in FIELD_NORMALIZERS:
        raise NormalizationError(f"normalizer {name!r} is not a field normalizer")
    if after is not None and name != "timestamp":
        raise NormalizationError("only the timestamp normalizer takes 'after'")
    return name, after


def _timestamp(value: Any, where: str, window: Window) -> datetime:
    if not isinstance(value, str):
        raise NormalizationError(f"{where} {value!r} is not an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise NormalizationError(f"{where} {value!r} is not an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise NormalizationError(f"{where} {value!r} must carry a UTC offset")
    if not window.start <= parsed <= window.end:
        raise NormalizationError(f"{where} {value!r} is outside the run window")
    return parsed


def _sequence_value(value: Any, where: str) -> int:
    if isinstance(value, bool):
        raise NormalizationError(f"monotonic needs an integer at {where}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value):
        return int(value)
    raise NormalizationError(f"monotonic needs an integer at {where}")


class _Ordinals:
    """First-appearance numbering of UUIDs within one normalization unit."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def token(self, value: str) -> str:
        number = self._seen.setdefault(value, len(self._seen) + 1)
        return f"@uuid:{number}@"


def _replacement(
    value: Any,
    name: str,
    after: str | None,
    *,
    where: str,
    window: Window,
    siblings: Mapping[str, Any] | None,
) -> Any:
    """Validate one value against its normalizer and return its replacement.

    Every replacement of a unit is computed from the original values before any is written, so
    an ``after`` check always reads the recorded sibling, never a token.
    """
    if name == "uuid_ordinal":
        if not (isinstance(value, str) and _UUID.fullmatch(value)):
            raise NormalizationError(f"uuid_ordinal needs a canonical UUID at {where}")
        return value
    if name == "timestamp":
        parsed = _timestamp(value, f"timestamp at {where}", window)
        if after is not None:
            if siblings is None or after not in siblings:
                raise NormalizationError(f"after names no column {after!r} for {where}")
            earlier = _timestamp(siblings[after], f"after column {after!r}", window)
            if parsed < earlier:
                raise NormalizationError(f"{where} is earlier than its after column {after!r}")
        return "@ts@"
    return value


def _apply_rules(
    document: Any,
    rules: Mapping[str, Any],
    *,
    window: Window,
    ordinals: _Ordinals,
    sequence: bool,
    allow_after: bool,
    where_prefix: str,
) -> list[str]:
    """Apply every rule to one document in place; return the monotonic pointers for the caller.

    Replacements are computed in document order (so uuid ordinals number by first appearance)
    and written only after every rule has validated.
    """
    order = {pointer: index for index, pointer in enumerate(_leaf_pointers(document))}
    monotonic: list[str] = []
    writes: list[tuple[Any, str, Any]] = []
    for pointer, spec in sorted(rules.items(), key=lambda item: order.get(item[0], -1)):
        name, after = _spec(spec)
        found = _parent_and_token(document, pointer)
        if found is None or pointer not in order:
            raise NormalizationError(f"normalizer pointer {where_prefix}{pointer} matches nothing")
        if after is not None and not allow_after:
            raise NormalizationError("'after' is declared only on db_delta normalizers")
        if name == "monotonic":
            if not sequence:
                raise NormalizationError(f"monotonic needs a sequence at {where_prefix}{pointer}")
            monotonic.append(pointer)
            continue
        container, token = found
        value = _replacement(
            container[token],
            name,
            after,
            where=f"{where_prefix}{pointer}",
            window=window,
            siblings=document if after is not None else None,
        )
        if name == "uuid_ordinal":
            value = ordinals.token(value)
        writes.append((container, token, value))
    for container, token, value in writes:
        container[token] = value
    return monotonic


def _check_monotonic(items: Sequence[Any], pointers: Sequence[str], where: str) -> None:
    """Assert strictly increasing values across the unit, then mask them."""
    for pointer in pointers:
        previous: int | None = None
        for index, item in enumerate(items):
            found = _parent_and_token(item, pointer)
            if found is None:
                raise NormalizationError(f"normalizer pointer {where}{pointer} matches nothing")
            container, token = found
            current = _sequence_value(container[token], f"{where}{pointer}[{index}]")
            if previous is not None and current <= previous:
                raise NormalizationError(
                    f"{where}{pointer} values must be strictly increasing (item {index})"
                )
            previous = current
            container[token] = "@seq@"


def normalize_document(document: Any, normalizers: Mapping[str, Any], *, window: Window) -> Any:
    """Normalize one document; every key is an RFC 6901 pointer from its root."""
    out = copy.deepcopy(json_form(document))
    _apply_rules(
        out,
        normalizers,
        window=window,
        ordinals=_Ordinals(),
        sequence=False,
        allow_after=False,
        where_prefix="",
    )
    return out


def _normalize_items(
    items: Sequence[Any],
    rules: Mapping[str, Any],
    *,
    window: Window,
    sequence: bool,
    allow_after: bool,
    where: str,
) -> list[Any]:
    """Normalize a sequence unit: rules are pointers relative to each item, in unit order."""
    out = [copy.deepcopy(json_form(item)) for item in items]
    ordinals = _Ordinals()
    monotonic: list[str] = []
    for index, item in enumerate(out):
        if not isinstance(item, Mapping | list):
            raise NormalizationError(f"{where}[{index}] must be an object")
        monotonic = _apply_rules(
            item,
            rules,
            window=window,
            ordinals=ordinals,
            sequence=sequence,
            allow_after=allow_after,
            where_prefix=f"{where}[{index}]",
        )
    if monotonic:
        _check_monotonic(out, monotonic, where)
    return out


def normalize_captures(
    captures: Sequence[Mapping[str, Any]], normalizers: Mapping[str, Any], *, window: Window
) -> list[Any]:
    """Normalize one side-effect entry's captures, in capture order (always a sequence)."""
    return _normalize_items(
        captures,
        normalizers,
        window=window,
        sequence=True,
        allow_after=False,
        where="captures",
    )


def normalize_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    key: Sequence[str],
    order: str,
    normalizers: Mapping[str, str | Mapping[str, Any]],
    window: Window,
) -> list[dict[str, Any]]:
    """Normalize one db_delta table's rows.

    ``order`` must be declared: ``set`` sorts the rows by key first, and ``as_recorded`` keeps the
    given order, which is then a sequence for monotonic.
    """
    if order not in ORDER_MODES:
        raise NormalizationError(f"order {order!r} must be 'set' or 'as_recorded'")
    if not key:
        raise NormalizationError("a table needs a non-empty key")
    json_rows = [json_form(row) for row in rows]
    for row in json_rows:
        missing = [column for column in key if column not in row]
        if missing:
            raise NormalizationError(f"row lacks key column {missing[0]!r}")
    if order == "set":
        json_rows.sort(key=lambda row: [json.dumps(row[column]) for column in key])
    return normalize_images(json_rows, order=order, normalizers=normalizers, window=window)


def normalize_images(
    images: Sequence[Mapping[str, Any]],
    *,
    order: str,
    normalizers: Mapping[str, str | Mapping[str, Any]],
    window: Window,
) -> list[dict[str, Any]]:
    """Normalize one db_delta entry's row images as one unit, in the order given.

    The recorder passes an entry's images in one fixed order (added, removed, changed before,
    changed after), so every uuid_ordinal shares one counter across the entry. Only
    ``as_recorded`` allows monotonic, and its values run across the images in that order.
    """
    if order not in ORDER_MODES:
        raise NormalizationError(f"order {order!r} must be 'set' or 'as_recorded'")
    if order == "set" and any(_spec(spec)[0] == "monotonic" for spec in normalizers.values()):
        raise NormalizationError("monotonic needs as_recorded order")
    rules = {f"/{column}": spec for column, spec in normalizers.items()}
    return _normalize_items(
        list(images),
        rules,
        window=window,
        sequence=order == "as_recorded",
        allow_after=True,
        where="rows",
    )

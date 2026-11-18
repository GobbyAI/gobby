"""Non-sensitive, conservative accounting for reversible end-of-composer edits."""

import re
from dataclasses import dataclass
from typing import Literal, cast

EditOp = Literal["insert", "backspace", "submit"]
MAX_EDIT_COUNT = 4 * 1024 * 1024
MAX_EDIT_OPERATIONS = 128
_ENTER = re.compile(r"\x1b(?:OM|\[13(?:;1(?::[12])?)?u|\[27;1;13~)")


@dataclass(frozen=True)
class ComposerEdit:
    op: EditOp
    count: int


def decode_edits(value: object, *, submit: bool | None) -> tuple[ComposerEdit, ...] | None:
    """Malformed/missing summaries convey no knowledge, even if a prefix is valid."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_EDIT_OPERATIONS:
        return None
    edits: list[ComposerEdit] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"op", "count"}:
            return None
        op, count = item["op"], item["count"]
        if not isinstance(op, str) or op not in {"insert", "backspace", "submit"}:
            return None
        if type(count) is not int:
            return None
        if not 1 <= count <= MAX_EDIT_COUNT or (op == "submit" and count != 1):
            return None
        edits.append(ComposerEdit(cast(EditOp, op), count))
    if submit is not any(edit.op == "submit" for edit in edits):
        return None
    return tuple(edits)


def summarize_input(payload: str) -> tuple[ComposerEdit, ...] | None:
    """Only printable ASCII, DEL Backspace and bare CR have known edit semantics."""
    edits: list[ComposerEdit] = []
    for character in _ENTER.sub("\r", payload):
        if " " <= character <= "~":
            op: EditOp = "insert"
        elif character == "\x7f":
            op = "backspace"
        elif character == "\r":
            op = "submit"
        else:
            return None
        if edits and edits[-1].op == op and op != "submit":
            edits[-1] = ComposerEdit(op, edits[-1].count + 1)
        else:
            edits.append(ComposerEdit(op, 1))
        if len(edits) > MAX_EDIT_OPERATIONS or edits[-1].count > MAX_EDIT_COUNT:
            return None
    return tuple(edits) or None


def reduce_edit(length: int | None, edit: ComposerEdit) -> int | None:
    if edit.op == "submit":
        return 0
    if length is None:
        return None
    if edit.op == "backspace":
        return max(0, length - edit.count)
    return length + edit.count


def summarize_write(kind: str, payload: str, *, submit: bool) -> tuple[ComposerEdit, ...] | None:
    if kind == "paste":
        return None
    if kind == "key":
        payload = {"enter": "\r", "kpenter": "\r", "backspace": "\x7f"}.get(payload, "")
    return summarize_input(payload + ("\r" if submit else ""))

"""Lossless changed-file manifest parsing and change-magnitude metadata."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from typing import Literal, NotRequired, TypedDict

_RENAME_OR_COPY = frozenset({b"R", b"C"})


class Utf8Content(TypedDict):
    encoding: Literal["utf-8"]
    text: str


class Base64Content(TypedDict):
    encoding: Literal["base64"]
    data: str


type EncodedContent = Utf8Content | Base64Content


class ManifestItem(TypedDict):
    commit: str
    status: str
    path: EncodedContent
    path_selector: str
    role: NotRequired[Literal["old", "new"]]
    lines_added: NotRequired[int | None]
    lines_deleted: NotRequired[int | None]


class DiffPagingError(ValueError):
    """Typed failure returned by every diff-paging surface."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "success": False,
            "error_code": self.code,
            "error": str(self),
        }
        if self.details:
            result["details"] = self.details
        return result


def encode_bytes(value: bytes) -> EncodedContent:
    """Encode arbitrary bytes without losing non-UTF-8 content."""
    try:
        return {"encoding": "utf-8", "text": value.decode("utf-8")}
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}


def _path_selector(commit: str, path: bytes) -> str:
    digest = hashlib.sha256(commit.encode("ascii") + b"\0" + path).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class ManifestParser:
    """Incrementally parse a NUL-delimited Git name-status manifest."""

    def __init__(self, commit: str, emit: Callable[[ManifestItem, bytes], None]) -> None:
        self.commit = commit
        self.emit = emit
        self.buffer = bytearray()
        self.status: bytes | None = None
        self.paths_remaining = 0
        self.path_index = 0

    def feed(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        while True:
            try:
                boundary = self.buffer.index(0)
            except ValueError:
                return
            token = bytes(self.buffer[:boundary])
            del self.buffer[: boundary + 1]
            self._token(token)

    def finish(self) -> None:
        if self.buffer.strip() or self.paths_remaining:
            raise DiffPagingError("git_failed", "git returned a malformed name-status manifest")

    def _token(self, token: bytes) -> None:
        if self.paths_remaining == 0:
            status = token.lstrip(b"\n")
            if not status:
                return
            self.status = status
            self.paths_remaining = 2 if status[:1] in _RENAME_OR_COPY else 1
            self.path_index = 0
            return
        assert self.status is not None
        role: Literal["old", "new"] | None = None
        if self.paths_remaining == 2:
            role = "old"
        elif self.path_index == 1:
            role = "new"
        item: ManifestItem = {
            "commit": self.commit,
            "status": self.status.decode("ascii", errors="replace"),
            "path": encode_bytes(token),
            "path_selector": _path_selector(self.commit, token),
        }
        if role is not None:
            item["role"] = role
        self.emit(item, token)
        self.paths_remaining -= 1
        self.path_index += 1


def _parse_count(value: bytes) -> int | None:
    if value == b"-":
        return None
    if not value.isdigit():
        raise DiffPagingError("git_failed", "git returned malformed numstat counts")
    return int(value)


def parse_numstat(data: bytes) -> dict[bytes, tuple[int | None, int | None]]:
    """Parse NUL-delimited ``git show --numstat -z`` output by raw path."""
    if not data:
        return {}
    tokens = data.split(b"\0")
    if tokens[-1] != b"":
        raise DiffPagingError("git_failed", "git returned a malformed numstat manifest")

    result: dict[bytes, tuple[int | None, int | None]] = {}
    index = 0
    terminal = len(tokens) - 1
    while index < terminal:
        record = tokens[index].lstrip(b"\n")
        index += 1
        parts = record.split(b"\t", 2)
        if len(parts) != 3:
            raise DiffPagingError("git_failed", "git returned a malformed numstat manifest")
        added = _parse_count(parts[0])
        deleted = _parse_count(parts[1])
        path = parts[2]
        if path:
            result[path] = (added, deleted)
            continue
        if index + 1 >= terminal:
            raise DiffPagingError("git_failed", "git returned a malformed rename numstat record")
        old_path = tokens[index]
        new_path = tokens[index + 1]
        index += 2
        if not old_path or not new_path:
            raise DiffPagingError("git_failed", "git returned a malformed rename numstat record")
        result[old_path] = (added, deleted)
        result[new_path] = (added, deleted)
    return result

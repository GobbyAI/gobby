"""Filesystem identity checks shared by coordinator-only bakeoff helpers."""

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

OWNER_TASK = "#25815"


class EvidenceError(Exception):
    """A required evidence, ownership, or containment check failed."""


def require(condition: object, message: str) -> None:
    # An explicit raise, not assert: python -O strips asserts, and these checks gate evidence.
    if not condition:
        raise EvidenceError(message)


def contained_file(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    relative = path.relative_to(root)
    require(".." not in relative.parts, "parent traversal in evidence path")
    current = root
    for part in relative.parts:
        current /= part
        require(not current.is_symlink(), f"symlink in evidence path: {current}")
    metadata = path.lstat()
    require(stat.S_ISREG(metadata.st_mode), f"not a regular evidence file: {path}")
    require(metadata.st_uid == os.getuid(), f"foreign-owned evidence file: {path}")
    require(path.resolve().is_relative_to(root.resolve()), "evidence path escaped runtime")
    return path


def assert_owned_runtime(root: Path, owner_session: str) -> None:
    metadata = root.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and not root.is_symlink(), "unsafe runtime root")
    require(stat.S_IMODE(metadata.st_mode) == 0o700, "runtime root must be private")
    require(metadata.st_uid == os.getuid(), "foreign-owned runtime root")
    ownership = json.loads(contained_file(root, "ownership.json").read_text())
    require(
        ownership["runtime_root"] == str(root),
        "check failed: ownership['runtime_root'] == str(root)",
    )
    require(
        ownership["owner_task"] == OWNER_TASK,
        f"check failed: ownership['owner_task'] == {OWNER_TASK!r}",
    )
    require(ownership["owner_session"] == owner_session, "runtime session owner mismatch")


def file_record(root: Path, value: str | Path) -> dict[str, Any]:
    path = contained_file(root, value)
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": digest}


def verify_file_record(root: Path, record: Mapping[str, Any]) -> Path:
    path = contained_file(root, record["path"])
    actual = file_record(root, path)
    require(record["bytes"] == actual["bytes"], f"artifact size changed: {path}")
    require(record["sha256"] == actual["sha256"], f"artifact hash changed: {path}")
    return path


def _symlink_relative(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    relative = path.relative_to(root)
    require(".." not in relative.parts, "parent traversal in evidence path")
    current = root
    for part in relative.parts[:-1]:
        current /= part
        require(not current.is_symlink(), f"symlink in evidence path: {current}")
    return relative


def symlink_record(root: Path, value: str | Path) -> dict[str, Any]:
    relative = _symlink_relative(root, value)
    path = root / relative
    require(path.is_symlink(), f"not a symlink record: {path}")
    return {"path": str(relative), "symlink": True, "target": os.readlink(path)}


def verify_symlink_record(root: Path, record: Mapping[str, Any]) -> Path:
    path = root / _symlink_relative(root, record["path"])
    require(path.is_symlink(), f"not a symlink record: {path}")
    require(os.readlink(path) == record["target"], f"symlink target changed: {path}")
    return path


def write_once(path: Path, payload: str, mode: int) -> None:
    for parent in path.parents:
        require(not parent.is_symlink(), f"symlink parent: {parent}")
    require(not path.is_symlink(), f"symlink output: {path}")
    if path.exists():
        require(
            path.read_text(encoding="utf-8") == payload, f"refusing to overwrite evidence: {path}"
        )
        require(stat.S_IMODE(path.stat().st_mode) == mode, f"output mode mismatch: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(payload)

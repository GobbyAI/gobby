"""AGY version-gate: one daemon probe per executable identity."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass

from gobby.install.bin_freshness_models import is_at_least_version
from gobby.servers.provider_model_discovery import get_cli_version

AGY_REQUIRED_VERSION = "1.1.18"
AGY_UNPUBLISHED_REASON = "version probe has not run"
AGY_REVALIDATING_REASON = "AGY binary changed; revalidating"

_VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)")
_path_locks_guard = asyncio.Lock()
_path_locks: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    """Resolved AGY binary identity captured with the version probe."""

    realpath: str
    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int


@dataclass(frozen=True, slots=True)
class AgySupportRecord:
    """Immutable AGY support record readable from synchronous consumers."""

    installed_version: str | None
    required_version: str
    supported: bool
    reason: str
    identity: ExecutableIdentity | None


_published: AgySupportRecord | None = None
_SENTINEL = AgySupportRecord(
    installed_version=None,
    required_version=AGY_REQUIRED_VERSION,
    supported=False,
    reason=AGY_UNPUBLISHED_REASON,
    identity=None,
)
_REVALIDATING = AgySupportRecord(
    installed_version=None,
    required_version=AGY_REQUIRED_VERSION,
    supported=False,
    reason=AGY_REVALIDATING_REASON,
    identity=None,
)


def reset_agy_support_for_tests() -> None:
    """Drop the published record so tests start unpublished."""
    global _published
    _published = None
    _path_locks.clear()


def agy_support_is_published() -> bool:
    """Return whether the daemon has published a complete support record."""
    return _published is not None


def assert_agy_support_published() -> None:
    """Fail closed when the startup probe has not published a record."""
    if _published is None:
        raise RuntimeError(AGY_UNPUBLISHED_REASON)


def peek_agy_support() -> AgySupportRecord:
    """Return the published record, a mismatch sentinel, or the unpublished sentinel.

    This is a no-subprocess peek: it may ``os.stat`` the resolved path and never
    awaits, subprocesses, or re-probes.
    """
    published = _published
    if published is None:
        return _SENTINEL
    if published.identity == _resolve_agy_identity():
        return published
    return _REVALIDATING


async def probe_and_publish_agy_support() -> AgySupportRecord:
    """Startup probe: resolve AGY once and publish when identity is stable."""
    probed = await _probe_and_maybe_publish()
    return probed if probed is not None else peek_agy_support()


async def ensure_agy_support() -> AgySupportRecord:
    """Return the record covering the current bytes, re-probing at most once."""
    current = _resolve_agy_identity()
    published = _published
    if published is not None and published.identity == current:
        return published
    path_key = current.realpath if current is not None else ""
    lock = await _lock_for(path_key)
    async with lock:
        current = _resolve_agy_identity()
        published = _published
        if published is not None and published.identity == current:
            return published
        probed = await _probe_and_maybe_publish()
        if probed is not None:
            return probed
        return peek_agy_support()


def _resolve_agy_identity() -> ExecutableIdentity | None:
    located = shutil.which("agy")
    if located is None:
        return None
    resolved = os.path.realpath(located)
    try:
        st = os.stat(resolved)
    except OSError:
        return None
    return ExecutableIdentity(
        realpath=resolved,
        st_dev=st.st_dev,
        st_ino=st.st_ino,
        st_size=st.st_size,
        st_mtime_ns=st.st_mtime_ns,
    )


def _parse_installed_version(output: str | None) -> str | None:
    if output is None:
        return None
    match = _VERSION_RE.search(output)
    return match.group(1) if match else None


def _unsupported_reason(installed_label: str) -> str:
    return (
        f"Installed AGY version {installed_label} does not meet required version "
        f"{AGY_REQUIRED_VERSION}."
    )


def _supported_reason(installed: str) -> str:
    return f"AGY {installed} meets required version {AGY_REQUIRED_VERSION}."


def _record_for(
    *,
    installed_version: str | None,
    identity: ExecutableIdentity | None,
    raw_output: str | None,
    binary_present: bool,
) -> AgySupportRecord:
    if not binary_present:
        return AgySupportRecord(
            installed_version=None,
            required_version=AGY_REQUIRED_VERSION,
            supported=False,
            reason=_unsupported_reason("none"),
            identity=None,
        )
    if installed_version is None:
        label = "unparseable" if raw_output else "none"
        return AgySupportRecord(
            installed_version=None,
            required_version=AGY_REQUIRED_VERSION,
            supported=False,
            reason=_unsupported_reason(label),
            identity=identity,
        )
    if is_at_least_version(installed_version, AGY_REQUIRED_VERSION):
        return AgySupportRecord(
            installed_version=installed_version,
            required_version=AGY_REQUIRED_VERSION,
            supported=True,
            reason=_supported_reason(installed_version),
            identity=identity,
        )
    return AgySupportRecord(
        installed_version=installed_version,
        required_version=AGY_REQUIRED_VERSION,
        supported=False,
        reason=_unsupported_reason(installed_version),
        identity=identity,
    )


def _publish(record: AgySupportRecord) -> None:
    global _published
    _published = record


async def _lock_for(path_key: str) -> asyncio.Lock:
    async with _path_locks_guard:
        lock = _path_locks.get(path_key)
        if lock is None:
            lock = asyncio.Lock()
            _path_locks[path_key] = lock
        return lock


async def _probe_and_maybe_publish() -> AgySupportRecord | None:
    pre = _resolve_agy_identity()
    if pre is None:
        record = _record_for(
            installed_version=None,
            identity=None,
            raw_output=None,
            binary_present=False,
        )
        _publish(record)
        return record
    try:
        output = await get_cli_version("agy", which=lambda _name: pre.realpath)
    except OSError:
        output = None
    post = _resolve_agy_identity()
    if post != pre:
        return None
    parsed = _parse_installed_version(output)
    record = _record_for(
        installed_version=parsed,
        identity=post,
        raw_output=output,
        binary_present=True,
    )
    _publish(record)
    return record


__all__ = [
    "AGY_REQUIRED_VERSION",
    "AGY_REVALIDATING_REASON",
    "AGY_UNPUBLISHED_REASON",
    "AgySupportRecord",
    "ExecutableIdentity",
    "agy_support_is_published",
    "assert_agy_support_published",
    "ensure_agy_support",
    "peek_agy_support",
    "probe_and_publish_agy_support",
    "reset_agy_support_for_tests",
]

"""Retained copies of the managed native binaries, so a promotion can roll back.

A generation copies the installed binaries and their receipts before a promotion replaces them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.install.bin_freshness_locks import NativeBinFileLock, try_acquire_native_bin_lock
from gobby.utils.native_bin import native_bin_name

logger = logging.getLogger(__name__)

GENERATIONS_DIR = "generations"
STATE_FILE = "state.json"
KEEP_GENERATIONS = 3
MANAGED_BINARIES = ("gcode", "gdaemon", "ghook", "gclient", "gmcp", "gterm")
GCLIENT_ALIAS = "gobby"

_FORMAT = 1
_MANIFEST = "manifest.json"
_LOCK_NAME = "generations"
_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
_FREE_SPACE_MARGIN = 16 * 1024 * 1024
_REFUSAL_PREFIX = "generation retention unavailable: "
_PROMOTION_PREFIX = "promotion refused: "
_SIDECAR_SUFFIXES = ("-release-identity.json", "-version", "-install.json", "-source-sha256")
_EXTRA_FILES = {
    "gdaemon": (".gdaemon-schema-identity.json",),
    "ghook": (".ghook-compatibility", ".ghook-runtime.json"),
}
_HASH_CHUNK = 1024 * 1024
_LOCK_WAIT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05


class GenerationStoreError(OSError):
    """The generation store cannot be read, written or locked.

    An OSError, so every caller that already reports a failed promotion write reports this.
    """


@dataclass(frozen=True)
class Generation:
    """One retained snapshot of the managed binaries."""

    id: str
    seq: int
    path: Path
    manifest: dict[str, Any]


def list_generations(bin_dir: Path) -> list[Generation]:
    """Readable format-1 generations, oldest first; an absent store lists nothing."""
    store = _store(bin_dir)
    if not store.exists():
        return []
    try:
        entries = list(store.iterdir())
    except OSError as exc:
        raise GenerationStoreError(f"generation store is unreadable: {store}: {exc}") from exc
    generations = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        manifest = _read_manifest(entry)
        if manifest is not None:
            generations.append(
                Generation(id=entry.name, seq=manifest["seq"], path=entry, manifest=manifest)
            )
    return sorted(generations, key=lambda generation: generation.seq)


def read_state(bin_dir: Path) -> dict[str, Any]:
    """The parsed state file, empty when missing; unreadable state raises."""
    path = _store(bin_dir) / STATE_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise GenerationStoreError(f"generation state is unreadable: {path}: {exc}") from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise GenerationStoreError(f"generation state is not JSON: {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise GenerationStoreError(f"generation state is not an object: {path}")
    return parsed


def retain_outgoing(
    bin_dir: Path, members: Iterable[str], *, now: datetime | None = None
) -> Generation | None:
    """Copy the installed managed binaries into a new generation unless they are kept.

    Returns None without writing when none of the named binaries is installed, or when the
    newest generation recording each one already holds the installed bytes.
    """
    names = _installed_managed(bin_dir, members)
    if not names:
        return None
    moment = _utc(now)
    try:
        # Kept bytes need no write, so a promotion of retained binaries never waits on the lock.
        if _unchanged(bin_dir, names, list_generations(bin_dir)):
            return None
        with _lock(bin_dir):
            state = read_state(bin_dir)
            generations = list_generations(bin_dir)
            if _unchanged(bin_dir, names, generations):
                return None
            _check_state(bin_dir, state, generations, moment)
            generation = _capture(bin_dir, names, state, generations, moment)
            state["next_seq"] = generation.seq + 1
            _write_state(bin_dir, state)
            _prune(bin_dir, state, moment)
    except GenerationStoreError:
        raise
    except OSError as exc:
        raise GenerationStoreError(f"could not retain outgoing binaries: {exc}") from exc
    return generation


def record_promotion(
    bin_dir: Path,
    members: Iterable[str],
    *,
    source_commit: str | None = None,
    now: datetime | None = None,
) -> None:
    """Record when each managed member was promoted, and under which hold if one is set."""
    names = {name for name in members if name in MANAGED_BINARIES}
    if not names:
        return
    moment = _utc(now)
    try:
        with _lock(bin_dir):
            state = read_state(bin_dir)
            hold = state.get("hold")
            identity = hold if isinstance(hold, dict) and not _hold_expired(hold, moment) else {}
            installed = _section(state, "installed")
            for name in names:
                binary = _binary(bin_dir, name)
                installed[name] = {
                    "promoted_at": moment.isoformat(),
                    "source_commit": source_commit,
                    "session_id": identity.get("session_id"),
                    "task_id": identity.get("task_id"),
                    "sha256": _sha256(binary) if binary.is_file() else None,
                }
            state["installed"] = installed
            _write_state(bin_dir, state)
    except GenerationStoreError:
        raise
    except OSError as exc:
        raise GenerationStoreError(f"could not record promotion: {exc}") from exc


def retention_refusal(bin_dir: Path) -> str | None:
    """Why a snapshot cannot be taken before a promotion, or None when one can."""
    if not bin_dir.exists():
        return None
    store = _store(bin_dir)
    try:
        store.mkdir(exist_ok=True)
        with tempfile.TemporaryFile(dir=store):
            pass
    except OSError as exc:
        return f"{_REFUSAL_PREFIX}{exc}"
    try:
        _check_state(bin_dir, read_state(bin_dir), list_generations(bin_dir), _utc(None))
        needed = _installed_size(bin_dir) + _FREE_SPACE_MARGIN
        free = shutil.disk_usage(store).free
    except OSError as exc:
        return f"{_REFUSAL_PREFIX}{exc}"
    if free < needed:
        return f"{_REFUSAL_PREFIX}{free} bytes free in {store}, {needed} needed"
    return None


def set_hold(
    bin_dir: Path,
    *,
    task_id: str,
    session_id: str,
    expires_at: datetime,
    now: datetime | None = None,
) -> str:
    """Keep the generations a rollback needs until expires_at; returns the token that holds them."""
    moment = _utc(now)
    _check_expiry(expires_at, moment)
    token = secrets.token_urlsafe(32)
    with _store_failures("set hold"), _lock(bin_dir):
        held = read_state(bin_dir).get("hold")
        if isinstance(held, dict) and not _hold_expired(held, moment):
            raise GenerationStoreError(
                f"hold is already set by task {held.get('task_id')} until {held.get('expires_at')}"
            )
        retain_outgoing(bin_dir, MANAGED_BINARIES, now=moment)
        state = read_state(bin_dir)
        state["hold"] = {
            "task_id": task_id,
            "session_id": session_id,
            "token": token,
            "set_at": moment.isoformat(),
            "expires_at": _utc(expires_at).isoformat(),
            "first_seq": _hold_first_seq(bin_dir, state, list_generations(bin_dir)),
        }
        _write_state(bin_dir, state)
    return token


def extend_hold(
    bin_dir: Path, token: str, *, expires_at: datetime, now: datetime | None = None
) -> None:
    """Move the stored hold's expiry; only the holder's token may do so."""
    moment = _utc(now)
    _check_expiry(expires_at, moment)
    with _store_failures("extend hold"), _lock(bin_dir):
        state = read_state(bin_dir)
        hold = state.get("hold")
        if not isinstance(hold, dict):
            raise GenerationStoreError("no generation hold is set to extend")
        if not _token_matches(hold, token):
            raise GenerationStoreError("generation hold token does not match")
        hold["expires_at"] = _utc(expires_at).isoformat()
        _write_state(bin_dir, state)


def clear_hold(bin_dir: Path) -> bool:
    """Remove the stored hold; True when one was stored."""
    if not _store(bin_dir).exists():
        return False
    with _store_failures("clear hold"), _lock(bin_dir):
        state = read_state(bin_dir)
        if state.get("hold") is None:
            return False
        state["hold"] = None
        _write_state(bin_dir, state)
        return True


def hold_status(bin_dir: Path, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The stored hold without its token, or None when none is stored."""
    hold = read_state(bin_dir).get("hold")
    if not isinstance(hold, dict):
        return None
    return {
        "task_id": hold.get("task_id"),
        "session_id": hold.get("session_id"),
        "set_at": hold.get("set_at"),
        "expires_at": hold.get("expires_at"),
        "first_seq": hold.get("first_seq"),
        "expired": _hold_expired(hold, _utc(now)),
    }


def burn_version(
    bin_dir: Path,
    name: str,
    version: str,
    *,
    generation: str | None = None,
    now: datetime | None = None,
) -> None:
    """Forbid promoting this version of a managed binary again; the first burn is kept."""
    if name not in MANAGED_BINARIES:
        raise GenerationStoreError(f"cannot burn {name!r}: it is not a managed binary")
    if not version:
        raise GenerationStoreError(f"cannot burn {name!r}: the version is empty")
    moment = _utc(now)
    with _store_failures("burn version"), _lock(bin_dir):
        state = read_state(bin_dir)
        burned = _section(state, "burned")
        versions = _section(burned, name)
        versions.setdefault(version, {"burned_at": moment.isoformat(), "generation": generation})
        burned[name] = versions
        state["burned"] = burned
        _write_state(bin_dir, state)


def promotion_refusal(
    bin_dir: Path,
    name: str,
    version: str | None,
    hold_token: str | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """Why promoting name at version is refused, or None; takes no lock and writes nothing."""
    if name not in MANAGED_BINARIES:
        return None
    try:
        reason = _refusal_reason(bin_dir, name, version, hold_token, _utc(now))
    except GenerationStoreError as exc:
        reason = str(exc)
    return None if reason is None else f"{_PROMOTION_PREFIX}{reason}"


@contextmanager
def promotion_guard(
    bin_dir: Path, members: Iterable[str], *, now: datetime | None = None
) -> Iterator[Generation | None]:
    """Hold the store lock across a promotion, yielding the capture taken on entry."""
    names = list(members)
    with _lock(bin_dir):
        yield retain_outgoing(bin_dir, names, now=now)


def _check_expiry(expires_at: datetime, moment: datetime) -> None:
    if _utc(expires_at) <= moment:
        raise GenerationStoreError(f"hold expiry {expires_at.isoformat()} is not later than now")


def _token_matches(hold: dict[str, Any], token: str) -> bool:
    stored = hold.get("token")
    return isinstance(stored, str) and hmac.compare_digest(stored.encode(), token.encode())


def _hold_expired(hold: dict[str, Any], moment: datetime) -> bool:
    """True once the hold's expiry is not later than moment; an unreadable expiry never expires."""
    expires_at = _parse_instant(hold.get("expires_at"))
    return expires_at is not None and expires_at <= moment


def _hold_first_seq(bin_dir: Path, state: dict[str, Any], generations: list[Generation]) -> int:
    """The lowest sequence holding the installed bytes of a member, or the next sequence."""
    seqs: list[int] = []
    for name in _installed_managed(bin_dir, MANAGED_BINARIES):
        sha = _sha256(_binary(bin_dir, name))
        for generation in _recording(generations, name):
            if _recorded_sha(generation, name) == sha:
                seqs.append(generation.seq)
                break
    return min(seqs) if seqs else _next_seq(state, generations)


def _refusal_reason(
    bin_dir: Path, name: str, version: str | None, hold_token: str | None, moment: datetime
) -> str | None:
    state = read_state(bin_dir)
    burned = _section(_section(state, "burned"), name)
    if version is not None and version in burned:
        return (
            f"{name} {version} is burned; a burned version is never promoted again, "
            "so bump the patch version"
        )
    hold = state.get("hold")
    if hold is None:
        return None
    if not isinstance(hold, dict):
        raise GenerationStoreError("generation hold is not an object")
    if _hold_expired(hold, moment):
        return None
    if hold_token is not None and _token_matches(hold, hold_token):
        return None
    return (
        f"the generation store is held by task {hold.get('task_id')} in session "
        f"{hold.get('session_id')} until {hold.get('expires_at')}; pass its hold token"
    )


def _store(bin_dir: Path) -> Path:
    return bin_dir / GENERATIONS_DIR


def _binary(bin_dir: Path, name: str) -> Path:
    """Where a managed member is installed; its file name carries the platform suffix."""
    return bin_dir / native_bin_name(name)


def _utc(now: datetime | None) -> datetime:
    return datetime.now(UTC) if now is None else now.astimezone(UTC)


class _HeldLocks(threading.local):
    """The store lock directories this thread holds, so its nested calls reuse them."""

    def __init__(self) -> None:
        self.bin_dirs: set[str] = set()


_HELD = _HeldLocks()


@contextmanager
def _lock(bin_dir: Path) -> Iterator[None]:
    """The store lock, reentrant on the thread that already holds it for this bin_dir."""
    key = str(bin_dir.resolve())
    if key in _HELD.bin_dirs:
        yield
        return
    lock = _acquire_lock(bin_dir)
    _HELD.bin_dirs.add(key)
    try:
        yield
    finally:
        _HELD.bin_dirs.discard(key)
        lock.release()


def _acquire_lock(bin_dir: Path) -> NativeBinFileLock:
    """Wait briefly for the store lock: a capture holds it for well under a second."""
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        try:
            lock = try_acquire_native_bin_lock(_LOCK_NAME, bin_dir=bin_dir)
        except OSError as exc:
            raise GenerationStoreError(f"generation store cannot be locked: {exc}") from exc
        if lock is not None:
            return lock
        if time.monotonic() >= deadline:
            raise GenerationStoreError(
                "generation store is busy; another promotion is retaining generations"
            )
        time.sleep(_LOCK_POLL_SECONDS)


@contextmanager
def _store_failures(action: str) -> Iterator[None]:
    """Report an OSError from a store write as a GenerationStoreError naming the action."""
    try:
        yield
    except GenerationStoreError:
        raise
    except OSError as exc:
        raise GenerationStoreError(f"could not {action}: {exc}") from exc


def _check_state(
    bin_dir: Path, state: dict[str, Any], generations: list[Generation], moment: datetime
) -> None:
    """Raise for state a capture or its prune would reject, before anything is written."""
    _next_seq(state, generations)
    _section(state, "installed")
    _kept_ids(bin_dir, generations, state, moment)


def _installed_managed(bin_dir: Path, members: Iterable[str]) -> list[str]:
    wanted = set(members)
    return [
        name for name in MANAGED_BINARIES if name in wanted and _binary(bin_dir, name).is_file()
    ]


def _installed_size(bin_dir: Path) -> int:
    return sum(
        (bin_dir / file_name).stat().st_size
        for name in _installed_managed(bin_dir, MANAGED_BINARIES)
        for file_name in _member_files(bin_dir, name)
    )


def _member_files(bin_dir: Path, name: str) -> list[str]:
    """Sorted names of the files a member's generation copies, for those that exist."""
    candidates = [
        native_bin_name(name),
        *(f".{name}{suffix}" for suffix in _SIDECAR_SUFFIXES),
        *_EXTRA_FILES.get(name, ()),
    ]
    if name == "gclient":
        candidates.append(native_bin_name(GCLIENT_ALIAS))
    return sorted(file_name for file_name in candidates if (bin_dir / file_name).is_file())


def _read_manifest(directory: Path) -> dict[str, Any] | None:
    try:
        manifest = json.loads((directory / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or manifest.get("format") != _FORMAT:
        return None
    if not isinstance(manifest.get("seq"), int) or not isinstance(manifest.get("members"), dict):
        return None
    return manifest


def _recording(generations: list[Generation], name: str) -> list[Generation]:
    """The generations that record a member, newest first."""
    newest_first = list(reversed(generations))
    return [generation for generation in newest_first if name in generation.manifest["members"]]


def _recorded_sha(generation: Generation, name: str) -> object:
    member = generation.manifest["members"].get(name)
    return member.get("sha256") if isinstance(member, dict) else None


def _unchanged(bin_dir: Path, names: list[str], generations: list[Generation]) -> bool:
    """True when each named binary's installed bytes are the newest retained copy."""
    for name in names:
        recording = _recording(generations, name)
        if not recording or _recorded_sha(recording[0], name) != _sha256(_binary(bin_dir, name)):
            return False
    return True


def _next_seq(state: dict[str, Any], generations: list[Generation]) -> int:
    next_seq = state.get("next_seq", 1)
    if not isinstance(next_seq, int) or next_seq < 1:
        raise GenerationStoreError("generation state next_seq is invalid")
    newest = generations[-1].seq if generations else 0
    return max(next_seq, newest + 1)


def _capture(
    bin_dir: Path,
    names: list[str],
    state: dict[str, Any],
    generations: list[Generation],
    moment: datetime,
) -> Generation:
    """Build the generation under a dot-named staging directory, then rename it into place."""
    store = _store(bin_dir)
    seq = _next_seq(state, generations)
    final = store / f"{seq:06d}-{moment.strftime(_TIME_FORMAT)}"
    if final.exists():
        raise GenerationStoreError(f"generation {final.name} already exists")
    store.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{final.name}-", dir=str(store)))
    try:
        installed = _section(state, "installed")
        members = {
            name: _member_record(bin_dir, name, staging, installed.get(name)) for name in names
        }
        manifest = {
            "format": _FORMAT,
            "id": final.name,
            "seq": seq,
            "created_at": moment.isoformat(),
            "schema_identity": _schema_identity(bin_dir),
            "members": members,
        }
        with (staging / _MANIFEST).open("xb") as writer:
            writer.write(_json_bytes(manifest))
            writer.flush()
            os.fsync(writer.fileno())
        _sync_directory(staging)
        os.rename(staging, final)
        _sync_directory(store)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return Generation(id=final.name, seq=seq, path=final, manifest=manifest)


def _member_record(bin_dir: Path, name: str, staging: Path, installed: object) -> dict[str, Any]:
    """Copy one member's files into staging and describe it for the manifest."""
    files = _member_files(bin_dir, name)
    for file_name in files:
        _copy_file(bin_dir / file_name, staging / file_name)
    receipt = _read_json_object(bin_dir / f".{name}-release-identity.json")
    sha256 = _sha256(staging / native_bin_name(name))
    # A promotion record describes these bytes only when it was written for them.
    promotion = (
        installed if isinstance(installed, dict) and installed.get("sha256") == sha256 else {}
    )
    record: dict[str, Any] = {
        "sha256": sha256,
        "version": _text_or_none(receipt.get("version")),
        "source_identity": _text_or_none(receipt.get("source_identity")),
        "promoted_at": _text_or_none(promotion.get("promoted_at")),
        "source_commit": _text_or_none(promotion.get("source_commit")),
        "session_id": _text_or_none(promotion.get("session_id")),
        "task_id": _text_or_none(promotion.get("task_id")),
        "files": files,
    }
    if name == "gclient":
        record["alias"] = native_bin_name(GCLIENT_ALIAS) in files
    return record


def _copy_file(source: Path, destination: Path) -> None:
    """Copy a file's bytes and mode into a new inode, durable before the rename."""
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copymode(source, destination)


def _sync_directory(path: Path) -> None:
    """Make a directory's entries durable; Windows has no directory handle to sync."""
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _schema_identity(bin_dir: Path) -> object:
    try:
        return json.loads((bin_dir / ".gdaemon-schema-identity.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _section(state: dict[str, Any], key: str) -> dict[str, Any]:
    """A state object keyed by name, empty when absent; a non-object is unreadable state."""
    value = state.get(key, {})
    if not isinstance(value, dict):
        raise GenerationStoreError(f"generation state {key} is not an object")
    return value


def _write_state(bin_dir: Path, state: dict[str, Any]) -> None:
    store = _store(bin_dir)
    store.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes({**state, "format": _FORMAT})
    fd, temporary = tempfile.mkstemp(dir=str(store), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, store / STATE_FILE)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _prune(bin_dir: Path, state: dict[str, Any], moment: datetime) -> None:
    """Delete generations nothing keeps, and staging directories a crashed capture left."""
    store = _store(bin_dir)
    generations = list_generations(bin_dir)
    kept = _kept_ids(bin_dir, generations, state, moment)
    for generation in generations:
        if generation.id not in kept:
            _remove_tree(generation.path)
    for entry in store.iterdir():
        if entry.name.startswith(".") and entry.is_dir():
            _remove_tree(entry)


def _kept_ids(
    bin_dir: Path, generations: list[Generation], state: dict[str, Any], moment: datetime
) -> set[str]:
    kept: set[str] = set()
    names = {member for generation in generations for member in generation.manifest["members"]}
    for name in names:
        newest = _recording(generations, name)[:KEEP_GENERATIONS]
        kept.update(generation.id for generation in newest)
    for generation_id, pin in _section(state, "pins").items():
        if _instant(pin.get("until") if isinstance(pin, dict) else None) > moment:
            kept.add(generation_id)
    hold = state.get("hold")
    if isinstance(hold, dict):
        first_seq = hold.get("first_seq")
        if not isinstance(first_seq, int):
            raise GenerationStoreError("generation hold has no first_seq")
        kept.update(generation.id for generation in generations if generation.seq >= first_seq)
    gdaemon = _binary(bin_dir, "gdaemon")
    if gdaemon.is_file():
        installed_sha = _sha256(gdaemon)
        kept.update(
            generation.id
            for generation in generations
            if _recorded_sha(generation, "gdaemon") == installed_sha
        )
    return kept


def _instant(value: object) -> datetime:
    parsed = _parse_instant(value)
    if parsed is None:
        raise GenerationStoreError(f"generation state has an invalid time: {value!r}")
    return parsed


def _parse_instant(value: object) -> datetime | None:
    """An aware UTC time from an ISO-8601 string; a naive one is taken as UTC, else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _remove_tree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError:
        logger.warning("could not remove generation directory %s", path, exc_info=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


__all__ = [
    "GCLIENT_ALIAS",
    "GENERATIONS_DIR",
    "KEEP_GENERATIONS",
    "MANAGED_BINARIES",
    "STATE_FILE",
    "Generation",
    "GenerationStoreError",
    "burn_version",
    "clear_hold",
    "extend_hold",
    "hold_status",
    "list_generations",
    "promotion_guard",
    "promotion_refusal",
    "read_state",
    "record_promotion",
    "retain_outgoing",
    "retention_refusal",
    "set_hold",
]

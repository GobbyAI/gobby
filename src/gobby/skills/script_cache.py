"""Shared synchronization and ownership contracts for script runtime caches."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover - Windows only
    import msvcrt
else:  # pragma: no branch - POSIX platforms share fcntl
    import fcntl

from gobby.sync.jsonl_io import atomic_write_text, export_file_lock

_READINESS_FILE = ".gobby-browser-ready.json"
SKILL_SCRIPTS_OWNER_FILE = "owner.json"
SKILL_SCRIPTS_PROVENANCE_FILE = "provenance.json"
PROCESS_OWNER_FILE = ".gobby-process-owner.json"
BROWSER_FETCH_OWNER_FILE = ".gobby-browser-fetch-owner.json"
DEPENDENCY_READY_FILE = ".gobby-dependencies-ready.json"
STAGE_PREFIX = ".gobby-stage-"
DELETION_TOMBSTONE_PREFIX = ".gobby-delete-"
_OWNER_SCHEMA_VERSION = 1
_LOCK_RETRY_SECONDS = 0.05


@dataclass(frozen=True)
class BrowserCacheReadiness:
    """Identity of browser artifacts compatible with one Puppeteer runtime."""

    platform: str
    puppeteer_version: str
    browser_build: str
    channel: str


@dataclass(frozen=True)
class SkillScriptsOwner:
    """Immutable facts proving one cache root belongs to a skill."""

    skill_id: str
    skill_name: str
    source_origin: str | None
    schema_version: int = _OWNER_SCHEMA_VERSION


def skill_scripts_namespace(home: Path) -> Path:
    """Return the configured namespace containing materialized skill roots."""
    return home / "cache" / "skill-scripts"


def skill_scripts_root(home: Path, skill_id: str) -> Path:
    """Return one skill's immutable-generation cache root."""
    return skill_scripts_namespace(home) / skill_id


def skill_scripts_namespace_lock_target(namespace: Path) -> Path:
    """Return the stable discovery lock identity for the namespace."""
    return namespace / "namespace"


def skill_scripts_root_lock_target(root: Path) -> Path:
    """Return a lock identity beside the root so locking cannot create it."""
    return root.with_name(f"{root.name}.gobby-root")


def browser_cache_lock_target(cache_root: Path) -> Path:
    """Return the lock identity shared by every Puppeteer cache producer."""
    return cache_root / ".gobby-browser-cache"


def stage_name(kind: str, token: str) -> str:
    """Return an ownership-recognizable name for unpublished state."""
    if not kind or not token or any(char in kind + token for char in "/\\"):
        raise ValueError("invalid stage identity")
    return f"{STAGE_PREFIX}{kind}-{token}"


def is_owned_stage(path: Path) -> bool:
    """Return whether a path uses Gobby's reserved stage namespace."""
    return path.name.startswith(STAGE_PREFIX)


def deletion_tombstone_name(skill_id: str, token: str) -> str:
    """Return an ownership-recognizable name for a root being deleted."""
    if not skill_id or not token or any(char in skill_id + token for char in "/\\"):
        raise ValueError("invalid deletion tombstone identity")
    return f"{DELETION_TOMBSTONE_PREFIX}{skill_id}-{token}"


def is_deletion_tombstone(path: Path) -> bool:
    """Return whether a path is an ownership-preserving deletion tombstone."""
    return path.name.startswith(DELETION_TOMBSTONE_PREFIX)


def read_skill_scripts_owner(root: Path) -> SkillScriptsOwner | None:
    """Read and validate a versioned cache-root ownership marker."""
    try:
        value = json.loads((root / SKILL_SCRIPTS_OWNER_FILE).read_text(encoding="utf-8"))
        owner = SkillScriptsOwner(
            schema_version=value["schema_version"],
            skill_id=value["skill_id"],
            skill_name=value["skill_name"],
            source_origin=value.get("source_origin"),
        )
    except (KeyError, OSError, TypeError, ValueError):
        return None
    if owner.schema_version != _OWNER_SCHEMA_VERSION:
        return None
    if not owner.skill_id or not owner.skill_name:
        return None
    if owner.source_origin is not None and not isinstance(owner.source_origin, str):
        return None
    return owner


def write_skill_scripts_owner(root: Path, owner: SkillScriptsOwner) -> None:
    """Write the immutable cache-root ownership marker."""
    if owner.schema_version != _OWNER_SCHEMA_VERSION:
        raise ValueError("unsupported skill scripts owner schema")
    atomic_write_text(
        root / SKILL_SCRIPTS_OWNER_FILE,
        json.dumps(asdict(owner), sort_keys=True) + "\n",
    )


def write_generation_provenance(generation: Path, value: dict[str, object]) -> None:
    """Publish normalized mutable revision provenance beside one generation."""
    atomic_write_text(
        generation / SKILL_SCRIPTS_PROVENANCE_FILE,
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
    )


def process_start_identity(pid: int) -> str | None:
    """Return a value that distinguishes PID reuse for one process."""
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21]
    except (IndexError, OSError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def write_process_owner(path: Path, pgid: int) -> None:
    """Record a process group and its leader identity before releasing a barrier."""
    identity = process_start_identity(pgid)
    if identity is None:
        raise RuntimeError("cannot record process identity")
    atomic_write_text(
        path,
        json.dumps({"leader_start": identity, "pgid": pgid}, sort_keys=True) + "\n",
    )
    fsync_directory(path.parent)


def process_owner_is_live(path: Path) -> bool:
    """Return whether a recorded process group still has its original leader."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        pgid = value["pgid"]
        identity = value["leader_start"]
        if not isinstance(pgid, int) or not isinstance(identity, str):
            return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    current = process_start_identity(pgid)
    if current is not None and current != identity:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def live_process_owner_records(root: Path) -> list[Path]:
    """Return live known writer records beneath a non-symlink root."""
    if root.is_symlink():
        return []
    owner_names = {PROCESS_OWNER_FILE, BROWSER_FETCH_OWNER_FILE}
    live: list[Path] = []

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, directories, files in os.walk(root, followlinks=False, onerror=raise_walk_error):
        current_path = Path(current)
        directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
        for name in owner_names.intersection(files):
            owner = current_path / name
            if process_owner_is_live(owner):
                live.append(owner)
    return sorted(live)


def collect_deletion_tombstones(parent: Path) -> list[Path]:
    """Finish crash-interrupted root removals without following symlinks."""
    removed: list[Path] = []
    try:
        candidates = list(parent.iterdir())
    except FileNotFoundError:
        return removed
    for candidate in candidates:
        if not is_deletion_tombstone(candidate):
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
        else:
            candidate.unlink(missing_ok=True)
        removed.append(candidate)
    return removed


def collect_stale_stages(parent: Path) -> list[Path]:
    """Remove inactive owned stages and return live stages that were preserved."""
    collect_deletion_tombstones(parent)
    live: list[Path] = []
    try:
        candidates = list(parent.iterdir())
    except FileNotFoundError:
        return live
    for candidate in candidates:
        if not is_owned_stage(candidate):
            continue
        if process_owner_is_live(candidate / PROCESS_OWNER_FILE):
            live.append(candidate)
            continue
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink(missing_ok=True)
    return live


def fsync_directory(path: Path) -> None:
    """Durably record directory entry changes."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root: Path) -> None:
    """Durably flush regular files and directories below a publication root."""
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        elif path.is_dir():
            fsync_directory(path)
    fsync_directory(root)


async def run_blocking_safely[**P, T](func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Finish a filesystem worker before cancellation can release its caller's lock."""
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _try_file_lock(fd: int) -> bool:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        os.ftruncate(fd, 1)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock_file(fd: int) -> None:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


@asynccontextmanager
async def async_export_file_lock(target: Path) -> AsyncIterator[None]:
    """Acquire an export-compatible file lock without blocking the event loop."""
    await run_blocking_safely(target.parent.mkdir, 0o777, True, True)
    lock_path = target.with_name(f".{target.name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        while not acquired:
            acquired = _try_file_lock(fd)
            if not acquired:
                await asyncio.sleep(_LOCK_RETRY_SECONDS)
        yield
    finally:
        try:
            if acquired:
                _unlock_file(fd)
        finally:
            os.close(fd)


@contextmanager
def browser_cache_lock(cache_root: Path) -> Iterator[None]:
    """Serialize producers that mutate one Puppeteer cache."""
    with export_file_lock(browser_cache_lock_target(cache_root)):
        yield


@asynccontextmanager
async def async_browser_cache_lock(cache_root: Path) -> AsyncIterator[None]:
    """Asynchronously serialize producers mutating one Puppeteer cache."""
    async with async_export_file_lock(browser_cache_lock_target(cache_root)):
        yield


def read_browser_cache_readiness(cache_root: Path) -> BrowserCacheReadiness | None:
    """Read a valid compatibility record, treating corruption as a cache miss."""
    try:
        value = json.loads((cache_root / _READINESS_FILE).read_text(encoding="utf-8"))
        fields = (
            value["platform"],
            value["puppeteer_version"],
            value["browser_build"],
            value["channel"],
        )
        if not all(isinstance(field, str) and field for field in fields):
            return None
        return BrowserCacheReadiness(*fields)
    except (KeyError, OSError, TypeError, ValueError):
        return None


def browser_cache_is_ready(cache_root: Path, expected: BrowserCacheReadiness) -> bool:
    """Return whether the durable record matches the producer's exact needs."""
    return read_browser_cache_readiness(cache_root) == expected


def write_browser_cache_readiness(
    cache_root: Path,
    readiness: BrowserCacheReadiness,
) -> None:
    """Publish a compatibility record atomically."""
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_text(
        cache_root / _READINESS_FILE,
        json.dumps(asdict(readiness), sort_keys=True) + "\n",
    )
    directory_fd = os.open(cache_root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

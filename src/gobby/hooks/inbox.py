"""Daemon-side replay for hook inbox envelopes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from random import SystemRandom
from typing import Any, Final

import httpx

from gobby.cli.utils import get_gobby_home
from gobby.hooks.envelope_dedupe import (
    ENVELOPE_ID_HEADER,
    DirectoryPruneResult,
    clear_stale_envelope_processing_marker,
    envelope_id_from_inbox_path,
    get_processed_envelope_dir,
    is_envelope_processed,
    is_envelope_processing_active,
    is_inbox_envelope_fresh,
    mark_envelope_processed,
    prune_directory_by_age,
    prune_processed_envelope_markers,
)
from gobby.hooks.runtime_compat import SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION
from gobby.utils.local_token import read_local_api_token

logger = logging.getLogger(__name__)
_JITTER_RANDOM = SystemRandom()
_DRAIN_LOCK_STATE_KEY = "_gobby_hook_inbox_drain_lock"

# How long an abandoned temp file must sit before the reaper takes it. ghook
# writes an envelope as create, write, fsync, rename with no waiting between
# the steps, so an hour is orders of magnitude past the longest plausible
# in-flight write and no temp file a running ghook could still rename falls
# inside it. The writer already removes its own temp on every error return
# (crates/ghook/src/transport.rs); this window exists for the one case the
# writer cannot handle, a killed process.
ORPHANED_TEMP_RETENTION_SECONDS: Final = 60 * 60.0

# Same bound and the same reason as the marker prune: one pass reads at most
# this many entries, so a backlog drains over successive passes while each pass
# stays short. The inbox holds tens of entries in steady state.
ORPHANED_TEMP_PRUNE_MAX_ENTRIES: Final = 100_000


def get_hook_inbox_dir() -> Path:
    """Return the daemon hook inbox directory."""
    return get_gobby_home() / "hooks" / "inbox"


def get_hook_quarantine_dir(inbox_dir: Path | None = None) -> Path:
    """Return the daemon hook inbox quarantine directory."""
    root = inbox_dir or get_hook_inbox_dir()
    return root / "quarantine"


def _iter_inbox_files(inbox_dir: Path) -> list[Path]:
    """Return replayable inbox envelope files in deterministic order."""
    if not inbox_dir.exists():
        return []
    return sorted(
        path
        for path in inbox_dir.iterdir()
        if path.is_file() and path.suffix == ".json" and not path.name.endswith(".tmp")
    )


def _quarantine_file(path: Path, *, reason: str, detail: str) -> bool:
    """Move an unreadable or invalid inbox file into quarantine with metadata."""
    quarantine_dir = get_hook_quarantine_dir(path.parent)
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    target = quarantine_dir / path.name
    meta_path = quarantine_dir / f"{path.name}.meta.json"

    try:
        target.write_bytes(path.read_bytes())
        path.unlink(missing_ok=True)
        meta_path.write_text(
            json.dumps({"reason": reason, "detail": detail}, indent=2) + "\n",
            encoding="utf-8",
        )
    except FileNotFoundError:
        logger.debug(
            "Hook inbox file %s disappeared before quarantine (reason=%s)",
            path,
            reason,
        )
        return True
    except Exception as exc:
        logger.exception(
            "Failed to quarantine hook inbox file %s (reason=%s, detail=%s): %s",
            path,
            reason,
            detail,
            exc,
        )
        return False
    return True


def _quarantine_or_warn(path: Path, *, reason: str, detail: str) -> None:
    """Best-effort quarantine with a warning when quarantine itself fails."""
    if not _quarantine_file(path, reason=reason, detail=detail):
        logger.warning(
            "Skipping hook inbox file %s after quarantine failed (reason=%s)",
            path,
            reason,
        )


def _load_envelope(path: Path) -> dict[str, Any] | None:
    """Load and minimally validate a replay envelope from disk."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _quarantine_or_warn(path, reason="invalid_json", detail=str(exc))
        return None

    if not isinstance(raw, dict):
        _quarantine_or_warn(
            path, reason="invalid_envelope", detail="Envelope must be a JSON object"
        )
        return None

    if raw.get("schema_version") != SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION:
        _quarantine_or_warn(
            path,
            reason="invalid_envelope",
            detail=(
                "Unsupported schema_version: "
                f"{raw.get('schema_version')}. Supported: {SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION}"
            ),
        )
        return None

    if not raw.get("hook_type") or not raw.get("source"):
        _quarantine_or_warn(
            path,
            reason="invalid_envelope",
            detail="Envelope must include hook_type and source",
        )
        return None

    return raw


async def _post_envelope(
    app: Any,
    envelope: dict[str, Any],
    *,
    envelope_id: str | None = None,
) -> httpx.Response:
    """Replay an inbox envelope through the real hook ingress route."""
    headers = envelope.get("headers")
    request_headers = (
        {
            str(key): str(value)
            for key, value in headers.items()
            if str(key).lower() != "authorization"
        }
        if isinstance(headers, dict)
        else {}
    )
    # Inbox replay runs inside the daemon and drains envelopes for every
    # session, so it must authenticate as the operator. An inherited
    # GOBBY_AGENT_API_TOKEN (daemon_auth_headers prefers it) would scope the
    # replay to one run's capability and 401 other sessions' envelopes.
    operator_token = read_local_api_token()
    if operator_token is not None:
        request_headers["Authorization"] = f"Bearer {operator_token}"
    if envelope_id:
        request_headers[ENVELOPE_ID_HEADER] = envelope_id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://gobby.internal",
        timeout=30.0,
    ) as client:
        return await client.post(
            "/api/hooks/execute",
            json=envelope,
            headers=request_headers,
        )


@dataclass(frozen=True)
class HookInboxBarrierResult:
    """Bounded startup replay outcome."""

    replayed: int
    timed_out: bool
    unresolved_run_ids: tuple[str, ...]
    unresolved_session_ids: tuple[str, ...]


def _get_hook_inbox_drain_lock(app: Any) -> asyncio.Lock:
    """Return the app-scoped lock coordinating hook inbox consumers."""
    lock: asyncio.Lock | None = getattr(app.state, _DRAIN_LOCK_STATE_KEY, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(app.state, _DRAIN_LOCK_STATE_KEY, lock)
    return lock


async def _drain_hook_inbox_once_locked(
    app: Any,
    inbox_dir: Path | None = None,
    *,
    include_fresh: bool = False,
) -> int:
    """Replay pending envelopes while the app-scoped drain lock is held."""
    pending_dir = inbox_dir or get_hook_inbox_dir()
    if not pending_dir.exists():
        return 0

    pending_files = _iter_inbox_files(pending_dir)
    if not pending_files:
        return 0
    if read_local_api_token() is None:
        logger.warning(
            "Daemon API token missing; run 'gobby install' or 'gobby auth token --rotate' "
            "on the hub machine and copy ~/.gobby/local_cli_token here",
            extra={
                "inbox_path": str(pending_dir),
                "pending_envelopes": len(pending_files),
            },
        )
        return 0

    replayed = 0
    processed_dir = get_processed_envelope_dir(pending_dir)
    for path in pending_files:
        envelope_id = envelope_id_from_inbox_path(path)
        if envelope_id and is_envelope_processed(envelope_id, processed_dir=processed_dir):
            logger.debug("Skipping already-processed hook inbox envelope %s", path.name)
            path.unlink(missing_ok=True)
            continue

        if not include_fresh and is_inbox_envelope_fresh(path):
            logger.debug("Skipping fresh hook inbox envelope %s", path.name)
            continue

        if envelope_id and is_envelope_processing_active(envelope_id, processed_dir=processed_dir):
            logger.debug("Skipping active hook inbox envelope %s", path.name)
            continue

        if envelope_id and clear_stale_envelope_processing_marker(
            envelope_id,
            processed_dir=processed_dir,
        ):
            logger.warning("Cleared stale processing marker for hook inbox envelope %s", path.name)

        envelope = _load_envelope(path)
        if envelope is None:
            continue

        try:
            response = await _post_envelope(app, envelope, envelope_id=envelope_id)
        except Exception as exc:
            logger.warning("Hook inbox replay failed for %s: %s", path.name, exc)
            continue

        if 200 <= response.status_code < 300:
            if not envelope_id:
                # The event was processed but cannot be marked in the
                # dedupe ledger; retaining it would replay it forever and
                # keep the startup barrier from ever settling.
                _quarantine_or_warn(
                    path,
                    reason="missing_envelope_id",
                    detail="Replay succeeded but the file name carries no envelope ID",
                )
                replayed += 1
                continue

            mark_envelope_processed(envelope_id, processed_dir=processed_dir)
            path.unlink(missing_ok=True)
            replayed += 1
            continue

        if response.status_code == 409:
            logger.debug(
                "Hook inbox replay found active processing marker for %s; retaining file",
                path.name,
            )
            continue

        logger.warning(
            "Hook inbox replay returned %s for %s",
            response.status_code,
            path.name,
        )

    return replayed


async def drain_hook_inbox_once(
    app: Any,
    inbox_dir: Path | None = None,
    *,
    include_fresh: bool = False,
) -> int:
    """Replay all pending hook envelopes once.

    Returns the number of envelopes successfully replayed and deleted.
    """
    async with _get_hook_inbox_drain_lock(app):
        return await _drain_hook_inbox_once_locked(
            app,
            inbox_dir,
            include_fresh=include_fresh,
        )


async def drain_hook_inbox_barrier(
    app: Any,
    inbox_dir: Path | None = None,
    *,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
) -> HookInboxBarrierResult:
    """Replay fresh and stale envelopes before agent restart classification."""
    pending_dir = inbox_dir or get_hook_inbox_dir()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    replayed = 0

    async with _get_hook_inbox_drain_lock(app):
        while True:
            replayed += await _drain_hook_inbox_once_locked(
                app,
                pending_dir,
                include_fresh=True,
            )
            pending_files = _iter_inbox_files(pending_dir) if pending_dir.exists() else []
            if not pending_files:
                return HookInboxBarrierResult(replayed, False, (), ())
            if time.monotonic() >= deadline:
                run_ids, session_ids = _unresolved_envelope_identities(pending_files)
                return HookInboxBarrierResult(
                    replayed,
                    True,
                    tuple(sorted(run_ids)),
                    tuple(sorted(session_ids)),
                )
            await asyncio.sleep(poll_interval_seconds)


def _unresolved_envelope_identities(paths: list[Path]) -> tuple[set[str], set[str]]:
    run_ids: set[str] = set()
    session_ids: set[str] = set()
    for path in paths:
        envelope = _load_envelope(path)
        if envelope is None:
            continue
        input_data = envelope.get("input_data")
        if not isinstance(input_data, dict):
            continue
        terminal_context = input_data.get("terminal_context")
        if isinstance(terminal_context, dict):
            run_id = terminal_context.get("gobby_agent_run_id")
            if isinstance(run_id, str) and run_id:
                run_ids.add(run_id)
            session_id = terminal_context.get("gobby_session_id")
            if isinstance(session_id, str) and session_id:
                session_ids.add(session_id)
        headers = envelope.get("headers")
        if isinstance(headers, dict):
            session_id = headers.get("X-Gobby-Session-Id") or headers.get("x-gobby-session-id")
            if isinstance(session_id, str) and session_id:
                session_ids.add(session_id)
    return run_ids, session_ids


def _compute_sleep_seconds(interval_seconds: int, jitter_seconds: float) -> float:
    """Return a non-negative poll interval with bounded jitter."""
    return max(
        0.0,
        interval_seconds + _JITTER_RANDOM.uniform(-jitter_seconds, jitter_seconds),
    )


def _is_orphaned_temp_name(name: str) -> bool:
    """True for the intermediate file ghook's atomic write leaves behind."""
    return name.endswith(".tmp")


def prune_orphaned_inbox_temp_files(
    inbox_dir: Path | None = None,
    *,
    now: float | None = None,
    retention_seconds: float = ORPHANED_TEMP_RETENTION_SECONDS,
    max_entries: int = ORPHANED_TEMP_PRUNE_MAX_ENTRIES,
) -> DirectoryPruneResult:
    """Delete temp files a dead writer left behind, bounded to one pass.

    Blocking: the caller must keep this off the event loop thread. Only `.tmp`
    names are candidates, because a pending envelope shares this directory and
    is legitimately older than any window whenever the daemon was down.
    """
    target = inbox_dir if inbox_dir is not None else get_hook_inbox_dir()
    cutoff = (now if now is not None else time.time()) - retention_seconds
    return prune_directory_by_age(
        target,
        cutoff=cutoff,
        max_entries=max_entries,
        matches=_is_orphaned_temp_name,
    )


def _prune_hook_inbox_blocking(
    inbox_dir: Path,
) -> tuple[DirectoryPruneResult, DirectoryPruneResult]:
    """Run both retention passes in one worker-thread hop."""
    markers = prune_processed_envelope_markers(get_processed_envelope_dir(inbox_dir))
    temps = prune_orphaned_inbox_temp_files(inbox_dir)
    return markers, temps


async def prune_hook_inbox(inbox_dir: Path | None = None) -> int:
    """Drop expired markers and abandoned temp files, off the loop thread.

    Both passes stat every entry they read, so they share one worker-thread hop
    however small the directories currently are. Returns the total deleted.
    """
    root = inbox_dir or get_hook_inbox_dir()
    markers, temps = await asyncio.to_thread(_prune_hook_inbox_blocking, root)
    processed_dir = get_processed_envelope_dir(root)
    if markers.deleted:
        logger.info(
            "Pruned %s expired hook envelope marker(s) from %s",
            markers.deleted,
            processed_dir,
            extra={
                "event": "hook_envelope_markers_pruned",
                "examined": markers.examined,
                "deleted": markers.deleted,
                "backlog_remaining": markers.truncated,
            },
        )
    if temps.deleted:
        logger.info(
            "Reaped %s abandoned hook envelope temp file(s) from %s",
            temps.deleted,
            root,
            extra={
                "event": "hook_envelope_temp_files_reaped",
                "examined": temps.examined,
                "deleted": temps.deleted,
                "backlog_remaining": temps.truncated,
            },
        )
    return markers.deleted + temps.deleted


async def drain_hook_inbox_loop(
    app: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 60,
    jitter_seconds: float = 5.0,
    prune_interval_seconds: float = 3600.0,
) -> None:
    """Background loop that replays pending hook inbox envelopes.

    The loop also owns inbox retention -- expired processed markers and temp
    files a dead writer abandoned both live in this directory, so pruning them
    here needs no second scheduled loop. Pruning runs on its own slower cadence
    because a drain has to be frequent and a prune does not.
    """
    try:
        replayed = await drain_hook_inbox_once(app)
        if replayed > 0:
            logger.debug("Hook inbox replayed %s pending envelope(s)", replayed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Initial hook inbox drain failed: %s", exc)

    next_prune_at = time.monotonic()
    while not is_shutdown_requested():
        try:
            sleep_seconds = _compute_sleep_seconds(interval_seconds, jitter_seconds)
            await asyncio.sleep(sleep_seconds)
            replayed = await drain_hook_inbox_once(app)
            if replayed > 0:
                logger.debug("Hook inbox replayed %s pending envelope(s)", replayed)
            if time.monotonic() >= next_prune_at:
                next_prune_at = time.monotonic() + prune_interval_seconds
                await prune_hook_inbox()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Hook inbox drain loop failed: %s", exc)

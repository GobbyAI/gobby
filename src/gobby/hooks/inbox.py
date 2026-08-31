"""Daemon-side replay for hook inbox envelopes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from random import SystemRandom
from typing import Any, Final

import httpx

from gobby.cli.utils import get_gobby_home
from gobby.hooks import grok_pending_context
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
    release_envelope_processing_claim,
    remove_envelope_marker,
)
from gobby.hooks.receipt_effects import apply_acknowledged_receipt
from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    envelope_has_hook_response_capability,
)
from gobby.utils.datetime import utc_now
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

# Quarantined envelopes are diagnostic artifacts. The window is fixed: no
# config override, measured on wall-clock time against the sidecar timestamp.
HOOK_QUARANTINE_RETENTION_WINDOW: Final = 24 * 60 * 60.0
HOOK_QUARANTINE_PRUNE_MAX_ENTRIES: Final = 100_000
_META_SUFFIX: Final = ".meta.json"


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
            json.dumps(
                {
                    "reason": reason,
                    "detail": detail,
                    "quarantined_at": utc_now().isoformat(),
                },
                indent=2,
            )
            + "\n",
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


def _consume_inbox_delivery_receipt(
    app: Any,
    envelope: dict[str, Any],
    path: Path,
    envelope_id: str | None,
    *,
    processed_dir: Path,
) -> None:
    """CAS the receipt without re-executing the original hook, then drop the file."""

    from gobby.storage.hook_receipts import acknowledge_receipt

    receipt_id = envelope.get("receipt_id")
    generation = envelope.get("delivery_generation")
    state = getattr(app, "state", None)
    hook_manager = getattr(state, "hook_manager", None)
    db = getattr(state, "database", None)
    if db is None:
        db = getattr(hook_manager, "db", None)
    if db is not None and isinstance(receipt_id, str) and isinstance(generation, int):
        try:
            committed = acknowledge_receipt(
                db,
                receipt_id=receipt_id,
                delivery_generation=generation,
            )
            if committed is not None:
                from gobby.workflows.state_manager import SessionVariableManager

                apply_acknowledged_receipt(
                    committed,
                    message_manager=getattr(hook_manager, "_inter_session_msg_manager", None),
                    variable_manager=SessionVariableManager(db),
                )
                # The acknowledged delivery terminalizes every envelope that
                # carried it; a retained original must never replay its hook.
                _mark_carrying_envelopes_processed(
                    committed,
                    ack_envelope_id=envelope_id,
                    processed_dir=processed_dir,
                )
        except Exception as exc:
            logger.warning(
                "Failed to consume delivery receipt %s generation %s: %s",
                receipt_id,
                generation,
                exc,
            )
    if envelope_id:
        mark_envelope_processed(envelope_id, processed_dir=processed_dir)
    path.unlink(missing_ok=True)


def consume_pending_delivery_receipts(app: Any, inbox_dir: Path | None = None) -> int:
    """Consume well-formed delivery-receipt acks waiting in the inbox.

    A receipted hook response re-prepares the session's newest undelivered
    receipt onto its own envelope, bumping the delivery generation. ghook
    writes acks back into the inbox, but the periodic drain (60s) is too slow
    for a busy session: by the time it runs, the ack's generation is stale and
    the CAS records a no-op, so the receipt re-prepares forever. Sweeping acks
    synchronously before the re-prepare lets an in-flight ack land while its
    generation is still current. Only files whose parsed body is a well-formed
    delivery receipt are touched; every other file is left for the drain and
    its quarantine rules.
    """
    pending_dir = inbox_dir or get_hook_inbox_dir()
    if not pending_dir.exists():
        return 0
    consumed = 0
    processed_dir = get_processed_envelope_dir(pending_dir)
    for path in _iter_inbox_files(pending_dir):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or raw.get("kind") != "delivery-receipt":
            continue
        receipt_id = raw.get("receipt_id")
        generation = raw.get("delivery_generation")
        if not isinstance(receipt_id, str) or not receipt_id:
            continue
        if not isinstance(generation, int) or generation < 1:
            continue
        _consume_inbox_delivery_receipt(
            app,
            raw,
            path,
            envelope_id_from_inbox_path(path),
            processed_dir=processed_dir,
        )
        consumed += 1
    return consumed


def _mark_carrying_envelopes_processed(
    receipt: Any,
    *,
    ack_envelope_id: str | None,
    processed_dir: Path,
) -> None:
    """Mark the receipt's original and current carrying envelopes processed."""
    carrying: list[str] = []
    for attribute in ("original_envelope_id", "current_envelope_id"):
        candidate = getattr(receipt, attribute, None)
        if (
            isinstance(candidate, str)
            and candidate
            and candidate != ack_envelope_id
            and candidate not in carrying
        ):
            carrying.append(candidate)
    for carried_id in carrying:
        try:
            mark_envelope_processed(carried_id, processed_dir=processed_dir)
        except Exception as exc:
            logger.warning(
                "Failed to terminalize carrying envelope %s for receipt %s: %s",
                carried_id,
                getattr(receipt, "receipt_id", None),
                exc,
            )


def _terminalize_below_floor_receipts(app: Any, envelope_id: str) -> None:
    """Best-effort: any prepared receipt for this envelope becomes undelivered."""
    from gobby.storage.hook_receipts import terminalize_receipts_for_envelope

    state = getattr(app, "state", None)
    db = getattr(state, "database", None)
    if db is None:
        hook_manager = getattr(state, "hook_manager", None)
        db = getattr(hook_manager, "db", None)
    if db is None:
        return
    try:
        terminalize_receipts_for_envelope(db, envelope_id=envelope_id)
    except Exception as exc:
        logger.warning(
            "Failed to terminalize receipts for below-floor envelope %s: %s",
            envelope_id,
            exc,
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

    if raw.get("kind") == "delivery-receipt":
        receipt_id = raw.get("receipt_id")
        generation = raw.get("delivery_generation")
        if not isinstance(receipt_id, str) or not receipt_id:
            _quarantine_or_warn(
                path,
                reason="invalid_envelope",
                detail="Delivery receipt must include receipt_id",
            )
            return None
        if not isinstance(generation, int) or generation < 1:
            _quarantine_or_warn(
                path,
                reason="invalid_envelope",
                detail="Delivery receipt must include a positive delivery_generation",
            )
            return None
        return raw

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
        envelope = _load_envelope(path)
        if envelope is None:
            continue

        if envelope.get("kind") == "delivery-receipt":
            _consume_inbox_delivery_receipt(
                app,
                envelope,
                path,
                envelope_id,
                processed_dir=processed_dir,
            )
            replayed += 1
            continue

        if not envelope_has_hook_response_capability(envelope.get("response_capability")):
            if envelope_id and is_envelope_processed(envelope_id, processed_dir=processed_dir):
                logger.debug("Skipping already-processed hook inbox envelope %s", path.name)
                path.unlink(missing_ok=True)
                continue
            if envelope_id:
                release_envelope_processing_claim(envelope_id, processed_dir=processed_dir)
                _terminalize_below_floor_receipts(app, envelope_id)
            _quarantine_or_warn(
                path,
                reason="below_floor_response_capability",
                detail="request-carried response_capability is below hook-response.v1",
            )
            replayed += 1
            continue

        hook_manager = getattr(getattr(app, "state", None), "hook_manager", None)
        if (
            envelope_id
            and hook_manager is not None
            and grok_pending_context.handle_ack_pending_inbox_envelope(
                hook_manager,
                envelope_id,
                envelope,
                path,
                remove_marker=lambda current_id: remove_envelope_marker(
                    current_id,
                    processed_dir=processed_dir,
                ),
            )
        ):
            continue
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
            if hook_manager is not None and grok_pending_context.handle_ack_pending_inbox_envelope(
                hook_manager,
                envelope_id,
                envelope,
                path,
                remove_marker=lambda current_id: remove_envelope_marker(
                    current_id,
                    processed_dir=processed_dir,
                ),
            ):
                replayed += 1
                continue
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


def _parse_quarantined_at(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _sidecar_quarantined_at(meta_path: Path) -> float | None:
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _parse_quarantined_at(payload.get("quarantined_at"))


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _unlink_quiet(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def _payload_name_for_quarantine_entry(name: str) -> str | None:
    if name.endswith(_META_SUFFIX):
        payload = name[: -len(_META_SUFFIX)]
        return payload if payload.endswith(".json") else None
    if name.endswith(".json"):
        return name
    return None


def _prune_quarantine_pair(
    quarantine_dir: Path,
    payload_name: str,
    *,
    cutoff: float,
) -> int:
    """Delete a payload/sidecar pair (or orphan) when strictly older than cutoff."""
    payload_path = quarantine_dir / payload_name
    meta_path = quarantine_dir / f"{payload_name}{_META_SUFFIX}"
    timestamp = _sidecar_quarantined_at(meta_path)
    if timestamp is None:
        timestamp = _file_mtime(payload_path) or _file_mtime(meta_path)
    if timestamp is None or timestamp >= cutoff:
        return 0
    deleted = 0
    if _unlink_quiet(payload_path):
        deleted += 1
    if _unlink_quiet(meta_path):
        deleted += 1
    return deleted


def prune_hook_quarantine(
    inbox_dir: Path | None = None,
    *,
    now: float | None = None,
    retention_seconds: float | None = None,
    max_entries: int = HOOK_QUARANTINE_PRUNE_MAX_ENTRIES,
) -> DirectoryPruneResult:
    """Drop quarantined envelopes past the retention window, in bounded batches.

    Eligibility is strictly after the cutoff against the sidecar timestamp.
    An entry exactly at the boundary is retained. Payload and sidecar are
    removed together; orphaned halves of a pair are recovered.
    """
    root = inbox_dir if inbox_dir is not None else get_hook_inbox_dir()
    quarantine_dir = get_hook_quarantine_dir(root)
    window = HOOK_QUARANTINE_RETENTION_WINDOW if retention_seconds is None else retention_seconds
    cutoff = (now if now is not None else time.time()) - window
    examined = 0
    deleted = 0
    truncated = False
    seen: set[str] = set()
    try:
        with os.scandir(quarantine_dir) as entries:
            for entry in entries:
                if examined >= max_entries:
                    truncated = True
                    break
                examined += 1
                payload_name = _payload_name_for_quarantine_entry(entry.name)
                if payload_name is None or payload_name in seen:
                    continue
                seen.add(payload_name)
                deleted += _prune_quarantine_pair(quarantine_dir, payload_name, cutoff=cutoff)
    except OSError:
        return DirectoryPruneResult(examined=examined, deleted=deleted)
    return DirectoryPruneResult(examined=examined, deleted=deleted, truncated=truncated)


async def hook_quarantine_retention_loop(
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 3600,
    inbox_dir: Path | None = None,
) -> None:
    """Periodic prune of the hook inbox quarantine directory."""
    try:
        await asyncio.to_thread(prune_hook_quarantine, inbox_dir)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Initial hook quarantine prune failed: %s", exc)

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            result = await asyncio.to_thread(prune_hook_quarantine, inbox_dir)
            if result.deleted:
                logger.info(
                    "Pruned %s expired hook quarantine file(s)",
                    result.deleted,
                    extra={
                        "event": "hook_quarantine_pruned",
                        "examined": result.examined,
                        "deleted": result.deleted,
                        "backlog_remaining": result.truncated,
                    },
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Hook quarantine prune loop failed: %s", exc)


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

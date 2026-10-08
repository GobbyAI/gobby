"""Daemon-side replay for hook inbox envelopes."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

from gobby.cli.utils import get_gobby_home
from gobby.hooks import grok_pending_context
from gobby.hooks.envelope_dedupe import (
    ENVELOPE_ID_HEADER,
    clear_stale_envelope_processing_marker,
    envelope_id_from_inbox_path,
    envelope_timestamp_ms_from_inbox_path,
    get_processed_envelope_dir,
    is_envelope_processed,
    mark_envelope_processed,
    release_envelope_processing_claim,
    remove_envelope_marker,
)
from gobby.hooks.inbox_lifecycle import replay_stopping, start_replay
from gobby.hooks.receipt_effects import apply_acknowledged_receipt
from gobby.hooks.replay_fence import archive_superseded_hook, defer_live_hook
from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    envelope_has_hook_response_capability,
)
from gobby.utils import daemon_url as daemon_address
from gobby.utils.datetime import utc_now
from gobby.utils.local_token import daemon_bootstrap_path, read_local_api_token

logger = logging.getLogger(__name__)
_DRAIN_LOCK_STATE_KEY = "_gobby_hook_inbox_drain_lock"
_SETTLE_LISTENERS_STATE_KEY = "_gobby_hook_inbox_settle_listeners"

# A consumer claims a delivery-receipt ack by renaming it to
# ``<ack>.<owner>.claimed.tmp``. Only the daemon consumes acks, and the owner
# token is fresh per process, so a claim under another token belongs to a dead
# daemon and is handed back to the inbox instead of being lost.
_RECEIPT_CLAIM_SUFFIX: Final = ".claimed.tmp"
_RECEIPT_CLAIM_OWNER = uuid.uuid4().hex

# The per-hook receipt sweep skips files it already decoded as some other kind
# instead of decoding them again on every hook (#23359). An entry holds the file's
# stat signature: ghook settles a delivered envelope by atomically replacing it
# with a delivery receipt at the same path, and the changed signature makes the
# sweep read it again. Each sweep keeps only paths still listed.
_NON_RECEIPT_FILES: dict[Path, tuple[int, int, int]] = {}
_NON_RECEIPT_FILES_LOCK = threading.Lock()


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


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
    target = quarantine_dir / path.name
    meta_path = quarantine_dir / f"{path.name}.meta.json"

    try:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
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
        path.unlink(missing_ok=True)
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
) -> bool:
    """Claim the ack, CAS the receipt without re-executing the hook, then drop it.

    Per-hook sweeps and the drain list and read the same ack file concurrently.
    The atomic rename lets exactly one of them acknowledge it; the others return
    False. The claimed name ends in ``.tmp``, so no sweep lists it, and it carries
    this process's owner token, so the next daemon restores a claim that a crash
    stranded before the CAS (``_restore_orphaned_receipt_claims``).
    """

    from gobby.storage.hook_receipts import acknowledge_receipt

    claimed = path.with_name(f"{path.name}.{_RECEIPT_CLAIM_OWNER}{_RECEIPT_CLAIM_SUFFIX}")
    try:
        path.rename(claimed)
    except FileNotFoundError:
        return False

    receipt_id = envelope.get("receipt_id")
    generation = envelope.get("delivery_generation")
    state = getattr(app, "state", None)
    hook_manager = getattr(state, "hook_manager", None)
    db = getattr(state, "database", None)
    if db is None:
        # The daemon app exposes the hub database as state.server.services.database.
        server = getattr(state, "server", None)
        db = getattr(getattr(server, "services", None), "database", None)
    if db is None:
        logger.warning(
            "No hub database resolvable from the app; dropping delivery receipt %s "
            "generation %s unacknowledged",
            receipt_id,
            generation,
        )
    if db is not None and isinstance(receipt_id, str) and isinstance(generation, int):
        try:
            committed = acknowledge_receipt(
                db,
                receipt_id=receipt_id,
                delivery_generation=generation,
            )
            if committed is None:
                logger.debug(
                    "Delivery receipt %s generation %s was stale or unknown at consumption",
                    receipt_id,
                    generation,
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
    claimed.unlink(missing_ok=True)
    return True


def _restore_orphaned_receipt_claims(inbox_dir: Path) -> int:
    """Hand acks that an earlier daemon claimed but never acknowledged back to the inbox.

    Blocking. A restored ack is consumed by the next sweep; if its receipt moved
    on meanwhile, the CAS records the usual stale no-op.
    """
    restored = 0
    for claimed in inbox_dir.glob(f"*{_RECEIPT_CLAIM_SUFFIX}"):
        ack_name, _, owner = claimed.name.removesuffix(_RECEIPT_CLAIM_SUFFIX).rpartition(".")
        if not ack_name or owner == _RECEIPT_CLAIM_OWNER:
            continue
        try:
            claimed.rename(claimed.with_name(ack_name))
        except FileNotFoundError:
            continue  # A concurrent sweep restored it first.
        except OSError as exc:
            logger.warning("Could not restore delivery-receipt claim %s: %s", claimed.name, exc)
            continue
        restored += 1
    if restored:
        logger.info("Restored %d delivery-receipt ack(s) an earlier daemon left claimed", restored)
    return restored


def consume_pending_delivery_receipts(app: Any, inbox_dir: Path | None = None) -> int:
    """Consume well-formed delivery-receipt acks waiting in the inbox.

    A receipted hook response re-prepares the session's newest undelivered
    receipt onto its own envelope, bumping the delivery generation. ghook
    writes acks back into the inbox, but the periodic drain (60s) is too slow
    for a busy session: by the time it runs, the ack's generation is stale and
    the CAS records a no-op, so the receipt re-prepares forever. Sweeping acks
    before the re-prepare lets an in-flight ack land while its generation is
    still current. Async callers must await this blocking sweep in a worker.
    Only files whose parsed body is a well-formed delivery receipt are touched;
    every other file is left for the drain and its quarantine rules.
    """
    pending_dir = inbox_dir or get_hook_inbox_dir()
    if not pending_dir.exists():
        return 0
    _restore_orphaned_receipt_claims(pending_dir)
    consumed = 0
    processed_dir = get_processed_envelope_dir(pending_dir)
    paths = _iter_inbox_files(pending_dir)
    with _NON_RECEIPT_FILES_LOCK:
        for gone in _NON_RECEIPT_FILES.keys() - set(paths):
            del _NON_RECEIPT_FILES[gone]
        known_non_receipts = dict(_NON_RECEIPT_FILES)
    for path in paths:
        # Taken before the read, so a replacement after it changes the signature.
        signature = _file_signature(path)
        if signature is None or known_non_receipts.get(path) == signature:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        if raw.get("kind") != "delivery-receipt":
            with _NON_RECEIPT_FILES_LOCK:
                _NON_RECEIPT_FILES[path] = signature
            continue
        receipt_id = raw.get("receipt_id")
        generation = raw.get("delivery_generation")
        if not isinstance(receipt_id, str) or not receipt_id:
            continue
        if not isinstance(generation, int) or generation < 1:
            continue
        if _consume_inbox_delivery_receipt(
            app,
            raw,
            path,
            envelope_id_from_inbox_path(path),
            processed_dir=processed_dir,
        ):
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
    if not operator_token:
        return httpx.Response(503)
    request_headers["Authorization"] = f"Bearer {operator_token}"
    if envelope_id:
        request_headers[ENVELOPE_ID_HEADER] = envelope_id

    # Shared operator keys are verified by gdaemon, not the Python backend.
    # Use this daemon's bound bootstrap, never an inherited client URL override.
    base_url = await asyncio.to_thread(
        daemon_address.resolve_daemon_url, daemon_bootstrap_path(), env={}
    )
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=30.0,
    ) as client:
        response = await client.post(
            "/api/hooks/execute",
            json=envelope,
            headers=request_headers,
        )
        if response.status_code == 401:
            # Rotation can race the first post. Retry once only for a changed
            # key; a disappearing key is a retryable startup state.
            refreshed_token = read_local_api_token()
            if not refreshed_token:
                return httpx.Response(503)
            if refreshed_token != operator_token:
                response = await client.post(
                    "/api/hooks/execute",
                    json=envelope,
                    headers={**request_headers, "Authorization": f"Bearer {refreshed_token}"},
                )
        return response


@dataclass(frozen=True)
class HookInboxBarrierResult:
    """Bounded startup replay outcome."""

    replayed: int
    timed_out: bool
    unresolved_run_ids: tuple[str, ...]
    unresolved_session_ids: tuple[str, ...]
    residue_hook_count: int = 0
    live_hook_count: int = 0
    receipt_count: int = 0


def _get_hook_inbox_drain_lock(app: Any) -> asyncio.Lock:
    """Return the app-scoped lock coordinating hook inbox consumers."""
    lock: asyncio.Lock | None = getattr(app.state, _DRAIN_LOCK_STATE_KEY, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(app.state, _DRAIN_LOCK_STATE_KEY, lock)
    return lock


def _get_hook_settle_listeners(app: Any) -> set[Callable[[], None]]:
    """Return the app-scoped callbacks every replay pass tells about a settled hook."""
    listeners: set[Callable[[], None]] | None = getattr(
        app.state, _SETTLE_LISTENERS_STATE_KEY, None
    )
    if listeners is None:
        listeners = set()
        setattr(app.state, _SETTLE_LISTENERS_STATE_KEY, listeners)
    return listeners


async def _drain_hook_inbox_once_locked(
    app: Any,
    inbox_dir: Path | None = None,
    *,
    include_fresh: bool = False,
    restart_horizon_ms: int | None = None,
    on_hook_settled: Callable[[], None] | None = None,
) -> int:
    """Replay pending envelopes while the app-scoped drain lock is held.

    ``on_hook_settled`` observes each hook envelope the pass settles, and so
    does every app-scoped settle listener. Delivery receipts are bookkeeping,
    not replay progress, and never reach either.
    """
    pending_dir = inbox_dir or get_hook_inbox_dir()
    if not pending_dir.exists():
        return 0

    await asyncio.to_thread(_restore_orphaned_receipt_claims, pending_dir)
    pending_files = await asyncio.to_thread(_iter_inbox_files, pending_dir)
    if not pending_files:
        return 0
    if read_local_api_token() is None:
        logger.warning(
            "Daemon API key missing; configure api_key in bootstrap.yaml "
            "with a live key for this machine (run 'gobby auth login').",
            extra={
                "inbox_path": str(pending_dir),
                "pending_envelopes": len(pending_files),
            },
        )
        return 0

    replayed = 0

    def hook_settled() -> None:
        nonlocal replayed
        replayed += 1
        if on_hook_settled is not None:
            on_hook_settled()
        for listener in tuple(_get_hook_settle_listeners(app)):
            listener()

    processed_dir = get_processed_envelope_dir(pending_dir)
    for path in pending_files:
        if replay_stopping(app):
            break
        envelope_id = envelope_id_from_inbox_path(path)
        # Reading, parsing and any quarantine move are disk work; keep them off
        # the event loop.
        envelope = await asyncio.to_thread(_load_envelope, path)
        if envelope is None:
            continue

        if envelope.get("kind") == "delivery-receipt":
            # The acknowledgement writes to the hub; keep it off the event loop,
            # as the per-hook sweep does.
            if await asyncio.to_thread(
                _consume_inbox_delivery_receipt,
                app,
                envelope,
                path,
                envelope_id,
                processed_dir=processed_dir,
            ):
                replayed += 1
            continue

        if restart_horizon_ms is not None and not _is_restart_residue(
            path, envelope, restart_horizon_ms
        ):
            logger.debug("Skipping live hook inbox envelope %s", path.name)
            continue

        hook_manager = getattr(getattr(app, "state", None), "hook_manager", None)
        if (
            envelope_has_hook_response_capability(envelope.get("response_capability"))
            and envelope_id
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
        if envelope_has_hook_response_capability(
            envelope.get("response_capability")
        ) and defer_live_hook(path, envelope_id, processed_dir, include_fresh):
            continue
        archived = await asyncio.to_thread(
            archive_superseded_hook, app, envelope, path, _quarantine_file
        )
        if archived is not None:
            if archived:
                hook_settled()
            continue

        if not envelope_has_hook_response_capability(envelope.get("response_capability")):
            if envelope_id:
                release_envelope_processing_claim(envelope_id, processed_dir=processed_dir)
                _terminalize_below_floor_receipts(app, envelope_id)
            _quarantine_or_warn(
                path,
                reason="below_floor_response_capability",
                detail="request-carried response_capability is below hook-response.v1",
            )
            hook_settled()
            continue

        if envelope_id and clear_stale_envelope_processing_marker(
            envelope_id,
            processed_dir=processed_dir,
        ):
            logger.warning("Cleared stale processing marker for hook inbox envelope %s", path.name)

        try:
            response = await _post_envelope(envelope, envelope_id=envelope_id)
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
                hook_settled()
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
                hook_settled()
                continue
            path.unlink(missing_ok=True)
            hook_settled()
            continue

        if response.status_code == 409:
            logger.debug(
                "Hook inbox replay found active processing marker for %s; retaining file",
                path.name,
            )
            continue

        if response.status_code == 401:
            quarantined = await asyncio.to_thread(
                _quarantine_file,
                path,
                reason="replay_auth_rejected",
                detail="Hook replay returned HTTP 401",
            )
            if quarantined:
                if envelope_id:
                    await asyncio.to_thread(
                        release_envelope_processing_claim,
                        envelope_id,
                        processed_dir=processed_dir,
                    )
                logger.warning(
                    "Hook inbox replay returned 401 for %s; quarantined",
                    path.name,
                )
                hook_settled()
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


async def _replay_inbox_holding_lock(
    app: Any,
    lock: asyncio.Lock,
    pending_dir: Path,
    restart_horizon_ms: int | None = None,
    on_hook_settled: Callable[[], None] | None = None,
) -> int:
    """Run one replay pass over an acquired drain lock and release it after."""
    try:
        return await _drain_hook_inbox_once_locked(
            app,
            pending_dir,
            include_fresh=True,
            restart_horizon_ms=restart_horizon_ms,
            on_hook_settled=on_hook_settled,
        )
    finally:
        lock.release()


def _is_restart_residue(
    path: Path,
    envelope: dict[str, Any] | None,
    restart_horizon_ms: int | None,
) -> bool:
    """Return whether a file is crash-window residue for restart classification."""
    if envelope is not None and envelope.get("kind") == "delivery-receipt":
        return False
    if restart_horizon_ms is None:
        return True
    timestamp_ms = envelope_timestamp_ms_from_inbox_path(path)
    if timestamp_ms is None:
        return True
    return timestamp_ms < restart_horizon_ms


def _classify_inbox_files(
    paths: list[Path],
    restart_horizon_ms: int | None,
) -> tuple[list[Path], int, int]:
    residue: list[Path] = []
    live_hooks = 0
    receipts = 0
    for path in paths:
        envelope = _load_envelope(path)
        if envelope is not None and envelope.get("kind") == "delivery-receipt":
            receipts += 1
            continue
        if _is_restart_residue(path, envelope, restart_horizon_ms):
            residue.append(path)
        else:
            live_hooks += 1
    return residue, live_hooks, receipts


def _barrier_result(
    replayed: int,
    *,
    timed_out: bool,
    pending_files: list[Path],
    restart_horizon_ms: int | None,
) -> HookInboxBarrierResult:
    residue, live_hooks, receipts = _classify_inbox_files(pending_files, restart_horizon_ms)
    run_ids, session_ids = _unresolved_envelope_identities(residue) if timed_out else (set(), set())
    return HookInboxBarrierResult(
        replayed,
        timed_out,
        tuple(sorted(run_ids)),
        tuple(sorted(session_ids)),
        residue_hook_count=len(residue),
        live_hook_count=live_hooks,
        receipt_count=receipts,
    )


async def drain_hook_inbox_barrier(
    app: Any,
    inbox_dir: Path | None = None,
    *,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
    restart_horizon_ms: int | None = None,
) -> HookInboxBarrierResult:
    """Replay crash-window envelopes before agent restart classification.

    ``timeout_seconds`` bounds the wait without replay progress. Downtime
    residue replays serially, so a healthy backlog can outlast any fixed
    budget: each hook envelope any replay pass settles restarts the budget,
    including a pass that holds the drain lock while this barrier waits for
    it. A stalled replay or a stalled lock holder still times out.
    """
    pending_dir = inbox_dir or get_hook_inbox_dir()
    budget = max(0.0, timeout_seconds)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    replayed = 0
    lock = _get_hook_inbox_drain_lock(app)
    settle_listeners = _get_hook_settle_listeners(app)

    def hook_settled() -> None:
        nonlocal replayed
        replayed += 1

    # The timeout bounds how long this caller waits, never a replay: a
    # cancelled replay would leave the envelope's processing lease live and
    # every later replay refused as a duplicate. Each replay runs in its own
    # task that owns the lock until it finishes, so a timed-out barrier
    # leaves it running and a later barrier waits on the lock.
    timeout = asyncio.timeout_at(deadline)

    def replay_progressed() -> None:
        # Registered only while the timeout is entered. Once it has expired
        # the barrier is already cancelled, so late progress cannot revive it.
        nonlocal deadline
        if not timeout.expired():
            deadline = loop.time() + budget
            timeout.reschedule(deadline)

    try:
        async with timeout:
            settle_listeners.add(replay_progressed)
            try:
                while True:
                    await lock.acquire()
                    replay = start_replay(
                        app,
                        _replay_inbox_holding_lock(
                            app,
                            lock,
                            pending_dir,
                            restart_horizon_ms=restart_horizon_ms,
                            on_hook_settled=hook_settled,
                        ),
                    )
                    # wait() leaves the replay running when this barrier is cancelled.
                    await asyncio.wait((replay,))
                    replay.result()
                    pending_files = await asyncio.to_thread(_iter_inbox_files, pending_dir)
                    residue, _live_hooks, _receipts = await asyncio.to_thread(
                        _classify_inbox_files, pending_files, restart_horizon_ms
                    )
                    if not residue:
                        return await asyncio.to_thread(
                            _barrier_result,
                            replayed,
                            timed_out=False,
                            pending_files=pending_files,
                            restart_horizon_ms=restart_horizon_ms,
                        )
                    if loop.time() >= deadline:
                        break
                    await asyncio.sleep(poll_interval_seconds)
            finally:
                settle_listeners.discard(replay_progressed)
    except TimeoutError:
        if not timeout.expired():
            raise

    # A replay still running keeps its envelope until it finishes; a separate
    # drain's lock owner remains untouched. Report pending identities so
    # startup can fence runs until a later barrier sees them settle.
    pending_files = await asyncio.to_thread(_iter_inbox_files, pending_dir)
    return await asyncio.to_thread(
        _barrier_result,
        replayed,
        timed_out=True,
        pending_files=pending_files,
        restart_horizon_ms=restart_horizon_ms,
    )


def _unresolved_envelope_identities(paths: list[Path]) -> tuple[set[str], set[str]]:
    run_ids: set[str] = set()
    session_ids: set[str] = set()
    for path in paths:
        envelope = _load_envelope(path)
        if envelope is None:
            continue
        input_data = envelope.get("input_data")
        if isinstance(input_data, dict):
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
            run_id = headers.get("X-Gobby-Agent-Run-Id") or headers.get("x-gobby-agent-run-id")
            if isinstance(run_id, str) and run_id:
                run_ids.add(run_id)
    return run_ids, session_ids

"""Daemon-side replay for hook inbox envelopes."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from random import SystemRandom
from typing import Any

import httpx

from gobby.cli.utils import get_gobby_home
from gobby.servers.routes.mcp.hooks import SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION

logger = logging.getLogger(__name__)
_JITTER_RANDOM = SystemRandom()


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
        logger.error(
            "Failed to quarantine hook inbox file %s (reason=%s, detail=%s): %s",
            path,
            reason,
            detail,
            exc,
            exc_info=True,
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


async def _post_envelope(app: Any, envelope: dict[str, Any]) -> httpx.Response:
    """Replay an inbox envelope through the real hook ingress route."""
    headers = envelope.get("headers")
    request_headers = (
        {str(key): str(value) for key, value in headers.items()}
        if isinstance(headers, dict)
        else {}
    )

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


async def drain_hook_inbox_once(app: Any, inbox_dir: Path | None = None) -> int:
    """Replay all pending hook envelopes once.

    Returns the number of envelopes successfully replayed and deleted.
    """
    pending_dir = inbox_dir or get_hook_inbox_dir()
    if not pending_dir.exists():
        return 0

    replayed = 0
    for path in _iter_inbox_files(pending_dir):
        envelope = _load_envelope(path)
        if envelope is None:
            continue

        try:
            response = await _post_envelope(app, envelope)
        except Exception as exc:
            logger.warning("Hook inbox replay failed for %s: %s", path.name, exc)
            continue

        if response.status_code == 200:
            path.unlink(missing_ok=True)
            replayed += 1
            continue

        logger.warning(
            "Hook inbox replay returned %s for %s",
            response.status_code,
            path.name,
        )

    return replayed


def _compute_sleep_seconds(interval_seconds: int, jitter_seconds: float) -> float:
    """Return a non-negative poll interval with bounded jitter."""
    return max(
        0.0,
        interval_seconds + _JITTER_RANDOM.uniform(-jitter_seconds, jitter_seconds),
    )


async def drain_hook_inbox_loop(
    app: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 60,
    jitter_seconds: float = 5.0,
) -> None:
    """Background loop that replays pending hook inbox envelopes."""
    try:
        replayed = await drain_hook_inbox_once(app)
        if replayed > 0:
            logger.info("Hook inbox replayed %s pending envelope(s)", replayed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Initial hook inbox drain failed: %s", exc)

    while not is_shutdown_requested():
        try:
            sleep_seconds = _compute_sleep_seconds(interval_seconds, jitter_seconds)
            await asyncio.sleep(sleep_seconds)
            replayed = await drain_hook_inbox_once(app)
            if replayed > 0:
                logger.info("Hook inbox replayed %s pending envelope(s)", replayed)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Hook inbox drain loop failed: %s", exc)

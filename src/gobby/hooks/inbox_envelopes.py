"""Hook inbox envelope decoding and quarantine file ownership."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from gobby.cli.utils import get_gobby_home
from gobby.hooks.runtime_compat import SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)
INBOX_CLAIM_SUFFIX = ".claimed.tmp"
INBOX_CLAIM_OWNER = uuid.uuid4().hex
type FileSignature = tuple[int, int, int]


def file_signature(path: Path) -> FileSignature | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


def original_inbox_name(path: Path) -> str:
    if path.name.endswith(INBOX_CLAIM_SUFFIX):
        return path.name.removesuffix(INBOX_CLAIM_SUFFIX).rpartition(".")[0]
    return path.name


def restore_inbox_claim(claimed: Path, path: Path) -> None:
    """Restore without overwriting a newer receipt at the public path."""
    try:
        path.hardlink_to(claimed)
    except (FileExistsError, FileNotFoundError):
        pass
    claimed.unlink(missing_ok=True)


@contextmanager
def claim_inbox_file(path: Path, expected: FileSignature) -> Iterator[Path | None]:
    """Claim only for removal, then verify we claimed the file that was read."""
    claimed = path.with_name(f"{path.name}.{INBOX_CLAIM_OWNER}-drain{INBOX_CLAIM_SUFFIX}")
    try:
        path.rename(claimed)
    except FileNotFoundError:
        yield None
        return
    try:
        yield claimed if file_signature(claimed) == expected else None
    finally:
        restore_inbox_claim(claimed, path)


def unlink_envelope(path: Path, expected: FileSignature) -> None:
    with claim_inbox_file(path, expected) as claimed:
        if claimed is not None:
            claimed.unlink(missing_ok=True)


def get_hook_inbox_dir() -> Path:
    """Return the daemon hook inbox directory."""
    return get_gobby_home() / "hooks" / "inbox"


def get_hook_quarantine_dir(inbox_dir: Path | None = None) -> Path:
    """Return the daemon hook inbox quarantine directory."""
    root = inbox_dir or get_hook_inbox_dir()
    return root / "quarantine"


def quarantine_file(
    path: Path, *, reason: str, detail: str, expected_signature: FileSignature | None = None
) -> bool:
    """Move an unreadable or invalid inbox file into quarantine with metadata."""
    if expected_signature is not None:
        with claim_inbox_file(path, expected_signature) as claimed:
            return claimed is not None and quarantine_file(claimed, reason=reason, detail=detail)
    quarantine_dir = get_hook_quarantine_dir(path.parent)
    name = original_inbox_name(path)
    target = quarantine_dir / name
    meta_path = quarantine_dir / f"{name}.meta.json"

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


def quarantine_or_warn(
    path: Path, *, reason: str, detail: str, expected_signature: FileSignature | None = None
) -> None:
    """Best-effort quarantine with a warning when quarantine itself fails."""
    if not quarantine_file(
        path, reason=reason, detail=detail, expected_signature=expected_signature
    ):
        logger.warning(
            "Skipping hook inbox file %s after quarantine failed (reason=%s)",
            path,
            reason,
        )


def load_envelope(path: Path) -> dict[str, Any] | None:
    """Load and minimally validate a replay envelope from disk."""
    signature = file_signature(path)
    if signature is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        quarantine_or_warn(
            path, reason="invalid_json", detail=str(exc), expected_signature=signature
        )
        return None

    if not isinstance(raw, dict):
        quarantine_or_warn(
            path,
            reason="invalid_envelope",
            detail="Envelope must be a JSON object",
            expected_signature=signature,
        )
        return None

    if raw.get("schema_version") != SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION:
        quarantine_or_warn(
            path,
            reason="invalid_envelope",
            expected_signature=signature,
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
            quarantine_or_warn(
                path,
                reason="invalid_envelope",
                expected_signature=signature,
                detail="Delivery receipt must include receipt_id",
            )
            return None
        if not isinstance(generation, int) or generation < 1:
            quarantine_or_warn(
                path,
                reason="invalid_envelope",
                expected_signature=signature,
                detail="Delivery receipt must include a positive delivery_generation",
            )
            return None
        return raw

    if not raw.get("hook_type") or not raw.get("source"):
        quarantine_or_warn(
            path,
            reason="invalid_envelope",
            expected_signature=signature,
            detail="Envelope must include hook_type and source",
        )
        return None

    return raw

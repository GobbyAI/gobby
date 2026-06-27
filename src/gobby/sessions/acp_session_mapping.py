"""ACP -> Gobby translation seam.

The single boundary that translates ACP protocol vocabulary into Gobby's
canonical Session paradigm. ACP camelCase keys (``sessionId``, ``cwd``,
``additionalDirectories``) and raw ``sessionCapabilities`` objects never leak
past this module: callers receive snake_case Gobby fields and a compact
``acp`` enrichment block only.

Pure functions only — no I/O, no DB access. Project resolution is injected by
the caller so this module stays trivially testable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Canonical Session vocabulary the seam emits.
SESSION_TYPE_WEB_CHAT = "web_chat"

# ACP lifecycle outcomes mapped onto Gobby's existing paradigm. ``close`` reuses
# the known ``expired`` status (no new "closed" status is introduced); ``delete``
# is a hard removal disposition.
ACP_CLOSE_STATUS = "expired"
ACP_DELETE_DISPOSITION = "removed"


@dataclass(frozen=True)
class MappedSessionInfo:
    """Canonical Session fields translated from an ACP ``SessionInfo``.

    ``project_id`` is ``None`` when the ACP ``cwd`` does not resolve to a known
    Gobby project; callers apply per-row resilience (skip + log) since the
    canonical Session model requires a project.
    """

    external_id: str
    source: str
    title: str | None
    project_id: str | None
    session_type: str = SESSION_TYPE_WEB_CHAT
    cwd: str | None = None
    updated_at: str | None = None
    additional_directories: tuple[str, ...] = field(default_factory=tuple)


def _clean_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def normalize_additional_directories(value: Any) -> tuple[str, ...]:
    """Normalize an ACP ``additionalDirectories`` payload to a string tuple."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def map_session_info(
    info: Mapping[str, Any],
    *,
    provider: str,
    resolve_project_id: Callable[[str | None], str | None],
) -> MappedSessionInfo | None:
    """Translate an ACP ``SessionInfo`` into canonical Session fields.

    Returns ``None`` when the payload carries no usable ``sessionId``. The
    ``provider`` (e.g. ``"grok"``/``"qwen"``) becomes the canonical ``source``;
    ACP has no literal ``"acp"`` source.
    """
    if not isinstance(info, Mapping):
        return None
    external_id = _clean_str(info.get("sessionId"))
    if external_id is None:
        return None
    cwd = _clean_str(info.get("cwd"))
    return MappedSessionInfo(
        external_id=external_id,
        source=provider,
        title=_clean_str(info.get("title")),
        project_id=resolve_project_id(cwd),
        cwd=cwd,
        updated_at=_clean_str(info.get("updatedAt")),
        additional_directories=normalize_additional_directories(info.get("additionalDirectories")),
    )


def build_acp_block(
    capabilities: Mapping[str, bool],
    *,
    additional_directories: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the normalized ``session.acp`` enrichment block.

    Exposes only the lifecycle capabilities the UI gates on (``resume``,
    ``close``, ``delete``) plus snake_case ``additional_directories``. The
    internal ``list`` capability stays backend-only and raw ACP capability
    objects never cross this seam.
    """
    return {
        "capabilities": {
            "resume": bool(capabilities.get("resume", False)),
            "close": bool(capabilities.get("close", False)),
            "delete": bool(capabilities.get("delete", False)),
        },
        "additional_directories": [
            item for item in additional_directories if isinstance(item, str) and item
        ],
    }


def status_for_close() -> str:
    """Gobby status an ACP ``session/close`` transitions a row to."""
    return ACP_CLOSE_STATUS


def disposition_for_delete() -> str:
    """Disposition for an ACP ``session/delete`` (hard removal)."""
    return ACP_DELETE_DISPOSITION

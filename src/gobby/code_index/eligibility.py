"""Machine+project eligibility for daemon-owned gcode maintenance."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gobby.code_index.models import CODE_INDEX_UUID_NAMESPACE

EligibilityKind = Literal["active", "overlay", "unregistered", "missing_root", "identity_mismatch"]


@dataclass(frozen=True)
class Eligibility:
    kind: EligibilityKind
    project_id: str
    root: Path | None


def code_index_id_for_root(root: Path) -> str:
    """Derive the code-index project id gcode assigns to ``root``.

    Mirrors ``gobby_core::project::code_index_id_for_root``: UUID5 in the
    code-index namespace over the canonicalized root path, falling back to the
    absolute path when canonicalization fails.
    """
    try:
        canonical = root.resolve(strict=True)
    except OSError:
        canonical = Path(os.path.abspath(root))
    return str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, str(canonical)))


def overlay_project_id_for_root(root: Path) -> str | None:
    """Return the derived overlay id when ``root`` is an isolation workspace.

    Mirrors gcode's identity resolution: a root whose ``.gobby/project.json``
    carries a non-self-referential isolation marker (``parent_project_path``
    and ``parent_project_id`` both set) indexes under its own derived
    code-index project id, not the parent's. Returns ``None`` for ordinary
    project roots and for malformed markers (gcode fails loudly on those).
    """
    payload = _project_json(root)
    if payload is None:
        return None
    parent_path = payload.get("parent_project_path")
    parent_id = payload.get("parent_project_id")
    if not (isinstance(parent_path, str) and parent_path):
        return None
    if not (isinstance(parent_id, str) and parent_id):
        return None
    resolved_parent = root / parent_path if not os.path.isabs(parent_path) else Path(parent_path)
    try:
        if resolved_parent.resolve() == root.resolve():
            return None
    except OSError:
        return None
    return code_index_id_for_root(root)


def resolve_indexed_project(
    project_id: str,
    root_path: str | None,
    *,
    project_exists: bool,
    project_deleted: bool,
) -> Eligibility:
    """Decide whether this machine's selector may run gcode or must reconcile."""
    if not project_exists or project_deleted:
        if not project_deleted and root_path:
            root = Path(root_path).expanduser()
            if root.is_dir() and code_index_id_for_root(root) == project_id:
                # A path-derived code-index identity (worktree/clone overlay or
                # standalone gcode root): live as long as its directory is.
                # Its writes are owned by overlay-claim launches, never by the
                # registry-keyed maintenance pass, so it is not "active".
                return Eligibility("overlay", project_id, root)
        return Eligibility("unregistered", project_id, None)
    if not root_path:
        return Eligibility("missing_root", project_id, None)
    root = Path(root_path).expanduser()
    if not root.is_dir():
        return Eligibility("missing_root", project_id, root)
    marker_id = _project_json_id(root)
    if marker_id != project_id:
        return Eligibility("identity_mismatch", project_id, root)
    return Eligibility("active", project_id, root)


def _project_json(root: Path) -> dict[str, object] | None:
    marker = root / ".gobby" / "project.json"
    try:
        payload = json.loads(marker.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _project_json_id(root: Path) -> str | None:
    payload = _project_json(root)
    if payload is None:
        return None
    value = payload.get("id")
    return value if isinstance(value, str) and value else None

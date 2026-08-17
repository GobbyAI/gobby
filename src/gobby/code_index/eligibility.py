"""Machine+project eligibility for daemon-owned gcode maintenance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

EligibilityKind = Literal["active", "unregistered", "missing_root", "identity_mismatch"]


@dataclass(frozen=True)
class Eligibility:
    kind: EligibilityKind
    project_id: str
    root: Path | None


def resolve_indexed_project(
    project_id: str,
    root_path: str | None,
    *,
    project_exists: bool,
    project_deleted: bool,
) -> Eligibility:
    """Decide whether this machine's selector may run gcode or must reconcile."""
    if not project_exists or project_deleted:
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


def _project_json_id(root: Path) -> str | None:
    marker = root / ".gobby" / "project.json"
    try:
        payload = json.loads(marker.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("id")
    return value if isinstance(value, str) and value else None

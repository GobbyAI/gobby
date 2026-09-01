"""Local filesystem validation for ordinary project checkout roots."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.utils.project_context import get_project_context

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class InvalidCheckoutRootError(ValueError):
    """Raised when a candidate root is not a platform-local normalized absolute path."""


class MarkerMismatchError(ValueError):
    """Raised when the on-disk project marker does not match the expected project id."""


def validate_checkout_root(
    db: HubDatabase,
    *,
    project_id: str,
    machine_id: str,
    candidate_path: str,
    expected_marker_id: str,
) -> str:
    """Return `candidate_path` when it is a valid ordinary checkout root.

    Never expands ``~`` or relative paths. Callers must pass a platform-local
    normalized absolute path. Overlay and marker checks run after path shape
    validation. Foreign-machine rejection belongs in the caller via
    ``require_local_machine_id``.
    """
    if (
        not candidate_path
        or candidate_path.startswith("~")
        or not os.path.isabs(candidate_path)
        or os.path.normpath(candidate_path) != candidate_path
        or not os.path.isdir(candidate_path)
    ):
        raise InvalidCheckoutRootError(
            f"checkout root {candidate_path!r} is not a platform-local normalized absolute path"
        )

    from gobby.storage.project_checkouts import OverlayRegistrationRejectedError
    from gobby.storage.projects import IsolatedAgentProjectPathError, LocalProjectManager

    manager = LocalProjectManager(db)
    if manager._is_isolated_agent_session() or manager._is_under_isolation_root(candidate_path):
        raise IsolatedAgentProjectPathError(
            "project repo_path cannot be changed from an isolated agent session "
            "or to an isolation path (registered or under the worktrees/clones roots)"
        )
    if manager._is_registered_isolation_path(candidate_path, machine_id=machine_id):
        raise OverlayRegistrationRejectedError(
            f"root {candidate_path} is a registered overlay on machine {machine_id}"
        )

    marker = get_project_context(Path(candidate_path))
    marker_id = None if marker is None else marker.get("id")
    if marker_id is None or str(marker_id) != expected_marker_id:
        raise MarkerMismatchError(
            f"marker at {candidate_path} does not match project {expected_marker_id}"
        )
    return candidate_path

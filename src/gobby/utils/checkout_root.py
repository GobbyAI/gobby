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


_ISOLATION_PATH_MESSAGE = (
    "project repo_path cannot be changed from an isolated agent session "
    "or to an isolation path (registered or under the worktrees/clones roots)"
)


def canonical_checkout_root(path: str) -> str:
    """Return the canonical (normalized, symlink-resolved) form of a checkout root.

    gcode canonicalizes its working root with ``std::fs::canonicalize`` before
    comparing it with ``project_checkouts.root_path``; every Python writer must
    store the same form or a symlinked checkout can never pass that fence.
    """
    return os.path.realpath(os.path.normpath(path))


def validate_checkout_root(
    db: HubDatabase,
    *,
    project_id: str,
    machine_id: str,
    candidate_path: str,
    expected_marker_id: str,
) -> str:
    """Hook-ingress validation: `validate_campaign_checkout_root` plus the session refusal.

    An isolated agent session (a worktree or clone child) may never rebind a
    project's primary checkout, whatever path it names.
    """
    _require_normalized_absolute_directory(candidate_path)

    from gobby.storage.projects import IsolatedAgentProjectPathError, LocalProjectManager

    if LocalProjectManager(db)._is_isolated_agent_session():
        raise IsolatedAgentProjectPathError(_ISOLATION_PATH_MESSAGE)
    return validate_campaign_checkout_root(
        db,
        project_id=project_id,
        machine_id=machine_id,
        candidate_path=candidate_path,
        expected_marker_id=expected_marker_id,
    )


def validate_campaign_checkout_root(
    db: HubDatabase,
    *,
    project_id: str,
    machine_id: str,
    candidate_path: str,
    expected_marker_id: str,
) -> str:
    """Return the canonical root when `candidate_path` is a valid ordinary checkout root.

    Judges only the path: the cutover campaign classifies every primary
    checkout from an operator terminal that may carry an ambient
    ``GOBBY_SESSION_ID``, so the caller's session never participates here.
    Never expands ``~`` or relative paths. Callers must pass a platform-local
    normalized absolute path; the returned root is its ``realpath``. Overlay
    and marker checks run after path shape validation. Foreign-machine
    rejection belongs in the caller via ``require_local_machine_id``.
    """
    del project_id  # Identity is proven by the marker, not the caller's claim.
    _require_normalized_absolute_directory(candidate_path)
    canonical_path = canonical_checkout_root(candidate_path)

    from gobby.storage.project_checkouts import OverlayRegistrationRejectedError
    from gobby.storage.projects import IsolatedAgentProjectPathError, LocalProjectManager

    manager = LocalProjectManager(db)
    if manager._is_under_isolation_root(canonical_path):
        raise IsolatedAgentProjectPathError(_ISOLATION_PATH_MESSAGE)
    if manager._is_registered_isolation_path(candidate_path, machine_id=machine_id):
        raise OverlayRegistrationRejectedError(
            f"root {candidate_path} is a registered overlay on machine {machine_id}"
        )

    marker = get_project_context(Path(canonical_path))
    marker_id = None if marker is None else marker.get("id")
    if marker_id is None or str(marker_id) != expected_marker_id:
        raise MarkerMismatchError(
            f"marker at {canonical_path} does not match project {expected_marker_id}"
        )
    return canonical_path


def _require_normalized_absolute_directory(candidate_path: str) -> None:
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

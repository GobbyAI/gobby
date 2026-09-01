"""
Shared project initialization utilities.

This module provides the core logic for initializing a Gobby project,
used by both the CLI and the hook system for auto-initialization.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg.errors import UniqueViolation

from gobby.agents.isolation_git_hygiene import is_generated_isolation_project_json
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import datetime_to_required_iso, utc_now

logger = logging.getLogger(__name__)

WORKTREE_LOCAL_PROJECT_KEYS = frozenset({"parent_project_id", "parent_project_path"})
NONPORTABLE_PROJECT_KEYS = WORKTREE_LOCAL_PROJECT_KEYS | frozenset(
    {"linear_team_id", "linear_project_id"}
)


def _atomic_write_project_json(project_file: Path, project_data: dict[str, Any]) -> None:
    """Serialize project data before atomically replacing project.json."""
    existing_mode = stat.S_IMODE(project_file.stat().st_mode) if project_file.exists() else None
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{project_file.name}.",
        suffix=".tmp",
        dir=str(project_file.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            if existing_mode is not None:
                os.fchmod(tmp.fileno(), existing_mode)
            json.dump(project_data, tmp, indent=2)
            tmp.write("\n")
        os.replace(tmp_name, project_file)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError as exc:
            logger.debug("Failed to clean up temp project.json %s: %s", tmp_name, exc)
        raise


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


_INIT_FAILPOINTS: dict[str, Callable[[], None]] = {}


def _hit_failpoint(name: str) -> None:
    hook = _INIT_FAILPOINTS.get(name)
    if hook is not None:
        hook()


def _marker_path(root: Path) -> Path:
    return root / ".gobby" / "project.json"


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_marker_file(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _reject_invalid_root(candidate_path: str) -> None:
    from gobby.utils.checkout_root import InvalidCheckoutRootError

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


def _publish_marker_exclusive(root: Path, payload: dict[str, Any]) -> bool:
    """Install a complete marker with create-if-absent. Return True if this writer won."""
    project_file = _marker_path(root)
    project_file.parent.mkdir(parents=True, exist_ok=True)
    _hit_failpoint("publish_before_temp_write")
    fd, tmp_name = tempfile.mkstemp(
        prefix=".project.json.",
        suffix=".tmp",
        dir=str(project_file.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(payload, tmp, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        _hit_failpoint("publish_after_file_fsync")
        try:
            os.link(tmp_name, project_file)
        except FileExistsError:
            return False
        _hit_failpoint("publish_after_install")
        _fsync_directory(project_file.parent)
        _hit_failpoint("publish_after_directory_fsync")
        return True
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _unlink_still_matching_marker(root: Path, expected_id: str, failpoint_prefix: str) -> None:
    from gobby.utils.durable_file import exclusive_file_lock

    project_file = _marker_path(root)
    _hit_failpoint(f"{failpoint_prefix}_before_unlink")
    with exclusive_file_lock(project_file):
        data = _read_marker_file(project_file)
        if data is None or str(data.get("id")) != expected_id:
            return
        try:
            project_file.unlink()
        except FileNotFoundError:
            return
        _hit_failpoint(f"{failpoint_prefix}_after_unlink")
        _fsync_directory(project_file.parent)
        _hit_failpoint(f"{failpoint_prefix}_after_dir_fsync")


def refresh_marker_expected_id(cwd: Path, expected_project_id: str, name: str) -> None:
    """Update only ``name`` on a still-matching marker. Refuse a replacement id."""
    from gobby.utils.checkout_root import MarkerMismatchError
    from gobby.utils.durable_file import exclusive_file_lock

    project_file = _marker_path(cwd)
    existing = _read_marker_file(project_file)
    if existing is None or str(existing.get("id")) != expected_project_id:
        raise MarkerMismatchError(f"marker at {cwd} does not match project {expected_project_id}")
    payload = dict(existing)
    payload["name"] = name
    project_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".project.json.",
        suffix=".tmp",
        dir=str(project_file.parent),
        text=True,
    )
    installed = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(payload, tmp, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        _hit_failpoint("refresh_after_temp_fsync")
        with exclusive_file_lock(project_file):
            current = _read_marker_file(project_file)
            if current is None or str(current.get("id")) != expected_project_id:
                raise MarkerMismatchError(
                    f"marker at {cwd} does not match project {expected_project_id}"
                )
            os.replace(tmp_name, project_file)
            installed = True
            _hit_failpoint("refresh_after_install")
            _fsync_directory(project_file.parent)
            _hit_failpoint("refresh_after_dir_fsync")
    finally:
        if not installed:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


@dataclass
class VerificationCommands:
    """Auto-detected verification commands for a project."""

    unit_tests: str | None = None
    type_check: str | None = None
    lint: str | None = None
    format: str | None = None
    build: str | None = None
    doc_tests: str | None = None
    integration: str | None = None
    custom: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        result: dict[str, Any] = {}
        if self.unit_tests:
            result["unit_tests"] = self.unit_tests
        if self.type_check:
            result["type_check"] = self.type_check
        if self.lint:
            result["lint"] = self.lint
        if self.format:
            result["format"] = self.format
        if self.build:
            result["build"] = self.build
        if self.doc_tests:
            result["doc_tests"] = self.doc_tests
        if self.integration:
            result["integration"] = self.integration
        if self.custom:
            result["custom"] = self.custom
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerificationCommands:
        """Create commands from project verification data."""
        custom = data.get("custom", {})
        return cls(
            unit_tests=_optional_str(data.get("unit_tests")),
            type_check=_optional_str(data.get("type_check")),
            lint=_optional_str(data.get("lint")),
            format=_optional_str(data.get("format")),
            build=_optional_str(data.get("build")),
            doc_tests=_optional_str(data.get("doc_tests")),
            integration=_optional_str(data.get("integration")),
            custom={str(k): str(v) for k, v in custom.items()} if isinstance(custom, dict) else {},
        )


@dataclass
class InitResult:
    """Result of project initialization."""

    project_id: str
    project_name: str
    project_path: str
    created_at: str
    already_existed: bool
    verification: VerificationCommands | None = None


_FRONTEND_SUBDIRS = ["web", "frontend", "client", "app", "ui", "packages/web", "packages/frontend"]


def _find_frontend_dirs(cwd: Path) -> list[tuple[Path, str]]:
    """Find directories containing package.json (root + common frontend subdirs).

    Returns list of (absolute_path, relative_name) tuples. "." for root.
    """
    results: list[tuple[Path, str]] = []

    # Check root first
    if (cwd / "package.json").exists():
        results.append((cwd, "."))

    # Check common frontend subdirectories
    for subdir in _FRONTEND_SUBDIRS:
        candidate = cwd / subdir
        if (candidate / "package.json").exists():
            results.append((candidate, subdir))

    return results


def detect_verification_commands(cwd: Path) -> VerificationCommands:
    """
    Auto-detect verification commands based on project files.

    Checks for pyproject.toml (Python) or package.json (Node.js) and suggests
    appropriate commands for testing, type checking, and linting.

    Args:
        cwd: Project root directory.

    Returns:
        VerificationCommands with detected commands.
    """
    from gobby.project_verification.refresh import refresh_project_verification_deterministic

    result = refresh_project_verification_deterministic(cwd)
    if not result.after:
        logger.debug("No recognized project type detected")
    return VerificationCommands.from_dict(result.after)


def _finish_init_result(
    root: Path,
    project_id: str,
    project_name: str,
    created_at: str,
    *,
    already_existed: bool,
) -> InitResult:
    update_project_json_fields(root)
    from gobby.project_verification.refresh import refresh_project_verification_deterministic

    refresh_result = refresh_project_verification_deterministic(root, fix=True)
    verification = VerificationCommands.from_dict(refresh_result.after)
    if refresh_result.written:
        logger.info("Updated verification commands in project.json")
    return InitResult(
        project_id=project_id,
        project_name=project_name,
        project_path=os.fspath(root),
        created_at=created_at,
        already_existed=already_existed,
        verification=verification if verification.to_dict() else None,
    )


def _init_with_marker(
    db: HubDatabase,
    root: Path,
    marker: dict[str, Any],
    github_url: str | None,
) -> InitResult:
    """Attach `root` to the project named by an existing marker.

    The marker predates this call (checked in, or published by another
    writer), so a hub refusal propagates and never unlinks it; only
    `_init_no_marker` removes the marker it published itself.
    """
    from gobby.storage.project_checkouts import (
        CheckoutSentinelRejectedError,
        LocalProjectCheckoutManager,
    )
    from gobby.storage.projects import (
        CHECKOUT_FREE_PROJECT_IDS,
        LocalProjectManager,
        NameAttachRejectedError,
    )
    from gobby.storage.workspace_machine_scope import require_local_machine_id
    from gobby.utils.checkout_root import MarkerMismatchError, validate_checkout_root

    del github_url
    project_id = str(marker["id"])
    if project_id in CHECKOUT_FREE_PROJECT_IDS:
        raise CheckoutSentinelRejectedError(
            f"checkout-free sentinel project {project_id} cannot own a checkout"
        )
    machine_id = require_local_machine_id(
        None, resource_kind="project_checkout", resource_id=project_id
    )
    root_str = os.fspath(root)
    manager = LocalProjectManager(db)
    project = manager.get(project_id)

    validate_checkout_root(
        db,
        project_id=project_id,
        machine_id=machine_id,
        candidate_path=root_str,
        expected_marker_id=project_id,
    )
    if project is not None and project.deleted_at is not None:
        active = manager.get_by_name(project.name)
        if active is not None and active.id != project.id:
            raise NameAttachRejectedError(
                f"project name {project.name!r} is active on another project"
            )
        with db.transaction():
            restored = manager.restore(project_id)
            if restored is None:
                raise RuntimeError(f"Failed to restore project {project_id}")
            LocalProjectCheckoutManager(db).register(machine_id, project_id, root_str)
            project = restored
    elif project is None:
        marker_name = str(marker.get("name") or root.name)
        taken = manager.get_by_name(marker_name, include_deleted=True)
        if taken is not None and taken.id != project_id:
            raise NameAttachRejectedError(
                f"project name {marker_name!r} already exists; init is marker-authoritative"
            )
        try:
            with db.transaction():
                manager.ensure_exists(project_id, marker_name)
                LocalProjectCheckoutManager(db).register(machine_id, project_id, root_str)
        except UniqueViolation as exc:
            raise NameAttachRejectedError(
                f"project name {marker_name!r} already exists; init is marker-authoritative"
            ) from exc
        project = manager.get(project_id)
        if project is None:
            raise RuntimeError(f"Project {project_id} not found after ID-targeted create")
    else:
        LocalProjectCheckoutManager(db).register(machine_id, project_id, root_str)

    if str(marker.get("name") or "") != project.name:
        try:
            refresh_marker_expected_id(root, project.id, project.name)
        except MarkerMismatchError:
            logger.warning("Refused stale marker refresh at %s", root)

    created_at = (
        datetime_to_required_iso(project.created_at)
        if project.created_at is not None
        else str(marker.get("created_at") or "")
    )
    return _finish_init_result(
        root,
        project.id,
        project.name,
        created_at,
        already_existed=True,
    )


def _init_no_marker(
    db: HubDatabase,
    root: Path,
    name: str | None,
    github_url: str | None,
) -> InitResult:
    from gobby.storage.project_checkouts import (
        CheckoutRootTakenError,
        LocalProjectCheckoutManager,
        OverlayRegistrationRejectedError,
    )
    from gobby.storage.projects import LocalProjectManager, NameAttachRejectedError
    from gobby.storage.workspace_machine_scope import require_local_machine_id
    from gobby.utils.checkout_root import validate_checkout_root
    from gobby.utils.git import get_github_url as detect_github_url

    manager = LocalProjectManager(db)
    project_name = name or root.name
    existing = manager.get_by_name(project_name, include_deleted=True)
    if existing is not None and existing.deleted_at is not None:
        raise NameAttachRejectedError(
            f"project name {project_name!r} already exists; init is marker-authoritative"
        )
    if github_url is None:
        github_url = detect_github_url(root)

    verification = detect_verification_commands(root)
    project_id = str(uuid.uuid4())
    created_at = utc_now().isoformat()
    payload: dict[str, Any] = {
        "id": project_id,
        "name": project_name,
        "created_at": created_at,
    }
    verification_dict = verification.to_dict()
    if verification_dict:
        payload["verification"] = verification_dict

    if not _publish_marker_exclusive(root, payload):
        winner = _read_marker_file(_marker_path(root))
        if winner is None or not winner.get("id"):
            from gobby.utils.checkout_root import MarkerMismatchError

            raise MarkerMismatchError(f"failed to publish marker at {root}")
        return _init_with_marker(db, root, winner, github_url)

    _hit_failpoint("after_marker_only")
    machine_id = require_local_machine_id(
        None, resource_kind="project_checkout", resource_id=project_id
    )
    root_str = os.fspath(root)
    try:
        validate_checkout_root(
            db,
            project_id=project_id,
            machine_id=machine_id,
            candidate_path=root_str,
            expected_marker_id=project_id,
        )
    except OverlayRegistrationRejectedError:
        _unlink_still_matching_marker(root, project_id, "overlay_validate")
        raise
    _hit_failpoint("after_validate_before_register")
    try:
        with db.transaction():
            manager.ensure_exists(project_id, project_name)
            if github_url is not None:
                manager.update(project_id, github_url=github_url)
            LocalProjectCheckoutManager(db).register(machine_id, project_id, root_str)
    except UniqueViolation as exc:
        _unlink_still_matching_marker(root, project_id, "name_reject")
        raise NameAttachRejectedError(
            f"project name {project_name!r} already exists; init is marker-authoritative"
        ) from exc
    except CheckoutRootTakenError:
        _unlink_still_matching_marker(root, project_id, "root_taken")
        raise
    except OverlayRegistrationRejectedError:
        _unlink_still_matching_marker(root, project_id, "overlay_recheck")
        raise

    project = manager.get(project_id)
    if project is None:
        raise RuntimeError(f"Project {project_id} not found after marker-first create")
    logger.info("Initialized project '%s' in %s", project.name, root)
    return _finish_init_result(
        root,
        project.id,
        project.name,
        datetime_to_required_iso(project.created_at),
        already_existed=False,
    )


def initialize_project(
    cwd: Path | None = None,
    name: str | None = None,
    github_url: str | None = None,
    db: HubDatabase | None = None,
) -> InitResult:
    """Initialize a Gobby project. Marker id is authoritative; names do not attach."""
    from gobby.storage.hub.runtime import runtime_hub_database
    from gobby.utils.checkout_root import MarkerMismatchError
    from gobby.utils.project_context import get_project_context

    if db is None:
        with runtime_hub_database(apply_migrations=False) as owned_db:
            return initialize_project(cwd=cwd, name=name, github_url=github_url, db=owned_db)

    if cwd is None:
        cwd = Path.cwd()
    root_str = os.fspath(cwd)
    _reject_invalid_root(root_str)

    marker_file = _marker_path(cwd)
    if marker_file.is_file():
        loaded = _read_marker_file(marker_file)
        if loaded is None or not loaded.get("id"):
            raise MarkerMismatchError(f"malformed marker at {marker_file}")
        return _init_with_marker(db, cwd, loaded, github_url)

    context = get_project_context(cwd)
    if context and context.get("id"):
        project_root = Path(str(context.get("project_path") or cwd))
        return _init_with_marker(db, project_root, context, github_url)

    return _init_no_marker(db, cwd, name, github_url)


def _update_project_json_verification(
    cwd: Path,
    verification: VerificationCommands,
) -> None:
    """Update verification commands in an existing project.json, preserving other fields."""
    project_file = cwd / ".gobby" / "project.json"
    if not project_file.exists():
        return

    try:
        with open(project_file) as f:
            project_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read project.json for update: %s", e)
        return

    verification_dict = verification.to_dict()

    # Merge: detected commands override, but preserve manually-added custom entries
    existing_verification = project_data.get("verification", {})
    existing_custom = existing_verification.get("custom", {})

    # Merge custom entries: new detection wins, but keep manual entries not in detection
    merged_custom = {**existing_custom, **verification_dict.get("custom", {})}

    project_data["verification"] = {k: v for k, v in verification_dict.items() if k != "custom"}
    if merged_custom:
        project_data["verification"]["custom"] = merged_custom

    _atomic_write_project_json(project_file, project_data)

    logger.debug("Updated verification in %s", project_file)


def update_project_json_fields(cwd: Path, **fields: Any) -> None:
    """Update portable project fields while removing machine-local metadata."""
    project_file = cwd / ".gobby" / "project.json"
    if not project_file.exists():
        return

    try:
        with open(project_file, encoding="utf-8") as f:
            project_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read project.json for field update: %s", e)
        return

    nonportable_keys = NONPORTABLE_PROJECT_KEYS
    parent_project_path = project_data.get("parent_project_path")
    if isinstance(parent_project_path, str) and is_generated_isolation_project_json(
        project_file,
        main_repo_path=parent_project_path,
    ):
        nonportable_keys -= WORKTREE_LOCAL_PROJECT_KEYS

    for key in nonportable_keys:
        project_data.pop(key, None)

    for key, value in fields.items():
        if key in nonportable_keys:
            continue
        project_data[key] = value

    _atomic_write_project_json(project_file, project_data)

    logger.debug("Updated project.json fields in %s", project_file)


def _write_project_json(
    cwd: Path,
    project_id: str,
    name: str,
    created_at: str,
    verification: VerificationCommands | None = None,
) -> None:
    """Write the .gobby/project.json file.

    Args:
        cwd: Project root directory.
        project_id: Project ID.
        name: Project name.
        created_at: Project creation timestamp.
        verification: Optional verification commands to include.
    """
    gobby_dir = cwd / ".gobby"
    gobby_dir.mkdir(exist_ok=True)

    project_file = gobby_dir / "project.json"
    project_data: dict[str, Any] = {
        "id": project_id,
        "name": name,
        "created_at": created_at,
    }
    # Add verification config if provided and has commands
    if verification:
        verification_dict = verification.to_dict()
        if verification_dict:
            project_data["verification"] = verification_dict

    _atomic_write_project_json(project_file, project_data)

    logger.debug("Wrote project.json to %s", project_file)

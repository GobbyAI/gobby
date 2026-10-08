"""Landing policy: activation classes, the direct-commit allowance and the freeze flag.

The protected branch takes code only through ``land_commit``. This module holds
the pure path rules that landing and the protected-branch guard share, and the
landing freeze stored at ``<git-common-dir>/gobby/landing-freeze.json``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from gobby.sync.jsonl_io import atomic_write_text

# Weakest first; the strongest class among a candidate's changed paths wins.
ACTIVATION_CLASSES = ("none", "sync", "reload", "ui_build", "restart", "cutover")

_CUTOVER_PREFIXES = ("crates/",)
_CUTOVER_FILES = frozenset(
    {"Cargo.toml", "Cargo.lock", "src/gobby/storage/schema_expected_identity.json"}
)
# The CLI is included because the daemon imports gobby.cli.
_RESTART_PREFIXES = ("src/gobby/",)
_RESTART_FILES = frozenset({"pyproject.toml", "uv.lock"})
# The daemon serves web/dist from disk, so `gobby ui build` activates it.
_UI_BUILD_PREFIXES = ("web/",)

# Paths the protected-branch guard lets a direct commit change.
DIRECT_COMMIT_MARKDOWN_DIRS = (".gobby/plans/", ".gobby/roles/", "docs/")
DIRECT_COMMIT_ANY_FILE_DIRS = (".gobby/plans/coverage/",)

FREEZE_FILE_NAME = "landing-freeze.json"
UNREADABLE_FREEZE_REASON = "unreadable landing freeze file"


def _path_class(path: str) -> str:
    if path in _CUTOVER_FILES or path.startswith(_CUTOVER_PREFIXES):
        return "cutover"
    # Project pipeline files are authoring inputs; runtime reads their synced DB rows.
    pipeline_file = path.removeprefix(".gobby/workflows/pipelines/")
    if (
        pipeline_file != path
        and "/" not in pipeline_file
        and pipeline_file.endswith((".yaml", ".yml"))
    ):
        return "sync"
    # reload_cache syncs these definitions to the DB read by each new spawn.
    agent_file = path.removeprefix("src/gobby/install/shared/workflows/agents/")
    if agent_file != path and "/" not in agent_file and agent_file.endswith(".yaml"):
        return "reload"
    if path in _RESTART_FILES or path.startswith(_RESTART_PREFIXES):
        return "restart"
    if path.startswith(_UI_BUILD_PREFIXES):
        return "ui_build"
    return "none"


def classify_paths(paths: Iterable[str]) -> str:
    """Return the strongest activation class among repository-relative paths."""
    classes = {_path_class(path) for path in paths}
    if {"reload", "ui_build"} <= classes:
        # Startup sync reloads agents; the UI still needs a separate gobby ui build.
        classes.add("restart")
    return max(
        classes,
        key=ACTIVATION_CLASSES.index,
        default="none",
    )


def requires_project_sync(paths: Iterable[str]) -> bool:
    """Report imported pipeline changes independently of activation precedence."""
    return any(_path_class(path) == "sync" for path in paths)


def is_direct_commit_path(path: str) -> bool:
    """Return whether a direct commit to the protected branch may change ``path``.

    Any file under a ``DIRECT_COMMIT_ANY_FILE_DIRS`` prefix qualifies. Otherwise
    the path must be Markdown either at the repository root (no ``/``) or under
    a ``DIRECT_COMMIT_MARKDOWN_DIRS`` prefix.
    """
    if path.startswith(DIRECT_COMMIT_ANY_FILE_DIRS):
        return True
    if not path.endswith(".md"):
        return False
    return "/" not in path or path.startswith(DIRECT_COMMIT_MARKDOWN_DIRS)


@dataclass(frozen=True)
class LandingFreeze:
    """Stored landing freeze state."""

    on: bool
    reason: str
    set_by_session_id: str | None
    set_at: str | None


_FREEZE_OFF = LandingFreeze(on=False, reason="", set_by_session_id=None, set_at=None)
_FREEZE_UNREADABLE = LandingFreeze(
    on=True, reason=UNREADABLE_FREEZE_REASON, set_by_session_id=None, set_at=None
)


def freeze_path(git_common_dir: str | Path) -> Path:
    """Return the freeze file for the repository that owns ``git_common_dir``."""
    return Path(git_common_dir) / "gobby" / FREEZE_FILE_NAME


def read_freeze(git_common_dir: str | Path) -> LandingFreeze:
    """Read the freeze. A missing file is off; an unreadable or malformed one is on."""
    try:
        raw = freeze_path(git_common_dir).read_text(encoding="utf-8")
    except FileNotFoundError:
        return _FREEZE_OFF
    except (OSError, UnicodeDecodeError):
        return _FREEZE_UNREADABLE
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _FREEZE_UNREADABLE
    if not isinstance(data, dict):
        return _FREEZE_UNREADABLE
    on = data.get("on")
    reason = data.get("reason")
    setter = data.get("set_by_session_id")
    set_at = data.get("set_at")
    if (
        not isinstance(on, bool)
        or not isinstance(reason, str)
        or not isinstance(setter, str | None)
        or not isinstance(set_at, str | None)
    ):
        return _FREEZE_UNREADABLE
    return LandingFreeze(on=on, reason=reason, set_by_session_id=setter, set_at=set_at)


def write_freeze(
    git_common_dir: str | Path, *, on: bool, reason: str, session_id: str
) -> LandingFreeze:
    """Store the freeze state set or cleared by ``session_id`` and return it."""
    freeze = LandingFreeze(
        on=on,
        reason=reason,
        set_by_session_id=session_id,
        set_at=datetime.now(UTC).isoformat(),
    )
    atomic_write_text(freeze_path(git_common_dir), json.dumps(asdict(freeze), indent=2) + "\n")
    return freeze

"""Repository-level import and line-count invariants."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.hook_manager import HookManager

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_GOBBY_DIR = REPO_ROOT / "src" / "gobby"
HOOKS_DIR = SRC_GOBBY_DIR / "hooks"
TESTS_DIR = REPO_ROOT / "tests"
SESSIONS_DIR = REPO_ROOT / "src" / "gobby" / "storage" / "sessions"
STORAGE_TASKS_DIR = REPO_ROOT / "src" / "gobby" / "storage" / "tasks"


def _iter_python_files(*roots: Path) -> list[Path]:
    return sorted(path for root in roots for path in root.rglob("*.py"))


def _find_token_hits(*roots: Path, token: str) -> list[str]:
    hits: list[str] = []
    for path in _iter_python_files(*roots):
        if token in path.read_text(encoding="utf-8"):
            hits.append(str(path.relative_to(REPO_ROOT)))
    return hits


def _count_lines(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for _ in handle:
            count += 1
    return count


def test_storage_sessions_package_files_stay_under_size_limits() -> None:
    files = sorted(SESSIONS_DIR.glob("*.py"))
    assert files, "Expected session storage package files to exist"

    line_counts = {path.name: _count_lines(path) for path in files}

    for name, count in line_counts.items():
        assert count < 1000, f"{name} exceeded 1000 LOC ({count})"
        if name not in {"__init__.py", "_manager.py"}:
            assert count < 400, f"{name} exceeded 400 LOC ({count})"


def test_storage_tasks_package_files_stay_under_size_limits() -> None:
    files = sorted(STORAGE_TASKS_DIR.glob("*.py"))
    assert files, "Expected task storage package files to exist"

    line_counts = {path.name: _count_lines(path) for path in files}

    for name, count in line_counts.items():
        assert count < 1000, f"{name} exceeded 1000 LOC ({count})"


def test_no_file_references_old_session_manager_names() -> None:
    legacy_tokens = (
        "Local" + "SessionManager",
        ".".join(("gobby", "sessions", "manager")),
    )

    offenders = {
        token: _find_token_hits(SRC_GOBBY_DIR, TESTS_DIR, token=token) for token in legacy_tokens
    }
    offenders = {token: hits for token, hits in offenders.items() if hits}

    assert not offenders, f"Found deprecated session-manager names in: {offenders}"


def test_no_session_storage_attribute_in_hooks() -> None:
    offenders = _find_token_hits(HOOKS_DIR, token="_session_storage")
    assert not offenders, f"Found deprecated _session_storage hook attribute in: {offenders}"


def test_hook_manager_has_single_session_attribute() -> None:
    components = SimpleNamespace(
        config=MagicMock(),
        database=MagicMock(),
        daemon_client=MagicMock(),
        transcript_processor=MagicMock(),
        session_task_manager=MagicMock(),
        memory_storage=MagicMock(),
        task_manager=MagicMock(),
        agent_run_manager=MagicMock(),
        worktree_manager=MagicMock(),
        stop_registry=MagicMock(),
        progress_tracker=MagicMock(),
        stuck_detector=MagicMock(),
        memory_manager=MagicMock(),
        workflow_loader=MagicMock(),
        skill_manager=MagicMock(),
        pipeline_executor=MagicMock(),
        workflow_handler=MagicMock(),
        webhook_dispatcher=MagicMock(),
        session_manager=MagicMock(),
        session_coordinator=MagicMock(),
        health_monitor=MagicMock(),
        hook_assembler=MagicMock(),
        event_handlers=MagicMock(),
    )
    components.webhook_dispatcher.config = MagicMock()
    components.webhook_dispatcher.config.enabled = False

    with (
        patch("gobby.hooks.hook_manager.HookManagerFactory.create", return_value=components),
        patch("gobby.hooks.hook_manager.asyncio.get_running_loop", side_effect=RuntimeError),
        patch("gobby.hooks.event_enrichment.EventEnricher"),
        patch("gobby.hooks.session_lookup.SessionLookupService"),
        patch("gobby.storage.inter_session_messages.InterSessionMessageManager"),
    ):
        manager = HookManager(log_file="/tmp/test-hook-manager.log")

    assert manager._session_manager is components.session_manager
    assert not hasattr(manager, "_session_storage")

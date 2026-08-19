"""Repository-level import and line-count invariants."""

from __future__ import annotations

import ast
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
DEAD_PYTHON_PATHS = (
    "src/gobby/cli/_install_legacy.py",
    "src/gobby/cli/export_import.py",
    "src/gobby/cli/pipelines_runtime.py",
    "src/gobby/code_index/prune_storage.py",
    "src/gobby/config/feature_candidate_defaults.py",
    "src/gobby/config/wiki_migration.py",
    "src/gobby/plans/convergence_regression.py",
    "src/gobby/postgres_pgsearch_assets.py",
    "src/gobby/servers/routes/stage_routes.py",
    "src/gobby/skills/injector.py",
    "src/gobby/utils/mathutil2.py",
    "src/gobby/workflows/summary_actions.py",
    "src/gobby/workflows/task_actions.py",
    "src/gobby/workflows/webhook_executor.py",
    "tests/plans/test_convergence_regression.py",
    "tests/skills/test_injector.py",
    "tests/utils/test_mathutil2.py",
    "tests/workflows/test_summary_actions.py",
    "tests/workflows/test_task_actions.py",
)
DEAD_PYTHON_MODULES = frozenset(
    ".".join(Path(path).with_suffix("").parts[1:])
    for path in DEAD_PYTHON_PATHS
    if path.startswith("src/")
)


def _iter_python_files(*roots: Path) -> list[Path]:
    return sorted(path for root in roots for path in root.rglob("*.py"))


def _find_token_hits(*roots: Path, token: str) -> list[str]:
    hits: list[str] = []
    for path in _iter_python_files(*roots):
        if token in path.read_text(encoding="utf-8"):
            hits.append(str(path.relative_to(REPO_ROOT)))
    return hits


def _module_parts(path: Path) -> list[str]:
    if path.is_relative_to(SRC_GOBBY_DIR):
        relative_path = path.relative_to(SRC_GOBBY_DIR.parent)
    else:
        relative_path = path.relative_to(REPO_ROOT)

    parts = list(relative_path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def _import_candidates(path: Path, node: ast.Import | ast.ImportFrom) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}

    if node.level:
        package_parts = _module_parts(path)
        if path.name != "__init__.py":
            package_parts.pop()
        parent_levels = node.level - 1
        base_parts = package_parts[: max(0, len(package_parts) - parent_levels)]
        if node.module:
            base_parts.extend(node.module.split("."))
        base_module = ".".join(base_parts)
    else:
        base_module = node.module or ""

    candidates = {base_module} if base_module else set()
    candidates.update(
        f"{base_module}.{alias.name}" if base_module else alias.name
        for alias in node.names
        if alias.name != "*"
    )
    return candidates


def _find_dead_module_imports(*roots: Path) -> list[str]:
    hits: list[str] = []
    for path in _iter_python_files(*roots):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            candidates = _import_candidates(path, node)
            for dead_module in DEAD_PYTHON_MODULES:
                if any(
                    candidate == dead_module or candidate.startswith(f"{dead_module}.")
                    for candidate in candidates
                ):
                    relative_path = path.relative_to(REPO_ROOT)
                    hits.append(f"{relative_path}:{node.lineno}: {dead_module}")
    return sorted(hits)


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


def test_dead_python_files_stay_absent() -> None:
    existing = [path for path in DEAD_PYTHON_PATHS if (REPO_ROOT / path).exists()]

    assert not existing, f"Dead Python files were restored: {existing}"


def test_no_file_imports_dead_python_modules() -> None:
    offenders = _find_dead_module_imports(SRC_GOBBY_DIR, TESTS_DIR)

    assert not offenders, f"Found imports of dead Python modules: {offenders}"


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
        session_end_auto_link_worker=MagicMock(),
        health_monitor=MagicMock(),
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
        manager = HookManager()

    assert manager._session_manager is components.session_manager
    assert not hasattr(manager, "_session_storage")


def test_test_module_basenames_are_unique() -> None:
    files = [path for path in _iter_python_files(TESTS_DIR) if path.name.startswith("test_")]
    by_name: dict[str, list[str]] = {}
    for path in files:
        by_name.setdefault(path.name, []).append(str(path.relative_to(REPO_ROOT)))
    collisions = {name: paths for name, paths in sorted(by_name.items()) if len(paths) > 1}
    assert collisions == {}, f"duplicate test module basenames: {collisions}"

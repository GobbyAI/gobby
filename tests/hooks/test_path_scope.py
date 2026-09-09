from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from gobby.hooks import _path_scope
from gobby.providers import provider_metadata

pytestmark = pytest.mark.unit


def _scope_results(
    paths: list[str],
    *,
    cwd: Path,
    project_root: Path,
) -> tuple[bool, bool]:
    return (
        _path_scope.paths_may_touch_project(paths, cwd=cwd, project_root=project_root),
        _path_scope.code_navigation_may_touch_project(
            paths,
            cwd=cwd,
            project_root=project_root,
        ),
    )


def _linked_checkout(project_root: Path, checkout: Path) -> None:
    common_dir = project_root / ".git"
    linked_git_dir = common_dir / "worktrees" / checkout.name
    linked_git_dir.mkdir(parents=True)
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")


def test_current_project_root_uses_project_path(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()

    assert _path_scope.current_project_root({"project_path": str(project_path)}) == project_path


@pytest.mark.parametrize("user_directory", [entry.user_directory for entry in provider_metadata()])
def test_every_provider_user_directory_is_external_path_scope(
    user_directory: str,
) -> None:
    candidate = Path.home() / user_directory / "scratch" / "plan.md"

    assert (
        _path_scope.paths_may_touch_project(
            [str(candidate)],
            cwd=Path.cwd(),
            project_root=Path.cwd(),
        )
        is False
    )


def test_current_project_root_ignores_legacy_project_root_key(tmp_path: Path) -> None:
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()

    assert _path_scope.current_project_root({"project_root": str(legacy_root)}) is None


def test_temp_scratchpad_accepts_cli_host_scratchpad_under_private_tmp() -> None:
    path = Path("/private/tmp/claude-501/-Users-josh-Projects-gobby/abc123/scratchpad/notes.md")

    assert _path_scope._is_temp_agent_scratchpad_path(path)


def test_temp_scratchpad_accepts_marker_under_tmp_root() -> None:
    path = Path("/tmp/claude-501/session/scratchpad/report.md").resolve(strict=False)

    assert _path_scope._is_temp_agent_scratchpad_path(path)


def test_temp_scratchpad_rejects_tmp_path_without_marker() -> None:
    path = Path("/private/tmp/claude-501/session/report.md")

    assert not _path_scope._is_temp_agent_scratchpad_path(path)


def test_temp_scratchpad_accepts_daemon_tempdir_marker() -> None:
    path = Path(tempfile.gettempdir()).resolve(strict=False) / "gobby-agent-scratchpad-x" / "out.md"

    assert _path_scope._is_temp_agent_scratchpad_path(path)


def test_temp_scratchpad_rejects_marker_paths_outside_temp_roots() -> None:
    path = Path("/opt/example/scratchpad/notes.md")

    assert not _path_scope._is_temp_agent_scratchpad_path(path)


def test_edit_and_navigation_scope_resolve_relative_absolute_and_external_paths(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    cwd = project_root / "nested"
    external = tmp_path / "external" / "helper.py"
    cwd.mkdir(parents=True)

    assert _scope_results(
        ["../src/app.py"],
        cwd=cwd,
        project_root=project_root,
    ) == (True, True)
    assert _scope_results(
        [str(project_root / "README.md")],
        cwd=cwd,
        project_root=project_root,
    ) == (True, True)
    assert _scope_results(
        [str(external)],
        cwd=cwd,
        project_root=project_root,
    ) == (False, False)


def test_edit_and_navigation_scope_canonicalize_symlink_targets(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    external_root = tmp_path / "external"
    project_root.mkdir()
    external_root.mkdir()
    project_alias = external_root / "project-alias"
    external_alias = project_root / "external-alias"
    project_alias.symlink_to(project_root, target_is_directory=True)
    external_alias.symlink_to(external_root, target_is_directory=True)

    assert _scope_results(
        [str(project_alias / "src" / "app.py")],
        cwd=project_root,
        project_root=project_root,
    ) == (True, True)
    assert _scope_results(
        [str(external_alias / "helper.py")],
        cwd=project_root,
        project_root=project_root,
    ) == (False, False)


def test_scratchpad_bypasses_same_repo_linkage_but_managed_worktree_does_not(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    scratchpad = tmp_path / "gobby-agent-scratchpad-session"
    managed_worktree = tmp_path / "managed-worktree"
    _linked_checkout(project_root, scratchpad)
    _linked_checkout(project_root, managed_worktree)

    assert _scope_results(
        [str(scratchpad / "helper.py")],
        cwd=project_root,
        project_root=project_root,
    ) == (False, False)
    assert _scope_results(
        [str(managed_worktree / "src" / "app.py")],
        cwd=project_root,
        project_root=project_root,
    ) == (True, True)


def test_current_project_root_falls_back_when_discovery_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()

    def raise_runtime_error(_cwd: Path) -> Path:
        raise RuntimeError("broken git discovery")

    monkeypatch.setattr(_path_scope, "find_project_root", raise_runtime_error)

    assert _path_scope.current_project_root({"cwd": str(cwd)}) is None

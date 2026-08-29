import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.mcp_proxy.tools.worktrees._helpers import copy_project_json_to_worktree
from gobby.storage.worktrees import Worktree
from gobby.utils.project_context import get_project_context
from gobby.worktrees.git import WorktreeGitManager

STORED_AT = "2026-01-01T00:00:00+00:00"

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_worktree_storage() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_git_manager() -> MagicMock:
    manager = MagicMock()
    manager.repo_path = "/tmp/repo"
    return manager


@pytest.fixture
def registry(
    mock_worktree_storage: MagicMock,
    mock_git_manager: MagicMock,
) -> InternalToolRegistry:
    return create_worktrees_registry(
        worktree_storage=mock_worktree_storage,
        git_manager=mock_git_manager,
        project_id="11111111-1111-4111-8111-111111110001",
    )


def test_create_worktree_requires_branch_name(registry) -> None:
    tool = registry.get_schema("create_worktree")

    assert tool is not None
    assert "branch_name" in tool["inputSchema"]["required"]


@pytest.mark.asyncio
async def test_create_worktree_success(registry, mock_worktree_storage, mock_git_manager) -> None:
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-123",
        project_id="11111111-1111-4111-8111-111111110001",
        task_id=None,
        branch_name="feature/test",
        worktree_path="/tmp/wt/feature-test",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at=STORED_AT,
        updated_at=STORED_AT,
        merged_at=None,
    )
    with patch(
        "gobby.mcp_proxy.tools.worktrees._create.emit_worktree_event",
        return_value={"event_type": "worktree_created", "worktree_id": "wt-123"},
    ) as emit_event:
        result = await registry.call(
            "create_worktree",
            {"branch_name": "feature/test", "worktree_path": "/tmp/wt/feature-test"},
        )
    assert result["success"] is True
    assert result["worktree_path"] == "/tmp/wt/feature-test"
    assert result["event"]["event_type"] == "worktree_created"
    mock_git_manager.create_worktree.assert_called_once_with(
        worktree_path="/tmp/wt/feature-test",
        branch_name="feature/test",
        base_branch="main",
        create_branch=True,
        use_local=False,
    )
    mock_worktree_storage.create.assert_called_once()
    emit_event.assert_called_once_with(
        "worktree_created",
        worktree_id="wt-123",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/test",
        worktree_path="/tmp/wt/feature-test",
        base_branch="main",
        task_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("base_branch", ["origin/main", "refs/remotes/origin/main"])
async def test_create_worktree_rejects_remote_base_before_side_effects(
    registry: InternalToolRegistry,
    mock_worktree_storage: MagicMock,
    mock_git_manager: MagicMock,
    base_branch: str,
) -> None:
    with patch(
        "gobby.mcp_proxy.tools.worktrees._create.resolve_project_context"
    ) as resolve_project_context:
        result = await registry.call(
            "create_worktree",
            {"branch_name": "feature/test", "base_branch": base_branch},
        )

    assert result == {
        "success": False,
        "error": f"Remote-style base branch is not allowed: {base_branch}",
        "error_code": "remote_base_branch_not_allowed",
    }
    resolve_project_context.assert_not_called()
    mock_git_manager.has_unpushed_commits.assert_not_called()
    mock_git_manager.create_worktree.assert_not_called()
    mock_worktree_storage.get_by_branch.assert_not_called()
    mock_worktree_storage.create.assert_not_called()


@pytest.mark.asyncio
async def test_create_worktree_preserves_project_json_trailing_newline(
    registry, mock_worktree_storage, mock_git_manager, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    repo_path.joinpath(".gobby").mkdir(parents=True)
    worktree_path.mkdir()
    project_data = {
        "id": "11111111-1111-4111-8111-111111110001",
        "name": "test-project",
    }
    expected_project_json = json.dumps(project_data, indent=2) + "\n"
    repo_path.joinpath(".gobby", "project.json").write_text(expected_project_json)

    mock_git_manager.repo_path = str(repo_path)
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-newline",
        project_id="11111111-1111-4111-8111-111111110001",
        task_id=None,
        branch_name="feature/newline",
        worktree_path=str(worktree_path),
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at=STORED_AT,
        updated_at=STORED_AT,
        merged_at=None,
    )

    result = await registry.call(
        "create_worktree",
        {"branch_name": "feature/newline", "worktree_path": str(worktree_path)},
    )

    assert result["success"] is True
    assert worktree_path.joinpath(".gobby", "project.json").read_text() == expected_project_json
    marker = json.loads(worktree_path.joinpath(".gobby", "isolation.json").read_text())
    assert marker["parent_project_path"] == str(repo_path.resolve())
    assert marker["parent_project_id"] == project_data["id"]


@pytest.mark.asyncio
async def test_create_worktree_actual_git_path_preserves_project_json_bytes_and_clean_status(
    mock_worktree_storage: MagicMock,
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    repo_path.joinpath(".gobby").mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repo_path,
        check=True,
        timeout=10,
    )
    project_id = "11111111-1111-4111-8111-111111110001"
    project_data = {
        "id": project_id,
        "name": "test-project",
        "unrelated_metadata": {"preserve": True},
    }
    project_bytes = (json.dumps(project_data, separators=(",", ":")) + "\n").encode()
    project_json = repo_path.joinpath(".gobby", "project.json")
    project_json.write_bytes(project_bytes)
    project_json.chmod(0o755)
    repo_path.joinpath(".gitignore").write_text(".gobby/isolation.json\n")
    subprocess.run(
        ["git", "add", ".gobby/project.json", ".gitignore"],
        cwd=repo_path,
        check=True,
        timeout=10,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gobby Tests",
            "-c",
            "user.email=gobby-tests@example.com",
            "commit",
            "--no-gpg-sign",
            "-q",
            "-m",
            "initial",
        ],
        cwd=repo_path,
        check=True,
        timeout=10,
    )

    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-actual-newline",
        project_id=project_id,
        task_id=None,
        branch_name="feature/actual-newline",
        worktree_path=str(worktree_path),
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at=STORED_AT,
        updated_at=STORED_AT,
        merged_at=None,
    )
    registry = create_worktrees_registry(
        worktree_storage=mock_worktree_storage,
        git_manager=WorktreeGitManager(repo_path),
        project_id=project_id,
    )

    with patch(
        "gobby.mcp_proxy.tools.worktrees._create.emit_worktree_event",
        return_value={"event_type": "worktree_created", "worktree_id": "wt-actual-newline"},
    ):
        result = await registry.call(
            "create_worktree",
            {
                "branch_name": "feature/actual-newline",
                "base_branch": "main",
                "worktree_path": str(worktree_path),
                "use_local": True,
            },
        )

    assert result["success"] is True
    worktree_project_json = worktree_path.joinpath(".gobby", "project.json")
    assert worktree_project_json.read_bytes() == project_bytes
    assert worktree_project_json.stat().st_mode & 0o777 == 0o755
    marker = json.loads(worktree_path.joinpath(".gobby", "isolation.json").read_text())
    assert marker["parent_project_path"] == str(repo_path.resolve())
    assert marker["parent_project_id"] == project_id
    ctx = get_project_context(worktree_path)
    assert ctx is not None
    assert ctx["parent_project_path"] == str(repo_path.resolve())
    assert ctx["parent_project_id"] == project_id
    copy_project_json_to_worktree(repo_path, worktree_path)
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert worktree_project_json.read_bytes() == project_bytes
    assert status.stdout == ""


@pytest.mark.asyncio
async def test_create_worktree_installs_droid_hooks(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-droid",
        project_id="11111111-1111-4111-8111-111111110001",
        task_id=None,
        branch_name="feature/droid",
        worktree_path="/tmp/wt/feature-droid",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at=STORED_AT,
        updated_at=STORED_AT,
        merged_at=None,
    )

    with (
        patch("gobby.mcp_proxy.tools.worktrees._create.copy_project_json_to_worktree"),
        patch(
            "gobby.mcp_proxy.tools.worktrees._create.install_provider_hooks",
            return_value=True,
        ) as mock_install,
    ):
        result = await registry.call(
            "create_worktree",
            {
                "branch_name": "feature/droid",
                "worktree_path": "/tmp/wt/feature-droid",
                "provider": "droid",
            },
        )

    assert result["success"] is True
    assert result["hooks_installed"] is True
    mock_install.assert_called_once_with("droid", "/tmp/wt/feature-droid")


@pytest.mark.asyncio
async def test_create_worktree_installs_codex_hooks(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-codex",
        project_id="11111111-1111-4111-8111-111111110001",
        task_id=None,
        branch_name="feature/codex",
        worktree_path="/tmp/wt/feature-codex",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at=STORED_AT,
        updated_at=STORED_AT,
        merged_at=None,
    )

    with (
        patch("gobby.mcp_proxy.tools.worktrees._create.copy_project_json_to_worktree"),
        patch(
            "gobby.mcp_proxy.tools.worktrees._create.install_provider_hooks",
            return_value=True,
        ) as mock_install,
    ):
        result = await registry.call(
            "create_worktree",
            {
                "branch_name": "feature/codex",
                "worktree_path": "/tmp/wt/feature-codex",
                "provider": "codex",
            },
        )

    assert result["success"] is True
    assert result["hooks_installed"] is True
    mock_install.assert_called_once_with("codex", "/tmp/wt/feature-codex")


@pytest.mark.asyncio
async def test_create_worktree_failure(registry, mock_worktree_storage, mock_git_manager) -> None:
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
    mock_git_manager.create_worktree.return_value.success = False
    mock_git_manager.create_worktree.return_value.error = "Git error"
    mock_worktree_storage.get_by_branch.return_value = None
    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/fail",
        },
    )
    assert result["success"] is False
    assert "Git error" in result["error"]
    mock_worktree_storage.create.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_task_cleanup_preserves_preexisting_branch(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None

    with patch(
        "gobby.mcp_proxy.tools.worktrees._context.RegistryContext.resolve_task_id",
        side_effect=ValueError("bad task ref"),
    ):
        result = await registry.call(
            "create_worktree",
            {
                "branch_name": "feature/existing",
                "task_id": "typo",
                "create_branch": False,
                "worktree_path": "/tmp/wt/existing",
            },
        )

    assert result["success"] is False
    mock_git_manager.delete_worktree.assert_called_once_with(
        "/tmp/wt/existing",
        force=True,
        delete_branch=False,
        force_delete_branch=False,
        branch_name="feature/existing",
    )


@pytest.mark.asyncio
async def test_database_failure_cleanup_preserves_preexisting_branch(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.side_effect = RuntimeError("database unavailable")

    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/existing",
            "create_branch": False,
            "worktree_path": "/tmp/wt/existing",
        },
    )

    assert result["success"] is False
    mock_git_manager.delete_worktree.assert_called_once_with(
        "/tmp/wt/existing",
        force=True,
        delete_branch=False,
        force_delete_branch=False,
        branch_name="feature/existing",
    )


@pytest.mark.asyncio
async def test_create_worktree_existing(registry, mock_worktree_storage) -> None:
    existing = Worktree(
        id="wt-123",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/exists",
        worktree_path="/tmp/exists",
        base_branch="main",
        status="active",
        created_at="2024-01-01",
        updated_at="2024-01-01",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get_by_branch.return_value = existing
    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/exists",
        },
    )
    assert result["success"] is False
    assert "already exists" in result["error"]


@pytest.mark.asyncio
async def test_create_worktree_auto_path(registry, mock_git_manager, mock_worktree_storage) -> None:
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-auto",
        project_id="11111111-1111-4111-8111-111111110001",
        task_id=None,
        branch_name="feature/auto",
        worktree_path="/tmp/gobby-worktrees/feature-auto",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at=STORED_AT,
        updated_at=STORED_AT,
        merged_at=None,
    )
    with patch(
        "gobby.mcp_proxy.tools.worktrees._helpers.get_worktree_base_dir",
        return_value=Path("/tmp/gobby-worktrees"),
    ):
        result = await registry.call(
            "create_worktree",
            {
                "branch_name": "feature/auto",
            },
        )
        assert result["success"] is True
        args, kwargs = mock_git_manager.create_worktree.call_args
        assert "feature-auto" in kwargs["worktree_path"]


@pytest.mark.asyncio
async def test_create_worktree_use_local_explicit(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test create_worktree with explicit use_local=True passes through."""
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-local",
        project_id="11111111-1111-4111-8111-111111110001",
        task_id=None,
        branch_name="feature/local",
        worktree_path="/tmp/wt/feature-local",
        base_branch="develop",
        agent_session_id=None,
        status="active",
        created_at=STORED_AT,
        updated_at=STORED_AT,
        merged_at=None,
    )
    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/local",
            "base_branch": "develop",
            "worktree_path": "/tmp/wt/feature-local",
            "use_local": True,
        },
    )
    assert result["success"] is True
    mock_git_manager.create_worktree.assert_called_once_with(
        worktree_path="/tmp/wt/feature-local",
        branch_name="feature/local",
        base_branch="develop",
        create_branch=True,
        use_local=True,
    )
    mock_git_manager.has_unpushed_commits.assert_not_called()


@pytest.mark.asyncio
async def test_create_worktree_auto_detects_unpushed(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test create_worktree auto-sets use_local=True when base_branch has unpushed commits."""
    mock_git_manager.has_unpushed_commits.return_value = (True, 3)
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-auto-local",
        project_id="11111111-1111-4111-8111-111111110001",
        task_id=None,
        branch_name="feature/auto-local",
        worktree_path="/tmp/wt/feature-auto-local",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at=STORED_AT,
        updated_at=STORED_AT,
        merged_at=None,
    )
    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/auto-local",
            "worktree_path": "/tmp/wt/feature-auto-local",
        },
    )
    assert result["success"] is True
    mock_git_manager.has_unpushed_commits.assert_called_once_with("main")
    mock_git_manager.create_worktree.assert_called_once_with(
        worktree_path="/tmp/wt/feature-auto-local",
        branch_name="feature/auto-local",
        base_branch="main",
        create_branch=True,
        use_local=True,
    )


@pytest.mark.asyncio
async def test_create_worktree_no_unpushed_uses_remote(
    registry: InternalToolRegistry,
    mock_worktree_storage: MagicMock,
    mock_git_manager: MagicMock,
) -> None:
    """Test create_worktree defaults to use_local=False when no unpushed commits."""
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-remote",
        project_id="11111111-1111-4111-8111-111111110001",
        task_id=None,
        branch_name="feature/remote",
        worktree_path="/tmp/wt/feature-remote",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at=STORED_AT,
        updated_at=STORED_AT,
        merged_at=None,
    )
    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/remote",
            "worktree_path": "/tmp/wt/feature-remote",
        },
    )
    assert result["success"] is True
    mock_git_manager.has_unpushed_commits.assert_called_once_with("main")
    mock_git_manager.create_worktree.assert_called_once_with(
        worktree_path="/tmp/wt/feature-remote",
        branch_name="feature/remote",
        base_branch="main",
        create_branch=True,
        use_local=False,
    )


@pytest.mark.asyncio
async def test_create_worktree_merge_and_delete_succeeds_with_one_committed_file(
    mock_worktree_storage: MagicMock,
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    repo_path.joinpath(".gobby").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_path, check=True, timeout=10)
    project_id = "11111111-1111-4111-8111-111111110002"
    project_json = repo_path / ".gobby" / "project.json"
    project_json.write_text(json.dumps({"id": project_id, "name": "test-project"}) + "\n")
    repo_path.joinpath(".gitignore").write_text(".gobby/isolation.json\n")
    repo_path.joinpath("README.md").write_text("base\n")
    subprocess.run(
        ["git", "add", ".gobby/project.json", ".gitignore", "README.md"],
        cwd=repo_path,
        check=True,
        timeout=10,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gobby Tests",
            "-c",
            "user.email=gobby-tests@example.com",
            "commit",
            "--no-gpg-sign",
            "-q",
            "-m",
            "initial",
        ],
        cwd=repo_path,
        check=True,
        timeout=10,
    )

    worktree = Worktree(
        id="wt-merge-delete",
        project_id=project_id,
        task_id=None,
        branch_name="feature/one-file",
        worktree_path=str(worktree_path),
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at=STORED_AT,
        updated_at=STORED_AT,
        merged_at=None,
    )
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = worktree
    mock_worktree_storage.get.return_value = worktree
    mock_worktree_storage.mark_merged.return_value = True
    mock_worktree_storage.delete.return_value = True
    git_manager = WorktreeGitManager(repo_path)
    registry = create_worktrees_registry(
        worktree_storage=mock_worktree_storage,
        git_manager=git_manager,
        project_id=project_id,
    )

    with patch(
        "gobby.mcp_proxy.tools.worktrees._create.emit_worktree_event",
        return_value={"event_type": "worktree_created", "worktree_id": worktree.id},
    ):
        created = await registry.call(
            "create_worktree",
            {
                "branch_name": "feature/one-file",
                "base_branch": "main",
                "worktree_path": str(worktree_path),
                "use_local": True,
            },
        )
    assert created["success"] is True
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert status.stdout == ""

    extra = worktree_path / "extra.txt"
    extra.write_text("one committed file\n")
    subprocess.run(["git", "add", "extra.txt"], cwd=worktree_path, check=True, timeout=10)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gobby Tests",
            "-c",
            "user.email=gobby-tests@example.com",
            "commit",
            "--no-gpg-sign",
            "-q",
            "-m",
            "one file",
        ],
        cwd=worktree_path,
        check=True,
        timeout=10,
    )

    merged = await registry.call(
        "merge_worktree",
        {"worktree_id": worktree.id, "target_branch": "main"},
    )
    assert merged["success"] is True, merged

    deleted = await registry.call("delete_worktree", {"worktree_id": worktree.id})
    assert deleted["success"] is True, deleted
    assert not worktree_path.exists()

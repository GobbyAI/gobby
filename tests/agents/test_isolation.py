"""
Tests for Isolation Handlers.

Tests the isolation abstraction layer for spawn_agent unified API.
"""

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.agents.code_index import (
    IndexInventoryError,
    _active_deployment_grant_context,
    _reap_stale_gcode_runtime_tokens,
)
from gobby.agents.isolation import (
    CloneIsolationHandler,
    IsolationContext,
    IsolationHandler,
    NoneIsolationHandler,
    SpawnConfig,
    WorktreeIsolationHandler,
    _patch_mcp_config_for_isolation,
    ensure_isolation_code_index,
    generate_branch_name,
    get_isolation_handler,
    provider_mcp_config_error,
    repair_isolation_environment,
)
from gobby.runtime_grants.service import DeploymentGrantContext
from gobby.storage.managed_credentials import ManagedCredential
from gobby.worktrees.git import WorktreeGitManager

pytestmark = pytest.mark.unit


class TestIsolationContext:
    """Tests for IsolationContext dataclass."""

    def test_isolation_context_fields(self) -> None:
        """Test IsolationContext has all required fields."""
        ctx = IsolationContext(
            cwd="/path/to/project",
            branch_name="feature-branch",
            worktree_id="wt-123",
            clone_id="clone-456",
            isolation_type="worktree",
        )

        assert ctx.cwd == "/path/to/project"
        assert ctx.branch_name == "feature-branch"
        assert ctx.worktree_id == "wt-123"
        assert ctx.clone_id == "clone-456"
        assert ctx.isolation_type == "worktree"

    def test_isolation_context_defaults(self) -> None:
        """Test IsolationContext default values."""
        ctx = IsolationContext(cwd="/path/to/project")

        assert ctx.cwd == "/path/to/project"
        assert ctx.branch_name is None
        assert ctx.worktree_id is None
        assert ctx.clone_id is None
        assert ctx.isolation_type == "none"
        assert ctx.extra == {}

    def test_isolation_context_extra_dict(self) -> None:
        """Test IsolationContext extra dict for additional metadata."""
        ctx = IsolationContext(
            cwd="/path/to/project",
            extra={"main_repo_path": "/path/to/main"},
        )

        assert ctx.extra["main_repo_path"] == "/path/to/main"


class TestEnsureIsolationCodeIndex:
    """Tests for pre-spawn gcode indexing in isolated workspaces."""

    _PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _SESSION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    _LEASE_TOKEN = "0123456789abcdef"
    _LEASE_EPOCH = 3
    _LEASE_SECRET = "lease-signing-secret"

    @staticmethod
    def _proc(returncode: int = 0, stderr: bytes = b"") -> AsyncMock:
        proc = AsyncMock()
        proc.returncode = returncode
        proc.communicate.return_value = (b"", stderr)
        return proc

    @staticmethod
    def _credential(tmp_path: Path) -> ManagedCredential:
        execution_id = uuid4()
        role_name = f"gobby_agent_{execution_id.hex}_1"
        bootstrap_path = tmp_path / "managed" / f"{execution_id}.json"
        bootstrap_path.parent.mkdir(exist_ok=True)
        bootstrap_path.write_text(
            json.dumps(
                {
                    "database_url": f"postgresql://{role_name}:secret@localhost/gobby",
                    "managed_execution_id": str(execution_id),
                }
            )
        )
        return ManagedCredential(
            managed_execution_id=execution_id,
            role_name=role_name,
            credential_generation=1,
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            bootstrap_path=bootstrap_path,
        )

    def _stub_grant_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "gobby.agents.code_index._active_deployment_grant_context",
            lambda: DeploymentGrantContext(
                token=self._LEASE_TOKEN,
                fencing_epoch=self._LEASE_EPOCH,
                signing_secret=self._LEASE_SECRET,
            ),
        )

    @staticmethod
    def _write_operator_token(source_home: Path, token: str = "isolated-agent-token") -> None:
        source_token = source_home / "local_cli_token"
        source_token.write_text(f"{token}\n")
        source_token.chmod(0o600)

    def _identity_env(self) -> dict[str, str]:
        return {
            "GOBBY_PROJECT_ID": self._PROJECT_ID,
            "GOBBY_SESSION_ID": self._SESSION_ID,
        }

    @pytest.mark.asyncio
    async def test_runs_gcode_index_in_workspace(self, tmp_path: Path) -> None:
        proc = self._proc()

        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ) as create_proc,
        ):
            result = await ensure_isolation_code_index(str(tmp_path))

        assert result.env == {}
        assert create_proc.await_count == 3
        calls = create_proc.await_args_list
        # The config probe must exercise the run-scoped grant path (status reads
        # through the scoped role and /api/runtime/config), never an
        # operator-only listing route the run token cannot call.
        assert calls[0].args[:4] == ("/tmp/gcode", "status", "--quiet", "--format")
        assert calls[0].args[4:8] == ("json", "--allow-stale", "--project", str(tmp_path))
        assert calls[1].args[:4] == ("/tmp/gcode", "index", "--quiet", "--project")
        assert calls[1].args[4] == str(tmp_path)
        assert calls[2].args[:3] == ("/tmp/gcode", "search-content", "__gobby_code_index_smoke__")
        assert "--allow-stale" in calls[2].args
        assert "--no-freshness" not in calls[2].args
        assert calls[0].kwargs["cwd"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_scoped_credential_creates_gcode_wrapper_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = self._proc()
        runtime_root = tmp_path / "runtime"
        workspace = tmp_path / "workspace"
        source_home = tmp_path / "home"
        credential = self._credential(tmp_path)
        monkeypatch.setenv("GOBBY_HOME", str(source_home))
        workspace.mkdir()
        source_home.mkdir()
        self._write_operator_token(source_home)
        self._stub_grant_context(monkeypatch)
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)

        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ) as create_proc,
        ):
            result = await ensure_isolation_code_index(
                str(workspace),
                credential=credential,
                runtime_root=runtime_root,
                identity_env=self._identity_env(),
            )

        wrapper = workspace / ".gobby" / "bin" / "gcode"
        assert result.wrapper_path == str(wrapper)
        assert result.runtime_home is not None
        assert result.env["PATH"].split(":")[0] == str(wrapper.parent)
        assert result.env["GOBBY_CODE_INDEX_RUNTIME_HOME"] == result.runtime_home
        grant_path = Path(result.env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"])
        assert grant_path.is_file()
        assert result.env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] == str(grant_path)
        assert wrapper.read_text() == (
            f"#!/bin/sh\nexport GOBBY_HOME={result.runtime_home}\n"
            f"export GOBBY_MANAGED_EXECUTION_BOOTSTRAP={grant_path}\n"
            'exec /tmp/gcode "$@"\n'
        )
        assert not (Path(result.runtime_home) / "bootstrap.yaml").exists()
        runtime_token = Path(result.runtime_home) / "local_cli_token"
        assert not runtime_token.exists()
        assert not runtime_token.is_symlink()
        assert create_proc.await_args_list[0].args[0] == str(wrapper)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        assert status.stdout == ""

    @pytest.mark.asyncio
    async def test_grant_file_isolation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = self._proc()
        runtime_root = tmp_path / "runtime"
        workspace = tmp_path / "workspace"
        source_home = tmp_path / "home"
        credential = self._credential(tmp_path)
        monkeypatch.setenv("GOBBY_HOME", str(source_home))
        workspace.mkdir()
        source_home.mkdir()
        self._write_operator_token(source_home)
        self._stub_grant_context(monkeypatch)
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)

        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ),
        ):
            result = await ensure_isolation_code_index(
                str(workspace),
                credential=credential,
                runtime_root=runtime_root,
                identity_env=self._identity_env(),
            )

        grant_path = Path(result.env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"])
        assert grant_path.is_file()
        assert grant_path.stat().st_mode & 0o777 == 0o600
        payload = json.loads(grant_path.read_text(encoding="utf-8"))
        assert payload["principal"]["kind"] in {"agent_run", "tool_chat"}
        assert payload["principal"]["execution_id"] == str(credential.managed_execution_id)
        assert payload["principal"]["project_id"] == self._PROJECT_ID
        assert payload["deployment"]["token"] == self._LEASE_TOKEN
        assert payload["deployment"]["fencing_epoch"] == self._LEASE_EPOCH
        assert not (Path(result.runtime_home or "") / "bootstrap.yaml").exists()
        assert "database_url" not in result.env
        assert "GOBBY_AGENT_API_TOKEN" not in result.env

        from gobby.agents.code_index import _prepare_gcode_runtime

        cleanup = _prepare_gcode_runtime(
            workspace=workspace,
            gcode_bin=Path("/tmp/gcode"),
            credential=None,
            runtime_root=runtime_root,
        )
        assert cleanup.env == {}
        if grant_path.exists():
            grant_path.unlink()
        assert not grant_path.exists()

    def test_active_deployment_context_reads_current_lease(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lease = SimpleNamespace(
            deployment_token="tok-live",
            fencing_epoch=9,
            grant_signing_secret="sec-live",
        )
        monkeypatch.setattr("gobby.daemon_lease.current_lease", lambda: lease)
        context = _active_deployment_grant_context()
        assert context.token == "tok-live"
        assert context.fencing_epoch == 9
        assert context.signing_secret == "sec-live"

    def test_missing_operator_token_fails_before_grant_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.agents.code_index import _prepare_gcode_runtime

        workspace = tmp_path / "workspace"
        source_home = tmp_path / "home"
        runtime_root = tmp_path / "runtime"
        workspace.mkdir()
        source_home.mkdir()
        monkeypatch.setenv("GOBBY_HOME", str(source_home))
        self._stub_grant_context(monkeypatch)
        signed = MagicMock(side_effect=AssertionError("grant must not be signed"))
        materialized = MagicMock(side_effect=AssertionError("grant must not be written"))
        monkeypatch.setattr("gobby.agents.code_index._signed_grant_from_credential", signed)
        monkeypatch.setattr("gobby.agents.code_index.materialize_managed_launch", materialized)

        with pytest.raises(IndexInventoryError, match="operator token unavailable") as exc_info:
            _prepare_gcode_runtime(
                workspace=workspace,
                gcode_bin=Path("/tmp/gcode"),
                credential=self._credential(tmp_path),
                runtime_root=runtime_root,
                project_id=self._PROJECT_ID,
            )

        assert exc_info.value.code == "operator_token_unavailable"
        signed.assert_not_called()
        materialized.assert_not_called()
        assert not any(runtime_root.rglob("grant.json"))

    def test_missing_project_id_fails_before_grant_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.agents.code_index import _prepare_gcode_runtime

        workspace = tmp_path / "workspace"
        source_home = tmp_path / "home"
        runtime_root = tmp_path / "runtime"
        workspace.mkdir()
        source_home.mkdir()
        monkeypatch.setenv("GOBBY_HOME", str(source_home))
        self._write_operator_token(source_home)
        self._stub_grant_context(monkeypatch)
        signed = MagicMock(side_effect=AssertionError("grant must not be signed"))
        monkeypatch.setattr("gobby.agents.code_index._signed_grant_from_credential", signed)

        with pytest.raises(
            IndexInventoryError, match="isolation grant requires project_id"
        ) as exc_info:
            _prepare_gcode_runtime(
                workspace=workspace,
                gcode_bin=Path("/tmp/gcode"),
                credential=self._credential(tmp_path),
                runtime_root=runtime_root,
            )

        assert exc_info.value.code == "project_required"
        signed.assert_not_called()

    def test_deadline_uses_remaining_lifetime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.agents.code_index import _prepare_gcode_runtime
        from gobby.runtime_grants.launch import materialize_managed_launch as real_launch

        frozen = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

        class FrozenDateTime:
            @staticmethod
            def now(tz: tzinfo | None = None) -> datetime:
                if tz is None:
                    return frozen.replace(tzinfo=None)
                return frozen.astimezone(tz)

        workspace = tmp_path / "workspace"
        source_home = tmp_path / "home"
        runtime_root = tmp_path / "runtime"
        workspace.mkdir()
        source_home.mkdir()
        monkeypatch.setenv("GOBBY_HOME", str(source_home))
        self._write_operator_token(source_home)
        self._stub_grant_context(monkeypatch)
        monkeypatch.setattr("gobby.agents.code_index.datetime", FrozenDateTime)
        credential = self._credential(tmp_path)
        object.__setattr__(credential, "issued_at", datetime(2026, 6, 1, 11, 0, tzinfo=UTC))
        object.__setattr__(credential, "expires_at", datetime(2026, 6, 1, 12, 10, tzinfo=UTC))
        deadlines: list[float] = []

        def _capture_launch(*args: Any, **kwargs: Any) -> Any:
            deadline = kwargs["deadline_seconds"]
            assert isinstance(deadline, float)
            deadlines.append(deadline)
            return real_launch(*args, **kwargs)

        monkeypatch.setattr("gobby.agents.code_index.materialize_managed_launch", _capture_launch)

        _prepare_gcode_runtime(
            workspace=workspace,
            gcode_bin=Path("/tmp/gcode"),
            credential=credential,
            runtime_root=runtime_root,
            project_id=self._PROJECT_ID,
        )

        assert deadlines == [600.0]

    @pytest.mark.asyncio
    async def test_runtime_home_excludes_kek_and_links_non_secret_assets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = self._proc()
        runtime_root = tmp_path / "runtime"
        workspace = tmp_path / "workspace"
        source_home = tmp_path / "home"
        monkeypatch.setenv("GOBBY_HOME", str(source_home))
        workspace.mkdir()
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
        source_home.mkdir()
        self._write_operator_token(source_home)
        self._stub_grant_context(monkeypatch)
        (source_home / "machine_id").write_text("machine-id\n")
        kek = source_home / ".secret_kek"
        kek.write_text("kek-key\n")
        kek.chmod(0o600)
        (source_home / "models").mkdir()
        (source_home / "services").mkdir()

        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ),
        ):
            result = await ensure_isolation_code_index(
                str(workspace),
                credential=self._credential(tmp_path),
                runtime_root=runtime_root,
                identity_env=self._identity_env(),
            )

        assert result.runtime_home is not None
        runtime_home = Path(result.runtime_home)
        for name in ("machine_id", "models", "services"):
            linked = runtime_home / name
            assert linked.is_symlink(), f"{name} not linked into runtime home"
            assert linked.resolve() == (source_home / name).resolve()
        assert not (runtime_home / ".secret_kek").exists()
        assert not (runtime_home / ".secret_kek").is_symlink()

    def test_reaps_stale_runtime_credentials_and_kek(self, tmp_path: Path) -> None:
        runtime_root = tmp_path / "gcode-runtime"
        symlink_home = runtime_root / "stale-symlink"
        file_home = runtime_root / "stale-file"
        live_home = runtime_root / "live"
        for runtime_home in (symlink_home, file_home, live_home):
            runtime_home.mkdir(parents=True)

        source_token = tmp_path / "operator-token"
        source_token.touch()
        symlink_token = symlink_home / "local_cli_token"
        symlink_token.symlink_to(source_token)
        file_token = file_home / "local_cli_token"
        file_token.touch()
        stale_kek = file_home / ".secret_kek"
        stale_kek.write_text("copied-kek", encoding="utf-8")
        symlink_kek = symlink_home / ".secret_kek"
        symlink_kek.symlink_to(tmp_path / "operator-kek")

        legacy_bootstraps = {
            symlink_home / "bootstrap.yaml": "symlink-home\n",
            file_home / "bootstrap.yaml": "file-home\n",
            live_home / "bootstrap.yaml": "live-home\n",
        }
        for path, contents in legacy_bootstraps.items():
            path.write_text(contents)

        _reap_stale_gcode_runtime_tokens(runtime_root)

        assert not symlink_token.exists()
        assert not symlink_token.is_symlink()
        assert not file_token.exists()
        assert not stale_kek.exists()
        assert not symlink_kek.is_symlink()
        assert source_token.exists()
        for path in legacy_bootstraps:
            assert not path.exists()

    def test_reaps_legacy_shared_dsn_bootstrap_and_preserves_scoped_bootstrap(
        self, tmp_path: Path
    ) -> None:
        runtime_root = tmp_path / "gcode-runtime"
        legacy_home = runtime_root / "legacy"
        scoped_home = runtime_root / "scoped"
        legacy_home.mkdir(parents=True)
        scoped_home.mkdir(parents=True)
        legacy_bootstrap = legacy_home / "bootstrap.yaml"
        scoped_bootstrap = scoped_home / "bootstrap.yaml"
        legacy_bootstrap.write_text(
            "database_url: postgresql://gobby:operator-secret@localhost/gobby\n",
            encoding="utf-8",
        )
        scoped_bootstrap.write_text(
            "database_url: "
            "postgresql://gobby_agent_0123456789abcdef0123456789abcdef_1:scoped@localhost/gobby\n",
            encoding="utf-8",
        )

        _reap_stale_gcode_runtime_tokens(runtime_root)

        assert not legacy_bootstrap.exists()
        assert scoped_bootstrap.exists()

    def test_reap_survives_unwritable_sibling_home(self, tmp_path: Path) -> None:
        runtime_root = tmp_path / "gcode-runtime"
        locked_home = runtime_root / "locked"
        reapable_home = runtime_root / "reapable"
        for runtime_home in (locked_home, reapable_home):
            runtime_home.mkdir(parents=True)
        (locked_home / "local_cli_token").touch()
        reapable_token = reapable_home / "local_cli_token"
        reapable_token.touch()
        locked_home.chmod(0o500)

        try:
            # A home owned by another session must never abort this session's
            # preflight; the reap is best effort and keeps going.
            _reap_stale_gcode_runtime_tokens(runtime_root)
        finally:
            locked_home.chmod(0o700)

        assert (locked_home / "local_cli_token").exists()
        assert not reapable_token.exists()

    @pytest.mark.asyncio
    async def test_api_token_reaches_probe_subprocess_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = self._proc()
        runtime_root = tmp_path / "runtime"
        workspace = tmp_path / "workspace"
        source_home = tmp_path / "home"
        monkeypatch.setenv("GOBBY_HOME", str(source_home))
        workspace.mkdir()
        source_home.mkdir()
        self._write_operator_token(source_home)
        self._stub_grant_context(monkeypatch)
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)

        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ) as create_proc,
        ):
            result = await ensure_isolation_code_index(
                str(workspace),
                credential=self._credential(tmp_path),
                runtime_root=runtime_root,
                api_token="operator-token-value",
                identity_env=self._identity_env(),
            )

        assert create_proc.await_count == 3
        for call in create_proc.await_args_list:
            env = call.kwargs["env"]
            assert env["GOBBY_AGENT_API_TOKEN"] == "operator-token-value"
        # The credential is ephemeral: never in the runtime home, the wrapper,
        # or the env additions handed to the spawned agent.
        assert result.runtime_home is not None
        assert not (Path(result.runtime_home) / "local_cli_token").exists()
        wrapper = workspace / ".gobby" / "bin" / "gcode"
        assert "operator-token-value" not in wrapper.read_text()
        assert "GOBBY_AGENT_API_TOKEN" not in result.env

    @pytest.mark.asyncio
    async def test_no_api_token_inherits_daemon_env_untouched(self, tmp_path: Path) -> None:
        proc = self._proc()

        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ) as create_proc,
        ):
            await ensure_isolation_code_index(str(tmp_path))

        for call in create_proc.await_args_list:
            assert call.kwargs.get("env") is None

    @pytest.mark.asyncio
    async def test_raises_when_gcode_index_fails(self, tmp_path: Path) -> None:
        proc_ok = self._proc()
        proc_fail = self._proc(returncode=2, stderr=b"parse failed")

        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=[proc_ok, proc_fail]),
            ),
        ):
            with pytest.raises(RuntimeError, match="gcode_index_failed:2:parse failed"):
                await ensure_isolation_code_index(str(tmp_path))

    @pytest.mark.asyncio
    async def test_cancelling_gcode_run_kills_child_process(self, tmp_path: Path) -> None:
        communicate_started = asyncio.Event()

        class HangingProcess:
            returncode: int | None = None
            killed = False

            async def communicate(self) -> tuple[bytes, bytes]:
                communicate_started.set()
                await asyncio.Future()
                return b"", b""

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            async def wait(self) -> int:
                self.returncode = -9
                return -9

        proc = HangingProcess()
        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ),
        ):
            task = asyncio.create_task(ensure_isolation_code_index(str(tmp_path)))
            await communicate_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert proc.killed is True


class TestRepairIsolationEnvironment:
    """Tests for shared isolated workspace repair."""

    @pytest.mark.asyncio
    async def test_preseeds_python_environment(self, tmp_path: Path) -> None:
        with (
            patch("gobby.agents.isolation_repair._copy_cli_hooks", new=AsyncMock()),
            patch("gobby.utils.project_context.ensure_project_json_for_isolation"),
            patch(
                "gobby.agents.isolation_repair.preseed_isolated_python_environment",
                new=AsyncMock(),
            ) as preseed,
            patch(
                "gobby.agents.isolation_repair._patch_mcp_config_for_isolation",
                new=AsyncMock(),
            ),
        ):
            result = await repair_isolation_environment(
                main_repo_path="/main/repo",
                isolated_path=str(tmp_path),
                provider="codex",
            )

        assert result is None
        assert tmp_path.exists()
        preseed.assert_awaited_once_with(str(tmp_path))


class TestSpawnConfig:
    """Tests for SpawnConfig dataclass."""

    def test_spawn_config_fields(self) -> None:
        """Test SpawnConfig has all required fields."""
        config = SpawnConfig(
            prompt="Test prompt",
            task_id="task-123",
            task_title="Implement feature",
            task_seq_num=6121,
            branch_name=None,
            branch_prefix="feat/",
            base_branch="main",
            project_id="proj-456",
            project_path="/path/to/project",
            provider="claude",
            parent_session_id="session-789",
        )

        assert config.prompt == "Test prompt"
        assert config.task_id == "task-123"
        assert config.task_title == "Implement feature"
        assert config.task_seq_num == 6121
        assert config.branch_name is None
        assert config.branch_prefix == "feat/"
        assert config.base_branch == "main"
        assert config.project_id == "proj-456"
        assert config.project_path == "/path/to/project"
        assert config.provider == "claude"
        assert config.parent_session_id == "session-789"


class TestGenerateBranchName:
    """Tests for generate_branch_name function."""

    def test_explicit_branch_name_returned(self) -> None:
        """Test explicit branch_name is returned as-is."""
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-explicit-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        branch = generate_branch_name(config)
        assert branch == "my-explicit-branch"

    def test_branch_from_task_title(self) -> None:
        """Test branch generated from task title and seq_num."""
        config = SpawnConfig(
            prompt="Test",
            task_id="task-123",
            task_title="Implement Login Feature",
            task_seq_num=6079,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        branch = generate_branch_name(config)
        assert branch == "task-6079-implement-login-feature"

    def test_branch_from_task_title_slug_truncated(self) -> None:
        """Test branch slug is truncated to 40 chars."""
        config = SpawnConfig(
            prompt="Test",
            task_id="task-123",
            task_title="This is a very long task title that should be truncated to forty characters",
            task_seq_num=6079,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        branch = generate_branch_name(config)
        # Slug should be max 40 chars after "task-6079-"
        assert branch.startswith("task-6079-")
        slug_part = branch[len("task-6079-") :]
        assert len(slug_part) <= 40

    def test_branch_from_task_title_special_chars_removed(self) -> None:
        """Test special characters are removed from slug."""
        config = SpawnConfig(
            prompt="Test",
            task_id="task-123",
            task_title="Fix bug #123: Handle @user's input!",
            task_seq_num=6080,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        branch = generate_branch_name(config)
        # Only alphanumeric and hyphens should remain
        assert branch == "task-6080-fix-bug-123-handle-users-input"

    def test_fallback_to_prefix_timestamp(self) -> None:
        """Test fallback to prefix+timestamp when no task."""
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=None,
            branch_prefix="agent/",
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        with (
            patch("time.time", return_value=1706297600),
            patch("uuid.uuid4", return_value=SimpleNamespace(hex="abcdef123456")),
        ):
            branch = generate_branch_name(config)
            assert branch == "agent/1706297600-abcdef12"

    def test_fallback_default_prefix(self) -> None:
        """Test default prefix 'agent/' when no prefix specified."""
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        with (
            patch("time.time", return_value=1706297600),
            patch("uuid.uuid4", return_value=SimpleNamespace(hex="abcdef123456")),
        ):
            branch = generate_branch_name(config)
            assert branch == "agent/1706297600-abcdef12"

    def test_branch_name_sanitizes_invalid_git_ref_chars(self) -> None:
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=" bad..branch @{name}.lock/",
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        branch = generate_branch_name(config)

        assert branch == "bad-branch--name"


class TestNoneIsolationHandler:
    """Tests for NoneIsolationHandler."""

    @pytest.mark.asyncio
    async def test_prepare_environment_returns_project_path(self) -> None:
        """Test prepare_environment returns IsolationContext with project_path as cwd."""
        handler = NoneIsolationHandler()
        config = SpawnConfig(
            prompt="Test prompt",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/my/project",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.cwd == "/path/to/my/project"
        assert ctx.isolation_type == "none"

    @pytest.mark.asyncio
    async def test_prepare_environment_no_branch_or_ids(self) -> None:
        """Test prepare_environment returns no branch, worktree_id, or clone_id."""
        handler = NoneIsolationHandler()
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.branch_name is None
        assert ctx.worktree_id is None
        assert ctx.clone_id is None

    def test_build_context_prompt_returns_unchanged(self) -> None:
        """Test build_context_prompt returns original prompt unchanged."""
        handler = NoneIsolationHandler()
        original_prompt = "Please implement the login feature."
        ctx = IsolationContext(cwd="/path/to/project")

        result = handler.build_context_prompt(original_prompt, ctx)

        assert result == original_prompt

    @pytest.mark.asyncio
    async def test_cleanup_environment_is_noop(self) -> None:
        """Test cleanup_environment does nothing for current handler."""
        handler = NoneIsolationHandler()
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        result = await handler.cleanup_environment(config)
        assert result is None
        assert handler.build_context_prompt("prompt", IsolationContext(cwd="/path")) == "prompt"

    def test_is_isolation_handler_subclass(self) -> None:
        """Test NoneIsolationHandler is a subclass of IsolationHandler."""
        assert issubclass(NoneIsolationHandler, IsolationHandler)

    def test_isolation_handler_is_abstract(self) -> None:
        """Test IsolationHandler cannot be instantiated directly."""
        with pytest.raises(TypeError):
            IsolationHandler()


class TestWorktreeIsolationHandler:
    """Tests for WorktreeIsolationHandler."""

    @pytest.mark.asyncio
    async def test_prepare_environment_creates_worktree(self) -> None:
        """Test prepare_environment creates worktree if not exists."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.create_worktree.return_value = MagicMock(
            success=True,
            worktree_path="/tmp/worktrees/my-branch",
        )
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = None  # No existing worktree
        mock_worktree_storage.create.return_value = MagicMock(
            id="wt-123",
            worktree_path="/tmp/worktrees/my-branch",
            branch_name="my-branch",
        )

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.isolation_type == "worktree"
        assert ctx.worktree_id == "wt-123"
        assert ctx.branch_name == "my-branch"
        mock_git_manager.create_worktree.assert_called_once()

    @pytest.mark.asyncio
    async def test_prepare_environment_reuses_existing_worktree(self) -> None:
        """Test prepare_environment reuses existing worktree for same branch."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.get_current_branch.return_value = "main"

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = MagicMock(
            id="existing-wt-456",
            worktree_path="/tmp/worktrees/existing-branch",
            branch_name="existing-branch",
        )
        mock_worktree_storage.is_claimed_by_live_session.return_value = False

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="existing-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        with (
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "gobby.agents.isolation_worktree.repair_isolation_environment",
                new=AsyncMock(),
            ) as repair,
            patch(
                "gobby.agents.isolation_worktree.worktree_reuse.sync_reused_worktree_to_base",
                new=AsyncMock(),
            ) as sync,
        ):
            ctx = await handler.prepare_environment(config)

        assert ctx.worktree_id == "existing-wt-456"
        assert ctx.cwd == "/tmp/worktrees/existing-branch"
        sync.assert_awaited_once_with(
            git_manager=mock_git_manager,
            worktree_path="/tmp/worktrees/existing-branch",
            base_branch="main",
        )
        repair.assert_awaited_once_with(
            main_repo_path="/path/to/main/repo",
            isolated_path="/tmp/worktrees/existing-branch",
            provider="claude",
        )
        mock_worktree_storage.touch.assert_called_once_with("existing-wt-456")
        # Should NOT create a new worktree
        mock_git_manager.create_worktree.assert_not_called()

    @pytest.mark.asyncio
    async def test_prepare_environment_refuses_live_claimed_existing_worktree(self) -> None:
        """Test prepare_environment refuses to reuse a worktree claimed by a live session."""
        checked_worktree_ids: list[str] = []
        existing = SimpleNamespace(
            id="existing-wt-456",
            worktree_path="/tmp/worktrees/existing-branch",
            branch_name="existing-branch",
        )

        def unexpected_create_worktree(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("claimed worktree reuse must not create a worktree")

        def get_by_branch(_project_id: str, _branch_name: str) -> SimpleNamespace:
            return existing

        def is_claimed_by_live_session(worktree_id: str) -> bool:
            checked_worktree_ids.append(worktree_id)
            return True

        git_manager = SimpleNamespace(
            repo_path="/path/to/main/repo",
            get_current_branch=lambda: "main",
            create_worktree=unexpected_create_worktree,
        )
        worktree_storage = SimpleNamespace(
            get_by_branch=get_by_branch,
            is_claimed_by_live_session=is_claimed_by_live_session,
        )
        handler = WorktreeIsolationHandler(
            git_manager=git_manager,
            worktree_storage=worktree_storage,
        )
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="existing-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        with (
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "gobby.agents.isolation_worktree.repair_isolation_environment",
                new=AsyncMock(),
            ) as repair,
            patch(
                "gobby.agents.isolation_worktree.worktree_reuse.sync_reused_worktree_to_base",
                new=AsyncMock(),
            ) as sync,
            pytest.raises(RuntimeError, match="Cannot reuse claimed live worktree"),
        ):
            await handler.prepare_environment(config)

        assert checked_worktree_ids == ["existing-wt-456"]
        sync.assert_not_awaited()
        repair.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prepare_environment_cleans_stale_record_before_recreate(self) -> None:
        """Test stale worktree records are pruned before recreating the deterministic path."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)
        mock_git_manager.create_worktree.return_value = MagicMock(success=True)
        mock_git_manager.delete_worktree.return_value = MagicMock(success=True, error=None)

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = MagicMock(
            id="stale-wt-456",
            worktree_path="/tmp/worktrees/stale-branch",
            branch_name="stale-branch",
            base_branch="main",
        )
        mock_worktree_storage.create.return_value = MagicMock(
            id="new-wt-789",
            worktree_path="/tmp/worktrees/stale-branch",
            branch_name="stale-branch",
        )

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )
        handler._generate_worktree_path = MagicMock(return_value="/tmp/worktrees/stale-branch")

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="stale-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        with (
            patch("pathlib.Path.is_dir", return_value=False),
            patch(
                "gobby.agents.isolation_worktree.repair_isolation_environment",
                new=AsyncMock(),
            ) as repair,
        ):
            ctx = await handler.prepare_environment(config)

        assert ctx.worktree_id == "new-wt-789"
        assert ctx.cwd == "/tmp/worktrees/stale-branch"
        mock_git_manager.delete_worktree.assert_called_once_with(
            worktree_path="/tmp/worktrees/stale-branch",
            force=True,
            delete_branch=True,
            branch_name="stale-branch",
            base_branch="main",
        )
        mock_worktree_storage.delete.assert_called_once_with("stale-wt-456")
        mock_git_manager.create_worktree.assert_called_once_with(
            worktree_path="/tmp/worktrees/stale-branch",
            branch_name="stale-branch",
            base_branch="main",
            create_branch=True,
            use_local=False,
        )
        mock_worktree_storage.create.assert_called_once_with(
            project_id="proj-123",
            branch_name="stale-branch",
            worktree_path="/tmp/worktrees/stale-branch",
            base_branch="main",
            task_id=None,
        )
        repair.assert_awaited_once_with(
            main_repo_path="/path/to/main/repo",
            isolated_path="/tmp/worktrees/stale-branch",
            provider="claude",
        )

    def test_build_context_prompt_prepends_warning(self) -> None:
        """Test build_context_prompt prepends the worktree context banner."""
        mock_git_manager = MagicMock()
        mock_worktree_storage = MagicMock()

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        original_prompt = "Please implement the login feature."
        ctx = IsolationContext(
            cwd="/tmp/worktrees/feature-branch",
            branch_name="feature-branch",
            worktree_id="wt-123",
            isolation_type="worktree",
            extra={"main_repo_path": "/path/to/main/repo"},
        )

        result = handler.build_context_prompt(original_prompt, ctx)

        assert "Worktree context — you are working in an isolated git worktree" in result
        assert original_prompt in result
        assert "feature-branch" in result

    @pytest.mark.asyncio
    async def test_cleanup_after_storage_create_failure(self) -> None:
        """Test cleanup removes worktree on disk when storage.create fails."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.create_worktree.return_value = MagicMock(success=True)
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = None
        mock_worktree_storage.create.side_effect = RuntimeError("DB error")

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        with pytest.raises(RuntimeError, match="DB error"):
            await handler.prepare_environment(config)

        await handler.cleanup_environment(config)

        # Should have called delete_worktree to clean up disk
        mock_git_manager.delete_worktree.assert_called_once()
        delete_kwargs = mock_git_manager.delete_worktree.call_args.kwargs
        assert delete_kwargs["force"] is True
        assert delete_kwargs["delete_branch"] is True
        assert delete_kwargs["force_delete_branch"] is True
        assert delete_kwargs["branch_name"] == "my-branch"
        assert "my-branch" in str(delete_kwargs["worktree_path"])
        mock_worktree_storage.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_after_storage_failure_deletes_created_branch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failed setup removes both the worktree and its newly created branch."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_path,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path,
            check=True,
        )
        (repo_path / "README.md").write_text("test\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        git_manager = WorktreeGitManager(repo_path)
        monkeypatch.setattr(git_manager, "has_unpushed_commits", lambda _branch: (True, 1))
        worktree_path = tmp_path / "worktree"

        worktree_storage = MagicMock()
        worktree_storage.get_by_branch.return_value = None
        worktree_storage.create.side_effect = RuntimeError("DB error")
        handler = WorktreeIsolationHandler(
            git_manager=git_manager,
            worktree_storage=worktree_storage,
        )
        monkeypatch.setattr(handler, "_generate_worktree_path", lambda *_args: str(worktree_path))
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path=str(repo_path),
            provider="claude",
            parent_session_id="sess-456",
        )

        with pytest.raises(RuntimeError, match="DB error"):
            await handler.prepare_environment(config)
        await handler.cleanup_environment(config)

        assert not worktree_path.exists()
        branch_check = subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/my-branch"],
            cwd=repo_path,
            capture_output=True,
        )
        assert branch_check.returncode != 0

    @pytest.mark.asyncio
    async def test_cleanup_after_hook_copy_failure(self) -> None:
        """Test cleanup removes worktree and storage record when hook copy fails."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.create_worktree.return_value = MagicMock(success=True)
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = None
        mock_worktree_storage.create.return_value = MagicMock(
            id="wt-123",
            worktree_path="/tmp/worktrees/my-branch",
            branch_name="my-branch",
        )

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        # Make _copy_cli_hooks raise
        with patch(
            "gobby.agents.isolation_repair._copy_cli_hooks",
            side_effect=OSError("Permission denied"),
        ):
            config = SpawnConfig(
                prompt="Test",
                task_id=None,
                task_title=None,
                task_seq_num=None,
                branch_name="my-branch",
                branch_prefix=None,
                base_branch="main",
                project_id="proj-123",
                project_path="/path/to/main/repo",
                provider="claude",
                parent_session_id="sess-456",
            )

            with pytest.raises(OSError, match="Permission denied"):
                await handler.prepare_environment(config)

        await handler.cleanup_environment(config)

        assert handler._partial_worktrees == {}
        mock_git_manager.delete_worktree.assert_called_once()
        mock_worktree_storage.delete.assert_called_once_with("wt-123")

    @pytest.mark.asyncio
    async def test_cleanup_noop_on_success(self) -> None:
        """Test cleanup does nothing after successful prepare."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.create_worktree.return_value = MagicMock(success=True)
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = None
        mock_worktree_storage.create.return_value = MagicMock(
            id="wt-123",
            worktree_path="/tmp/worktrees/my-branch",
            branch_name="my-branch",
        )

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        context = await handler.prepare_environment(config)

        assert context.isolation_type == "worktree"
        assert context.worktree_id == "wt-123"

        await handler.cleanup_environment(config)

        # Should NOT call delete since nothing to clean up
        mock_git_manager.delete_worktree.assert_not_called()
        mock_worktree_storage.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_prepare_calls_ensure_project_json(self) -> None:
        """Test prepare_environment calls ensure_project_json_for_isolation."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.create_worktree.return_value = MagicMock(success=True)
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = None
        mock_worktree_storage.create.return_value = MagicMock(
            id="wt-123",
            worktree_path="/tmp/worktrees/my-branch",
            branch_name="my-branch",
        )

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        with patch("gobby.utils.project_context.ensure_project_json_for_isolation") as mock_ensure:
            await handler.prepare_environment(config)
            mock_ensure.assert_called_once_with(
                "/path/to/main/repo",
                handler._generate_worktree_path("my-branch", "repo"),
            )
            assert mock_ensure.call_count == 1
            assert mock_ensure.call_args is not None

    def test_is_isolation_handler_subclass(self) -> None:
        """Test WorktreeIsolationHandler is a subclass of IsolationHandler."""
        assert issubclass(WorktreeIsolationHandler, IsolationHandler)


class TestCloneIsolationHandler:
    """Tests for CloneIsolationHandler."""

    @pytest.mark.asyncio
    async def test_prepare_environment_creates_clone(self) -> None:
        """Test prepare_environment creates shallow clone if not exists."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(
            success=True,
            clone_path="/tmp/clones/my-branch",
        )

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None  # No existing clone
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-123",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.isolation_type == "clone"
        assert ctx.clone_id == "clone-123"
        assert ctx.branch_name == "my-branch"
        mock_clone_manager.create_clone.assert_called_once()
        # Should default to shallow=True, use_local=False when no git_manager
        call_kwargs = mock_clone_manager.create_clone.call_args.kwargs
        assert call_kwargs.get("shallow") is True
        assert call_kwargs.get("use_local") is False

    @pytest.mark.asyncio
    async def test_prepare_environment_uses_local_with_unpushed_commits(self) -> None:
        """Test prepare_environment uses local clone when unpushed commits detected."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(
            success=True,
            clone_path="/tmp/clones/my-branch",
        )

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-456",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        mock_git_manager = MagicMock()
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (True, 3)

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
            git_manager=mock_git_manager,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.isolation_type == "clone"
        assert ctx.clone_id == "clone-456"
        # Should use full clone from local when unpushed commits exist
        call_kwargs = mock_clone_manager.create_clone.call_args.kwargs
        assert call_kwargs.get("use_local") is True
        assert call_kwargs.get("shallow") is False

    @pytest.mark.asyncio
    async def test_prepare_environment_no_local_without_unpushed(self) -> None:
        """Test prepare_environment uses remote clone when no unpushed commits."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(
            success=True,
            clone_path="/tmp/clones/my-branch",
        )

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-789",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        mock_git_manager = MagicMock()
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
            git_manager=mock_git_manager,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.isolation_type == "clone"
        # Should use shallow remote clone when no unpushed commits
        call_kwargs = mock_clone_manager.create_clone.call_args.kwargs
        assert call_kwargs.get("use_local") is False
        assert call_kwargs.get("shallow") is True

    @pytest.mark.asyncio
    async def test_prepare_environment_reuses_existing_clone(self) -> None:
        """Test prepare_environment reuses existing clone for same branch."""
        mock_clone_manager = MagicMock()

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = MagicMock(
            id="existing-clone-456",
            clone_path="/tmp/clones/existing-branch",
            branch_name="existing-branch",
        )

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="existing-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        with (
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "gobby.agents.isolation_clone.repair_isolation_environment",
                new=AsyncMock(),
            ) as repair,
        ):
            ctx = await handler.prepare_environment(config)

        assert ctx.clone_id == "existing-clone-456"
        assert ctx.cwd == "/tmp/clones/existing-branch"
        repair.assert_awaited_once_with(
            main_repo_path="/path/to/main/repo",
            isolated_path="/tmp/clones/existing-branch",
            provider="claude",
        )
        # Should NOT create a new clone
        mock_clone_manager.create_clone.assert_not_called()

    @pytest.mark.asyncio
    async def test_prepare_environment_concurrent_same_branch_uses_distinct_paths(self) -> None:
        """Test concurrent clone creation for same branch targets distinct paths."""
        clone_paths: list[str] = []

        def create_clone(**kwargs: str) -> MagicMock:
            clone_paths.append(kwargs["clone_path"])
            return MagicMock(success=True)

        def create_record(**kwargs: str) -> MagicMock:
            return MagicMock(
                id=f"clone-{len(clone_paths)}",
                clone_path=kwargs["clone_path"],
                branch_name=kwargs["branch_name"],
            )

        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.side_effect = create_clone
        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.side_effect = create_record
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="feature/shared",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        with patch("gobby.agents.isolation_clone.repair_isolation_environment", new=AsyncMock()):
            results = await asyncio.gather(
                CloneIsolationHandler(mock_clone_manager, mock_clone_storage).prepare_environment(
                    config
                ),
                CloneIsolationHandler(mock_clone_manager, mock_clone_storage).prepare_environment(
                    config
                ),
            )

        assert len(set(clone_paths)) == 2
        assert results[0].cwd != results[1].cwd

    def test_generate_clone_path_deconflicts_sanitized_equal_branches(self) -> None:
        """Test branches with the same safe path prefix still get unique paths."""
        handler = CloneIsolationHandler(MagicMock(), MagicMock())

        with patch("gobby.agents.isolation_clone.uuid4") as mock_uuid4:
            mock_uuid4.side_effect = [
                MagicMock(hex="a" * 32),
                MagicMock(hex="b" * 32),
                MagicMock(hex="c" * 32),
                MagicMock(hex="d" * 32),
            ]
            same_branch_a = handler._generate_clone_path("feature/shared", "repo")
            same_branch_b = handler._generate_clone_path("feature/shared", "repo")
            sanitized_a = handler._generate_clone_path("feat/x", "repo")
            sanitized_b = handler._generate_clone_path("feat-x", "repo")

        assert same_branch_a != same_branch_b
        assert sanitized_a != sanitized_b
        assert Path(same_branch_a).name == "feature-shared-aaaaaaaa"
        assert Path(sanitized_a).name == "feat-x-cccccccc"

    def test_build_context_prompt_prepends_warning(self) -> None:
        """Test build_context_prompt prepends the clone context banner."""
        mock_clone_manager = MagicMock()
        mock_clone_storage = MagicMock()

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        original_prompt = "Please implement the login feature."
        ctx = IsolationContext(
            cwd="/tmp/clones/feature-branch",
            branch_name="feature-branch",
            clone_id="clone-123",
            isolation_type="clone",
            extra={"source_repo": "https://github.com/user/repo.git"},
        )

        result = handler.build_context_prompt(original_prompt, ctx)

        assert "Clone context — you are working in an isolated shallow clone" in result
        assert original_prompt in result
        assert "feature-branch" in result

    @pytest.mark.asyncio
    async def test_cleanup_after_storage_create_failure(self) -> None:
        """Test cleanup removes clone on disk when storage.create fails."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(success=True)

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.side_effect = RuntimeError("DB error")

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        with pytest.raises(RuntimeError, match="DB error"):
            await handler.prepare_environment(config)

        await handler.cleanup_environment(config)

        mock_clone_manager.delete_clone.assert_called_once()
        delete_kwargs = mock_clone_manager.delete_clone.call_args.kwargs
        assert delete_kwargs["force"] is True
        assert "my-branch" in delete_kwargs["clone_path"]
        mock_clone_storage.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_after_hook_copy_failure(self) -> None:
        """Test cleanup removes clone and storage record when hook copy fails."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(success=True)

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-123",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        with patch(
            "gobby.agents.isolation_repair._copy_cli_hooks",
            side_effect=OSError("Permission denied"),
        ):
            config = SpawnConfig(
                prompt="Test",
                task_id=None,
                task_title=None,
                task_seq_num=None,
                branch_name="my-branch",
                branch_prefix=None,
                base_branch="main",
                project_id="proj-123",
                project_path="/path/to/main/repo",
                provider="claude",
                parent_session_id="sess-456",
            )

            with pytest.raises(OSError, match="Permission denied"):
                await handler.prepare_environment(config)

        await handler.cleanup_environment(config)

        assert handler._partial_clones == {}
        mock_clone_manager.delete_clone.assert_called_once()
        mock_clone_storage.delete.assert_called_once_with("clone-123")

    @pytest.mark.asyncio
    async def test_cleanup_noop_on_success(self) -> None:
        """Test cleanup does nothing after successful prepare."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(success=True)

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-123",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        context = await handler.prepare_environment(config)

        assert context.isolation_type == "clone"
        assert context.clone_id == "clone-123"

        await handler.cleanup_environment(config)

        mock_clone_manager.delete_clone.assert_not_called()
        mock_clone_storage.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_prepare_calls_ensure_project_json(self) -> None:
        """Test prepare_environment calls ensure_project_json_for_isolation."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(success=True)

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-123",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/source/repo",
            provider="qwen",
            parent_session_id="sess-456",
        )

        with patch("gobby.utils.project_context.ensure_project_json_for_isolation") as mock_ensure:
            await handler.prepare_environment(config)
            clone_path = mock_clone_manager.create_clone.call_args.kwargs["clone_path"]
            mock_ensure.assert_called_once_with(
                "/path/to/source/repo",
                clone_path,
            )
            assert mock_ensure.call_count == 1
            assert mock_ensure.call_args is not None

    def test_is_isolation_handler_subclass(self) -> None:
        """Test CloneIsolationHandler is a subclass of IsolationHandler."""
        assert issubclass(CloneIsolationHandler, IsolationHandler)


class TestGetIsolationHandler:
    """Tests for get_isolation_handler factory function."""

    def test_get_isolation_handler_none(self) -> None:
        """Test get_isolation_handler('none') returns NoneIsolationHandler."""
        handler = get_isolation_handler("none")

        assert isinstance(handler, NoneIsolationHandler)

    def test_get_isolation_handler_worktree(self) -> None:
        """Test get_isolation_handler('worktree', ...) returns WorktreeIsolationHandler."""
        mock_git_manager = MagicMock()
        mock_worktree_storage = MagicMock()

        handler = get_isolation_handler(
            "worktree",
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        assert isinstance(handler, WorktreeIsolationHandler)

    def test_get_isolation_handler_clone(self) -> None:
        """Test get_isolation_handler('clone', ...) returns CloneIsolationHandler."""
        mock_clone_manager = MagicMock()
        mock_clone_storage = MagicMock()

        handler = get_isolation_handler(
            "clone",
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        assert isinstance(handler, CloneIsolationHandler)

    def test_get_isolation_handler_invalid_mode_raises(self) -> None:
        """Test get_isolation_handler raises ValueError for invalid mode."""
        with pytest.raises(ValueError, match="Unknown isolation mode"):
            get_isolation_handler("invalid")

    def test_get_isolation_handler_worktree_missing_deps_raises(self) -> None:
        """Test get_isolation_handler('worktree') raises if dependencies missing."""
        with pytest.raises(ValueError, match="git_manager.*required"):
            get_isolation_handler("worktree")

    def test_get_isolation_handler_clone_missing_deps_raises(self) -> None:
        """Test get_isolation_handler('clone') raises if dependencies missing."""
        with pytest.raises(ValueError, match="clone_manager.*required"):
            get_isolation_handler("clone")


class TestPatchMcpConfigForIsolation:
    """Tests for _patch_mcp_config_for_isolation."""

    @pytest.mark.asyncio
    async def test_writes_mcp_json(self, tmp_path: Path) -> None:
        """Writes .mcp.json with --project pointing to main repo."""
        isolated_path = str(tmp_path / "worktree")
        Path(isolated_path).mkdir()
        main_repo = "/path/to/main/repo"

        await _patch_mcp_config_for_isolation(main_repo, isolated_path, "qwen")

        mcp_json = Path(isolated_path) / ".mcp.json"
        assert mcp_json.exists()
        data = json.loads(mcp_json.read_text())
        gobby_server = data["mcpServers"]["gobby"]
        assert gobby_server["command"] == "uv"
        assert "--project" in gobby_server["args"]
        assert main_repo in gobby_server["args"]
        assert "gobby" in gobby_server["args"]
        assert "mcp-server" in gobby_server["args"]

    @pytest.mark.asyncio
    async def test_patches_claude_json_for_claude_provider(self, tmp_path: Path) -> None:
        """For claude provider, registers isolated path in ~/.claude.json."""
        isolated_path = str(tmp_path / "worktree")
        Path(isolated_path).mkdir()
        main_repo = "/path/to/main/repo"

        fake_claude_json = tmp_path / ".claude.json"
        fake_claude_json.write_text("{}")

        with patch("pathlib.Path.home", return_value=tmp_path):
            await _patch_mcp_config_for_isolation(main_repo, isolated_path, "claude")

        data = json.loads(fake_claude_json.read_text())
        assert isolated_path in data["projects"]
        project_config = data["projects"][isolated_path]
        assert "gobby" in project_config["mcpServers"]

    @pytest.mark.asyncio
    async def test_does_not_patch_claude_json_for_qwen(self, tmp_path: Path) -> None:
        """For non-claude provider, does not touch ~/.claude.json."""
        isolated_path = str(tmp_path / "worktree")
        Path(isolated_path).mkdir()

        fake_claude_json = tmp_path / ".claude.json"
        # File doesn't exist initially

        with patch("pathlib.Path.home", return_value=tmp_path):
            await _patch_mcp_config_for_isolation("/main", isolated_path, "qwen")

        # Should NOT have created ~/.claude.json
        assert not fake_claude_json.exists()

    @pytest.mark.asyncio
    async def test_preserves_existing_claude_json_data(self, tmp_path: Path) -> None:
        """Patching should preserve existing data in ~/.claude.json."""
        isolated_path = str(tmp_path / "worktree")
        Path(isolated_path).mkdir()

        fake_claude_json = tmp_path / ".claude.json"
        existing_project_config = {
            "allowedTools": ["Bash(git status:*)"],
            "hasCompletedProjectOnboarding": True,
            "hasTrustDialogAccepted": True,
            "history": [{"display": "gobby status", "pastedContents": {}}],
            "mcpServers": {"old": {"command": "old-mcp"}},
        }
        existing = {
            "existingKey": "value",
            "projects": {
                "/other": {"foo": "bar"},
                isolated_path: existing_project_config,
            },
        }
        fake_claude_json.write_text(json.dumps(existing))

        with patch("pathlib.Path.home", return_value=tmp_path):
            await _patch_mcp_config_for_isolation("/main", isolated_path, "claude")

        data = json.loads(fake_claude_json.read_text())
        assert data["existingKey"] == "value"
        assert "/other" in data["projects"]
        assert isolated_path in data["projects"]
        project_config = data["projects"][isolated_path]
        assert project_config["allowedTools"] == existing_project_config["allowedTools"]
        assert project_config["hasCompletedProjectOnboarding"] is True
        assert project_config["hasTrustDialogAccepted"] is True
        assert project_config["history"] == existing_project_config["history"]
        assert "gobby" in project_config["mcpServers"]

    @pytest.mark.asyncio
    async def test_handles_write_failure_gracefully(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Should log warning but not raise on write failure."""
        # Non-existent parent dir will cause write failure
        isolated_path = str(tmp_path / "nonexistent" / "deep" / "path")

        # Should not raise
        await _patch_mcp_config_for_isolation("/main", isolated_path, "claude")

        # Verify warning was logged
        assert any("Failed to write" in msg for msg in caplog.messages)


class TestProviderMcpConfigPreflight:
    """Tests for provider_mcp_config_error."""

    def test_reports_missing_mcp_json(self, tmp_path: Path) -> None:
        assert provider_mcp_config_error(str(tmp_path), "qwen").startswith(
            "provider_mcp_config_missing:"
        )

    def test_accepts_non_claude_mcp_json(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "gobby": {
                            "command": "uv",
                            "args": ["run", "--project", "/main", "gobby", "mcp-server"],
                        }
                    }
                }
            )
        )

        assert provider_mcp_config_error(str(tmp_path), "qwen") is None

    def test_requires_claude_project_config(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "gobby": {
                            "command": "uv",
                            "args": ["run", "--project", "/main", "gobby", "mcp-server"],
                        }
                    }
                }
            )
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            error = provider_mcp_config_error(str(tmp_path), "claude")

        assert error is not None
        assert error.startswith("provider_mcp_config_missing:")

"""Tests for isolated Python environment seeding."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.agents.python_env_seed import (
    preseed_isolated_python_environment,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_skips_non_uv_workspace(tmp_path: Path) -> None:
    result = await preseed_isolated_python_environment(str(tmp_path))

    assert result.attempted is False
    assert result.success is False
    assert result.skipped_reason == "pyproject_missing"


@pytest.mark.asyncio
async def test_uses_host_cache_and_offline_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0'\n")
    (tmp_path / "uv.lock").write_text("")
    host_cache = tmp_path / "host-cache"
    host_cache.mkdir()
    monkeypatch.setenv("UV_CACHE_DIR", "/tmp/gobby/uv-cache/session")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/tmp/main/.venv")
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/main/.venv")

    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate.return_value = (b"seeded", b"")

    with (
        patch("gobby.agents.python_env_seed.resolve_native_bin", return_value="/bin/uv"),
        patch(
            "gobby.agents.python_env_seed._resolve_default_uv_cache_dir",
            return_value=str(host_cache),
        ),
        patch(
            "gobby.agents.python_env_seed.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as create_proc,
    ):
        result = await preseed_isolated_python_environment(str(tmp_path))

    assert result.attempted is True
    assert result.success is True
    assert result.cache_dir == str(host_cache)
    create_proc.assert_awaited_once()
    args = create_proc.call_args.args
    assert args == (
        "/bin/uv",
        "sync",
        "--offline",
        "--frozen",
        "--no-progress",
        "--link-mode",
        "copy",
        "--cache-dir",
        str(host_cache),
    )
    env = create_proc.call_args.kwargs["env"]
    assert env["UV_CACHE_DIR"] == str(host_cache)
    assert env["UV_PYTHON_DOWNLOADS"] == "never"
    assert "UV_PROJECT_ENVIRONMENT" not in env
    assert "VIRTUAL_ENV" not in env


@pytest.mark.asyncio
async def test_reports_offline_sync_failure(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0'\n")
    (tmp_path / "uv.lock").write_text("")
    host_cache = tmp_path / "host-cache"
    host_cache.mkdir()

    proc = AsyncMock()
    proc.returncode = 1
    proc.communicate.return_value = (b"", b"missing wheel")

    with (
        patch("gobby.agents.python_env_seed.resolve_native_bin", return_value="/bin/uv"),
        patch(
            "gobby.agents.python_env_seed._resolve_default_uv_cache_dir",
            return_value=str(host_cache),
        ),
        patch(
            "gobby.agents.python_env_seed.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ),
    ):
        result = await preseed_isolated_python_environment(str(tmp_path))

    assert result.attempted is True
    assert result.success is False
    assert result.error == "uv_sync_failed:1:missing wheel"

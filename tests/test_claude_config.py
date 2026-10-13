"""Claude config writers must cooperate with the native mkdir lock."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.agents.isolation_repair import _patch_mcp_config_for_isolation, provider_mcp_config_error
from gobby.agents.trust import pre_approve_directory
from gobby.claude_config import ClaudeConfigError, update_claude_config
from gobby.cli.installers.mcp_config import (
    configure_mcp_server_json,
    configure_project_mcp_server,
    remove_mcp_server_json,
    remove_project_mcp_server,
)


@pytest.mark.parametrize("selector", ["custom", "legacy", "oauth"])
@pytest.mark.asyncio
async def test_isolation_preflight_reads_selected_host_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)
    if selector == "custom":
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    elif selector == "legacy":
        legacy = tmp_path / ".claude" / ".config.json"
        legacy.parent.mkdir()
        legacy.write_text("{}")
    else:
        monkeypatch.setenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", "https://example.invalid")
    with patch("pathlib.Path.home", return_value=tmp_path):
        await _patch_mcp_config_for_isolation("/main", str(workspace), "claude")
        assert provider_mcp_config_error(str(workspace), "claude") is None


def test_remove_missing_config_does_not_create_parent(tmp_path: Path) -> None:
    settings = tmp_path / "missing" / "mcp.json"
    result = remove_mcp_server_json(settings)
    assert result["success"] is True
    assert result["removed"] is False
    assert not settings.parent.exists()


def test_native_lock_under_symlinked_config_home_blocks_writer(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    config_path = alias / ".claude.json"
    config_path.write_text('{"original": true}')
    lock_path = alias / ".claude.json.lock"
    lock_path.mkdir()
    started = threading.Event()

    def update(data: dict[str, Any]) -> bool:
        data["updated"] = True
        return True

    def invoke() -> bool:
        started.set()
        return update_claude_config(config_path, update)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(invoke)
        try:
            assert started.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
            config_path.write_text('{"concurrent": true}')
        finally:
            lock_path.rmdir()
        assert future.result(timeout=5) is True
    assert json.loads(config_path.read_text()) == {"concurrent": True, "updated": True}


def test_lock_is_released_if_heartbeat_cannot_start(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"retained": true}')
    with patch("gobby.claude_config.threading.Thread.start", side_effect=RuntimeError("no thread")):
        with pytest.raises(RuntimeError, match="no thread"):
            update_claude_config(config_path, lambda data: True)
    assert config_path.read_text() == '{"retained": true}'
    assert not config_path.with_suffix(".json.lock").exists()


def test_stale_native_lock_is_recovered(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    lock_path = tmp_path / "config.json.lock"
    lock_path.mkdir()
    old = time.time() - 11
    os.utime(lock_path, (old, old))

    def update(data: dict[str, Any]) -> bool:
        data["accepted"] = True
        return True

    assert update_claude_config(config_path, update) is True
    assert json.loads(config_path.read_text()) == {"accepted": True}
    assert not lock_path.exists()


def test_held_native_lock_times_out_without_read_or_write(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("not JSON")
    lock_path = tmp_path / "config.json.lock"
    lock_path.mkdir()
    with patch("gobby.claude_config._LOCK_TIMEOUT_SECONDS", 0.01):
        with pytest.raises(ClaudeConfigError, match="Timed out waiting"):
            update_claude_config(config_path, lambda data: True)
    assert config_path.read_text() == "not JSON"
    assert lock_path.is_dir()


def test_heartbeat_keeps_lock_fresh_during_update(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    lock_path = tmp_path / "config.json.lock"
    heartbeat = threading.Event()
    original_utime = os.utime

    def touch(path: Path, times: Any = None) -> None:
        original_utime(path, times)
        heartbeat.set()

    def update(data: dict[str, Any]) -> bool:
        before = lock_path.stat().st_mtime_ns
        assert heartbeat.wait(timeout=2)
        assert lock_path.stat().st_mtime_ns > before
        data["retained"] = True
        return True

    with (
        patch("gobby.claude_config._LOCK_UPDATE_SECONDS", 0.01),
        patch("gobby.claude_config.os.utime", side_effect=touch),
    ):
        assert update_claude_config(config_path, update) is True
    assert json.loads(config_path.read_text()) == {"retained": True}
    assert not lock_path.exists()


def test_compromised_lock_preserves_config_and_replacement_lock(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"original": true}')
    lock_path = tmp_path / "config.json.lock"
    old_lock_path = tmp_path / "old.lock"

    def update(data: dict[str, Any]) -> bool:
        lock_path.rename(old_lock_path)
        lock_path.mkdir()
        data["unsafe"] = True
        return True

    with pytest.raises(ClaudeConfigError, match="ownership changed"):
        update_claude_config(config_path, update)
    assert config_path.read_text() == '{"original": true}'
    assert lock_path.is_dir()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("failure", ["callback", "replace"])
def test_failed_update_preserves_original_and_releases_lock(tmp_path: Path, failure: str) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"original": true}')

    def update(data: dict[str, Any]) -> bool:
        data["partial"] = True
        if failure == "callback":
            raise ValueError("callback failed")
        return True

    with patch("gobby.claude_config.os.replace", side_effect=OSError("replace failed")):
        with pytest.raises((ValueError, ClaudeConfigError), match=f"{failure} failed"):
            update_claude_config(config_path, update)
    assert config_path.read_text() == '{"original": true}'
    assert not (tmp_path / "config.json.lock").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_updater_can_decline_persisting_changes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"original": true}')

    def update(data: dict[str, Any]) -> bool:
        data["partial"] = True
        return False

    assert update_claude_config(config_path, update) is False
    assert config_path.read_text() == '{"original": true}'
    assert not (tmp_path / "config.json.lock").exists()


@pytest.mark.parametrize("invalid", ["not JSON", "[]"])
def test_invalid_config_is_preserved_and_lock_released(tmp_path: Path, invalid: str) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(invalid)
    with pytest.raises(ClaudeConfigError, match="Failed to parse"):
        update_claude_config(config_path, lambda data: True)
    assert config_path.read_text() == invalid
    assert not (tmp_path / "config.json.lock").exists()


@pytest.mark.parametrize("redirect", ["config", "lock"])
def test_symlink_config_or_lock_is_rejected(tmp_path: Path, redirect: str) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"original": true}')
    config_path = tmp_path / "config.json"
    lock_path = tmp_path / "config.json.lock"
    if redirect == "config":
        config_path.symlink_to(target)
        message = "must not be a symlink"
    else:
        config_path.write_text('{"original": true}')
        lock_target = tmp_path / "target.lock"
        lock_target.mkdir()
        lock_path.symlink_to(lock_target, target_is_directory=True)
        message = "not a directory"
    with pytest.raises(ClaudeConfigError, match=message):
        update_claude_config(config_path, lambda data: True)
    assert target.read_text() == '{"original": true}'
    assert config_path.read_text() == '{"original": true}'
    if redirect == "lock":
        assert lock_path.is_symlink()


@pytest.mark.parametrize(
    "writer", ["trust", "isolation", "install", "remove", "install_project", "remove_project"]
)
def test_host_writers_wait_for_claude_lock_and_reread(
    tmp_path: Path, writer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    config_path = tmp_path / ".claude.json"
    initial: dict[str, Any] = (
        {"mcpServers": {"gobby": {"command": "existing"}}} if writer == "remove" else {}
    )
    if writer == "remove_project":
        initial = {"projects": {str(workspace.resolve()): {"mcpServers": {"gobby": {}}}}}
    if writer == "isolation":
        initial = {"projects": {str(workspace): {"mcpServers": {"other": {"command": "other"}}}}}
    config_path.write_text(json.dumps(initial))
    lock_path = tmp_path / ".claude.json.lock"
    lock_path.mkdir()
    started = threading.Event()

    def invoke() -> None:
        started.set()
        if writer == "trust":
            pre_approve_directory("claude", workspace)
        elif writer == "isolation":
            asyncio.run(_patch_mcp_config_for_isolation("/main", str(workspace), "claude"))
        elif writer == "install":
            assert configure_mcp_server_json(config_path)["success"] is True
        elif writer == "remove":
            assert remove_mcp_server_json(config_path)["success"] is True
        elif writer == "install_project":
            assert configure_project_mcp_server(workspace)["success"] is True
        else:
            assert remove_project_mcp_server(workspace)["success"] is True

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch.dict(os.environ, {"CLAUDE_CODE_CUSTOM_OAUTH_URL": ""}),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        future = executor.submit(invoke)
        try:
            assert started.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
            # The native lock holder saves another key before releasing the lock.
            config_path.write_text(json.dumps({**initial, "concurrentClaudeKey": "retained"}))
        finally:
            lock_path.rmdir()
        future.result(timeout=5)

    config = json.loads(config_path.read_text())
    assert config["concurrentClaudeKey"] == "retained"
    if writer == "trust":
        assert config["projects"][str(workspace)]["hasTrustDialogAccepted"] is True
    elif writer == "isolation":
        assert config["projects"][str(workspace)]["mcpServers"]["gobby"]["command"] == "uv"
        assert config["projects"][str(workspace)]["mcpServers"]["other"] == {"command": "other"}
    elif writer == "install":
        assert config["mcpServers"]["gobby"]["args"] == ["mcp-server"]
    elif writer == "remove":
        assert "mcpServers" not in config
    elif writer == "install_project":
        assert (
            config["projects"][str(workspace.resolve())]["mcpServers"]["gobby"]["type"] == "stdio"
        )
    else:
        assert "mcpServers" not in config.get("projects", {}).get(str(workspace.resolve()), {})
    assert not lock_path.exists()


@pytest.mark.parametrize("configured_home", ["", "~/custom-claude"])
def test_trust_uses_native_literal_config_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_home: str
) -> None:
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    config_path = (
        tmp_path / "~" / "custom-claude" / ".claude.json"
        if configured_home
        else tmp_path / ".config.json"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('{"theme": "dark"}')
    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch.dict(
            os.environ,
            {
                "HOME": str(tmp_path),
                "CLAUDE_CONFIG_DIR": configured_home,
                "CLAUDE_CODE_CUSTOM_OAUTH_URL": "",
            },
        ),
    ):
        pre_approve_directory("claude", workspace)

    config = json.loads(config_path.read_text())
    assert config["theme"] == "dark"
    assert config["projects"][str(workspace)]["hasTrustDialogAccepted"] is True


def test_default_global_trust_does_not_depend_on_transcript_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)
    transcript_target = tmp_path / "transcripts"
    transcript_target.mkdir()
    (tmp_path / ".claude").symlink_to(transcript_target, target_is_directory=True)
    with patch("pathlib.Path.home", return_value=tmp_path):
        pre_approve_directory("claude", tmp_path / "workspace")

    config = json.loads((tmp_path / ".claude.json").read_text())
    assert config["projects"][str(tmp_path / "workspace")]["hasTrustDialogAccepted"] is True

"""Tests for workspace trust pre-approval."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import tomllib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from typing import Any
from unittest.mock import patch

import pytest

from gobby.agents import trust
from gobby.agents.trust import (
    _MODEL_DISCOVERY_TRUST_LOCKS,
    TrustSeedResult,
    _encode_claude_project_path,
    authorize_model_discovery_trust,
    pre_approve_directory,
    seed_gobby_home_trust,
)


class TestEncodePath:
    def test_basic_path(self) -> None:
        assert (
            _encode_claude_project_path("/Users/josh/Projects/gobby")
            == "-Users-josh-Projects-gobby"
        )

    def test_clone_path(self) -> None:
        assert (
            _encode_claude_project_path("/private/tmp/gobby-clones/9990-2048-game")
            == "-private-tmp-gobby-clones-9990-2048-game"
        )

    def test_worktree_path(self) -> None:
        assert (
            _encode_claude_project_path("/private/tmp/gobby-worktrees/gobby-task-9395")
            == "-private-tmp-gobby-worktrees-gobby-task-9395"
        )

    def test_hidden_directory_dots_replaced_with_dashes(self) -> None:
        """Dots must become dashes to match Claude Code's actual encoding."""
        assert (
            _encode_claude_project_path("/Users/josh/.gobby/clones/epic-9915")
            == "-Users-josh--gobby-clones-epic-9915"
        )

    def test_windows_style_path(self) -> None:
        assert (
            _encode_claude_project_path(r"C:\Users\josh\.gobby\clones\task")
            == "C--Users-josh--gobby-clones-task"
        )


class TestPreApproveClaude:
    def test_creates_project_directory(self, tmp_path: Path) -> None:
        clone_dir = "/private/tmp/gobby-clones/test-task"
        claude_projects = tmp_path / ".claude" / "projects"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("claude", clone_dir)

        expected = claude_projects / "-private-tmp-gobby-clones-test-task"
        assert expected.is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("claude", clone_dir)
            pre_approve_directory("claude", clone_dir)

        expected = tmp_path / ".claude" / "projects" / "-private-tmp-gobby-clones-test-task"
        assert expected.is_dir()

    def test_resolves_symlinks(self, tmp_path: Path) -> None:
        """On macOS /tmp -> /private/tmp; both paths should get trust entries."""
        clone_dir = "/tmp/gobby-clones/symlink-test"
        resolved_dir = "/private/tmp/gobby-clones/symlink-test"

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch("gobby.agents.trust.os.path.realpath", return_value=resolved_dir),
        ):
            pre_approve_directory("claude", clone_dir)

        projects = tmp_path / ".claude" / "projects"
        assert (projects / "-tmp-gobby-clones-symlink-test").is_dir()
        assert (projects / "-private-tmp-gobby-clones-symlink-test").is_dir()

    def test_install_trust_creates_gobby_home_project_directory(self, tmp_path: Path) -> None:
        gobby_home = tmp_path / ".gobby"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = seed_gobby_home_trust("claude", gobby_home=gobby_home)

        assert result["success"] is True
        expected = tmp_path / ".claude" / "projects" / _encode_claude_project_path(gobby_home)
        assert expected.is_dir()


class TestPreApproveQwen:
    def test_creates_projects_json(self, tmp_path: Path) -> None:
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("qwen", clone_dir)

        projects_file = tmp_path / ".qwen" / "projects.json"
        assert projects_file.exists()
        data = json.loads(projects_file.read_text())
        assert clone_dir in data["projects"]
        assert data["projects"][clone_dir] == "test-task"

    def test_creates_trusted_folders_json(self, tmp_path: Path) -> None:
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("qwen", clone_dir)

        trust_file = tmp_path / ".qwen" / "trustedFolders.json"
        assert trust_file.exists()
        data = json.loads(trust_file.read_text())
        assert data[clone_dir] == "TRUST_PARENT"

    def test_preserves_existing_entries(self, tmp_path: Path) -> None:
        qwen_dir = tmp_path / ".qwen"
        qwen_dir.mkdir()
        projects_file = qwen_dir / "projects.json"
        projects_file.write_text(json.dumps({"projects": {"/existing/path": "existing"}}))
        trust_file = qwen_dir / "trustedFolders.json"
        trust_file.write_text(json.dumps({"/existing/path": "TRUST_FOLDER"}))

        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("qwen", clone_dir)

        data = json.loads(projects_file.read_text())
        assert "/existing/path" in data["projects"]
        assert clone_dir in data["projects"]

        trusted = json.loads(trust_file.read_text())
        assert trusted["/existing/path"] == "TRUST_FOLDER"
        assert trusted[clone_dir] == "TRUST_PARENT"

    def test_idempotent(self, tmp_path: Path) -> None:
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("qwen", clone_dir)
            pre_approve_directory("qwen", clone_dir)

        projects_file = tmp_path / ".qwen" / "projects.json"
        data = json.loads(projects_file.read_text())
        assert data["projects"][clone_dir] == "test-task"

        trust_file = tmp_path / ".qwen" / "trustedFolders.json"
        trusted = json.loads(trust_file.read_text())
        assert trusted[clone_dir] == "TRUST_PARENT"

    def test_json_writes_are_atomic(self, tmp_path: Path) -> None:
        clone_dir = "/private/tmp/gobby-clones/test-task"
        replace_calls: list[tuple[Path, Path]] = []
        original_replace = os.replace

        def tracking_replace(src: str, dst: str) -> None:
            replace_calls.append((Path(src), Path(dst)))
            original_replace(src, dst)

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch("gobby.agents.trust.os.replace", side_effect=tracking_replace),
        ):
            pre_approve_directory("qwen", clone_dir)

        assert {dst.name for _, dst in replace_calls} == {
            "projects.json",
            "trustedFolders.json",
        }
        assert not list((tmp_path / ".qwen").glob("*.tmp"))

    def test_concurrent_pre_approve_preserves_all_paths(self, tmp_path: Path) -> None:
        clone_dirs = [f"/private/tmp/gobby-clones/task-{index}" for index in range(12)]
        original_load = trust._load_json_object
        project_load_condition = threading.Condition()
        project_loads = 0
        release_project_loads = False

        def delayed_load(path: Path, *, reset_label: str) -> dict[str, Any]:
            nonlocal project_loads, release_project_loads

            data = original_load(path, reset_label=reset_label)
            if path.name == "projects.json":
                with project_load_condition:
                    if not release_project_loads:
                        project_loads += 1
                        if project_loads == len(clone_dirs):
                            release_project_loads = True
                            project_load_condition.notify_all()
                        else:
                            project_load_condition.wait_for(
                                lambda: release_project_loads,
                                timeout=0.2,
                            )
                            release_project_loads = True
                            project_load_condition.notify_all()
            return data

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch("gobby.agents.trust._load_json_object", side_effect=delayed_load),
            ThreadPoolExecutor(max_workers=len(clone_dirs)) as executor,
        ):
            list(
                executor.map(lambda clone_dir: pre_approve_directory("qwen", clone_dir), clone_dirs)
            )

        projects_file = tmp_path / ".qwen" / "projects.json"
        trust_file = tmp_path / ".qwen" / "trustedFolders.json"
        projects = json.loads(projects_file.read_text())["projects"]
        trusted = json.loads(trust_file.read_text())

        assert set(projects) == set(clone_dirs)
        assert trusted == dict.fromkeys(clone_dirs, "TRUST_PARENT")

    def test_install_trust_uses_configured_and_real_gobby_home(self, tmp_path: Path) -> None:
        configured = "/tmp/gobby-home-link"
        resolved = "/private/tmp/gobby-home-real"

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch("gobby.agents.trust.os.path.realpath", return_value=resolved),
        ):
            result = seed_gobby_home_trust("qwen", gobby_home=configured)

        assert result["paths"] == [configured, resolved]
        projects = json.loads((tmp_path / ".qwen" / "projects.json").read_text())
        trusted = json.loads((tmp_path / ".qwen" / "trustedFolders.json").read_text())
        assert set(projects["projects"]) == {configured, resolved}
        assert trusted[configured] == "TRUST_PARENT"
        assert trusted[resolved] == "TRUST_PARENT"

    def test_install_trust_does_not_force_enable_folder_trust(self, tmp_path: Path) -> None:
        qwen_home = tmp_path / ".qwen"
        qwen_home.mkdir()
        settings_file = qwen_home / "settings.json"
        settings = {"security": {"folderTrust": False}, "general": {"enableHooks": True}}
        settings_file.write_text(json.dumps(settings))

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = seed_gobby_home_trust("qwen", gobby_home="/Users/josh/.gobby")

        assert result["success"] is True
        assert (qwen_home / "projects.json").exists()
        assert not (qwen_home / "trustedFolders.json").exists()
        assert json.loads(settings_file.read_text()) == settings
        assert any(entry["status"] == "skipped" for entry in result["entries"])


class TestModelDiscoveryTrust:
    @pytest.fixture(autouse=True)
    def clear_model_discovery_locks(self) -> Iterator[None]:
        _MODEL_DISCOVERY_TRUST_LOCKS.clear()
        yield
        _MODEL_DISCOVERY_TRUST_LOCKS.clear()

    @pytest.mark.asyncio
    async def test_qwen_authorization_writes_only_qwen_stores(self, tmp_path: Path) -> None:
        discovery_cwd = (tmp_path / "gobby-home" / "provider-model-discovery" / "qwen").resolve()
        discovery_cwd.mkdir(parents=True)

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = await authorize_model_discovery_trust("qwen", discovery_cwd)

        assert result.success is True
        assert result.skipped is False
        projects_file = tmp_path / ".qwen" / "projects.json"
        trust_file = tmp_path / ".qwen" / "trustedFolders.json"
        projects = json.loads(projects_file.read_text())
        trusted = json.loads(trust_file.read_text())
        assert projects["projects"] == {os.fspath(discovery_cwd): "qwen"}
        assert trusted == {os.fspath(discovery_cwd): "TRUST_PARENT"}
        assert not (tmp_path / ".claude").exists()
        assert not (tmp_path / ".codex").exists()

    @pytest.mark.asyncio
    async def test_folder_trust_disabled_skips_trusted_folders_write(
        self,
        tmp_path: Path,
    ) -> None:
        qwen_home = tmp_path / ".qwen"
        qwen_home.mkdir()
        settings_file = qwen_home / "settings.json"
        settings_file.write_text(json.dumps({"security": {"folderTrust": False}}))
        discovery_cwd = (tmp_path / "gobby-home" / "provider-model-discovery" / "qwen").resolve()
        discovery_cwd.mkdir(parents=True)

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = await authorize_model_discovery_trust("qwen", discovery_cwd)

        assert result.success is True
        assert result.skipped is False
        assert (qwen_home / "projects.json").exists()
        assert not (qwen_home / "trustedFolders.json").exists()
        assert any(
            entry["store"] == "trusted_folders" and entry["status"] == "skipped"
            for entry in result.entries
        )

    @pytest.mark.parametrize("cli", ["claude", "codex", "droid", "unknown"])
    @pytest.mark.asyncio
    async def test_unsupported_cli_skips_without_writes(self, cli: str, tmp_path: Path) -> None:
        discovery_cwd = tmp_path / "gobby-home" / "provider-model-discovery" / cli

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = await authorize_model_discovery_trust(cli, discovery_cwd)

        assert result.success is True
        assert result.skipped is True
        assert (
            result.reason
            == f"Unsupported CLI for model discovery trust: {cli}; supported CLIs: qwen"
        )
        assert not result.entries
        assert not result.files_written
        assert not (tmp_path / ".claude").exists()
        assert not (tmp_path / ".codex").exists()
        assert not (tmp_path / ".droid").exists()
        assert not (tmp_path / ".qwen").exists()

    @pytest.mark.asyncio
    async def test_same_cli_authorizations_are_serialized(self, tmp_path: Path) -> None:
        active = 0
        max_active = 0
        counter_lock = threading.Lock()
        first_started = threading.Event()
        release_seed = threading.Event()
        waits_completed: list[bool] = []

        def slow_seed(
            cli: str,
            directory: os.PathLike[str],
            *,
            respect_folder_trust_setting: bool,
        ) -> TrustSeedResult:
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                if Path(directory).name == "a":
                    first_started.set()
                    waits_completed.append(release_seed.wait(timeout=1))
                return TrustSeedResult(cli=cli, paths=[os.fspath(directory)])
            finally:
                with counter_lock:
                    active -= 1

        with patch("gobby.agents.trust.seed_cli_trust", side_effect=slow_seed):
            first_task = asyncio.create_task(
                authorize_model_discovery_trust("qwen", tmp_path / "a")
            )
            assert await asyncio.to_thread(first_started.wait, 1)
            second_task = asyncio.create_task(
                authorize_model_discovery_trust("qwen", tmp_path / "b")
            )
            await asyncio.to_thread(lambda: None)
            release_seed.set()
            first, second = await asyncio.gather(first_task, second_task)

        assert first.success is True
        assert second.success is True
        assert waits_completed == [True]
        assert max_active == 1


class TestCodexTrust:
    def test_codex_pre_approves_workspace_path(self, tmp_path: Path) -> None:
        """Runtime Codex workspace trust is seeded for the spawned workspace."""
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("codex", clone_dir)

        config_file = tmp_path / ".codex" / "config.toml"
        parsed = tomllib.loads(config_file.read_text())
        assert parsed["projects"][clone_dir]["trust_level"] == "trusted"

    def test_install_trust_writes_codex_toml_projects(self, tmp_path: Path) -> None:
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config_file = codex_home / "config.toml"
        config_file.write_text('model = "gpt-5"\n\n[features]\ncodex_hooks = true\n')
        gobby_home = PureWindowsPath("C:/Users/josh/.gobby")

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = seed_gobby_home_trust("codex", gobby_home=gobby_home)

        assert result["success"] is True
        config_text = config_file.read_text()
        assert '[projects."C:\\\\Users\\\\josh\\\\.gobby"]' in config_text
        parsed = tomllib.loads(config_text)
        assert parsed["features"]["codex_hooks"] is True
        assert parsed["model"] == "gpt-5"
        assert parsed["projects"][r"C:\Users\josh\.gobby"]["trust_level"] == "trusted"


class TestDroidNoop:
    def test_droid_is_noop_with_debug_log(self, tmp_path: Path, caplog) -> None:
        """Droid uses --auto for spawned-agent permissions, so no trust file is written."""
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            caplog.at_level("DEBUG", logger="gobby.agents.trust"),
        ):
            pre_approve_directory("droid", clone_dir)

        assert "Droid workspace trust pre-approval is a no-op" in caplog.text
        assert not (tmp_path / ".factory").exists()
        assert not (tmp_path / ".claude").exists()

    def test_install_trust_returns_noop_result(self, tmp_path: Path) -> None:
        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = seed_gobby_home_trust("droid", gobby_home=tmp_path / ".gobby")

        assert result["success"] is True
        assert result["skipped"] is True
        assert "trusted-folder store" in result["reason"]
        assert not (tmp_path / ".factory").exists()

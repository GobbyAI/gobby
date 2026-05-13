"""Tests for workspace trust pre-approval."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

import pytest

from gobby.agents.trust import (
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


class TestPreApproveGemini:
    def test_creates_projects_json(self, tmp_path: Path) -> None:
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("gemini", clone_dir)

        projects_file = tmp_path / ".gemini" / "projects.json"
        assert projects_file.exists()
        data = json.loads(projects_file.read_text())
        assert clone_dir in data["projects"]
        assert data["projects"][clone_dir] == "test-task"

    def test_creates_trusted_folders_json(self, tmp_path: Path) -> None:
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("gemini", clone_dir)

        trust_file = tmp_path / ".gemini" / "trustedFolders.json"
        assert trust_file.exists()
        data = json.loads(trust_file.read_text())
        assert data[clone_dir] == "TRUST_PARENT"

    def test_preserves_existing_entries(self, tmp_path: Path) -> None:
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        projects_file = gemini_dir / "projects.json"
        projects_file.write_text(json.dumps({"projects": {"/existing/path": "existing"}}))
        trust_file = gemini_dir / "trustedFolders.json"
        trust_file.write_text(json.dumps({"/existing/path": "TRUST_FOLDER"}))

        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("gemini", clone_dir)

        data = json.loads(projects_file.read_text())
        assert "/existing/path" in data["projects"]
        assert clone_dir in data["projects"]

        trusted = json.loads(trust_file.read_text())
        assert trusted["/existing/path"] == "TRUST_FOLDER"
        assert trusted[clone_dir] == "TRUST_PARENT"

    def test_idempotent(self, tmp_path: Path) -> None:
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("gemini", clone_dir)
            pre_approve_directory("gemini", clone_dir)

        projects_file = tmp_path / ".gemini" / "projects.json"
        data = json.loads(projects_file.read_text())
        assert data["projects"][clone_dir] == "test-task"

        trust_file = tmp_path / ".gemini" / "trustedFolders.json"
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
            pre_approve_directory("gemini", clone_dir)

        assert {dst.name for _, dst in replace_calls} == {
            "projects.json",
            "trustedFolders.json",
        }
        assert not list((tmp_path / ".gemini").glob("*.tmp"))

    def test_install_trust_uses_configured_and_real_gobby_home(self, tmp_path: Path) -> None:
        configured = "/tmp/gobby-home-link"
        resolved = "/private/tmp/gobby-home-real"

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch("gobby.agents.trust.os.path.realpath", return_value=resolved),
        ):
            result = seed_gobby_home_trust("gemini", gobby_home=configured)

        assert result["paths"] == [configured, resolved]
        projects = json.loads((tmp_path / ".gemini" / "projects.json").read_text())
        trusted = json.loads((tmp_path / ".gemini" / "trustedFolders.json").read_text())
        assert set(projects["projects"]) == {configured, resolved}
        assert trusted[configured] == "TRUST_PARENT"
        assert trusted[resolved] == "TRUST_PARENT"

    def test_install_trust_does_not_force_enable_folder_trust(self, tmp_path: Path) -> None:
        gemini_home = tmp_path / ".gemini"
        gemini_home.mkdir()
        settings_file = gemini_home / "settings.json"
        settings = {"security": {"folderTrust": False}, "general": {"enableHooks": True}}
        settings_file.write_text(json.dumps(settings))

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = seed_gobby_home_trust("gemini", gobby_home="/Users/josh/.gobby")

        assert result["success"] is True
        assert (gemini_home / "projects.json").exists()
        assert not (gemini_home / "trustedFolders.json").exists()
        assert json.loads(settings_file.read_text()) == settings
        assert any(entry["status"] == "skipped" for entry in result["entries"])


class TestPreApproveQwen:
    def test_install_trust_writes_qwen_json_stores(self, tmp_path: Path) -> None:
        gobby_home = "/Users/josh/.gobby"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = seed_gobby_home_trust("qwen", gobby_home=gobby_home)

        assert result["success"] is True
        projects = json.loads((tmp_path / ".qwen" / "projects.json").read_text())
        trusted = json.loads((tmp_path / ".qwen" / "trustedFolders.json").read_text())
        assert projects["projects"][gobby_home] == ".gobby"
        assert trusted[gobby_home] == "TRUST_PARENT"


class TestModelDiscoveryTrust:
    def test_gemini_authorization_writes_only_gemini_stores(self, tmp_path: Path) -> None:
        discovery_cwd = (tmp_path / "gobby-home" / "provider-model-discovery" / "gemini").resolve()
        discovery_cwd.mkdir(parents=True)

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = authorize_model_discovery_trust("gemini", discovery_cwd)

        assert result.success is True
        assert result.skipped is False
        projects_file = tmp_path / ".gemini" / "projects.json"
        trust_file = tmp_path / ".gemini" / "trustedFolders.json"
        projects = json.loads(projects_file.read_text())
        trusted = json.loads(trust_file.read_text())
        assert projects["projects"] == {os.fspath(discovery_cwd): "gemini"}
        assert trusted == {os.fspath(discovery_cwd): "TRUST_PARENT"}
        assert not (tmp_path / ".qwen").exists()
        assert not (tmp_path / ".claude").exists()
        assert not (tmp_path / ".codex").exists()

    def test_qwen_authorization_writes_only_qwen_stores(self, tmp_path: Path) -> None:
        discovery_cwd = (tmp_path / "gobby-home" / "provider-model-discovery" / "qwen").resolve()
        discovery_cwd.mkdir(parents=True)

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = authorize_model_discovery_trust("qwen", discovery_cwd)

        assert result.success is True
        assert result.skipped is False
        projects_file = tmp_path / ".qwen" / "projects.json"
        trust_file = tmp_path / ".qwen" / "trustedFolders.json"
        projects = json.loads(projects_file.read_text())
        trusted = json.loads(trust_file.read_text())
        assert projects["projects"] == {os.fspath(discovery_cwd): "qwen"}
        assert trusted == {os.fspath(discovery_cwd): "TRUST_PARENT"}
        assert not (tmp_path / ".gemini").exists()
        assert not (tmp_path / ".claude").exists()
        assert not (tmp_path / ".codex").exists()

    def test_folder_trust_disabled_skips_trusted_folders_write(
        self,
        tmp_path: Path,
    ) -> None:
        gemini_home = tmp_path / ".gemini"
        gemini_home.mkdir()
        settings_file = gemini_home / "settings.json"
        settings_file.write_text(json.dumps({"security": {"folderTrust": False}}))
        discovery_cwd = (tmp_path / "gobby-home" / "provider-model-discovery" / "gemini").resolve()
        discovery_cwd.mkdir(parents=True)

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = authorize_model_discovery_trust("gemini", discovery_cwd)

        assert result.success is True
        assert result.skipped is False
        assert (gemini_home / "projects.json").exists()
        assert not (gemini_home / "trustedFolders.json").exists()
        assert any(
            entry["store"] == "trusted_folders" and entry["status"] == "skipped"
            for entry in result.entries
        )

    @pytest.mark.parametrize("cli", ["claude", "codex", "droid", "unknown"])
    def test_unsupported_cli_skips_without_writes(self, cli: str, tmp_path: Path) -> None:
        discovery_cwd = tmp_path / "gobby-home" / "provider-model-discovery" / cli

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = authorize_model_discovery_trust(cli, discovery_cwd)

        assert result.success is True
        assert result.skipped is True
        assert result.reason == f"Unsupported CLI for model discovery trust: {cli}"
        assert not result.entries
        assert not result.files_written
        assert not (tmp_path / ".claude").exists()
        assert not (tmp_path / ".codex").exists()
        assert not (tmp_path / ".droid").exists()
        assert not (tmp_path / ".gemini").exists()
        assert not (tmp_path / ".qwen").exists()


class TestCodexNoop:
    def test_codex_is_noop(self, tmp_path: Path) -> None:
        """Runtime Codex workspace trust is seeded at install time, not per spawn."""
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("codex", clone_dir)

        # Should not create any files
        assert not (tmp_path / ".claude").exists()
        assert not (tmp_path / ".gemini").exists()
        assert not (tmp_path / ".codex").exists()

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
        assert not (tmp_path / ".gemini").exists()

    def test_install_trust_returns_noop_result(self, tmp_path: Path) -> None:
        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = seed_gobby_home_trust("droid", gobby_home=tmp_path / ".gobby")

        assert result["success"] is True
        assert result["skipped"] is True
        assert "trusted-folder store" in result["reason"]
        assert not (tmp_path / ".factory").exists()

"""Tests for workspace trust pre-approval."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

import pytest

from gobby.agents import trust
from gobby.agents.trust import (
    encode_claude_project_path,
    pre_approve_directory,
    seed_gobby_home_trust,
)


class TestEncodePath:
    @pytest.mark.parametrize(
        "directory,expected",
        [
            ("/work/my_project", "-work-my-project"),
            ("/work/my project", "-work-my-project"),
            ("/work/café@team", "-work-caf--team"),
        ],
    )
    def test_non_alphanumeric_characters(self, directory: str, expected: str) -> None:
        assert encode_claude_project_path(directory) == expected

    def test_basic_path(self) -> None:
        assert (
            encode_claude_project_path("/Users/josh/Projects/gobby") == "-Users-josh-Projects-gobby"
        )

    def test_clone_path(self) -> None:
        assert (
            encode_claude_project_path("/private/tmp/gobby-clones/9990-2048-game")
            == "-private-tmp-gobby-clones-9990-2048-game"
        )

    def test_worktree_path(self) -> None:
        assert (
            encode_claude_project_path("/private/tmp/gobby-worktrees/gobby-task-9395")
            == "-private-tmp-gobby-worktrees-gobby-task-9395"
        )

    def test_hidden_directory_dots_replaced_with_dashes(self) -> None:
        """Dots must become dashes to match Claude Code's actual encoding."""
        assert (
            encode_claude_project_path("/Users/josh/.gobby/clones/epic-9915")
            == "-Users-josh--gobby-clones-epic-9915"
        )

    def test_windows_style_path(self) -> None:
        assert (
            encode_claude_project_path(r"C:\Users\josh\.gobby\clones\task")
            == "C--Users-josh--gobby-clones-task"
        )


class TestPreApproveClaude:
    @pytest.mark.parametrize(
        "config_variant", ["default", "custom", "legacy", "custom-legacy", "oauth", "custom-oauth"]
    )
    def test_seeds_persisted_trust_for_workspace_and_resolved_path(
        self, tmp_path: Path, config_variant: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(workspace, target_is_directory=True)
        custom_config = config_variant.startswith("custom")
        legacy = "legacy" in config_variant
        oauth = "oauth" in config_variant
        config_dir = tmp_path / "custom-claude" if custom_config else tmp_path / ".claude"
        config_dir.mkdir()
        config_name = ".claude-custom-oauth.json" if oauth else ".claude.json"
        config_path = (
            config_dir / ".config.json"
            if legacy
            else (config_dir if custom_config else tmp_path) / config_name
        )
        projects = {
            str(alias): {"hasTrustDialogAccepted": False, "allowedTools": ["Read"]},
            "/other/workspace": {"hasTrustDialogAccepted": True},
        }
        original = {
            "theme": "dark",
            "projects": projects,
        }
        config_path.write_text(json.dumps(original))
        # An existing transcript directory must not skip persisted acceptance.
        (config_dir / "projects" / encode_claude_project_path(alias)).mkdir(parents=True)

        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        selectors = {
            "CLAUDE_CODE_CUSTOM_OAUTH_URL": "https://example.invalid" if oauth else "",
        }
        if custom_config:
            selectors["CLAUDE_CONFIG_DIR"] = str(config_dir)
        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch.dict(os.environ, selectors),
        ):
            pre_approve_directory("claude", alias)

        expected = {
            **original,
            "projects": {
                **projects,
                str(alias): {"hasTrustDialogAccepted": True, "allowedTools": ["Read"]},
                str(workspace.resolve()): {"hasTrustDialogAccepted": True},
            },
        }
        assert json.loads(config_path.read_text()) == expected

    def test_install_trust_seeds_persisted_gobby_home_acceptance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gobby_home = tmp_path / ".gobby"
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch.dict(os.environ, {"CLAUDE_CODE_CUSTOM_OAUTH_URL": ""}),
        ):
            result = seed_gobby_home_trust("claude", gobby_home=gobby_home)

        assert result["success"] is True
        config_path = tmp_path / ".claude.json"
        config = json.loads(config_path.read_text())
        assert config["projects"][str(gobby_home)]["hasTrustDialogAccepted"] is True
        assert str(config_path) in result["files_written"]

    @pytest.mark.parametrize("invalid", ["json", "root", "projects", "entry", "symlink"])
    def test_invalid_config_is_preserved(
        self, tmp_path: Path, invalid: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = tmp_path / "workspace"
        config_path = tmp_path / ".claude.json"
        content = {
            "json": "{invalid",
            "root": "[]",
            "projects": '{"projects": []}',
            "entry": json.dumps({"projects": {str(workspace): []}}),
            "symlink": '{"theme": "dark"}',
        }[invalid]
        source = tmp_path / "redirected.json" if invalid == "symlink" else config_path
        source.write_text(content)
        if invalid == "symlink":
            config_path.symlink_to(source)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch.dict(os.environ, {"CLAUDE_CODE_CUSTOM_OAUTH_URL": ""}),
        ):
            result = seed_gobby_home_trust("claude", gobby_home=workspace)

        assert result["success"] is False
        assert result["errors"]
        assert result["files_written"] == []
        assert source.read_text() == content

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
        """A workspace symlink and its resolved target both get trust entries."""
        resolved_dir = tmp_path / "workspace"
        resolved_dir.mkdir()
        clone_dir = tmp_path / "alias"
        clone_dir.symlink_to(resolved_dir, target_is_directory=True)

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("claude", clone_dir)

        projects = tmp_path / ".claude" / "projects"
        assert (projects / encode_claude_project_path(clone_dir)).is_dir()
        assert (projects / encode_claude_project_path(resolved_dir.resolve())).is_dir()

    def test_install_trust_creates_gobby_home_project_directory(self, tmp_path: Path) -> None:
        gobby_home = tmp_path / ".gobby"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            result = seed_gobby_home_trust("claude", gobby_home=gobby_home)

        assert result["success"] is True
        expected = tmp_path / ".claude" / "projects" / encode_claude_project_path(gobby_home)
        assert expected.is_dir()


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

    def test_existing_codex_trust_skips_tomlkit_parse(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config_file = codex_home / "config.toml"
        config_file.write_text(
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch(
                "gobby.agents.trust._load_toml_config",
                side_effect=AssertionError("slow parser used"),
            ),
        ):
            pre_approve_directory("codex", workspace)

        assert (
            tomllib.loads(config_file.read_text(encoding="utf-8"))["projects"][str(workspace)][
                "trust_level"
            ]
            == "trusted"
        )

    def test_new_codex_trust_prunes_stale_generated_projects_without_tomlkit(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / ".gobby" / "worktrees" / "active"
        workspace.mkdir(parents=True)
        stale_workspace = tmp_path / ".gobby" / "worktrees" / "stale"
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config_file = codex_home / "config.toml"
        config_file.write_text(
            'model = "gpt-5"\n\n'
            f"[projects.{json.dumps(str(stale_workspace))}]\n"
            'trust_level = "trusted"\n\n'
            "[features]\n"
            "codex_hooks = true\n",
            encoding="utf-8",
        )

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch(
                "gobby.agents.trust._load_toml_config",
                side_effect=AssertionError("slow parser used"),
            ),
        ):
            pre_approve_directory("codex", workspace)

        parsed = tomllib.loads(config_file.read_text(encoding="utf-8"))
        assert parsed["model"] == "gpt-5"
        assert parsed["features"]["codex_hooks"] is True
        assert str(stale_workspace) not in parsed["projects"]
        assert parsed["projects"][str(workspace)]["trust_level"] == "trusted"

    def test_new_codex_trust_prunes_stale_clones_and_tmp_gobby_projects(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / ".gobby" / "clones" / "gobby" / "active"
        workspace.mkdir(parents=True)
        unique = os.urandom(6).hex()
        stale = [
            str(tmp_path / ".gobby" / "clones" / "gobby" / "stale"),
            f"/tmp/gobby-review-{unique}",
            f"/tmp/gobby-close-{unique}/checkout",
        ]
        kept = [f"/tmp/user-scratch-{unique}", f"/opt/user-project-{unique}"]
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config_file = codex_home / "config.toml"
        config_file.write_text(
            "".join(
                f'[projects.{json.dumps(path)}]\ntrust_level = "trusted"\n\n'
                for path in [*stale, *kept]
            ),
            encoding="utf-8",
        )

        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch(
                "gobby.agents.trust.tempfile.gettempdir",
                return_value=str(tmp_path / "unrelated-tempdir"),
            ),
        ):
            pre_approve_directory("codex", workspace)

        projects = tomllib.loads(config_file.read_text(encoding="utf-8"))["projects"]
        assert all(path not in projects for path in stale)
        assert all(projects[path]["trust_level"] == "trusted" for path in kept)
        assert projects[str(workspace)]["trust_level"] == "trusted"

    def test_missing_generated_root_does_not_mark_children_stale(self, tmp_path: Path) -> None:
        """An absent or unmounted root proves nothing about its children; never prune them."""
        with (
            patch("gobby.agents.trust.Path.home", return_value=tmp_path),
            patch(
                "gobby.agents.trust.tempfile.gettempdir",
                return_value=str(tmp_path / "unrelated-tempdir"),
            ),
        ):
            assert not trust.is_stale_generated_path(
                str(tmp_path / ".gobby" / "worktrees" / "gobby" / "x")
            )
            (tmp_path / ".gobby" / "worktrees").mkdir(parents=True)
            assert trust.is_stale_generated_path(
                str(tmp_path / ".gobby" / "worktrees" / "gobby" / "x")
            )


class TestDroidNoop:
    def test_droid_is_noop_with_debug_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
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

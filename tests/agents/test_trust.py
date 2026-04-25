"""Tests for workspace trust pre-approval."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from gobby.agents.trust import (
    _encode_claude_project_path,
    pre_approve_directory,
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


class TestCodexNoop:
    def test_codex_is_noop(self, tmp_path: Path) -> None:
        """Codex uses daemon-owned sandbox flags; no trust pre-approval needed."""
        clone_dir = "/private/tmp/gobby-clones/test-task"

        with patch("gobby.agents.trust.Path.home", return_value=tmp_path):
            pre_approve_directory("codex", clone_dir)

        # Should not create any files
        assert not (tmp_path / ".claude").exists()
        assert not (tmp_path / ".gemini").exists()

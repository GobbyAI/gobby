"""Tests for gobby.sync.integrity module."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gobby.install.manifest import (
    build_bundled_content_manifest,
    hash_file_bytes,
    load_bundled_content_manifest,
    write_bundled_content_manifest,
)
from gobby.storage.workflow_definitions import compute_definition_hash
from gobby.sync.integrity import (
    BUNDLED_SYNC_CONTENT_TYPES,
    CONTENT_TYPE_DIRS,
    IntegrityResult,
    _to_shared_relative_path,
    get_dirty_content_types,
    verify_bundled_integrity,
)

pytestmark = pytest.mark.unit


class TestIntegrityResult:
    """Tests for the IntegrityResult dataclass."""

    def test_all_clean_when_empty(self) -> None:
        result = IntegrityResult()
        assert result.all_clean is True

    def test_not_clean_with_dirty_files(self) -> None:
        result = IntegrityResult(dirty_files=["some/file.yaml"])
        assert result.all_clean is False

    def test_not_clean_with_untracked_files(self) -> None:
        result = IntegrityResult(untracked_files=["some/new.yaml"])
        assert result.all_clean is False

    def test_defaults(self) -> None:
        result = IntegrityResult()
        assert result.clean_files == []
        assert result.dirty_files == []
        assert result.untracked_files == []
        assert result.errors == []
        assert result.git_available is True
        assert result.checked is False
        assert result.source == "none"


class TestBundledContentManifest:
    """Tests for deterministic raw-byte manifest generation."""

    def test_manifest_generation_is_deterministic_and_hashes_raw_bytes(
        self, tmp_path: Path
    ) -> None:
        shared = tmp_path / "shared"
        (shared / "skills" / "z").mkdir(parents=True)
        (shared / "skills" / "a").mkdir(parents=True)
        z_file = shared / "skills" / "z" / "SKILL.md"
        a_file = shared / "skills" / "a" / "SKILL.md"
        z_file.write_bytes(b"z-content\n")
        a_file.write_bytes(b"a-content\n")
        (shared / "skills" / "__pycache__").mkdir()
        (shared / "skills" / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
        (shared / "skills" / "ignored.pyo").write_bytes(b"cache")

        manifest = build_bundled_content_manifest(shared)

        assert list(manifest["files"]) == [
            "skills/a/SKILL.md",
            "skills/z/SKILL.md",
        ]
        assert manifest["files"]["skills/a/SKILL.md"] == hashlib.sha256(b"a-content\n").hexdigest()
        assert manifest == build_bundled_content_manifest(shared)

    def test_manifest_generation_excludes_symlinks(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"
        skill_dir = shared / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        target = skill_dir / "target.md"
        target.write_text("target", encoding="utf-8")
        (skill_dir / "linked.md").symlink_to(target)

        manifest = build_bundled_content_manifest(shared)

        assert list(manifest["files"]) == ["skills/demo/target.md"]

    def test_manifest_load_read_error_returns_validation_error(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "bundled_content_manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")

        with patch("pathlib.Path.read_text", side_effect=OSError("denied")):
            files, errors = load_bundled_content_manifest(tmp_path)

        assert files is None
        assert errors == ["Failed to read bundled content manifest: denied"]

    def test_hash_distinguishes_yaml_reformat_from_normalized_definition_hash(
        self, tmp_path: Path
    ) -> None:
        first = tmp_path / "first.yaml"
        second = tmp_path / "second.yaml"
        first.write_text("name: demo\ntype: pipeline\nsteps: []\n", encoding="utf-8")
        second.write_text("name: demo\n\ntype: pipeline\nsteps: []\n", encoding="utf-8")

        first_definition = json.dumps(yaml.safe_load(first.read_text(encoding="utf-8")))
        second_definition = json.dumps(yaml.safe_load(second.read_text(encoding="utf-8")))

        assert hash_file_bytes(first) != hash_file_bytes(second)
        assert compute_definition_hash(first_definition) == compute_definition_hash(
            second_definition
        )


class TestVerifyBundledIntegrity:
    """Tests for verify_bundled_integrity."""

    def test_missing_shared_dir(self, tmp_path: Path) -> None:
        """Returns error when shared/ dir doesn't exist."""
        result = verify_bundled_integrity(tmp_path)
        assert result.git_available is False
        assert len(result.errors) == 1
        assert "Shared directory not found" in result.errors[0]

    def test_not_a_git_repo(self, tmp_path: Path) -> None:
        """Returns unchecked when git and manifest are unavailable."""
        shared = tmp_path / "shared"
        shared.mkdir()

        with patch("gobby.sync.integrity.run_git_command", return_value=None):
            result = verify_bundled_integrity(tmp_path)

        assert result.git_available is False
        assert result.checked is False
        assert result.source == "none"
        assert result.all_clean is True
        assert "manifest not found" in result.errors[0]

    def test_non_git_clean_manifest_verifies_cleanly(self, tmp_path: Path) -> None:
        """Non-git installs verify against the packaged manifest."""
        shared = tmp_path / "shared"
        (shared / "skills" / "demo").mkdir(parents=True)
        (shared / "skills" / "demo" / "SKILL.md").write_text("content", encoding="utf-8")
        write_bundled_content_manifest(tmp_path)

        with patch("gobby.sync.integrity.run_git_command", return_value=None):
            result = verify_bundled_integrity(tmp_path)

        assert result.git_available is False
        assert result.checked is True
        assert result.source == "manifest"
        assert result.all_clean is True
        assert result.clean_files == ["shared/skills/demo/SKILL.md"]

    def test_non_git_tampered_yaml_maps_to_skipped_content_type(self, tmp_path: Path) -> None:
        """Manifest hash mismatches become dirty files with sync target mapping."""
        shared = tmp_path / "shared"
        pipeline = shared / "workflows" / "pipelines" / "demo.yaml"
        pipeline.parent.mkdir(parents=True)
        pipeline.write_text("name: demo\n", encoding="utf-8")
        write_bundled_content_manifest(tmp_path)
        pipeline.write_text("name: demo\n# tampered\n", encoding="utf-8")

        with patch("gobby.sync.integrity.run_git_command", return_value=None):
            result = verify_bundled_integrity(tmp_path)

        assert result.checked is True
        assert result.source == "manifest"
        assert result.all_clean is False
        assert result.dirty_files == ["shared/workflows/pipelines/demo.yaml"]
        assert get_dirty_content_types(result.dirty_files, tmp_path) == {"pipelines"}

    def test_non_git_unreadable_manifest_file_is_dirty(self, tmp_path: Path) -> None:
        """Manifest file read failures are dirty, not silently clean."""
        shared = tmp_path / "shared"
        skill = shared / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("content", encoding="utf-8")
        write_bundled_content_manifest(tmp_path)

        with (
            patch("gobby.sync.integrity.run_git_command", return_value=None),
            patch("gobby.sync.integrity.hash_file_bytes", side_effect=OSError("denied")),
        ):
            result = verify_bundled_integrity(tmp_path)

        assert result.checked is True
        assert result.all_clean is False
        assert result.dirty_files == ["shared/skills/demo/SKILL.md"]
        assert "Failed to read shared/skills/demo/SKILL.md" in result.errors[0]

    def test_non_git_extra_bundled_yaml_is_untracked(self, tmp_path: Path) -> None:
        """Extra protected files are treated like untracked git content."""
        shared = tmp_path / "shared"
        rule = shared / "rules" / "build" / "known.yaml"
        rule.parent.mkdir(parents=True)
        rule.write_text("rules: {}\n", encoding="utf-8")
        write_bundled_content_manifest(tmp_path)
        extra = shared / "rules" / "build" / "extra.yaml"
        extra.write_text("rules: {}\n", encoding="utf-8")

        with patch("gobby.sync.integrity.run_git_command", return_value=None):
            result = verify_bundled_integrity(tmp_path)

        assert result.untracked_files == ["shared/rules/build/extra.yaml"]
        assert get_dirty_content_types(result.untracked_files, tmp_path) == {"rules"}

    def test_clean_repo(self, tmp_path: Path) -> None:
        """All files clean when git reports no changes."""
        shared = tmp_path / "shared"
        shared.mkdir()

        def mock_git(cmd: list[str], cwd: str | Path, **kwargs: object) -> str | None:
            if "rev-parse" in cmd and "--show-toplevel" in cmd:
                return str(tmp_path)
            if "diff" in cmd:
                return ""
            if "ls-files" in cmd:
                if "--others" in cmd:
                    return ""
                # Tracked files
                return "src/gobby/install/shared/workflows/default.yaml"
            return None

        with patch("gobby.sync.integrity.run_git_command", side_effect=mock_git):
            result = verify_bundled_integrity(tmp_path)

        assert result.git_available is True
        assert result.checked is True
        assert result.source == "git"
        assert result.all_clean is True
        assert len(result.dirty_files) == 0
        assert len(result.untracked_files) == 0

    def test_dirty_files_detected(self, tmp_path: Path) -> None:
        """Detects modified tracked files."""
        shared = tmp_path / "shared"
        shared.mkdir()

        dirty_file = "src/gobby/install/shared/workflows/default.yaml"

        def mock_git(cmd: list[str], cwd: str | Path, **kwargs: object) -> str | None:
            if "rev-parse" in cmd and "--show-toplevel" in cmd:
                return str(tmp_path)
            if "diff" in cmd and "--cached" not in cmd:
                return dirty_file
            if "diff" in cmd and "--cached" in cmd:
                return ""
            if "ls-files" in cmd:
                if "--others" in cmd:
                    return ""
                return dirty_file
            return None

        with patch("gobby.sync.integrity.run_git_command", side_effect=mock_git):
            result = verify_bundled_integrity(tmp_path)

        assert result.git_available is True
        assert result.all_clean is False
        assert dirty_file in result.dirty_files

    def test_untracked_files_detected(self, tmp_path: Path) -> None:
        """Detects untracked files in shared content dirs."""
        shared = tmp_path / "shared"
        shared.mkdir()

        untracked_file = "src/gobby/install/shared/skills/evil.yaml"

        def mock_git(cmd: list[str], cwd: str | Path, **kwargs: object) -> str | None:
            if "rev-parse" in cmd and "--show-toplevel" in cmd:
                return str(tmp_path)
            if "diff" in cmd:
                return ""
            if "ls-files" in cmd:
                if "--others" in cmd:
                    return untracked_file
                return ""
            return None

        with patch("gobby.sync.integrity.run_git_command", side_effect=mock_git):
            result = verify_bundled_integrity(tmp_path)

        assert result.all_clean is False
        assert untracked_file in result.untracked_files

    def test_staged_changes_detected(self, tmp_path: Path) -> None:
        """Staged (cached) changes are also detected as dirty."""
        shared = tmp_path / "shared"
        shared.mkdir()

        staged_file = "src/gobby/install/shared/prompts/system.yaml"

        def mock_git(cmd: list[str], cwd: str | Path, **kwargs: object) -> str | None:
            if "rev-parse" in cmd and "--show-toplevel" in cmd:
                return str(tmp_path)
            if "diff" in cmd and "--cached" in cmd:
                return staged_file
            if "diff" in cmd:
                return ""
            if "ls-files" in cmd:
                if "--others" in cmd:
                    return ""
                return staged_file
            return None

        with patch("gobby.sync.integrity.run_git_command", side_effect=mock_git):
            result = verify_bundled_integrity(tmp_path)

        assert result.all_clean is False
        assert staged_file in result.dirty_files


class TestGetDirtyContentTypes:
    """Tests for get_dirty_content_types."""

    def test_maps_pipeline_paths(self, tmp_path: Path) -> None:
        """Maps pipeline file paths to 'pipelines' content type."""
        shared = tmp_path / "shared"
        shared.mkdir()

        dirty = ["shared/workflows/pipelines/default.yaml"]

        with patch("gobby.sync.integrity.run_git_command", return_value=str(tmp_path)):
            result = get_dirty_content_types(dirty, tmp_path)

        assert "pipelines" in result

    def test_maps_multiple_types(self, tmp_path: Path) -> None:
        """Maps files across multiple content type directories."""
        shared = tmp_path / "shared"
        shared.mkdir()

        dirty = [
            "shared/workflows/pipelines/default.yaml",
            "shared/skills/evil.yaml",
            "shared/workflows/agents/rogue.yaml",
        ]

        with patch("gobby.sync.integrity.run_git_command", return_value=str(tmp_path)):
            result = get_dirty_content_types(dirty, tmp_path)

        assert result == {"pipelines", "skills", "agents"}

    def test_ignores_non_content_dirs(self, tmp_path: Path) -> None:
        """Files in non-content dirs (hooks, plugins) are ignored."""
        shared = tmp_path / "shared"
        shared.mkdir()

        dirty = [
            "shared/hooks/validate_settings.py",
            "shared/plugins/foo.py",
        ]

        with patch("gobby.sync.integrity.run_git_command", return_value=str(tmp_path)):
            result = get_dirty_content_types(dirty, tmp_path)

        assert result == set()

    def test_maps_manifest_paths_without_git(self, tmp_path: Path) -> None:
        """Maps manifest-shaped paths without requiring git."""
        shared = tmp_path / "shared"
        shared.mkdir()

        with patch("gobby.sync.integrity.run_git_command", return_value=None):
            result = get_dirty_content_types(["shared/workflows/pipelines/x.yaml"], tmp_path)

        assert result == {"pipelines"}

    def test_unrelated_relative_path_returns_no_shared_path(self, tmp_path: Path) -> None:
        assert _to_shared_relative_path("README.md", tmp_path) is None

    def test_maps_top_level_workflows_rules_variables_and_build_profiles(
        self, tmp_path: Path
    ) -> None:
        """Covers every DB-synced bundled root."""
        dirty = [
            "shared/workflows/dev.yaml",
            "shared/workflows/rules/tool-hygiene/require-uv.yaml",
            "shared/rules/build/build-agent-safety.yaml",
            "shared/workflows/variables/gobby-default-variables.yaml",
            "shared/registry/build_profiles.yaml",
        ]

        result = get_dirty_content_types(dirty, tmp_path)

        assert result == {"pipelines", "rules", "variables", "build_profiles"}

    def test_content_type_dirs_matches_sync_targets(self) -> None:
        """CONTENT_TYPE_DIRS covers all DB-synced content types."""
        expected = BUNDLED_SYNC_CONTENT_TYPES
        assert set(CONTENT_TYPE_DIRS.values()) == expected

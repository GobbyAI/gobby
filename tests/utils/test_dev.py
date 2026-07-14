"""Tests for dev mode detection utilities."""

from pathlib import Path

from gobby.utils.dev import is_dev_mode, is_gobby_project


class TestIsGobbyProject:
    """Tests for is_gobby_project()."""

    def test_true_for_gobby_source_repo(self, tmp_path: Path) -> None:
        """Detects the gobby source repo by marker dir + pyproject."""
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "gobby"\n')
        assert is_gobby_project(tmp_path) is True

    def test_false_without_marker_dir(self, tmp_path: Path) -> None:
        """Returns False if src/gobby/install/shared/ doesn't exist."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "gobby"\n')
        assert is_gobby_project(tmp_path) is False

    def test_false_without_pyproject(self, tmp_path: Path) -> None:
        """Returns False if pyproject.toml is missing."""
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        assert is_gobby_project(tmp_path) is False

    def test_false_for_different_project(self, tmp_path: Path) -> None:
        """Returns False if pyproject.toml is for a different project."""
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "not-gobby"\n')
        assert is_gobby_project(tmp_path) is False

    def test_single_quote_name(self, tmp_path: Path) -> None:
        """Handles single-quoted project name."""
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'gobby'\n")
        assert is_gobby_project(tmp_path) is True


class TestIsDevMode:
    """Tests for is_dev_mode()."""

    def test_true_for_gobby_project(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "gobby"\n')
        assert is_dev_mode(tmp_path) is True

    def test_true_for_gobby_project_subdirectory(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "gobby"\n')
        subdirectory = tmp_path / "src" / "gobby" / "cli"
        subdirectory.mkdir(parents=True)

        assert is_dev_mode(subdirectory) is True

    def test_false_for_random_dir(self, tmp_path: Path) -> None:
        assert is_dev_mode(tmp_path) is False

    def test_defaults_to_cwd(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        assert is_dev_mode() is False

"""Tests for code_index.security."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.code_index.security import (
    has_secret_extension,
    is_binary,
    is_symlink_safe,
    should_exclude,
    validate_path,
)

pytestmark = pytest.mark.unit


# ── validate_path ───────────────────────────────────────────────────────


def test_validate_path_inside_root(tmp_path: Path) -> None:
    """Path inside root is valid."""
    child = tmp_path / "src" / "main.py"
    child.parent.mkdir(parents=True, exist_ok=True)
    child.touch()
    assert validate_path(child, tmp_path) is True


def test_validate_path_traversal_blocked(tmp_path: Path) -> None:
    """Path with .. traversal outside root is rejected."""
    evil = tmp_path / "src" / ".." / ".." / "etc" / "passwd"
    assert validate_path(evil, tmp_path) is False


def test_validate_path_root_itself(tmp_path: Path) -> None:
    """Root directory itself is valid."""
    assert validate_path(tmp_path, tmp_path) is True


# ── is_symlink_safe ─────────────────────────────────────────────────────


def test_is_symlink_safe_regular_file(tmp_path: Path) -> None:
    """Regular (non-symlink) files are safe."""
    f = tmp_path / "normal.py"
    f.write_text("x = 1")
    assert is_symlink_safe(f, tmp_path) is True


def test_is_symlink_safe_internal_link(tmp_path: Path) -> None:
    """Symlink to a file within root is safe."""
    target = tmp_path / "real.py"
    target.write_text("x = 1")
    link = tmp_path / "link.py"
    link.symlink_to(target)
    assert is_symlink_safe(link, tmp_path) is True


def test_is_symlink_safe_external_link(tmp_path: Path) -> None:
    """Symlink pointing outside root is unsafe."""
    import tempfile

    with tempfile.TemporaryDirectory() as outside:
        outside_file = Path(outside) / "secret.py"
        outside_file.write_text("secret = True")
        link = tmp_path / "escape.py"
        link.symlink_to(outside_file)
        assert is_symlink_safe(link, tmp_path) is False


# ── is_binary ───────────────────────────────────────────────────────────


def test_is_binary_text_file(tmp_path: Path) -> None:
    """Normal text file is not binary."""
    f = tmp_path / "script.py"
    f.write_text("print('hello')\n")
    assert is_binary(f) is False


def test_is_binary_null_bytes(tmp_path: Path) -> None:
    """File with null bytes is detected as binary."""
    f = tmp_path / "image.bin"
    f.write_bytes(b"\x89PNG\x00\x00\x00data")
    assert is_binary(f) is True


def test_is_binary_nonexistent(tmp_path: Path) -> None:
    """Non-existent file returns True (fail-safe)."""
    assert is_binary(tmp_path / "ghost.txt") is True


# ── should_exclude ──────────────────────────────────────────────────────


def test_should_exclude_node_modules() -> None:
    """node_modules directory is excluded."""
    path = Path("project/node_modules/lodash/index.js")
    assert should_exclude(path, ["node_modules"]) is True


def test_should_exclude_git_dir() -> None:
    """.git directory is excluded."""
    path = Path("project/.git/config")
    assert should_exclude(path, [".git"]) is True


def test_should_exclude_pycache() -> None:
    """__pycache__ directory is excluded."""
    path = Path("src/__pycache__/module.cpython-313.pyc")
    assert should_exclude(path, ["__pycache__"]) is True


def test_should_exclude_no_match() -> None:
    """Normal source file is not excluded."""
    path = Path("src/gobby/main.py")
    assert should_exclude(path, ["node_modules", ".git", "__pycache__"]) is False


def test_should_exclude_venv() -> None:
    """.venv directory is excluded."""
    path = Path(".venv/lib/python3.13/site-packages/pkg.py")
    assert should_exclude(path, [".venv"]) is True


def test_should_exclude_multiple_patterns() -> None:
    """Test with multiple patterns where only one matches."""
    path = Path("build/output/bundle.js")
    assert should_exclude(path, ["node_modules", "build", "dist"]) is True


# ── has_secret_extension ────────────────────────────────────────────────


def test_has_secret_extension_env() -> None:
    """.env file is flagged."""
    assert has_secret_extension(Path("config/.env")) is True


def test_has_secret_extension_pem() -> None:
    """.pem file is flagged."""
    assert has_secret_extension(Path("certs/server.pem")) is True


def test_has_secret_extension_key() -> None:
    """.key file is flagged."""
    assert has_secret_extension(Path("keys/private.key")) is True


def test_has_secret_extension_credentials_prefix() -> None:
    """File starting with 'credentials' is flagged."""
    assert has_secret_extension(Path("credentials.json")) is True


def test_has_secret_extension_dot_env_prefix() -> None:
    """File starting with '.env' is flagged (e.g., .env.local)."""
    assert has_secret_extension(Path(".env.local")) is True


def test_has_secret_extension_normal_file() -> None:
    """Normal source file is not flagged."""
    assert has_secret_extension(Path("src/main.py")) is False
    assert has_secret_extension(Path("README.md")) is False


def test_has_secret_extension_keystore() -> None:
    """.keystore file is flagged."""
    assert has_secret_extension(Path("app.keystore")) is True

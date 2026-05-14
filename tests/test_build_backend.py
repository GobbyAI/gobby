"""Tests for the PEP 517 build backend wrapper that stages the web UI."""

from __future__ import annotations

import importlib
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _load_backend(repo_root: Path) -> object:
    """Import build_backend rooted at ``repo_root``."""
    sys.path.insert(0, str(repo_root))
    try:
        if "build_backend" in sys.modules:
            del sys.modules["build_backend"]
        return importlib.import_module("build_backend")
    finally:
        sys.path.pop(0)


def _write_wheel(path: Path, members: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as wheel:
        for member in members:
            wheel.writestr(member, "")


def test_stage_ui_copies_dist_to_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    # Symlink __init__.py from the real module so we exercise the actual code.
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())

    web = repo_root / "web"
    dist = web / "dist"
    dist.mkdir(parents=True)
    (web / "package.json").write_text("{}")
    (dist / "index.html").write_text("<html></html>")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("// js")

    # Skip the npm step; we only want to test the copy phase.
    monkeypatch.setenv("GOBBY_SKIP_UI_BUILD", "0")
    # Force no-npm path so npm ci is never invoked.
    monkeypatch.setattr("shutil.which", lambda name: None if name == "npm" else "/usr/bin/" + name)

    backend = _load_backend(repo_root)
    backend._stage_ui()

    staged = repo_root / "src" / "gobby" / "ui" / "web" / "dist"
    assert (staged / "index.html").read_text() == "<html></html>"
    assert (staged / "assets" / "app.js").read_text() == "// js"


def test_stage_ui_skip_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())

    # Create a stale dist; staging must NOT touch it.
    web_dist = repo_root / "web" / "dist"
    web_dist.mkdir(parents=True)
    (web_dist / "index.html").write_text("fresh")
    staged_dir = repo_root / "src" / "gobby" / "ui" / "web" / "dist"
    staged_dir.mkdir(parents=True)
    (staged_dir / "index.html").write_text("stale")

    monkeypatch.setenv("GOBBY_SKIP_UI_BUILD", "1")

    backend = _load_backend(repo_root)
    backend._stage_ui()

    assert (staged_dir / "index.html").read_text() == "stale"


def test_stage_ui_reuses_pre_staged_when_no_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())

    # No web/ tree at all; only a pre-staged dist exists (sdist install scenario).
    staged_dir = repo_root / "src" / "gobby" / "ui" / "web" / "dist"
    staged_dir.mkdir(parents=True)
    (staged_dir / "index.html").write_text("pre-staged")

    monkeypatch.delenv("GOBBY_SKIP_UI_BUILD", raising=False)

    backend = _load_backend(repo_root)
    backend._stage_ui()

    assert (staged_dir / "index.html").read_text() == "pre-staged"


def test_stage_ui_runs_npm_commands_with_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    web = repo_root / "web"
    web.mkdir()
    (web / "package.json").write_text("{}")

    backend = _load_backend(repo_root)
    monkeypatch.setattr(backend.shutil, "which", lambda name: "/usr/bin/npm")
    calls: list[tuple[list[str], Path, bool, bool, bool, int]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> None:
        calls.append((command, cwd, check, capture_output, text, timeout))

    monkeypatch.setattr(backend.subprocess, "run", fake_run)

    backend._stage_ui()

    assert calls == [
        (["npm", "ci"], web, True, True, True, 600),
        (["npm", "run", "build"], web, True, True, True, 600),
    ]


def test_stage_ui_npm_timeout_raises_contextual_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    web = repo_root / "web"
    web.mkdir()
    (web / "package.json").write_text("{}")

    backend = _load_backend(repo_root)
    monkeypatch.setattr(backend.shutil, "which", lambda name: "/usr/bin/npm")

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> None:
        raise backend.subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    monkeypatch.setattr(backend.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        backend._stage_ui()

    message = str(exc_info.value)
    assert "npm ci" in message
    assert str(web) in message
    assert "600" in message


@pytest.mark.parametrize(
    ("failed_command", "expected_command"),
    [
        (["npm", "ci"], "npm ci"),
        (["npm", "run", "build"], "npm run build"),
    ],
)
def test_stage_ui_npm_failure_raises_output_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_command: list[str],
    expected_command: str,
) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    web = repo_root / "web"
    web.mkdir()
    (web / "package.json").write_text("{}")

    backend = _load_backend(repo_root)
    monkeypatch.setattr(backend.shutil, "which", lambda name: "/usr/bin/npm")

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> None:
        if command == failed_command:
            raise subprocess.CalledProcessError(
                returncode=17,
                cmd=command,
                output="out text",
                stderr="err text",
            )

    monkeypatch.setattr(backend.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        backend._stage_ui()

    message = str(exc_info.value)
    assert expected_command in message
    assert "return code 17" in message
    assert "stdout:\nout text" in message
    assert "stderr:\nerr text" in message


def test_build_wheel_accepts_wheel_with_ui_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    wheel_dir = repo_root / "dist"
    wheel_dir.mkdir()

    backend = _load_backend(repo_root)
    monkeypatch.setattr(backend, "_stage_ui", lambda: None)

    def fake_build_wheel(
        wheel_directory: str,
        config_settings: dict[str, object] | None,
        metadata_directory: str | None,
    ) -> str:
        wheel_path = Path(wheel_directory) / "gobby-0-py3-none-any.whl"
        _write_wheel(wheel_path, ["gobby/ui/web/dist/index.html"])
        return wheel_path.name

    monkeypatch.setattr(backend, "_orig", lambda: SimpleNamespace(build_wheel=fake_build_wheel))

    assert backend.build_wheel(str(wheel_dir)) == "gobby-0-py3-none-any.whl"


def test_build_wheel_rejects_wheel_missing_ui_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    wheel_dir = repo_root / "dist"
    wheel_dir.mkdir()

    backend = _load_backend(repo_root)
    monkeypatch.setattr(backend, "_stage_ui", lambda: None)

    def fake_build_wheel(
        wheel_directory: str,
        config_settings: dict[str, object] | None,
        metadata_directory: str | None,
    ) -> str:
        wheel_path = Path(wheel_directory) / "gobby-0-py3-none-any.whl"
        _write_wheel(wheel_path, ["gobby/__init__.py"])
        return wheel_path.name

    monkeypatch.setattr(backend, "_orig", lambda: SimpleNamespace(build_wheel=fake_build_wheel))

    with pytest.raises(RuntimeError, match="gobby/ui/web/dist/index.html"):
        backend.build_wheel(str(wheel_dir))

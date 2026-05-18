"""Tests for the PEP 517 build backend wrapper that stages the web UI."""

from __future__ import annotations

import importlib
import json
import logging
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.unit


class SubprocessCall(NamedTuple):
    command: list[str]
    cwd: Path
    check: bool
    capture_output: bool
    text: bool
    timeout: int


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


def _copy_manifest_module(repo_root: Path) -> None:
    real_module = (
        Path(__file__).resolve().parent.parent / "src" / "gobby" / "install" / "manifest.py"
    )
    target = repo_root / "src" / "gobby" / "install" / "manifest.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(real_module.read_text())


def _write_shared_content(repo_root: Path) -> Path:
    shared_file = (
        repo_root / "src" / "gobby" / "install" / "shared" / "skills" / "demo" / "SKILL.md"
    )
    shared_file.parent.mkdir(parents=True, exist_ok=True)
    shared_file.write_text("demo skill", encoding="utf-8")
    return shared_file


def test_stage_ui_copies_dist_to_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage existing web/dist assets into package data for wheel builds."""
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
    """GOBBY_SKIP_UI_BUILD should bypass npm and preserve staged assets."""
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
    """Source distributions without web/ should reuse pre-staged UI assets."""
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
    """UI staging should pass the configured timeout to both npm commands."""
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    web = repo_root / "web"
    web.mkdir()
    (web / "package.json").write_text("{}")

    backend = _load_backend(repo_root)
    monkeypatch.setattr(backend.shutil, "which", lambda name: "/usr/bin/npm")
    calls: list[SubprocessCall] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> SimpleNamespace:
        calls.append(SubprocessCall(command, cwd, check, capture_output, text, timeout))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(backend.subprocess, "run", fake_run)

    backend._stage_ui()

    assert calls == [
        SubprocessCall(["npm", "ci"], web, False, True, True, 600),
        SubprocessCall(["npm", "run", "build"], web, False, True, True, 600),
    ]


@pytest.mark.parametrize(
    ("env_value", "expected_timeout", "expected_warning"),
    [
        ("30", 30, None),
        ("", 600, "Invalid GOBBY_NPM_BUILD_TIMEOUT=''"),
        ("0", 600, "Non-positive GOBBY_NPM_BUILD_TIMEOUT='0'"),
        ("-1", 600, "Non-positive GOBBY_NPM_BUILD_TIMEOUT='-1'"),
        ("invalid", 600, "Invalid GOBBY_NPM_BUILD_TIMEOUT='invalid'"),
        (None, 600, None),
    ],
)
def test_npm_timeout_env_parses_at_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    env_value: str | None,
    expected_timeout: int,
    expected_warning: str | None,
) -> None:
    """GOBBY_NPM_BUILD_TIMEOUT should parse once at import and warn on fallback."""
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())

    if env_value is None:
        monkeypatch.delenv("GOBBY_NPM_BUILD_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("GOBBY_NPM_BUILD_TIMEOUT", env_value)

    with caplog.at_level(logging.WARNING, logger="build_backend"):
        backend = _load_backend(repo_root)

    assert backend._NPM_BUILD_TIMEOUT_SECONDS == expected_timeout
    if expected_warning is None:
        assert "GOBBY_NPM_BUILD_TIMEOUT" not in caplog.text
    else:
        assert expected_warning in caplog.text
        assert "using default 600 seconds" in caplog.text


def test_stage_ui_npm_timeout_raises_contextual_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """npm command timeouts should include command, cwd, and timeout context."""
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
    """npm command failures should surface return code and captured output."""
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
    ) -> SimpleNamespace:
        if command == failed_command:
            return SimpleNamespace(returncode=17, stdout="out text", stderr="err text")
        return SimpleNamespace(returncode=0)

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
    """Wheels containing required staged assets should be accepted."""
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    wheel_dir = repo_root / "dist"
    wheel_dir.mkdir()

    backend = _load_backend(repo_root)
    monkeypatch.setattr(backend, "_stage_ui", lambda: None)
    monkeypatch.setattr(backend, "_stage_bundled_content_manifest", lambda: None)

    def fake_build_wheel(
        wheel_directory: str,
        config_settings: dict[str, object] | None,
        metadata_directory: str | None,
    ) -> str:
        wheel_path = Path(wheel_directory) / "gobby-0-py3-none-any.whl"
        _write_wheel(
            wheel_path,
            [
                "gobby/ui/web/dist/index.html",
                "gobby/install/bundled_content_manifest.json",
            ],
        )
        return wheel_path.name

    monkeypatch.setattr(backend, "_orig", lambda: SimpleNamespace(build_wheel=fake_build_wheel))

    assert backend.build_wheel(str(wheel_dir)) == "gobby-0-py3-none-any.whl"


def test_build_wheel_rejects_wheel_missing_ui_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wheels missing the staged UI index should fail the release guard."""
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    wheel_dir = repo_root / "dist"
    wheel_dir.mkdir()

    backend = _load_backend(repo_root)
    monkeypatch.setattr(backend, "_stage_ui", lambda: None)
    monkeypatch.setattr(backend, "_stage_bundled_content_manifest", lambda: None)

    def fake_build_wheel(
        wheel_directory: str,
        config_settings: dict[str, object] | None,
        metadata_directory: str | None,
    ) -> str:
        wheel_path = Path(wheel_directory) / "gobby-0-py3-none-any.whl"
        _write_wheel(wheel_path, ["gobby/install/bundled_content_manifest.json"])
        return wheel_path.name

    monkeypatch.setattr(backend, "_orig", lambda: SimpleNamespace(build_wheel=fake_build_wheel))

    with pytest.raises(RuntimeError, match="gobby/ui/web/dist/index.html"):
        backend.build_wheel(str(wheel_dir))


def test_stage_bundled_content_manifest_writes_manifest(tmp_path: Path) -> None:
    """Build backend should generate the packaged bundled-content manifest."""
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    _copy_manifest_module(repo_root)
    _write_shared_content(repo_root)

    backend = _load_backend(repo_root)

    manifest_path = backend._stage_bundled_content_manifest()

    assert (
        manifest_path == repo_root / "src" / "gobby" / "install" / "bundled_content_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["hash_algorithm"] == "sha256"
    assert manifest["root"] == "shared"
    assert list(manifest["files"]) == ["skills/demo/SKILL.md"]


def test_stage_bundled_content_manifest_rejects_invalid_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build backend should fail clearly when the manifest helper shape is invalid."""
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())

    backend = _load_backend(repo_root)
    backend._MANIFEST_MODULE.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        backend,
        "_load_manifest_module",
        lambda: SimpleNamespace(write_bundled_content_manifest="not-callable"),
    )

    with pytest.raises(RuntimeError) as exc_info:
        backend._stage_bundled_content_manifest()

    message = str(exc_info.value)
    assert "write_bundled_content_manifest" in message
    assert str(backend._MANIFEST_MODULE) in message


def test_stage_bundled_content_manifest_wraps_helper_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build backend should include helper context when manifest generation fails."""
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())

    backend = _load_backend(repo_root)

    def fail_manifest(_install_dir: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(
        backend,
        "_load_manifest_module",
        lambda: SimpleNamespace(write_bundled_content_manifest=fail_manifest),
    )

    with pytest.raises(RuntimeError) as exc_info:
        backend._stage_bundled_content_manifest()

    message = str(exc_info.value)
    assert "Bundled content manifest helper failed" in message
    assert "disk full" in message


def test_build_sdist_generates_bundled_content_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sdist builds should stage a fresh bundled-content manifest."""
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    _copy_manifest_module(repo_root)
    _write_shared_content(repo_root)
    sdist_dir = repo_root / "dist"
    sdist_dir.mkdir()

    backend = _load_backend(repo_root)
    monkeypatch.setattr(backend, "_stage_ui", lambda: None)

    def fake_build_sdist(
        sdist_directory: str,
        config_settings: dict[str, object] | None,
    ) -> str:
        assert sdist_directory == str(sdist_dir)
        assert config_settings is None
        manifest = repo_root / "src" / "gobby" / "install" / "bundled_content_manifest.json"
        assert manifest.is_file()
        return "gobby-0.tar.gz"

    monkeypatch.setattr(backend, "_orig", lambda: SimpleNamespace(build_sdist=fake_build_sdist))

    assert backend.build_sdist(str(sdist_dir)) == "gobby-0.tar.gz"


def test_build_wheel_rejects_wheel_missing_bundled_content_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wheel verification should reject artifacts without the manifest."""
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    wheel_dir = repo_root / "dist"
    wheel_dir.mkdir()

    backend = _load_backend(repo_root)
    monkeypatch.setattr(backend, "_stage_ui", lambda: None)
    monkeypatch.setattr(backend, "_stage_bundled_content_manifest", lambda: None)

    def fake_build_wheel(
        wheel_directory: str,
        config_settings: dict[str, object] | None,
        metadata_directory: str | None,
    ) -> str:
        wheel_path = Path(wheel_directory) / "gobby-0-py3-none-any.whl"
        _write_wheel(wheel_path, ["gobby/ui/web/dist/index.html"])
        return wheel_path.name

    monkeypatch.setattr(backend, "_orig", lambda: SimpleNamespace(build_wheel=fake_build_wheel))

    with pytest.raises(RuntimeError, match="gobby/install/bundled_content_manifest.json"):
        backend.build_wheel(str(wheel_dir))


def test_build_editable_delegates_to_setuptools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editable builds should pass through to setuptools without staging UI assets."""
    repo_root = tmp_path
    (repo_root / "build_backend").mkdir()
    real_module = Path(__file__).resolve().parent.parent / "build_backend" / "__init__.py"
    (repo_root / "build_backend" / "__init__.py").write_text(real_module.read_text())
    wheel_dir = repo_root / "dist"
    wheel_dir.mkdir()

    backend = _load_backend(repo_root)

    def fake_build_editable(
        wheel_directory: str,
        config_settings: dict[str, object] | None,
        metadata_directory: str | None,
    ) -> str:
        assert wheel_directory == str(wheel_dir)
        assert config_settings == {"editable": True}
        assert metadata_directory == "meta"
        return "gobby-0-editable.whl"

    def fail_stage_ui() -> None:
        raise AssertionError("_stage_ui must not run")

    monkeypatch.setattr(
        backend,
        "_orig",
        lambda: SimpleNamespace(build_editable=fake_build_editable),
    )
    monkeypatch.setattr(backend, "_stage_ui", fail_stage_ui)

    assert (
        backend.build_editable(str(wheel_dir), {"editable": True}, "meta") == "gobby-0-editable.whl"
    )

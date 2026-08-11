"""CLI tests for projects refresh-verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from gobby.cli.projects import projects
from gobby.config.app import DaemonConfig

pytestmark = [pytest.mark.unit]


def write_project(root: Path, verification: dict[str, Any] | None = None) -> None:
    gobby_dir = root / ".gobby"
    gobby_dir.mkdir()
    payload: dict[str, Any] = {
        "id": "proj-1",
        "name": "example",
        "created_at": "2026-01-01T00:00:00Z",
    }
    if verification is not None:
        payload["verification"] = verification
    (gobby_dir / "project.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def add_python_project(root: Path) -> None:
    (root / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "tests").mkdir()


def test_refresh_verification_preview_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    before = (tmp_path / ".gobby" / "project.json").read_text(encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off"])

    assert result.exit_code == 0
    assert "Previewing verification refresh" in result.output
    assert "Run with --fix to write changes." in result.output
    assert (tmp_path / ".gobby" / "project.json").read_text(encoding="utf-8") == before


def test_refresh_verification_from_subdirectory_uses_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    subdirectory = tmp_path / "src" / "example"
    subdirectory.mkdir()
    monkeypatch.chdir(subdirectory)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off"])

    assert result.exit_code == 0
    assert "Previewing verification refresh" in result.output
    assert f"gobby init -C {subdirectory}" not in result.output


def test_refresh_verification_fix_writes_project_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off", "--fix"])

    assert result.exit_code == 0
    project_data = json.loads((tmp_path / ".gobby" / "project.json").read_text(encoding="utf-8"))
    assert project_data["verification"]["unit_tests"] == "pytest tests/ -v"
    assert project_data["verification"]["lint"] == "ruff check src/"


@pytest.mark.parametrize(
    "corrupt_content",
    [
        pytest.param('{"name": "broken"', id="malformed-json"),
        pytest.param('["not", "an", "object"]', id="non-object-json"),
    ],
)
def test_refresh_verification_fix_backs_up_corrupt_project_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_content: str,
) -> None:
    gobby_dir = tmp_path / ".gobby"
    gobby_dir.mkdir()
    project_json = gobby_dir / "project.json"
    project_json.write_text(corrupt_content, encoding="utf-8")
    add_python_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off", "--fix"])

    assert result.exit_code == 0
    assert "Updated verification commands" in result.output
    assert project_json.with_suffix(".json.bak").read_text(encoding="utf-8") == corrupt_content
    project_data = json.loads(project_json.read_text(encoding="utf-8"))
    assert project_data["verification"]["unit_tests"] == "pytest tests/ -v"
    assert project_data["verification"]["lint"] == "ruff check src/"


def test_refresh_verification_fix_reports_project_json_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    project_json = tmp_path / ".gobby" / "project.json"
    original = project_json.read_bytes()
    original_open = Path.open

    def fail_project_json_read(
        path: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if path == project_json and mode == "rb":
            raise OSError("read denied")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_project_json_read)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off", "--fix"])
    monkeypatch.setattr(Path, "open", original_open)

    assert result.exit_code == 1
    assert "Refusing to update" in result.output
    assert "read denied" in result.output
    assert "Traceback" not in result.output
    assert project_json.read_bytes() == original
    assert not project_json.with_suffix(".json.bak").exists()


def test_refresh_verification_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["changed"] is True
    assert payload["after"]["unit_tests"] == "pytest tests/ -v"
    assert payload["ai"]["mode"] == "off"


def test_refresh_verification_auto_falls_back_when_ai_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    runtime = MagicMock()
    runtime.require_config.return_value = DaemonConfig()
    monkeypatch.setattr("gobby.cli.runtime.get_cli_runtime", lambda: runtime)
    monkeypatch.setattr("gobby.ai.build_daemon_text_generation_service", lambda _config: None)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "auto"])

    assert result.exit_code == 0
    assert "AI synthesis unavailable" in result.output
    assert "Previewing verification refresh" in result.output


def test_refresh_verification_ai_on_requires_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    runtime = MagicMock()
    runtime.require_config.return_value = DaemonConfig()
    monkeypatch.setattr("gobby.cli.runtime.get_cli_runtime", lambda: runtime)
    monkeypatch.setattr("gobby.ai.build_daemon_text_generation_service", lambda _config: None)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "on"])

    assert result.exit_code == 1
    assert "No text generation service is available." in result.output


def test_refresh_verification_uninitialized_path_has_init_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off"])

    assert result.exit_code == 1
    assert f"gobby init -C {tmp_path}" in result.output


def test_refresh_verification_fix_reports_oversized_project_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project(tmp_path, {"unit_tests": "pytest tests/custom"})
    project_json = tmp_path / ".gobby" / "project.json"
    payload = json.loads(project_json.read_text(encoding="utf-8"))
    payload["large_user_config"] = "x" * (70 * 1024)
    project_json.write_text(json.dumps(payload), encoding="utf-8")
    original = project_json.read_bytes()
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off", "--fix"])

    assert result.exit_code == 1
    assert "Refusing to update" in result.output
    assert "exceeds MAX_FILE_BYTES" in result.output
    assert project_json.read_bytes() == original

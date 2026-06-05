"""CLI tests for projects refresh-verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from gobby.cli.projects import projects
from gobby.config.app import DaemonConfig


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
    monkeypatch,
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


def test_refresh_verification_fix_writes_project_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off", "--fix"])

    assert result.exit_code == 0
    project_data = json.loads((tmp_path / ".gobby" / "project.json").read_text(encoding="utf-8"))
    assert project_data["verification"]["unit_tests"] == "uv run pytest tests/ -v"
    assert project_data["verification"]["lint"] == "uv run ruff check src/"


def test_refresh_verification_json_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["changed"] is True
    assert payload["after"]["unit_tests"] == "uv run pytest tests/ -v"
    assert payload["ai"]["mode"] == "off"


def test_refresh_verification_auto_falls_back_when_ai_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("gobby.config.app.load_config", lambda: DaemonConfig())
    monkeypatch.setattr("gobby.ai.build_daemon_text_generation_service", lambda _config: None)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "auto"])

    assert result.exit_code == 0
    assert "AI synthesis unavailable" in result.output
    assert "Previewing verification refresh" in result.output


def test_refresh_verification_ai_on_requires_service(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_project(tmp_path)
    add_python_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("gobby.config.app.load_config", lambda: DaemonConfig())
    monkeypatch.setattr("gobby.ai.build_daemon_text_generation_service", lambda _config: None)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "on"])

    assert result.exit_code == 1
    assert "No text generation service is available." in result.output


def test_refresh_verification_uninitialized_path_has_init_hint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(projects, ["refresh-verification", "--ai", "off"])

    assert result.exit_code == 1
    assert f"gobby init -C {tmp_path}" in result.output

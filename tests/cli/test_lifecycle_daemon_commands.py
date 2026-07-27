from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

import pytest
from click.testing import CliRunner

import gobby.cli.utils_config as utils_config
from gobby.cli.embeddings import embeddings
from gobby.cli.projects import projects

pytestmark = pytest.mark.unit

projects_module = importlib.import_module("gobby.cli.projects")


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class TextResponse:
    status_code = 200
    text = "switch accepted"

    def json(self) -> dict[str, Any]:
        raise ValueError("not JSON")


class ErrorTextResponse(TextResponse):
    status_code = 503
    text = "daemon unavailable"


class FakeDaemonClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def call_http_api(
        self,
        endpoint: str,
        method: str = "POST",
        json_data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        self.calls.append((endpoint, method, json_data))
        return FakeResponse({"run_id": "run-1", "status": "started", "message": "started"})


def test_embedding_switch_cli_only_calls_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = FakeDaemonClient()
    monkeypatch.setattr(utils_config, "get_daemon_client", lambda **_kwargs: daemon)

    result = CliRunner().invoke(
        embeddings,
        ["switch", "qwen3-8b-q8", "--provider", "ollama"],
    )

    assert result.exit_code == 0, result.output
    assert daemon.calls == [
        (
            "/api/embeddings/switch/start",
            "POST",
            {"catalog_key": "qwen3-8b-q8", "provider": "ollama"},
        )
    ]


def test_embedding_switch_accepts_successful_non_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = FakeDaemonClient()
    monkeypatch.setattr(
        daemon,
        "call_http_api",
        lambda *_args, **_kwargs: TextResponse(),
    )
    monkeypatch.setattr(utils_config, "get_daemon_client", lambda **_kwargs: daemon)

    result = CliRunner().invoke(
        embeddings,
        ["switch", "qwen3-8b-q8", "--provider", "ollama"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "switch accepted"


@pytest.mark.parametrize(
    "args",
    [
        ["switch", "qwen3-8b-q8", "--provider", "ollama"],
        ["switch", "--resume"],
        ["switch", "--abort"],
    ],
)
def test_embedding_switch_mutations_refuse_when_daemon_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    def unavailable(**_kwargs: object) -> None:
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(utils_config, "get_daemon_client", unavailable)

    result = CliRunner().invoke(embeddings, args)

    assert result.exit_code == 1
    assert "daemon unavailable" in result.output


@dataclass
class Project:
    id: str
    name: str


class ProjectManager:
    def get(self, project_ref: str) -> Project | None:
        return Project(project_ref, project_ref)

    def get_by_name(self, project_ref: str, include_deleted: bool = False) -> Project | None:
        return None


def test_project_purge_cli_only_calls_shared_daemon_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = FakeDaemonClient()
    monkeypatch.setattr(projects_module, "get_project_manager", lambda: ProjectManager())
    monkeypatch.setattr(utils_config, "get_daemon_client", lambda **_kwargs: daemon)

    result = CliRunner().invoke(
        projects,
        ["purge", "project-1", "--confirm", "project-1"],
    )

    assert result.exit_code == 0, result.output
    assert daemon.calls == [("/api/projects/project-1/purge", "POST", None)]


def test_project_purge_rejects_confirmation_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = FakeDaemonClient()
    monkeypatch.setattr(projects_module, "get_project_manager", lambda: ProjectManager())
    monkeypatch.setattr(utils_config, "get_daemon_client", lambda **_kwargs: daemon)

    result = CliRunner().invoke(
        projects,
        ["purge", "project-1", "--confirm", "different-project"],
    )

    assert result.exit_code == 1
    assert "Confirmation mismatch" in result.output
    assert daemon.calls == []


def test_project_purge_reports_non_json_daemon_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = FakeDaemonClient()
    monkeypatch.setattr(
        daemon,
        "call_http_api",
        lambda *_args, **_kwargs: ErrorTextResponse(),
    )
    monkeypatch.setattr(projects_module, "get_project_manager", lambda: ProjectManager())
    monkeypatch.setattr(utils_config, "get_daemon_client", lambda **_kwargs: daemon)

    result = CliRunner().invoke(
        projects,
        ["purge", "project-1", "--confirm", "project-1"],
    )

    assert result.exit_code == 1
    assert "Purge failed: daemon unavailable" in result.output

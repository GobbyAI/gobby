from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
DEADLINE = "2026-09-09T12:10:00Z"


class _Result:
    def __init__(self, status: str, *, outcome: str | None = None) -> None:
        self.payload = {
            "run_id": "ask-run-1",
            "status": status,
            "current_stage": None if status == "completed" else "investigate",
            "answer_outcome": outcome,
            "typed_error": None,
            "deadline_at": DEADLINE,
            "profile_identities": {
                "investigator": "ask-investigator@sha256:one",
                "reviewer": "ask-reviewer@sha256:two",
            },
            "tool_identities": ["gcode@contract-9"],
            "artifact_manifest": None,
        }

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return deepcopy(self.payload)


class _AskService:
    def __init__(self) -> None:
        self.start_call: tuple[Any, Path, str] | None = None
        self.wait_call: tuple[str, str, float | None] | None = None
        self.cancel_calls: list[str] = []

    async def start(self, request: Any, *, project_root: Path, caller_session_id: str) -> _Result:
        self.start_call = (request, project_root, caller_session_id)
        return _Result("running")

    async def wait(self, run_id: str, *, project_id: str, timeout: float | None = None) -> _Result:
        self.wait_call = (run_id, project_id, timeout)
        return _Result("completed", outcome="unknown")

    async def cancel(self, run_id: str, **_: Any) -> _Result:
        self.cancel_calls.append(run_id)
        return _Result("cancelled")


def test_shared_run_contract_and_event_driven_wait(tmp_path: Path) -> None:
    from gobby.servers.routes.ask import create_ask_router

    service = _AskService()
    server = SimpleNamespace(services=SimpleNamespace(ask_service=service))
    app = FastAPI()
    app.include_router(
        create_ask_router(
            cast("HTTPServer", server),
            project_root_resolver=lambda _project_id: tmp_path,
        )
    )
    client = TestClient(app)

    started = client.post(
        "/api/ask/runs",
        headers={"X-Gobby-Session-Id": SESSION_ID},
        json={
            "question": "Where is the source of truth?",
            "project_id": PROJECT_ID,
            "commit_ref": "HEAD",
            "timeout_seconds": 600,
            "retrieval_mode": "deterministic",
        },
    )
    assert started.status_code == 202
    assert started.json() == _Result("running").payload

    waited = client.get(
        "/api/ask/runs/ask-run-1/wait",
        params={"project_id": PROJECT_ID, "timeout_seconds": 3},
    )
    assert waited.status_code == 200
    assert waited.json() == _Result("completed", outcome="unknown").payload
    assert waited.json()["deadline_at"] == started.json()["deadline_at"]
    assert service.wait_call == ("ask-run-1", PROJECT_ID, 3.0)
    assert service.cancel_calls == []

    assert service.start_call is not None
    request, project_root, caller_session_id = service.start_call
    assert project_root == tmp_path
    assert caller_session_id == SESSION_ID
    assert request.project_id == PROJECT_ID
    assert request.investigator_profile == "ask-investigator"
    assert request.reviewer_profile == "ask-reviewer"


def test_export_propagates_archive_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tarfile

    from gobby.servers.routes.ask import _tar_stream

    (tmp_path / "answer.md").write_text("verified answer")

    def fail_add(*args: object, **kwargs: object) -> None:
        raise OSError("publication read failed")

    monkeypatch.setattr(tarfile.TarFile, "add", fail_add)
    with pytest.raises(OSError, match="publication read failed"):
        list(_tar_stream(tmp_path))


def test_export_stream_roundtrip(tmp_path: Path) -> None:
    import io
    import tarfile

    from gobby.servers.routes.ask import _tar_stream

    content = b"verified answer\n"
    (tmp_path / "answer.md").write_bytes(content)
    with tarfile.open(fileobj=io.BytesIO(b"".join(_tar_stream(tmp_path)))) as archive:
        assert archive.getnames() == ["answer.md"]
        member = archive.extractfile("answer.md")
        assert member is not None
        assert member.read() == content


@pytest.mark.timeout(5)
def test_export_disconnect_releases_writer(tmp_path: Path) -> None:
    import threading

    from gobby.servers.routes.ask import _tar_stream

    existing = set(threading.enumerate())
    (tmp_path / "answer.md").write_bytes(b"x" * (1024 * 1024))
    stream = _tar_stream(tmp_path)
    assert next(stream)
    stream.close()
    assert not [thread for thread in threading.enumerate() if thread not in existing]


def test_ask_router_is_composed_with_http_app(monkeypatch: pytest.MonkeyPatch) -> None:
    import gobby.servers.routes as routes
    import gobby.servers.routes.ask as ask_routes
    from gobby.servers._app_routes import register_routes

    included: list[str] = []
    for name in routes.__all__:
        if name.startswith("create_"):
            monkeypatch.setattr(routes, name, lambda *_args, _name=name: _name)
    monkeypatch.setattr(ask_routes, "create_ask_router", lambda *_args: "create_ask_router")

    app = SimpleNamespace(include_router=included.append)
    register_routes(cast(FastAPI, app), cast("HTTPServer", SimpleNamespace()))

    assert "create_ask_router" in included

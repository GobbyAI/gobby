from __future__ import annotations

import asyncio
import json
import os
import socket
import tarfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast
from unittest.mock import patch

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from gobby.ask.errors import AskLifecycleConflict, AskRunNotFound
from gobby.ask.service import AskService
from gobby.mcp_proxy.tools.ask import create_ask_registry
from gobby.servers.auth_service import AuthService
from gobby.servers.middleware.auth import AuthMiddleware
from gobby.servers.routes.ask import create_ask_router
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.utils.local_token import issue_agent_api_token, issue_tool_api_token
from gobby.utils.project_context import reset_project_context, set_project_context
from tests.ask.service_support import (
    CompletingPipelineExecutor,
    RecordingCompletionRegistry,
    build_ask_service,
)
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    insert_overlay,
    install_isolated_checkout_project,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
DEADLINE = "2026-09-09T12:10:00Z"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"

_AskCli = Callable[..., Awaitable[tuple[int, dict[str, Any], str]]]

_AskOperation = Literal["start", "get", "wait", "resume", "cancel", "export"]
_ManagedOwner = Literal["agent_run", "managed_execution"]


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
    def __init__(self, publication_root: Path | None = None) -> None:
        self.operations: list[str] = []
        self.start_call: tuple[Any, Path, str] | None = None
        self.wait_call: tuple[str, str, float | None] | None = None
        self.cancel_calls: list[str] = []
        self.errors: dict[str, Exception] = {}
        self._publication_root = publication_root

    def _raise_for(self, operation: str) -> None:
        error = self.errors.get(operation)
        if error is not None:
            raise error

    async def start(self, request: Any, *, project_root: Path, caller_session_id: str) -> _Result:
        self.operations.append("start")
        self._raise_for("start")
        self.start_call = (request, project_root, caller_session_id)
        return _Result("running")

    def get(self, run_id: str, *, project_id: str) -> _Result:
        self.operations.append("get")
        self._raise_for("get")
        return _Result("running")

    async def wait(self, run_id: str, *, project_id: str, timeout: float | None = None) -> _Result:
        self.operations.append("wait")
        self._raise_for("wait")
        self.wait_call = (run_id, project_id, timeout)
        return _Result("completed", outcome="unknown")

    async def resume(self, run_id: str, **_: Any) -> _Result:
        self.operations.append("resume")
        self._raise_for("resume")
        return _Result("running")

    async def cancel(self, run_id: str, **_: Any) -> _Result:
        self.operations.append("cancel")
        self._raise_for("cancel")
        self.cancel_calls.append(run_id)
        return _Result("cancelled")

    def publication_root(self, run_id: str, *, project_id: str) -> Path:
        self.operations.append("export")
        self._raise_for("export")
        if self._publication_root is None:
            raise AssertionError("publication root was not configured")
        return self._publication_root


@dataclass
class _AuthHarness:
    client: TestClient
    service: _AskService
    agent_headers: dict[str, str]
    managed_execution_headers: dict[str, str]
    ask_principal_headers: dict[str, str]
    operator_headers: dict[str, str]
    project_id: str
    resolved_projects: list[str]
    resolved_project_roots: list[str]
    invalidate_after_auth: Callable[[_ManagedOwner], None]


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture
def authenticated_ask_harness(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    project_id = str(sample_project["id"])
    token_file = tmp_path / "local_cli_token"
    token_file.write_text("operator-token")
    AuthStore(temp_db).set_local_api_token_hash(hash_token("operator-token"))
    auth_service = AuthService(lambda: temp_db, token_file=token_file)
    managed_execution_live = [True]
    original_fetchone = temp_db.fetchone

    def fetchone(
        query: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Any:
        if "managed_execution_is_login_capable" in query:
            return {"login_capable": managed_execution_live[0]}
        return original_fetchone(query, params)

    monkeypatch.setattr(temp_db, "fetchone", fetchone)
    publication_root = tmp_path / "publication"
    publication_root.mkdir()
    (publication_root / "answer.md").write_text("verified answer\n")
    ask_service = _AskService(publication_root)
    resolved_projects: list[str] = []
    resolved_project_roots: list[str] = []

    def resolve_service(resolved_project_id: str) -> _AskService:
        resolved_projects.append(resolved_project_id)
        return ask_service

    def resolve_project_root(resolved_project_id: str) -> Path:
        resolved_project_roots.append(resolved_project_id)
        return tmp_path

    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        parent = session_manager.register(
            external_id="ask-route-auth-parent",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=project_id,
        )
        manager = LocalAgentRunManager(temp_db)
        ordinary_run = manager.create(
            parent_session_id=parent.id,
            provider="codex",
            prompt="Call the public Ask API",
        )
        ask_principal_run = manager.create(
            parent_session_id=parent.id,
            provider="codex",
            prompt="Attempt recursive Ask lifecycle access",
            workflow_name="native-ask",
            agent_name="ask-investigator",
        )
        managed_execution_id = "33333333-3333-4333-8333-333333333333"
        pending_invalidation: list[_ManagedOwner | None] = [None]

        def headers_for(run_id: str) -> dict[str, str]:
            token = issue_agent_api_token(
                "operator-token",
                agent_run_id=run_id,
                session_id=parent.id,
                project_id=project_id,
            )
            return {
                "Authorization": f"Bearer {token}",
                "X-Gobby-Agent-Run-Id": run_id,
                "X-Gobby-Session-Id": parent.id,
                "X-Gobby-Caller-Project-Id": project_id,
            }

        managed_execution_token = issue_tool_api_token(
            "operator-token",
            managed_execution_id=managed_execution_id,
            session_id=parent.id,
            project_id=project_id,
            timeout_seconds=60,
        )
        managed_execution_headers = {
            "Authorization": f"Bearer {managed_execution_token}",
            "X-Gobby-Managed-Execution-Id": managed_execution_id,
            "X-Gobby-Session-Id": parent.id,
            "X-Gobby-Caller-Project-Id": project_id,
        }

        def invalidate_after_auth(owner: _ManagedOwner) -> None:
            pending_invalidation[0] = owner

        async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
            result = await asyncio.to_thread(func, *args, **kwargs)
            owner = pending_invalidation[0]
            if owner is not None and getattr(func, "__name__", None) == "authenticate":
                pending_invalidation[0] = None
                if owner == "agent_run":
                    await asyncio.to_thread(manager.complete, ordinary_run.id)
                else:
                    managed_execution_live[0] = False
            return result

        server = SimpleNamespace(
            auth_service=auth_service,
            services=SimpleNamespace(
                get_ask_service=resolve_service,
                http_admission_closed=False,
                session_manager=session_manager,
            ),
            run_db=run_db,
        )
        app = FastAPI()
        app.include_router(
            create_ask_router(
                cast("HTTPServer", server),
                project_root_resolver=resolve_project_root,
            )
        )
        app.add_middleware(AuthMiddleware, server=cast("HTTPServer", server))

        with TestClient(app, raise_server_exceptions=False) as client:
            yield _AuthHarness(
                client=client,
                service=ask_service,
                agent_headers=headers_for(ordinary_run.id),
                managed_execution_headers=managed_execution_headers,
                ask_principal_headers=headers_for(ask_principal_run.id),
                operator_headers={
                    "Authorization": "Bearer operator-token",
                    "X-Gobby-Session-Id": parent.id,
                },
                project_id=project_id,
                resolved_projects=resolved_projects,
                resolved_project_roots=resolved_project_roots,
                invalidate_after_auth=invalidate_after_auth,
            )


@dataclass
class _RealAskHarness:
    """An HTTP surface over a real ``AskService``, stubbed only at the spawn boundary."""

    client: AsyncClient
    service: AskService
    executor: CompletingPipelineExecutor
    completions: RecordingCompletionRegistry
    headers: dict[str, str]
    project_id: str
    app: FastAPI


@pytest.fixture
async def real_ask_harness(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> AsyncIterator[_RealAskHarness]:
    project_id = str(sample_project["id"])
    token_file = tmp_path / "local_cli_token"
    token_file.write_text("operator-token")
    AuthStore(temp_db).set_local_api_token_hash(hash_token("operator-token"))
    auth_service = AuthService(lambda: temp_db, token_file=token_file)
    ask = build_ask_service(temp_db, project_id=project_id, state_root=tmp_path / "state")
    insert_isolated_machine(temp_db, LOCAL_MACHINE_ID)
    LocalProjectCheckoutManager(temp_db).register(LOCAL_MACHINE_ID, project_id, str(tmp_path))

    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        parent = session_manager.register(
            external_id="ask-route-real-parent",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=project_id,
        )
        agent_run = LocalAgentRunManager(temp_db).create(
            parent_session_id=parent.id,
            provider="codex",
            prompt="Call the public Ask API",
        )
        token = issue_agent_api_token(
            "operator-token",
            agent_run_id=agent_run.id,
            session_id=parent.id,
            project_id=project_id,
        )
        server = SimpleNamespace(
            auth_service=auth_service,
            services=SimpleNamespace(
                get_ask_service=lambda _project_id: ask.service,
                http_admission_closed=False,
                database=temp_db,
                session_manager=session_manager,
            ),
            run_db=_run_db,
        )
        app = FastAPI()
        app.include_router(
            create_ask_router(
                cast("HTTPServer", server),
            )
        )
        app.add_middleware(AuthMiddleware, server=cast("HTTPServer", server))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield _RealAskHarness(
                client=client,
                service=ask.service,
                executor=ask.executor,
                completions=ask.completions,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Gobby-Agent-Run-Id": agent_run.id,
                    "X-Gobby-Session-Id": parent.id,
                    "X-Gobby-Caller-Project-Id": project_id,
                },
                project_id=project_id,
                app=app,
            )


def _request_ask(
    client: TestClient,
    operation: _AskOperation,
    *,
    project_id: str,
    headers: dict[str, str],
) -> httpx2.Response:
    if operation == "start":
        return client.post(
            "/api/ask/runs",
            headers=headers,
            json={"question": "Where is authority enforced?", "project_id": project_id},
        )
    suffix = "" if operation == "get" else f"/{operation}"
    return client.request(
        "POST" if operation in {"resume", "cancel"} else "GET",
        f"/api/ask/runs/ask-run-1{suffix}",
        headers=headers,
        params={"project_id": project_id},
    )


async def _start_gated_run(harness: _RealAskHarness, question: str) -> dict[str, Any]:
    """Start a run whose pipeline execution is held open by the harness gate."""
    response = await harness.client.post(
        "/api/ask/runs",
        headers=harness.headers,
        json={
            "question": question,
            "project_id": harness.project_id,
            "timeout_seconds": 600,
            "retrieval_mode": "deterministic",
        },
    )
    assert response.status_code == 202
    # The run is live once the executor has marked its execution RUNNING; waiting on
    # that keeps every later assertion free of a scheduling race.
    await asyncio.wait_for(harness.executor.running.wait(), timeout=10)
    payload: dict[str, Any] = response.json()
    return payload


@pytest.mark.parametrize("location", ["primary", "worktree", "foreign", "unregistered"])
async def test_start_binds_only_to_authorized_caller_checkout(
    real_ask_harness: _RealAskHarness,
    temp_db: HubDatabase,
    tmp_path: Path,
    location: str,
) -> None:
    harness = real_ask_harness
    root = tmp_path if location == "primary" else tmp_path / location
    root.mkdir(exist_ok=True)
    if location in {"worktree", "foreign"}:
        machine = LOCAL_MACHINE_ID if location == "worktree" else insert_isolated_machine(temp_db)
        insert_overlay(
            temp_db,
            project_id=harness.project_id,
            machine_id=machine,
            path=str(root),
            kind="worktree",
        )
    response = await harness.client.post(
        "/api/ask/runs",
        headers=harness.headers,
        json={
            "question": "Which checkout supplies the evidence?",
            "project_id": harness.project_id,
            "project_path": str(root),
        },
    )
    if location in {"foreign", "unregistered"}:
        assert response.status_code == 403
        assert "not a registered worktree or clone" in response.json()["detail"]
        return
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    record = harness.service.storage.get(run_id)
    assert record is not None
    assert record.binding.repository_root == str(root.resolve())
    assert record.binding.project_id == harness.project_id
    await harness.service.wait(run_id, project_id=harness.project_id, timeout=5)


@pytest.fixture
async def real_ask_cli(
    real_ask_harness: _RealAskHarness, tmp_path: Path
) -> AsyncIterator[tuple[_AskCli, str]]:
    harness = real_ask_harness
    binary = Path(os.environ.get("GOBBY_GCODE_BIN", "target/debug/gcode")).resolve(strict=True)
    home = tmp_path / "cli-home"
    home.mkdir()
    marker = tmp_path / ".gobby"
    marker.mkdir()
    (marker / "project.json").write_text(
        json.dumps({"id": harness.project_id, "name": "ask-service-test"})
    )
    git = await asyncio.create_subprocess_exec("git", "init", "--quiet", str(tmp_path))
    assert await git.wait() == 0
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets)
            ready.set()

    server = Server(uvicorn.Config(harness.app, log_level="error", lifespan="off", ws="none"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("GOBBY_", "GIT_"))
    }
    env.update(
        GOBBY_HOME=str(home),
        GOBBY_DAEMON_URL=url,
        GOBBY_SESSION_ID=harness.headers["X-Gobby-Session-Id"],
        GOBBY_AGENT_RUN_ID=harness.headers["X-Gobby-Agent-Run-Id"],
        GOBBY_AGENT_API_TOKEN=harness.headers["Authorization"].removeprefix("Bearer "),
    )

    async def cli(*args: str) -> tuple[int, dict[str, Any], str]:
        process = await asyncio.create_subprocess_exec(
            str(binary),
            "--format",
            "json",
            "--project",
            str(tmp_path),
            "ask",
            *args,
            cwd=tmp_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        assert process.returncode is not None
        return process.returncode, json.loads(stdout), stderr.decode()

    try:
        await asyncio.wait_for(ready.wait(), 5)
        yield cli, url
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()


async def test_installed_cli_lifecycle_against_authenticated_service(
    real_ask_harness: _RealAskHarness, real_ask_cli: tuple[_AskCli, str], tmp_path: Path
) -> None:
    from gobby.ask.artifacts import AskArtifactStore
    from gobby.ask.publication import publish_answer, replay_publication
    from gobby.ask.stages import AskStage
    from gobby.ask.validation import validate_claims, validate_review
    from tests.ask.test_validation import _valid_case

    harness = real_ask_harness
    cli, url = real_ask_cli
    harness.executor.fail_next = True
    code, interrupted, stderr = await cli("What does alpha return?")
    assert code == 2
    assert interrupted["status"] == "failed"
    run_id = interrupted["run_id"]
    code, completed, stderr = await cli("--resume", run_id)
    assert code == 0, stderr
    assert completed["run_id"] == run_id
    assert completed["binding"] == interrupted["binding"]
    assert completed["status"] == "completed"
    # The controlled executor completes without publishing an answer; the
    # lifecycle adapter must preserve the service's null outcome verbatim.
    assert completed["answer_outcome"] is None
    run_id = completed["run_id"]
    code, fetched, stderr = await cli("--status", run_id)
    assert code == 0, stderr
    assert fetched == harness.service.get(run_id, project_id=harness.project_id).model_dump(
        mode="json"
    )

    # Seed a validated publication at the agent boundary; export still traverses
    # the installed CLI, authenticated HTTP and real service verifier.
    draft, evidence, blobs, review = _valid_case(run_id=run_id, project_id=harness.project_id)
    deterministic = validate_claims(draft, evidence, pinned_blobs=blobs)
    reviewed = validate_review(draft, evidence, deterministic, review)
    published = publish_answer(
        AskArtifactStore(tmp_path / "publication-state", harness.project_id, run_id),
        draft,
        evidence,
        deterministic,
        reviewed,
        request={"question": draft.question},
        binding=evidence.repository_binding.model_dump(mode="json"),
        profiles={"investigator": "profile-a", "reviewer": "profile-b"},
        tool_identities=("gobby-code@0.5.0",),
        attempt_history=({"attempt": 1, "status": "reviewed"},),
    )
    harness.service.stages.checkpoint(
        run_id,
        stage=AskStage.PUBLISH,
        boundary_id=f"publish:{published.manifest_sha256}",
        publication={"root": str(published.root), "manifest_sha256": published.manifest_sha256},
        answer_outcome=published.outcome,
    )
    destination = tmp_path / "requested-export"
    code, exported, stderr = await cli("--export", run_id, "--output", str(destination))
    assert code == 0, stderr
    archive_path = destination / f"ask-{run_id}.tar"
    assert exported == {"run_id": run_id, "status": "exported", "output": str(archive_path)}
    assert archive_path.is_file()
    unpacked = tmp_path / "unpacked-export"
    with tarfile.open(archive_path) as archive:
        archive.extractall(unpacked, filter="data")
    replay = replay_publication(unpacked)
    assert replay.manifest_sha256 == published.manifest_sha256
    source_files = {
        p.relative_to(published.root): p.read_bytes()
        for p in published.root.rglob("*")
        if p.is_file()
    }
    assert {
        p.relative_to(unpacked): p.read_bytes() for p in unpacked.rglob("*") if p.is_file()
    } == source_files

    release = asyncio.Event()
    harness.executor.gate(release)
    code, started, stderr = await cli("Hold this run for cancellation", "--background")
    assert code == 0, stderr
    pending_id = started["run_id"]
    await asyncio.wait_for(harness.executor.running.wait(), 5)
    async with AsyncClient(base_url=url, headers=harness.headers) as client:
        timed_out = await client.get(
            f"/api/ask/runs/{pending_id}/wait",
            params={"project_id": harness.project_id, "timeout_seconds": 0.01},
        )
        assert timed_out.status_code == 408
        missing = await client.get(
            "/api/ask/runs/not-a-run", params={"project_id": harness.project_id}
        )
        assert missing.status_code == 404
        denied = await client.get(f"/api/ask/runs/{pending_id}", params={"project_id": PROJECT_ID})
        assert denied.status_code == 403
        removed_flag = await client.post(
            "/api/ask/runs",
            json={"project_id": harness.project_id, "question": "Where?", "commit_ref": "HEAD"},
        )
        assert removed_flag.status_code == 422
    code, cancelled, stderr = await cli("--cancel", pending_id)
    assert code == 2
    assert cancelled["status"] == "cancelled"
    assert "ask_cancelled" in stderr
    release.set()


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled"])
async def test_shared_run_contract_and_event_driven_wait(
    real_ask_harness: _RealAskHarness,
    real_ask_cli: tuple[_AskCli, str],
    tmp_path: Path,
    terminal_status: str,
) -> None:
    release = asyncio.Event()
    real_ask_harness.executor.gate(release)
    started = await _start_gated_run(real_ask_harness, "Where is the source of truth?")
    run_id = started["run_id"]
    # 202 answers before the pipeline finishes, so the run is still in flight.
    assert started["status"] not in {"completed", "failed", "cancelled"}
    assert started["deadline_at"]

    fetched = await real_ask_harness.client.get(
        f"/api/ask/runs/{run_id}",
        headers=real_ask_harness.headers,
        params={"project_id": real_ask_harness.project_id},
    )
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "running"
    direct = real_ask_harness.service.get(run_id, project_id=real_ask_harness.project_id)
    assert fetched.json() == direct.model_dump(mode="json")
    assert fetched.json()["deadline_at"] == started["deadline_at"]
    registry = create_ask_registry(
        lambda _project_id: real_ask_harness.service,
        project_root_resolver=lambda _project_id, _project_path: tmp_path,
    )

    async def assert_adapters(expected: dict[str, Any]) -> None:
        code, cli_record, stderr = await real_ask_cli[0]("--status", run_id)
        assert code == (2 if expected["status"] == "cancelled" else 0), stderr
        if expected["status"] == "cancelled":
            assert "ask_cancelled" in stderr
        context = set_project_context({"id": real_ask_harness.project_id})
        try:
            mcp_record = await registry.call("get_ask_run", {"run_id": run_id})
        finally:
            reset_project_context(context)
        assert cli_record == mcp_record == expected

    await assert_adapters(fetched.json())
    # The route, not the caller, chooses the two Ask profiles.
    record = real_ask_harness.service.storage.get(run_id)
    assert record is not None
    assert record.investigator.identifier == "ask-investigator"
    assert record.reviewer.identifier == "ask-reviewer"

    waiting = asyncio.ensure_future(
        real_ask_harness.client.get(
            f"/api/ask/runs/{run_id}/wait",
            headers=real_ask_harness.headers,
            params={"project_id": real_ask_harness.project_id, "timeout_seconds": 10},
        )
    )
    # A polling wait would answer with the running record; an event-driven one parks
    # until the completion event fires, so this must still be pending.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(waiting), timeout=0.25)
    # And it is parked on the shared completion registry rather than re-reading
    # storage, which is what lets the pipeline wake it instead of a poll interval.
    assert real_ask_harness.completions.awaited == [f"ask:{run_id}"]

    # Disconnecting one foreground client must not cancel the durable run.
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert (
        real_ask_harness.service.get(run_id, project_id=real_ask_harness.project_id).status
        == "running"
    )
    waiting = asyncio.ensure_future(
        real_ask_harness.client.get(
            f"/api/ask/runs/{run_id}/wait",
            headers=real_ask_harness.headers,
            params={"project_id": real_ask_harness.project_id, "timeout_seconds": 10},
        )
    )
    if terminal_status == "cancelled":
        cancelled = await real_ask_harness.client.post(
            f"/api/ask/runs/{run_id}/cancel",
            headers=real_ask_harness.headers,
            params={"project_id": real_ask_harness.project_id},
        )
        assert cancelled.status_code == 200
    release.set()
    waited = await waiting
    assert waited.status_code == 200
    assert waited.json()["status"] == terminal_status
    assert waited.json()["run_id"] == run_id
    assert waited.json()["deadline_at"] == started["deadline_at"]
    await assert_adapters(waited.json())
    if terminal_status == "cancelled":
        assert waited.json()["typed_error"] is None


async def test_waiter_disconnect_does_not_cancel_the_run(
    real_ask_harness: _RealAskHarness,
) -> None:
    release = asyncio.Event()
    real_ask_harness.executor.gate(release)
    started = await _start_gated_run(real_ask_harness, "Does a hung up caller kill the run?")
    run_id = started["run_id"]

    abandoned = asyncio.ensure_future(
        real_ask_harness.client.get(
            f"/api/ask/runs/{run_id}/wait",
            headers=real_ask_harness.headers,
            params={"project_id": real_ask_harness.project_id, "timeout_seconds": 10},
        )
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(abandoned), timeout=0.25)
    assert real_ask_harness.completions.awaited == [f"ask:{run_id}"]
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned

    release.set()
    rejoined = await real_ask_harness.service.wait(
        run_id, project_id=real_ask_harness.project_id, timeout=10
    )
    assert rejoined.status == "completed"
    assert rejoined.run_id == run_id


@pytest.mark.parametrize("owner", ["agent_run", "managed_execution"])
@pytest.mark.parametrize("operation", ["start", "get", "wait", "resume", "cancel", "export"])
def test_signed_managed_token_can_access_same_project_ask_routes(
    operation: _AskOperation,
    owner: _ManagedOwner,
    authenticated_ask_harness: _AuthHarness,
) -> None:
    headers = (
        authenticated_ask_harness.agent_headers
        if owner == "agent_run"
        else authenticated_ask_harness.managed_execution_headers
    )
    response = _request_ask(
        authenticated_ask_harness.client,
        operation,
        project_id=authenticated_ask_harness.project_id,
        headers=headers,
    )

    assert response.status_code == (202 if operation == "start" else 200)
    assert authenticated_ask_harness.resolved_projects == [authenticated_ask_harness.project_id]


@pytest.mark.parametrize("owner", ["agent_run", "managed_execution"])
@pytest.mark.parametrize("target", ["same", "foreign"])
@pytest.mark.parametrize("operation", ["start", "get", "wait", "resume", "cancel", "export"])
def test_managed_token_revoked_between_middleware_and_route_fails_closed(
    operation: _AskOperation,
    target: Literal["same", "foreign"],
    owner: _ManagedOwner,
    authenticated_ask_harness: _AuthHarness,
) -> None:
    harness = authenticated_ask_harness
    headers = harness.agent_headers if owner == "agent_run" else harness.managed_execution_headers
    project_id = harness.project_id if target == "same" else "foreign-project"
    harness.invalidate_after_auth(owner)

    response = _request_ask(
        harness.client,
        operation,
        project_id=project_id,
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Ask project access denied"}
    assert harness.resolved_projects == []
    assert harness.resolved_project_roots == []
    assert harness.service.operations == []


@pytest.mark.parametrize("operation", ["start", "get", "wait", "resume", "cancel", "export"])
def test_signed_managed_token_cannot_substitute_body_or_query_project(
    operation: _AskOperation,
    authenticated_ask_harness: _AuthHarness,
) -> None:
    response = _request_ask(
        authenticated_ask_harness.client,
        operation,
        project_id="foreign-project",
        headers=authenticated_ask_harness.agent_headers,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Ask project access denied"}
    assert authenticated_ask_harness.resolved_projects == []


@pytest.mark.parametrize("operation", ["start", "get", "wait", "resume", "cancel", "export"])
def test_operator_token_retains_cross_project_ask_access(
    operation: _AskOperation,
    authenticated_ask_harness: _AuthHarness,
    temp_db: HubDatabase,
    tmp_path: Path,
    session_manager: SessionManager,
) -> None:
    selected = install_isolated_checkout_project(
        temp_db,
        tmp_path / "operator-selected",
        machine_id=LOCAL_MACHINE_ID,
        name="operator-selected-project",
    )
    selected_id = str(selected.project.id)
    response = _request_ask(
        authenticated_ask_harness.client,
        operation,
        project_id=selected_id,
        headers=authenticated_ask_harness.operator_headers,
    )

    assert response.status_code == (202 if operation == "start" else 200)
    assert authenticated_ask_harness.resolved_projects == [selected_id]
    if operation == "start":
        start_call = authenticated_ask_harness.service.start_call
        assert start_call is not None
        caller = session_manager.get(start_call[2])
        assert caller is not None
        assert caller.project_id == selected_id
        assert caller.id != authenticated_ask_harness.operator_headers["X-Gobby-Session-Id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_operator_ask_without_session_reuses_project_launcher(
    real_ask_harness: _RealAskHarness,
    session_manager: SessionManager,
) -> None:
    harness = real_ask_harness
    callers = []
    for question in ("What is the shared platform?", "Which systems remain standalone?"):
        response = await harness.client.post(
            "/api/ask/runs",
            headers={"Authorization": "Bearer operator-token"},
            json={"question": question, "project_id": harness.project_id},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        inputs = harness.service.storage.execution_inputs(run_id)
        callers.append(inputs["caller_session_id"])
        result = await harness.service.wait(run_id, project_id=harness.project_id, timeout=5)
        assert result.status == "completed"
    assert callers[0] == callers[1]
    caller = session_manager.get(callers[0])
    assert caller is not None
    assert caller.project_id == harness.project_id


@pytest.mark.parametrize("operation", ["start", "get", "wait", "resume", "cancel", "export"])
def test_managed_ask_principal_cannot_recursively_call_public_lifecycle(
    operation: _AskOperation,
    authenticated_ask_harness: _AuthHarness,
) -> None:
    response = _request_ask(
        authenticated_ask_harness.client,
        operation,
        project_id=authenticated_ask_harness.project_id,
        headers=authenticated_ask_harness.ask_principal_headers,
    )

    assert response.status_code == 401
    assert authenticated_ask_harness.resolved_projects == []


@pytest.mark.parametrize(
    ("operation", "error", "expected_status"),
    [
        ("get", AskRunNotFound("Ask run not found: missing"), 404),
        ("resume", AskLifecycleConflict("Ask run is already running"), 409),
        ("export", AskLifecycleConflict("Ask run has no completed publication"), 409),
        ("wait", TimeoutError("timed out waiting for Ask run"), 408),
        ("cancel", PermissionError("Ask cancellation denied"), 403),
    ],
)
def test_ask_http_boundary_translates_typed_failures(
    operation: _AskOperation,
    error: Exception,
    expected_status: int,
    authenticated_ask_harness: _AuthHarness,
) -> None:
    authenticated_ask_harness.service.errors[operation] = error

    response = _request_ask(
        authenticated_ask_harness.client,
        operation,
        project_id=authenticated_ask_harness.project_id,
        headers=authenticated_ask_harness.operator_headers,
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": str(error)}


def test_ask_http_boundary_preserves_unexpected_500(
    authenticated_ask_harness: _AuthHarness,
) -> None:
    authenticated_ask_harness.service.errors["get"] = RuntimeError("storage corruption")

    response = _request_ask(
        authenticated_ask_harness.client,
        "get",
        project_id=authenticated_ask_harness.project_id,
        headers=authenticated_ask_harness.operator_headers,
    )

    assert response.status_code == 500
    assert response.text == "Internal Server Error"


@pytest.mark.parametrize(
    "invalid_field",
    [
        {"timeout_seconds": 0},
        {"question": "   "},
    ],
)
def test_start_rejects_invalid_request_before_service_access(
    invalid_field: dict[str, object],
    authenticated_ask_harness: _AuthHarness,
) -> None:
    body: dict[str, object] = {
        "question": "Can this run forever?",
        "project_id": authenticated_ask_harness.project_id,
    }
    body.update(invalid_field)
    response = authenticated_ask_harness.client.post(
        "/api/ask/runs",
        headers=authenticated_ask_harness.agent_headers,
        json=body,
    )

    assert response.status_code == 422
    assert authenticated_ask_harness.resolved_projects == []


def test_wait_rejects_invalid_timeout_before_service_access(
    authenticated_ask_harness: _AuthHarness,
) -> None:
    response = authenticated_ask_harness.client.get(
        "/api/ask/runs/ask-run-1/wait",
        headers=authenticated_ask_harness.agent_headers,
        params={
            "project_id": authenticated_ask_harness.project_id,
            "timeout_seconds": 0,
        },
    )

    assert response.status_code == 422
    assert authenticated_ask_harness.resolved_projects == []


@pytest.mark.parametrize("operation", ["get", "wait", "resume", "cancel", "export"])
def test_unavailable_project_never_uses_default_service(operation: str, tmp_path: Path) -> None:
    from gobby.servers.routes.ask import create_ask_router

    resolved_projects: list[str] = []

    def resolve_service(project_id: str) -> None:
        resolved_projects.append(project_id)

    server = SimpleNamespace(
        auth_service=SimpleNamespace(request_principal=lambda _request: None),
        services=SimpleNamespace(ask_service=_AskService(), get_ask_service=resolve_service),
        run_db=_run_db,
    )
    app = FastAPI()
    app.include_router(
        create_ask_router(
            cast("HTTPServer", server), project_root_resolver=lambda _project_id: tmp_path
        )
    )
    with TestClient(app) as client:
        suffix = "" if operation == "get" else f"/{operation}"
        response = client.request(
            "POST" if operation in {"resume", "cancel"} else "GET",
            f"/api/ask/runs/ask-run-1{suffix}",
            params={"project_id": PROJECT_ID},
            headers={"X-Gobby-Session-Id": SESSION_ID},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Ask service is unavailable"}
    assert resolved_projects == [PROJECT_ID]


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

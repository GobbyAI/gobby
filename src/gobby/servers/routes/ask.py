"""Authenticated HTTP adapters for native Ask runs."""

from __future__ import annotations

import os
import tarfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gobby.ask.contracts import AskRequest, RetrievalMode
from gobby.servers.http import HTTPServer

_INVESTIGATOR_PROFILE = "ask-investigator"
_REVIEWER_PROFILE = "ask-reviewer"
_SESSION_HEADER = "X-Gobby-Session-Id"


class StartAskRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    project_id: str
    commit_ref: str = "HEAD"
    timeout_seconds: float = Field(default=600, gt=0, allow_inf_nan=False)
    retrieval_mode: Literal["deterministic", "hybrid"] = "deterministic"
    idempotency_key: str | None = None


def _payload(result: Any) -> dict[str, Any]:
    return dict(result.model_dump(mode="json"))


def _caller_session_id(server: HTTPServer, request: Request) -> str:
    auth_service = getattr(server, "auth_service", None)
    claims = auth_service.verified_agent_claims(request) if auth_service is not None else None
    session_id = claims.session_id if claims is not None else request.headers.get(_SESSION_HEADER)
    if not session_id:
        raise HTTPException(status_code=400, detail="X-Gobby-Session-Id is required")
    return session_id


def _tar_stream(root: Path) -> Iterator[bytes]:
    read_fd, write_fd = os.pipe()

    def produce() -> None:
        try:
            with (
                os.fdopen(write_fd, "wb") as output,
                tarfile.open(fileobj=output, mode="w|") as tar,
            ):
                for path in sorted(
                    candidate for candidate in root.rglob("*") if candidate.is_file()
                ):
                    tar.add(path, arcname=path.relative_to(root), recursive=False)
        except BrokenPipeError:
            pass

    threading.Thread(target=produce, name="ask-export", daemon=True).start()
    with os.fdopen(read_fd, "rb") as source:
        while chunk := source.read(64 * 1024):
            yield chunk


def create_ask_router(
    server: HTTPServer,
    *,
    project_root_resolver: Callable[[str], Path] | None = None,
) -> APIRouter:
    """Create Ask lifecycle and immutable-export routes."""
    router = APIRouter(prefix="/api/ask/runs", tags=["ask"])

    def service() -> Any:
        resolved = getattr(server.services, "ask_service", None)
        if resolved is None:
            raise HTTPException(status_code=503, detail="Ask service is unavailable")
        return resolved

    async def project_root(project_id: str) -> Path:
        if project_root_resolver is not None:
            return project_root_resolver(project_id)
        from gobby.storage.project_checkouts import require_root
        from gobby.storage.workspace_machine_scope import require_local_machine_id

        machine_id = require_local_machine_id(
            None,
            resource_kind="project_checkout",
            resource_id=project_id,
        )
        root = await server.run_db(require_root, server.services.database, project_id, machine_id)
        return Path(root)

    @router.post("", status_code=202)
    async def start_ask_run(body: StartAskRunRequest, request: Request) -> dict[str, Any]:
        retrieval_mode = (
            RetrievalMode.AUDITED_HYBRID
            if body.retrieval_mode == "hybrid"
            else RetrievalMode.DETERMINISTIC
        )
        ask_request = AskRequest(
            question=body.question,
            project_id=body.project_id,
            commit_ref=body.commit_ref,
            timeout_seconds=body.timeout_seconds,
            retrieval_mode=retrieval_mode,
            investigator_profile=_INVESTIGATOR_PROFILE,
            reviewer_profile=_REVIEWER_PROFILE,
            idempotency_key=body.idempotency_key,
        )
        result = await service().start(
            ask_request,
            project_root=await project_root(body.project_id),
            caller_session_id=_caller_session_id(server, request),
        )
        return _payload(result)

    @router.get("/{run_id}")
    async def get_ask_run(run_id: str, project_id: str) -> dict[str, Any]:
        return _payload(service().get(run_id, project_id=project_id))

    @router.get("/{run_id}/wait")
    async def wait_for_ask_run(
        run_id: str,
        project_id: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        result = await service().wait(
            run_id,
            project_id=project_id,
            timeout=timeout_seconds,
        )
        return _payload(result)

    @router.post("/{run_id}/resume")
    async def resume_ask_run(run_id: str, project_id: str, request: Request) -> dict[str, Any]:
        result = await service().resume(
            run_id,
            project_id=project_id,
            caller_session_id=_caller_session_id(server, request),
        )
        return _payload(result)

    @router.post("/{run_id}/cancel")
    async def cancel_ask_run(run_id: str, project_id: str, request: Request) -> dict[str, Any]:
        result = await service().cancel(
            run_id,
            project_id=project_id,
            caller_session_id=_caller_session_id(server, request),
        )
        return _payload(result)

    @router.get("/{run_id}/export")
    async def export_ask_run(run_id: str, project_id: str) -> StreamingResponse:
        root = service().publication_root(run_id, project_id=project_id)
        return StreamingResponse(
            _tar_stream(root),
            media_type="application/x-tar",
            headers={
                "Content-Disposition": f'attachment; filename="ask-{run_id}.tar"',
                "X-Gobby-Ask-Run-Id": run_id,
            },
        )

    return router

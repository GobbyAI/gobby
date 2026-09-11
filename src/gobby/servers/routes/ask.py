"""Authenticated HTTP adapters for native Ask runs."""

from __future__ import annotations

import os
import tarfile
import threading
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.ask.contracts import AskRequest, RetrievalMode
from gobby.ask.errors import AskLifecycleConflict, AskRunNotFound
from gobby.servers.http import HTTPServer
from gobby.utils.local_token import AgentApiTokenClaims

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

    @field_validator("question", "project_id", "commit_ref")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


def _payload(result: Any) -> dict[str, Any]:
    return dict(result.model_dump(mode="json"))


def _caller_session_id(request: Request, claims: AgentApiTokenClaims | None) -> str:
    session_id = claims.session_id if claims is not None else request.headers.get(_SESSION_HEADER)
    if not session_id:
        raise HTTPException(status_code=400, detail="X-Gobby-Session-Id is required")
    return session_id


async def _authorize_project(
    server: HTTPServer,
    request: Request,
    project_id: str,
) -> AgentApiTokenClaims | None:
    auth_service = getattr(server, "auth_service", None)
    if auth_service is None:
        raise HTTPException(status_code=503, detail="Authentication service is unavailable")
    principal = cast(
        AgentApiTokenClaims | None | Literal[False],
        await server.run_db(auth_service.request_principal, request),
    )
    if principal is False or (principal is not None and principal.project_id != project_id):
        raise HTTPException(status_code=403, detail="Ask project access denied")
    return principal


def _ask_http_exception(error: Exception) -> HTTPException:
    if isinstance(error, AskRunNotFound):
        status_code = 404
    elif isinstance(error, AskLifecycleConflict):
        status_code = 409
    elif isinstance(error, TimeoutError):
        status_code = 408
    elif isinstance(error, PermissionError):
        status_code = 403
    else:
        raise error
    return HTTPException(status_code=status_code, detail=str(error))


_TRANSLATED_ASK_ERRORS = (AskRunNotFound, AskLifecycleConflict, TimeoutError, PermissionError)


def _tar_stream(root: Path) -> Generator[bytes]:
    read_fd, write_fd = os.pipe()
    failures: list[Exception] = []

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
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=produce, name="ask-export", daemon=True)
    worker.start()
    try:
        with os.fdopen(read_fd, "rb") as source:
            while chunk := source.read(64 * 1024):
                yield chunk
    finally:
        worker.join()
    if failures:
        raise failures[0]


def create_ask_router(
    server: HTTPServer,
    *,
    project_root_resolver: Callable[[str], Path] | None = None,
) -> APIRouter:
    """Create Ask lifecycle and immutable-export routes."""
    router = APIRouter(prefix="/api/ask/runs", tags=["ask"])

    def service(project_id: str) -> Any:
        resolver = getattr(server.services, "get_ask_service", None)
        resolved = resolver(project_id) if resolver is not None else None
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
        claims = await _authorize_project(server, request, body.project_id)
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
        try:
            result = await service(body.project_id).start(
                ask_request,
                project_root=await project_root(body.project_id),
                caller_session_id=_caller_session_id(request, claims),
            )
        except _TRANSLATED_ASK_ERRORS as error:
            raise _ask_http_exception(error) from error
        return _payload(result)

    @router.get("/{run_id}")
    async def get_ask_run(run_id: str, project_id: str, request: Request) -> dict[str, Any]:
        await _authorize_project(server, request, project_id)
        try:
            result = service(project_id).get(run_id, project_id=project_id)
        except _TRANSLATED_ASK_ERRORS as error:
            raise _ask_http_exception(error) from error
        return _payload(result)

    @router.get("/{run_id}/wait")
    async def wait_for_ask_run(
        run_id: str,
        project_id: str,
        request: Request,
        timeout_seconds: Annotated[float | None, Query(gt=0, allow_inf_nan=False)] = None,
    ) -> dict[str, Any]:
        await _authorize_project(server, request, project_id)
        try:
            result = await service(project_id).wait(
                run_id,
                project_id=project_id,
                timeout=timeout_seconds,
            )
        except _TRANSLATED_ASK_ERRORS as error:
            raise _ask_http_exception(error) from error
        return _payload(result)

    @router.post("/{run_id}/resume")
    async def resume_ask_run(run_id: str, project_id: str, request: Request) -> dict[str, Any]:
        claims = await _authorize_project(server, request, project_id)
        try:
            result = await service(project_id).resume(
                run_id,
                project_id=project_id,
                caller_session_id=_caller_session_id(request, claims),
            )
        except _TRANSLATED_ASK_ERRORS as error:
            raise _ask_http_exception(error) from error
        return _payload(result)

    @router.post("/{run_id}/cancel")
    async def cancel_ask_run(run_id: str, project_id: str, request: Request) -> dict[str, Any]:
        claims = await _authorize_project(server, request, project_id)
        try:
            result = await service(project_id).cancel(
                run_id,
                project_id=project_id,
                caller_session_id=_caller_session_id(request, claims),
            )
        except _TRANSLATED_ASK_ERRORS as error:
            raise _ask_http_exception(error) from error
        return _payload(result)

    @router.get("/{run_id}/export")
    async def export_ask_run(run_id: str, project_id: str, request: Request) -> StreamingResponse:
        await _authorize_project(server, request, project_id)
        try:
            root = service(project_id).publication_root(run_id, project_id=project_id)
        except _TRANSLATED_ASK_ERRORS as error:
            raise _ask_http_exception(error) from error
        return StreamingResponse(
            _tar_stream(root),
            media_type="application/x-tar",
            headers={
                "Content-Disposition": f'attachment; filename="ask-{run_id}.tar"',
                "X-Gobby-Ask-Run-Id": run_id,
            },
        )

    return router

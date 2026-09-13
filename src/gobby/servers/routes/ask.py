"""Authenticated HTTP adapters for native Ask runs."""

from __future__ import annotations

import asyncio
import tarfile
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.agents.launcher_session import aget_or_create_launcher_session
from gobby.ask.contracts import AskRequest, RetrievalMode
from gobby.ask.errors import AskLifecycleConflict, AskRunNotFound
from gobby.servers.http import HTTPServer
from gobby.storage.project_checkouts import CheckoutNotFoundError, OverlayRegistrationRejectedError
from gobby.utils.local_token import AgentApiTokenClaims

_INVESTIGATOR_PROFILE = "ask-investigator"
_REVIEWER_PROFILE = "ask-reviewer"


class StartAskRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    project_id: str
    project_path: str | None = None
    timeout_seconds: float = Field(default=600, gt=0, allow_inf_nan=False)
    retrieval_mode: Literal["deterministic", "hybrid"] = "deterministic"
    idempotency_key: str | None = None

    @field_validator("question", "project_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


def _payload(result: Any) -> dict[str, Any]:
    return dict(result.model_dump(mode="json"))


async def _caller_session_id(
    server: HTTPServer, project_id: str, claims: AgentApiTokenClaims | None
) -> str:
    if claims is not None:
        return claims.session_id
    session_manager = server.services.session_manager
    if session_manager is None:
        raise HTTPException(status_code=503, detail="Session manager is unavailable")
    # Operator credentials may select another project or run outside an agent session.
    # Reuse the ordinary project launcher so grants bind to that selected project.
    return await aget_or_create_launcher_session(session_manager, project_id, "ask_launcher")


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
    if isinstance(error, (AskRunNotFound, CheckoutNotFoundError)):
        status_code = 404
    elif isinstance(error, AskLifecycleConflict):
        status_code = 409
    elif isinstance(error, TimeoutError):
        status_code = 408
    elif isinstance(error, (PermissionError, OverlayRegistrationRejectedError)):
        status_code = 403
    else:
        raise error
    return HTTPException(status_code=status_code, detail=str(error))


_TRANSLATED_ASK_ERRORS = (
    AskRunNotFound,
    AskLifecycleConflict,
    TimeoutError,
    PermissionError,
    CheckoutNotFoundError,
    OverlayRegistrationRejectedError,
)


def _tar_stream(files: dict[str, bytes]) -> Generator[bytes]:
    """Render database records as a downloadable tar without server-side output files."""
    import io

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in sorted(files.items()):
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            entry.mode = 0o600
            archive.addfile(entry, io.BytesIO(payload))
    buffer.seek(0)
    while chunk := buffer.read(65536):
        yield chunk


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

    async def project_root(project_id: str, project_path: str | None) -> Path:
        if project_root_resolver is not None and project_path is None:
            return project_root_resolver(project_id)
        from gobby.storage.project_checkouts import require_root, resolve_operation_root
        from gobby.storage.workspace_machine_scope import require_local_machine_id

        machine_id = require_local_machine_id(
            None,
            resource_kind="project_checkout",
            resource_id=project_id,
        )
        primary = await server.run_db(
            require_root, server.services.database, project_id, machine_id
        )
        if project_path is None or Path(project_path).resolve() == Path(primary).resolve():
            return Path(primary)
        root = await server.run_db(
            resolve_operation_root,
            server.services.database,
            project_id,
            machine_id,
            overlay_path=project_path,
        )
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
            timeout_seconds=body.timeout_seconds,
            retrieval_mode=retrieval_mode,
            investigator_profile=_INVESTIGATOR_PROFILE,
            reviewer_profile=_REVIEWER_PROFILE,
            idempotency_key=body.idempotency_key,
        )
        try:
            result = await service(body.project_id).start(
                ask_request,
                project_root=await project_root(body.project_id, body.project_path),
                caller_session_id=await _caller_session_id(server, body.project_id, claims),
            )
        except _TRANSLATED_ASK_ERRORS as error:
            raise _ask_http_exception(error) from error
        return _payload(result)

    @router.get("/{run_id}/citations/{evidence_id:path}")
    async def read_citation(
        run_id: str, evidence_id: str, project_id: str, request: Request
    ) -> dict[str, Any]:
        await _authorize_project(server, request, project_id)
        try:
            return await asyncio.to_thread(
                service(project_id).citation, run_id, evidence_id, project_id=project_id
            )
        except _TRANSLATED_ASK_ERRORS as error:
            raise _ask_http_exception(error) from error

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
                caller_session_id=await _caller_session_id(server, project_id, claims),
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
                caller_session_id=await _caller_session_id(server, project_id, claims),
            )
        except _TRANSLATED_ASK_ERRORS as error:
            raise _ask_http_exception(error) from error
        return _payload(result)

    @router.get("/{run_id}/export")
    async def export_ask_run(run_id: str, project_id: str, request: Request) -> StreamingResponse:
        await _authorize_project(server, request, project_id)
        try:
            files = await asyncio.to_thread(
                service(project_id).publication_files, run_id, project_id=project_id
            )
        except _TRANSLATED_ASK_ERRORS as error:
            raise _ask_http_exception(error) from error
        return StreamingResponse(
            _tar_stream(files),
            media_type="application/x-tar",
            headers={
                "Content-Disposition": f'attachment; filename="ask-{run_id}.tar"',
                "X-Gobby-Ask-Run-Id": run_id,
            },
        )

    return router

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Body, HTTPException, Query, Request, UploadFile

from gobby.files_home_http import is_remote_files_mode
from gobby.gwiki_gateway import (
    GENERATION_GWIKI_TIMEOUT_SECONDS,
    INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
    INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS,
    GwikiCommandError,
    GwikiGateway,
    GwikiGatewayError,
    normalize_ai_mode,
    normalize_kind,
    normalize_page_write_mode,
)
from gobby.servers.chat_attachment_limits import (
    DEFAULT_ATTACHMENT_MAX_FILE_BYTES,
    resolve_server_attachment_limits,
)
from gobby.servers.upload_limits import ensure_disk_space
from gobby.wiki import WikiUpdateCoordinator
from gobby.wiki.owner_dispatch import (
    as_json_object,
    maybe_proxy_owner_request,
    proxy_owner_request,
)
from gobby.wiki.scope_resolution import (
    ResolvedWikiScope,
    WikiScopeResolutionError,
    resolve_wiki_scope,
)
from gobby.wiki.status import collect_wiki_status

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


GatewayCall = Callable[[GwikiGateway], Awaitable[dict[str, Any]]]
UPLOAD_CHUNK_SIZE = 64 * 1024
_GRAPH_INCLUDE_VALUES = {"all", "knowledge", "code"}


def create_wiki_router(server: HTTPServer) -> APIRouter:
    router = APIRouter(prefix="/api/wiki", tags=["wiki"])

    @router.get("/status")
    async def status(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        proxied = await maybe_proxy_owner_request(request, project=project, topic=topic)
        if proxied is not None:
            return as_json_object(proxied)
        resolved = await _resolve_scope(server, project, topic)
        return await collect_wiki_status(
            gateway=_gateway_from_scope(resolved),
            runner=_runner(server),
        )

    @router.post("/index")
    async def index(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, request, project, topic, lambda gateway: gateway.index())

    @router.get("/search")
    async def search(
        request: Request,
        q: str | None = Query(None),
        query: str | None = Query(None),
        limit: int | None = Query(None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        search_query = _one_query(q, query)
        return await _read(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.search(search_query, limit=limit),
        )

    @router.get("/read")
    async def read(
        request: Request,
        path: str | None = Query(None),
        title: str | None = Query(None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        if (path is None) == (title is None):
            raise HTTPException(status_code=400, detail="Provide exactly one of path or title")
        return await _read(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.read(path=path, title=title),
        )

    @router.get("/graph")
    async def graph(
        request: Request,
        include: str = Query("all"),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        include_value = _normalize_include(include)
        return await _read(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.graph(include=include_value),
        )

    @router.get("/pages")
    async def pages(
        request: Request,
        prefix: str | None = Query(None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(
            server, request, project, topic, lambda gateway: gateway.pages(prefix=prefix)
        )

    @router.get("/backlinks")
    async def backlinks(
        request: Request,
        target: str = Query(...),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(
            server, request, project, topic, lambda gateway: gateway.backlinks(target)
        )

    @router.get("/health")
    async def health(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.health(),
            timeout_seconds=INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS,
        )

    @router.get("/sources")
    async def sources(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, request, project, topic, lambda gateway: gateway.sources())

    @router.post("/attach")
    async def attach(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        proxied = await maybe_proxy_owner_request(
            request, project=project, topic=topic, stream_body=True
        )
        if proxied is not None:
            return as_json_object(proxied)
        form = await request.form()
        uploaded = form.get("file")
        if uploaded is None or isinstance(uploaded, (bytes, str)) or not hasattr(uploaded, "read"):
            raise HTTPException(status_code=400, detail="file is required")
        file = cast(UploadFile, uploaded)
        gateway = await _gateway(server, project, topic)
        max_upload_bytes = resolve_server_attachment_limits(server).max_file_bytes
        staged_path = await _stage_upload(file, max_bytes=max_upload_bytes)
        try:
            result = await _map_gateway_errors(lambda: gateway.ingest_file(staged_path))
        finally:
            staged_path.unlink(missing_ok=True)
        return await _write(gateway, result)

    @router.post("/ingest")
    async def ingest(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        proxied = await maybe_proxy_owner_request(request, project=project, topic=topic)
        if proxied is not None:
            return as_json_object(proxied)
        request_body = body or {}
        urls = _string_sequence(request_body.get("urls"))
        paths = _ingest_paths(request_body)
        if not urls and not paths:
            raise HTTPException(status_code=400, detail="Provide path, paths, or urls")

        resolved = await _resolve_scope(server, project, topic)
        if paths:
            paths = _resolve_ingest_paths(paths, resolved.project_root)
        gateway = _gateway_from_scope(resolved)
        if urls and paths:
            result = await _ingest_mixed(gateway, urls, paths)
        elif urls:
            result = await _map_gateway_errors(lambda: gateway.ingest_url(urls))
        elif len(paths) == 1:
            result = await _map_gateway_errors(lambda: gateway.ingest_file(paths[0]))
        else:
            result = await _ingest_many(gateway, paths)
        return await _write(gateway, result)

    @router.post("/write")
    async def write_page(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        payload = body or {}
        path = _required_string(payload.get("path"), "path is required")
        content = payload.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="content must be a string")
        expected_hash = _optional_string(payload.get("expected_hash"))
        try:
            mode = normalize_page_write_mode(_optional_string(payload.get("mode")) or "upsert")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _write_call(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.write_page(
                path=path,
                content=content,
                mode=mode,
                expected_hash=expected_hash,
            ),
            command_status=_page_mutation_status,
        )

    @router.post("/delete")
    async def delete_page(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        payload = body or {}
        path = _required_string(payload.get("path"), "path is required")
        return await _write_call(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.delete_page(path=path),
            command_status=_page_mutation_status,
        )

    @router.post("/collect")
    async def collect(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        query = _optional_string((body or {}).get("query"))
        return await _write_call(
            server, request, project, topic, lambda gateway: gateway.collect(query)
        )

    @router.post("/compile")
    async def compile_wiki(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        payload = body or {}
        compile_topic = _optional_string(payload.get("compile_topic"))
        kind = _normalize_kind(_optional_string(payload.get("kind")))
        sources = _string_sequence(payload.get("sources")) or None
        outline = _string_sequence(payload.get("outline")) or None
        target = _optional_string(payload.get("target"))
        write_intent = bool(payload.get("write_intent", False))
        ai_value = _optional_string(payload.get("ai"))
        ai = _normalize_ai(ai_value) if ai_value is not None else None
        # Compile defaults to AI routing inside gwiki and its synthesis scales
        # with vault size, so it always gets the generation guard.
        return await _write_call(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.compile(
                compile_topic,
                kind=kind,
                sources=sources,
                outline=outline,
                target=target,
                write_intent=write_intent,
                ai=ai,
            ),
            timeout_seconds=GENERATION_GWIKI_TIMEOUT_SECONDS,
        )

    @router.post("/audit")
    async def audit(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, request, project, topic, lambda gateway: gateway.audit())

    @router.post("/remove-source")
    async def remove_source(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        payload = body or {}
        source_id = _required_string(payload.get("id"), "id is required")
        yes = bool(payload.get("yes", False))
        explicit_dry_run = bool(payload.get("dry_run", False))
        if explicit_dry_run and yes:
            raise HTTPException(status_code=400, detail="dry_run and yes cannot both be true")
        dry_run = explicit_dry_run or not yes
        keep_asset = bool(payload.get("keep_asset", False))
        return await _write_call(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.remove_source(
                source_id,
                dry_run=dry_run,
                yes=yes,
                keep_asset=keep_asset,
            ),
        )

    @router.get("/trust")
    async def trust(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, request, project, topic, lambda gateway: gateway.trust())

    @router.post("/refresh")
    async def refresh(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        payload = body or {}
        source_ids = _string_sequence(payload.get("source_ids")) or None
        dry_run = bool(payload.get("dry_run", False))
        return await _write_call(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.refresh(source_ids=source_ids, dry_run=dry_run),
        )

    @router.post("/export")
    async def export_pages(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, request, project, topic, lambda gateway: gateway.export_pages())

    @router.post("/graph-artifacts")
    async def graph_artifacts(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(
            server, request, project, topic, lambda gateway: gateway.graph_artifacts()
        )

    @router.post("/sync-sessions")
    async def sync_sessions(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
        limit: int | None = Query(None),
    ) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "")
        stream_body = "octet-stream" in content_type or "multipart" in content_type
        proxied = await maybe_proxy_owner_request(
            request, project=project, topic=topic, stream_body=stream_body
        )
        if proxied is not None:
            return as_json_object(proxied)
        if stream_body:
            return await _ingest_sync_container(server, request, project, topic, limit)
        return await _write_call(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.sync_sessions(limit=limit),
        )

    @router.post("/upkeep")
    async def upkeep(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        payload = body or {}
        ai_value = _optional_string(payload.get("ai"))
        ai = _normalize_ai(ai_value) if ai_value is not None else None
        return await _write_call(
            server,
            request,
            project,
            topic,
            lambda gateway: gateway.upkeep(
                dry_run=bool(payload.get("dry_run", False)),
                ai=ai,
                max_pages=payload.get("max_pages"),
                time_budget_seconds=payload.get("time_budget_seconds"),
            ),
        )

    @router.post("/librarian")
    async def librarian(
        request: Request,
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, request, project, topic, lambda gateway: gateway.librarian())

    @router.post("/recap")
    async def recap(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        date = _optional_string((body or {}).get("date"))
        return await _write_call(
            server, request, project, topic, lambda gateway: gateway.recap(date=date)
        )

    @router.post("/prune")
    async def prune(request: Request) -> dict[str, Any]:
        if is_remote_files_mode():
            return as_json_object(await proxy_owner_request(request))
        result = await GwikiGateway().prune_all_scopes()
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "duration_seconds": result.duration_seconds,
            "timeout_seconds": result.timeout_seconds,
            "timed_out": result.timed_out,
        }

    return router


async def _read(
    server: HTTPServer,
    request: Request,
    project: str | None,
    topic: str | None,
    call: GatewayCall,
    timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    proxied = await maybe_proxy_owner_request(request, project=project, topic=topic)
    if proxied is not None:
        return as_json_object(proxied)
    gateway = await _gateway(server, project, topic, timeout_seconds=timeout_seconds)
    return await _map_gateway_errors(lambda: call(gateway))


async def _write_call(
    server: HTTPServer,
    request: Request,
    project: str | None,
    topic: str | None,
    call: GatewayCall,
    timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
    command_status: Callable[[GwikiCommandError], int] | None = None,
) -> dict[str, Any]:
    proxied = await maybe_proxy_owner_request(request, project=project, topic=topic)
    if proxied is not None:
        return as_json_object(proxied)
    gateway = await _gateway(server, project, topic, timeout_seconds=timeout_seconds)
    result = await _map_gateway_errors(lambda: call(gateway), command_status=command_status)
    return await _write(gateway, result)


async def _write(gateway: GwikiGateway, result: dict[str, Any]) -> dict[str, Any]:
    coordinator = WikiUpdateCoordinator(gateway)
    return await _map_gateway_errors(lambda: coordinator.handle_write_result(result))


async def _resolve_scope(
    server: HTTPServer,
    project: str | None,
    topic: str | None,
) -> ResolvedWikiScope:
    services = getattr(server, "services", None)
    try:
        return await resolve_wiki_scope(
            getattr(services, "database", None),
            project=project,
            topic=topic,
            default_project_id=getattr(services, "project_id", None),
        )
    except WikiScopeResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _gateway(
    server: HTTPServer,
    project: str | None,
    topic: str | None,
    timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
) -> GwikiGateway:
    # Keep server in the helper signature for route factory compatibility.
    return _gateway_from_scope(
        await _resolve_scope(server, project, topic),
        timeout_seconds=timeout_seconds,
    )


def _gateway_from_scope(
    resolved: ResolvedWikiScope,
    timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
) -> GwikiGateway:
    return GwikiGateway(
        binary=None,
        project_root=resolved.project_root,
        topic=resolved.topic,
        timeout_seconds=timeout_seconds,
    )


def _runner(server: HTTPServer) -> object | None:
    get_runner = getattr(server, "get_runner", None)
    if not callable(get_runner):
        return None
    runner: object | None = get_runner()
    return runner


async def _map_gateway_errors(
    call: Callable[[], Awaitable[dict[str, Any]]],
    *,
    command_status: Callable[[GwikiCommandError], int] | None = None,
) -> dict[str, Any]:
    try:
        return await call()
    except GwikiCommandError as exc:
        status = command_status(exc) if command_status is not None else 502
        raise HTTPException(status_code=status, detail=exc.to_envelope()) from exc
    except GwikiGatewayError as exc:
        raise HTTPException(status_code=503, detail=_gateway_error_envelope(exc)) from exc


# gwiki page-mutation error codes with a precise HTTP status; anything else
# stays a 502 upstream-command failure.
_PAGE_MUTATION_STATUS = {
    "already_exists": 409,
    "not_found": 404,
    "precondition_failed": 412,
}


def _page_mutation_status(exc: GwikiCommandError) -> int:
    payload = exc.payload or {}
    code = payload.get("code")
    if isinstance(code, str):
        return _PAGE_MUTATION_STATUS.get(code, 502)
    return 502


def _gateway_error_envelope(exc: GwikiGatewayError) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "failed",
        "payload": None,
        "stderr": "",
        "error": {"type": exc.__class__.__name__, "message": str(exc)},
    }


def _one_query(q: str | None, query: str | None) -> str:
    if (q is None) == (query is None):
        raise HTTPException(status_code=400, detail="Provide exactly one of q or query")
    value = q if q is not None else query
    assert value is not None
    return value


def _normalize_ai(value: str) -> str:
    try:
        return normalize_ai_mode(value) or "auto"
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _normalize_include(value: str) -> str:
    include = value.strip().lower()
    if include not in _GRAPH_INCLUDE_VALUES:
        allowed = ", ".join(sorted(_GRAPH_INCLUDE_VALUES))
        raise HTTPException(status_code=400, detail=f"include must be one of {allowed}")
    return include


def _normalize_kind(value: str | None) -> str | None:
    try:
        return normalize_kind(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _required_string(value: Any, detail: str) -> str:
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=400, detail=detail)
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise HTTPException(status_code=400, detail="Expected a list of strings")
    strings = [item for item in value if isinstance(item, str) and item]
    if len(strings) != len(value):
        raise HTTPException(status_code=400, detail="Expected a list of strings")
    return strings


def _ingest_paths(request: dict[str, Any]) -> list[str]:
    paths = _string_sequence(request.get("paths"))
    path = request.get("path")
    if path is not None:
        paths.insert(0, _required_string(path, "path must be a string"))
    return paths


def _resolve_ingest_paths(paths: list[str], project_root: Path | None) -> list[str]:
    if project_root is None:
        raise HTTPException(status_code=400, detail="Project scope is required for path ingestion")

    root = project_root.resolve()
    resolved_paths: list[str] = []
    for path in paths:
        source = Path(path).expanduser()
        if source.is_absolute():
            raise HTTPException(status_code=403, detail="Ingest path must stay inside project")
        resolved = (root / source).resolve()
        if not resolved.is_relative_to(root):
            raise HTTPException(status_code=403, detail="Ingest path must stay inside project")
        resolved_paths.append(str(resolved))
    return resolved_paths


async def _ingest_many(gateway: GwikiGateway, paths: list[str]) -> dict[str, Any]:
    results = []
    for path in paths:
        results.append(await _map_gateway_awaitable(gateway.ingest_file(path)))
    return _aggregate_ingest_results(results, command="ingest_file")


async def _ingest_mixed(gateway: GwikiGateway, urls: list[str], paths: list[str]) -> dict[str, Any]:
    results = [await _map_gateway_awaitable(gateway.ingest_url(urls))]
    if len(paths) == 1:
        results.append(await _map_gateway_awaitable(gateway.ingest_file(paths[0])))
    else:
        many_result = await _ingest_many(gateway, paths)
        payload = many_result.get("payload")
        nested_results = payload.get("results") if isinstance(payload, dict) else None
        if isinstance(nested_results, list):
            results.extend(item for item in nested_results if isinstance(item, dict))
        else:
            results.append(many_result)
    return _aggregate_ingest_results(results, command="ingest_file")


def _aggregate_ingest_results(results: list[dict[str, Any]], *, command: str) -> dict[str, Any]:
    changed_paths: list[str] = []
    stderr: list[str] = []
    for result in results:
        payload = result.get("payload")
        if isinstance(payload, dict):
            raw_changed_paths = payload.get("changed_paths", [])
            if isinstance(raw_changed_paths, Sequence) and not isinstance(
                raw_changed_paths, (str, bytes)
            ):
                changed_paths.extend(
                    item for item in raw_changed_paths if isinstance(item, str) and item
                )
        if isinstance(result.get("stderr"), str) and result["stderr"]:
            stderr.append(result["stderr"])
    return {
        "ok": all(bool(result.get("ok", False)) for result in results),
        "command": command,
        "payload": {
            "command": "ingest-file",
            "results": results,
            "changed_paths": list(dict.fromkeys(changed_paths)),
        },
        "stderr": "\n".join(dict.fromkeys(stderr)),
    }


async def _map_gateway_awaitable(awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    return await _map_gateway_errors(lambda: awaitable)


async def _ingest_sync_container(
    server: HTTPServer,
    request: Request,
    project: str | None,
    topic: str | None,
    limit: int | None,
) -> dict[str, Any]:
    import tarfile
    import tempfile

    from gobby.wiki.sync_container import SyncContainerError

    staged = Path(tempfile.mkdtemp(prefix="gobby-sync-stage-"))
    archive_path = staged / "incoming.tar"
    try:
        with archive_path.open("wb") as handle:
            async for chunk in request.stream():
                handle.write(chunk)
        with tarfile.open(archive_path, mode="r:") as archive:
            archive.extractall(staged, filter="data")
        archive_dir = staged / "archives"
        wiki_dir = staged / "wiki"
        if not wiki_dir.is_dir():
            raise HTTPException(status_code=400, detail="wiki_dir is required")
        gateway = await _gateway(server, project, topic)
        result = await _map_gateway_errors(
            lambda: gateway.sync_sessions(archive_dir=archive_dir, limit=limit)
        )
        return await _write(gateway, result)
    except SyncContainerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        import shutil

        shutil.rmtree(staged, ignore_errors=True)


async def _stage_upload(
    file: UploadFile,
    *,
    max_bytes: int = DEFAULT_ATTACHMENT_MAX_FILE_BYTES,
) -> Path:
    suffix = Path(file.filename or "").suffix
    staged_path: Path | None = None
    size = 0
    try:
        with tempfile.NamedTemporaryFile(
            prefix="gobby-wiki-", suffix=suffix, delete=False
        ) as staged:
            staged_path = Path(staged.name)
            known_file_size = getattr(file, "size", None)
            reserved_bytes = (
                min(max_bytes, known_file_size)
                if isinstance(known_file_size, int) and known_file_size >= 0
                else max_bytes
            )
            await asyncio.to_thread(
                ensure_disk_space,
                staged_path.parent,
                reserved_bytes,
                label="Wiki upload",
            )
            while True:
                remaining = max_bytes - size
                read_size = min(UPLOAD_CHUNK_SIZE, max(remaining + 1, 1))
                chunk = await file.read(read_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Wiki upload exceeds {max_bytes} byte limit",
                    )
                staged.write(chunk)
            return staged_path
    except Exception:
        # Broad by design: any upload read/write/staging failure must clean up
        # the temporary file before the route maps the original exception.
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        raise

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile

from gobby.gwiki_gateway import GwikiCommandError, GwikiGateway, GwikiGatewayError
from gobby.wiki import WikiUpdateCoordinator
from gobby.wiki.status import collect_wiki_status

if TYPE_CHECKING:
    from gobby.servers.http_server import HTTPServer


GatewayCall = Callable[[GwikiGateway], Awaitable[dict[str, Any]]]


def create_wiki_router(server: HTTPServer) -> APIRouter:
    router = APIRouter(prefix="/api/wiki", tags=["wiki"])

    @router.get("/status")
    async def status(
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        gateway = _gateway(server, project, topic)
        return await collect_wiki_status(gateway=gateway, runner=_runner(server))

    @router.post("/index")
    async def index(
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, project, topic, lambda gateway: gateway.index())

    @router.get("/search")
    async def search(
        q: str | None = Query(None),
        query: str | None = Query(None),
        limit: int | None = Query(None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        search_query = _one_query(q, query)
        return await _read(
            server,
            project,
            topic,
            lambda gateway: gateway.search(search_query, limit=limit),
        )

    @router.get("/read")
    async def read(
        path: str | None = Query(None),
        title: str | None = Query(None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        if (path is None) == (title is None):
            raise HTTPException(status_code=400, detail="Provide exactly one of path or title")
        return await _read(
            server,
            project,
            topic,
            lambda gateway: gateway.read(path=path, title=title),
        )

    @router.get("/backlinks")
    async def backlinks(
        target: str = Query(...),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, project, topic, lambda gateway: gateway.backlinks(target))

    @router.get("/health")
    async def health(
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, project, topic, lambda gateway: gateway.health())

    @router.get("/sources")
    async def sources(
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, project, topic, lambda gateway: gateway.sources())

    @router.post("/attach")
    async def attach(
        file: UploadFile = File(...),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        gateway = _gateway(server, project, topic)
        staged_path = await _stage_upload(file)
        try:
            result = await _map_gateway_errors(lambda: gateway.ingest_file(staged_path))
        finally:
            staged_path.unlink(missing_ok=True)
        return await _write(gateway, result)

    @router.post("/ingest")
    async def ingest(
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        request = body or {}
        urls = _string_sequence(request.get("urls"))
        paths = _ingest_paths(request)
        if urls and paths:
            raise HTTPException(status_code=400, detail="Provide file paths or URLs, not both")
        if not urls and not paths:
            raise HTTPException(status_code=400, detail="Provide path, paths, or urls")

        gateway = _gateway(server, project, topic)
        if urls:
            result = await _map_gateway_errors(lambda: gateway.ingest_url(urls))
        elif len(paths) == 1:
            result = await _map_gateway_errors(lambda: gateway.ingest_file(paths[0]))
        else:
            result = await _ingest_many(gateway, paths)
        return await _write(gateway, result)

    @router.post("/collect")
    async def collect(
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        query = _optional_string((body or {}).get("query"))
        return await _write_call(server, project, topic, lambda gateway: gateway.collect(query))

    @router.post("/research")
    async def research(
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        query = _optional_string((body or {}).get("query"))
        return await _write_call(server, project, topic, lambda gateway: gateway.research(query))

    @router.post("/compile")
    async def compile_wiki(
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        output = _optional_string((body or {}).get("output"))
        return await _write_call(server, project, topic, lambda gateway: gateway.compile(output))

    @router.post("/audit")
    async def audit(
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        return await _read(server, project, topic, lambda gateway: gateway.audit())

    @router.post("/remove-source")
    async def remove_source(
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        request = body or {}
        source_id = _required_string(request.get("id"), "id is required")
        yes = bool(request.get("yes", False))
        dry_run_requested = bool(request.get("dry_run", False))
        if dry_run_requested and yes:
            raise HTTPException(status_code=400, detail="dry_run and yes cannot both be true")
        dry_run = dry_run_requested or not yes
        keep_asset = bool(request.get("keep_asset", False))
        return await _write_call(
            server,
            project,
            topic,
            lambda gateway: gateway.remove_source(
                source_id,
                dry_run=dry_run,
                yes=yes,
                keep_asset=keep_asset,
            ),
        )

    return router


async def _read(
    server: HTTPServer,
    project: str | None,
    topic: str | None,
    call: GatewayCall,
) -> dict[str, Any]:
    gateway = _gateway(server, project, topic)
    return await _map_gateway_errors(lambda: call(gateway))


async def _write_call(
    server: HTTPServer,
    project: str | None,
    topic: str | None,
    call: GatewayCall,
) -> dict[str, Any]:
    gateway = _gateway(server, project, topic)
    result = await _map_gateway_errors(lambda: call(gateway))
    return await _write(gateway, result)


async def _write(gateway: GwikiGateway, result: dict[str, Any]) -> dict[str, Any]:
    coordinator = WikiUpdateCoordinator(gateway)
    return await _map_gateway_errors(lambda: coordinator.handle_write_result(result))


def _gateway(server: HTTPServer, project: str | None, topic: str | None) -> GwikiGateway:
    if project is not None and topic is not None:
        raise HTTPException(status_code=400, detail="Provide project or topic scope, not both")

    config = getattr(getattr(server, "services", None), "config", None)
    wiki_config = getattr(config, "wiki", None)
    return GwikiGateway(
        binary=getattr(wiki_config, "binary", None),
        project=project,
        topic=topic,
        timeout_seconds=float(getattr(wiki_config, "timeout_seconds", 30.0)),
    )


def _runner(server: HTTPServer) -> object | None:
    get_runner = getattr(server, "get_runner", None)
    if not callable(get_runner):
        return None
    runner: object | None = get_runner()
    return runner


async def _map_gateway_errors(call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    try:
        return await call()
    except GwikiCommandError as exc:
        raise HTTPException(status_code=502, detail=exc.to_envelope()) from exc
    except GwikiGatewayError as exc:
        raise HTTPException(status_code=503, detail=_gateway_error_envelope(exc)) from exc


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


async def _ingest_many(gateway: GwikiGateway, paths: list[str]) -> dict[str, Any]:
    results = []
    for path in paths:
        results.append(await _map_gateway_awaitable(gateway.ingest_file(path)))
    changed_paths: list[str] = []
    for result in results:
        payload = result.get("payload")
        if isinstance(payload, dict):
            changed_paths.extend(_string_sequence(payload.get("changed_paths")))
    return {
        "ok": True,
        "command": "ingest_file",
        "payload": {
            "command": "ingest-file",
            "results": results,
            "changed_paths": list(dict.fromkeys(changed_paths)),
        },
        "stderr": "",
    }


async def _map_gateway_awaitable(awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    return await _map_gateway_errors(lambda: awaitable)


async def _stage_upload(file: UploadFile) -> Path:
    suffix = Path(file.filename or "").suffix
    with tempfile.NamedTemporaryFile(prefix="gobby-wiki-", suffix=suffix, delete=False) as staged:
        staged.write(await file.read())
        return Path(staged.name)

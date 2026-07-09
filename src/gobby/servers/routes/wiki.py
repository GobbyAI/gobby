from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile

from gobby.gwiki_gateway import (
    GENERATION_GWIKI_TIMEOUT_SECONDS,
    INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
    INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS,
    GwikiCommandError,
    GwikiGateway,
    GwikiGatewayError,
    normalize_kind,
    resolve_ask_timeout,
)
from gobby.wiki import WikiUpdateCoordinator
from gobby.wiki.scope_resolution import (
    ResolvedWikiScope,
    WikiScopeResolutionError,
    resolve_wiki_scope,
)
from gobby.wiki.status import collect_wiki_status

if TYPE_CHECKING:
    from gobby.servers.http_server import HTTPServer


GatewayCall = Callable[[GwikiGateway], Awaitable[dict[str, Any]]]
UPLOAD_CHUNK_SIZE = 64 * 1024
_AI_VALUES = {"auto", "daemon", "direct", "off"}


def create_wiki_router(server: HTTPServer) -> APIRouter:
    router = APIRouter(prefix="/api/wiki", tags=["wiki"])

    @router.get("/status")
    async def status(
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        resolved = await _resolve_scope(server, project, topic)
        return await collect_wiki_status(
            gateway=_gateway_from_scope(resolved),
            runner=_runner(server),
        )

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

    @router.get("/ask")
    async def ask(
        q: str | None = Query(None),
        query: str | None = Query(None),
        llm: bool = Query(False),
        ai: str | None = Query(None),
        require_ai: bool = Query(False),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        ask_query = _one_query(q, query)
        ai_value = _normalize_ai(ai) if ai is not None else None
        timeout_seconds = resolve_ask_timeout(llm, ai_value)
        try:
            return await _read(
                server,
                project,
                topic,
                lambda gateway: gateway.ask(
                    ask_query,
                    llm=llm,
                    ai=ai_value,
                    require_ai=require_ai,
                ),
                timeout_seconds=timeout_seconds,
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc) or "Invalid wiki ask request") from exc

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
        return await _read(
            server,
            project,
            topic,
            lambda gateway: gateway.health(),
            timeout_seconds=INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS,
        )

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
        gateway = await _gateway(server, project, topic)
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
        if not urls and not paths:
            raise HTTPException(status_code=400, detail="Provide path, paths, or urls")

        gateway = await _gateway(server, project, topic)
        if urls and paths:
            result = await _ingest_mixed(gateway, urls, paths)
        elif urls:
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

    @router.post("/compile")
    async def compile_wiki(
        body: dict[str, Any] | None = Body(default=None),
        project: str | None = Query(None),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        request = body or {}
        compile_topic = _optional_string(request.get("compile_topic"))
        kind = _normalize_kind(_optional_string(request.get("kind")))
        sources = _string_sequence(request.get("sources")) or None
        outline = _string_sequence(request.get("outline")) or None
        target = _optional_string(request.get("target"))
        write_intent = bool(request.get("write_intent", False))
        ai_value = _optional_string(request.get("ai"))
        ai = _normalize_ai(ai_value) if ai_value is not None else None
        # Compile defaults to AI routing inside gwiki and its synthesis scales
        # with vault size, so it always gets the generation guard.
        return await _write_call(
            server,
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
        explicit_dry_run = bool(request.get("dry_run", False))
        if explicit_dry_run and yes:
            raise HTTPException(status_code=400, detail="dry_run and yes cannot both be true")
        dry_run = explicit_dry_run or not yes
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
    timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    gateway = await _gateway(server, project, topic, timeout_seconds=timeout_seconds)
    return await _map_gateway_errors(lambda: call(gateway))


async def _write_call(
    server: HTTPServer,
    project: str | None,
    topic: str | None,
    call: GatewayCall,
    timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    gateway = await _gateway(server, project, topic, timeout_seconds=timeout_seconds)
    result = await _map_gateway_errors(lambda: call(gateway))
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


def _normalize_ai(value: str) -> str:
    ai = value.strip().lower()
    if ai not in _AI_VALUES:
        allowed = ", ".join(sorted(_AI_VALUES))
        raise HTTPException(status_code=400, detail=f"ai must be one of {allowed}")
    return ai


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


async def _stage_upload(file: UploadFile) -> Path:
    suffix = Path(file.filename or "").suffix
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="gobby-wiki-", suffix=suffix, delete=False
        ) as staged:
            staged_path = Path(staged.name)
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                staged.write(chunk)
            return staged_path
    except Exception:
        # Broad by design: any upload read/write/staging failure must clean up
        # the temporary file before the route maps the original exception.
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        raise

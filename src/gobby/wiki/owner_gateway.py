"""Remote HTTP stand-in for GwikiGateway."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gobby.files_home_http import require_hub_daemon_url
from gobby.gwiki_gateway import INTERACTIVE_GWIKI_TIMEOUT_SECONDS
from gobby.utils.daemon_client import DaemonClient
from gobby.wiki.scope_resolution import ResolvedWikiScope


class RemoteWikiGateway:
    """HTTP stand-in for GwikiGateway on a remote node."""

    def __init__(
        self,
        resolved: ResolvedWikiScope,
        *,
        timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
    ) -> None:
        self._resolved = resolved
        self._timeout = timeout_seconds
        self._client = DaemonClient.from_url(require_hub_daemon_url(), timeout=timeout_seconds)

    def _params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self._resolved.topic:
            params["topic"] = self._resolved.topic
        elif self._resolved.project_id:
            params["project"] = self._resolved.project_id
        return params

    async def _json(
        self,
        method: str,
        path: str,
        *,
        json_data: Mapping[str, Any] | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = dict(self._params())
        if extra_params:
            params.update({key: value for key, value in extra_params.items() if value is not None})
        response = await self._client.request_raw(
            method,
            path,
            params=params,
            json_data=dict(json_data) if json_data is not None else None,
            hop=True,
            accept_statuses=(200,),
            deadline_seconds=self._timeout,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            return {"ok": False, "status": "failed", "payload": payload}
        return payload

    async def status(self) -> dict[str, Any]:
        return await self._json("GET", "/api/wiki/status")

    async def index(self) -> dict[str, Any]:
        return await self._json("POST", "/api/wiki/index")

    async def search(
        self, query: str, *, limit: int | None = None, token_budget: int | None = None
    ) -> dict[str, Any]:
        return await self._json(
            "GET",
            "/api/wiki/search",
            extra_params={"q": query, "limit": limit, "token_budget": token_budget},
        )

    async def read(
        self, *, path: str | Path | None = None, title: str | None = None
    ) -> dict[str, Any]:
        return await self._json(
            "GET",
            "/api/wiki/read",
            extra_params={"path": None if path is None else str(path), "title": title},
        )

    async def graph(self, *, include: str = "all") -> dict[str, Any]:
        return await self._json("GET", "/api/wiki/graph", extra_params={"include": include})

    async def pages(self, *, prefix: str | None = None) -> dict[str, Any]:
        return await self._json("GET", "/api/wiki/pages", extra_params={"prefix": prefix})

    async def backlinks(self, target: str) -> dict[str, Any]:
        return await self._json("GET", "/api/wiki/backlinks", extra_params={"target": target})

    async def health(self) -> dict[str, Any]:
        return await self._json("GET", "/api/wiki/health")

    async def sources(self) -> dict[str, Any]:
        return await self._json("GET", "/api/wiki/sources")

    async def trust(self) -> dict[str, Any]:
        return await self._json("GET", "/api/wiki/trust")

    async def write_page(
        self,
        *,
        path: str,
        content: str,
        mode: str = "upsert",
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            "/api/wiki/write",
            json_data={
                "path": path,
                "content": content,
                "mode": mode,
                "expected_hash": expected_hash,
            },
        )

    async def delete_page(self, *, path: str) -> dict[str, Any]:
        return await self._json("POST", "/api/wiki/delete", json_data={"path": path})

    async def collect(self, query: str | None = None) -> dict[str, Any]:
        return await self._json("POST", "/api/wiki/collect", json_data={"query": query})

    async def compile(self, compile_topic: str | None = None, **kwargs: Any) -> dict[str, Any]:
        body = {"compile_topic": compile_topic, **kwargs}
        return await self._json("POST", "/api/wiki/compile", json_data=body)

    async def audit(self) -> dict[str, Any]:
        return await self._json("POST", "/api/wiki/audit")

    async def remove_source(
        self,
        source_id: str,
        *,
        dry_run: bool,
        yes: bool,
        keep_asset: bool,
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            "/api/wiki/remove-source",
            json_data={"id": source_id, "dry_run": dry_run, "yes": yes, "keep_asset": keep_asset},
        )

    async def refresh(
        self,
        *,
        source_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            "/api/wiki/refresh",
            json_data={"source_ids": source_ids, "dry_run": dry_run},
        )

    async def export_pages(self) -> dict[str, Any]:
        return await self._json("POST", "/api/wiki/export")

    async def graph_artifacts(self) -> dict[str, Any]:
        return await self._json("POST", "/api/wiki/graph-artifacts")

    async def upkeep(self, **kwargs: Any) -> dict[str, Any]:
        return await self._json("POST", "/api/wiki/upkeep", json_data=kwargs)

    async def librarian(self) -> dict[str, Any]:
        return await self._json("POST", "/api/wiki/librarian")

    async def recap(self, *, date: str | None = None) -> dict[str, Any]:
        return await self._json("POST", "/api/wiki/recap", json_data={"date": date})

    async def ingest_url(
        self, urls: list[str], *, max_age_hours: int | None = None
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            "/api/wiki/ingest",
            json_data={"urls": urls, "max_age_hours": max_age_hours},
        )

    async def ingest_file(self, path: str | Path) -> dict[str, Any]:
        source = Path(path)
        data = source.read_bytes()
        response = await self._client.request_raw(
            "POST",
            "/api/wiki/attach",
            params=self._params(),
            headers={
                "content-type": "application/octet-stream",
                "x-gobby-filename": source.name,
            },
            content=data,
            hop=True,
            accept_statuses=(200,),
            deadline_seconds=self._timeout,
        )
        payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": False, "payload": payload}

    async def sync_sessions(
        self,
        *,
        archive_dir: str | Path | None = None,
        limit: int | None = None,
        wiki_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        from gobby.paths import get_gobby_home
        from gobby.sessions.transcript_archive import get_archive_dir
        from gobby.wiki.sync_container import build_sync_container

        archives = Path(archive_dir) if archive_dir is not None else get_archive_dir()
        wiki = Path(wiki_dir) if wiki_dir is not None else get_gobby_home() / "session_wiki"
        container = build_sync_container(archive_dir=archives, wiki_dir=wiki)
        try:
            extra = {"limit": limit} if limit is not None else {}
            response = await self._client.request_raw(
                "POST",
                "/api/wiki/sync-sessions",
                params={**self._params(), **extra},
                headers={"content-type": "application/octet-stream"},
                content=container.read_bytes(),
                hop=True,
                accept_statuses=(200,),
                deadline_seconds=self._timeout,
            )
        finally:
            container.unlink(missing_ok=True)
        payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": False, "payload": payload}

    async def prune_all_scopes(self, *, timeout: float | None = None) -> Any:
        from gobby.gwiki_gateway import GwikiCommandResult

        response = await self._client.request_raw(
            "POST",
            "/api/wiki/prune",
            hop=True,
            accept_statuses=(200,),
            deadline_seconds=timeout or self._timeout,
        )
        payload = response.json()
        if isinstance(payload, dict) and "returncode" in payload:
            return GwikiCommandResult(
                command=("gwiki", "prune", "--force"),
                returncode=int(payload.get("returncode") or 0),
                stdout=str(payload.get("stdout") or ""),
                stderr=str(payload.get("stderr") or ""),
                started_at=str(payload.get("started_at") or ""),
                completed_at=str(payload.get("completed_at") or ""),
                duration_seconds=float(payload.get("duration_seconds") or 0),
                timeout_seconds=float(payload.get("timeout_seconds") or 0),
                timed_out=bool(payload.get("timed_out")),
            )
        return payload

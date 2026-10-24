"""Exercise the actual pinned browser MCPs against isolated loopback servers."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from gobby.mcp_proxy.bundled import resolve_runtime_stdio_args
from gobby.servers.http import HTTPServer
from tests.servers.routes.test_browser_sign_in import managed_headers, sign_in_server

# Importing fixtures makes the isolated DB/HTTPServer shared by both acceptance files.
__all__ = ["managed_headers", "sign_in_server"]
ROOT = Path(__file__).resolve().parents[3]


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="off", log_config=None)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("isolated HTTP server did not start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


def rendered(result: Any) -> str:
    return "\n".join(item.text for item in result.content if item.type == "text")


@pytest.mark.parametrize("browser", ["playwright", "chrome-devtools"])
@pytest.mark.asyncio
async def test_locked_browser_managed_sign_in_and_fresh_profile(
    browser: str,
    sign_in_server: HTTPServer,
    managed_headers: dict[str, str],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="gobby")
    template = yaml.safe_load(
        (ROOT / "src/gobby/install/shared/mcp/templates" / f"{browser}.yaml").read_text()
    )
    args = template["args"]
    assert "--isolated" in args, "each MCP must launch a fresh profile"
    assert all("@latest" not in arg for arg in args), "browser dependencies must be exact pins"
    runtime_args = resolve_runtime_stdio_args(template["runtime_hook"], args)
    page_app = FastAPI()

    @page_app.get("/")
    async def page() -> HTMLResponse:
        return HTMLResponse("<title>Isolated Vite origin</title><h1>loopback fixture</h1>")

    @page_app.get("/redirect")
    async def redirect() -> RedirectResponse:
        return RedirectResponse("https://example.com/")

    async with serving(sign_in_server.app) as daemon, serving(page_app) as vite:
        async with httpx.AsyncClient(trust_env=False) as client:
            mint = await client.post(f"{daemon}/api/auth/sign-in-link", headers=managed_headers)
            assert mint.status_code == 200
            link = mint.json()["url"]
        # Run twice: the second launch must have neither cookies nor localStorage.
        for launch in range(2):
            params = StdioServerParameters(
                command="npx",
                args=[*runtime_args, "--headless"],
                # macOS SingletonSocket has a short Unix-socket path limit.
                env={
                    **os.environ,
                    **template.get("env", {}),
                    "HOME": str(tmp_path),
                    "TMPDIR": "/tmp",
                    "CI": "1",
                },
            )
            async with asyncio.timeout(120):
                async with (
                    stdio_client(params) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()

                    async def navigate(url: str) -> Any:
                        if browser == "playwright":
                            return await session.call_tool("browser_navigate", {"url": url})
                        return await session.call_tool("navigate_page", {"type": "url", "url": url})

                    async def evaluate(function: str) -> str:
                        name = "browser_evaluate" if browser == "playwright" else "evaluate_script"
                        return rendered(await session.call_tool(name, {"function": function}))

                    if browser == "chrome-devtools":
                        initial = await session.call_tool("new_page", {"url": vite})
                    else:
                        initial = await navigate(vite)
                    assert not initial.is_error, rendered(initial)
                    assert "Isolated Vite origin" in await evaluate("() => document.title")
                    assert "null" in await evaluate("() => localStorage.getItem('profile-proof')")
                    await evaluate("() => localStorage.setItem('profile-proof', 'present')")
                    await navigate(f"{daemon}/api/auth/status")
                    assert "false" in await evaluate("() => document.body.innerText")
                    if launch == 0:
                        await navigate(link)
                        status = await evaluate(
                            "async () => (await fetch('/api/auth/status')).json()"
                        )
                        assert "true" in status
                        readable = await evaluate(
                            "() => ({cookie: document.cookie, local: {...localStorage}, session: {...sessionStorage}, url: location.href})"
                        )
                        assert "gobby_session" not in readable
                        assert link.split("token=", 1)[1] not in readable
                        process = await asyncio.create_subprocess_exec(
                            "ps", "-axo", "command", stdout=asyncio.subprocess.PIPE
                        )
                        commands, _ = await process.communicate()
                        assert link.split("token=", 1)[1].encode() not in commands
                        await evaluate(
                            "async () => (await fetch('/api/auth/logout', {method:'POST'})).json()"
                        )
                        assert "false" in await evaluate(
                            "async () => (await fetch('/api/auth/status')).json()"
                        )
                    await navigate(vite)
                    # no-cors succeeds for unrestricted cross-origin requests; CORS cannot fake this refusal.
                    for external in ["https://example.com/", "http://1.1.1.1/"]:
                        function = f"async () => {{ try {{ await fetch({json.dumps(external)}, {{mode:'no-cors', signal:AbortSignal.timeout(8000)}}); return 'escaped'; }} catch(e) {{ return 'blocked'; }} }}"
                        assert "blocked" in await evaluate(function)
                        result = await navigate(external)
                        assert result.is_error or "ERR_" in rendered(result)
                        await navigate(vite)
                    redirected = await navigate(f"{vite}/redirect")
                    assert redirected.is_error or "ERR_" in rendered(redirected)
        assert link.split("token=", 1)[1] not in caplog.text

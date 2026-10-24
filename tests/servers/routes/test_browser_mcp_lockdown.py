"""Exercise the actual pinned browser MCPs against isolated loopback servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from gobby.mcp_proxy.bundled import resolve_chrome_devtools_executable_path
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPServerConfig
from gobby.servers.http import HTTPServer
from tests.servers.routes.test_browser_sign_in import managed_headers, sign_in_server

# Importing fixtures makes the isolated DB/HTTPServer shared by both acceptance files.
__all__ = ["managed_headers", "sign_in_server"]
ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.slow,
    pytest.mark.e2e,
    pytest.mark.timeout(240, method="thread"),
    pytest.mark.skipif(
        bool(os.environ.get("SANDBOX_RUNTIME"))
        or shutil.which("npx") is None
        or resolve_chrome_devtools_executable_path() is None,
        reason="Browser proof requires npx and Chrome on the unsandboxed host",
    ),
]


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
    caplog.set_level("INFO", logger=__name__)
    template = yaml.safe_load(
        (ROOT / "src/gobby/install/shared/mcp/templates" / f"{browser}.yaml").read_text()
    )
    args = template["args"]
    assert "--isolated" in args, "each MCP must launch a fresh profile"
    assert all("@latest" not in arg for arg in args), "browser dependencies must be exact pins"
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
            config = MCPServerConfig(
                id=browser,
                name=browser,
                project_id="browser-proof",
                transport="stdio",
                command="npx",
                args=[*args, "--headless"],
                runtime_hook=template["runtime_hook"],
                connect_timeout=60,
                # macOS SingletonSocket has a short Unix-socket path limit.
                env={
                    **os.environ,
                    **template.get("env", {}),
                    "HOME": str(tmp_path),
                    "TMPDIR": "/tmp",
                    "CI": "1",
                },
            )
            manager = MCPClientManager(server_configs=[config])
            step_number = 0

            async def call(
                name: str,
                arguments: dict[str, Any],
                step: str,
                proxy: MCPClientManager = manager,
                launch_number: int = launch,
            ) -> Any:
                nonlocal step_number
                step_number += 1
                label = f"{browser} launch {launch_number} step {step_number}: {step}"
                logging.getLogger(__name__).info("Browser proof starting %s", label)
                try:
                    async with asyncio.timeout(25):
                        result = await proxy.call_tool(browser, name, arguments, timeout=20)
                except TimeoutError:
                    pytest.fail(f"Browser proof timed out: {label}")
                logging.getLogger(__name__).info("Browser proof finished %s", label)
                return result

            async with asyncio.timeout(150):
                try:
                    logging.getLogger(__name__).info("Browser proof discovering %s", browser)
                    try:
                        async with asyncio.timeout(95):
                            inventory = (await manager.list_tools(browser))[browser]
                    except TimeoutError:
                        pytest.fail(f"Browser proof timed out: {browser} tool discovery")
                    assert not {"browser_run_code", "browser_file_upload", "upload_file"} & {
                        tool["name"] for tool in inventory
                    }
                    forbidden_calls: list[tuple[str, dict[str, Any]]] = [
                        (
                            "browser_run_code",
                            {
                                "code": "async (page) => page.context().browser().newContext({proxy:{server:'http://127.0.0.1:9',bypass:'*'}})"
                            },
                        ),
                        ("browser_file_upload", {"paths": ["/etc/hosts"]}),
                        ("upload_file", {"filePath": "/etc/hosts", "uid": "1"}),
                    ]
                    for forbidden, arguments in forbidden_calls:
                        with pytest.raises(ValueError, match="Loopback browser"):
                            await call(forbidden, arguments, f"refuse {forbidden}")

                    async def navigate(url: str) -> Any:
                        step = f"navigate {urlsplit(url).scheme}://{urlsplit(url).netloc}{urlsplit(url).path}"
                        if browser == "playwright":
                            return await call("browser_navigate", {"url": url}, step)
                        return await call(
                            "navigate_page", {"type": "url", "url": url, "timeout": 10000}, step
                        )

                    async def evaluate(function: str) -> str:
                        name = "browser_evaluate" if browser == "playwright" else "evaluate_script"
                        return rendered(
                            await call(name, {"function": function}, f"evaluate {function[:120]}")
                        )

                    if browser == "chrome-devtools":
                        initial = await call(
                            "new_page", {"url": vite, "timeout": 10000}, "new Vite page"
                        )
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
                        assert (
                            b"--proxy-bypass-list=<-loopback>;127.0.0.1;[::1];localhost" in commands
                        )
                        await evaluate(
                            "async () => (await fetch('/api/auth/logout', {method:'POST'})).json()"
                        )
                        assert "false" in await evaluate(
                            "async () => (await fetch('/api/auth/status')).json()"
                        )
                    await navigate(vite)
                    # no-cors succeeds for unrestricted cross-origin requests; CORS cannot fake this refusal.
                    for external in [
                        "https://example.com/",
                        "http://1.1.1.1/",
                        "http://169.254.169.254/",
                        "http://[fe80::1]/",
                    ]:
                        function = f"async () => {{ try {{ await fetch({json.dumps(external)}, {{mode:'no-cors', signal:AbortSignal.timeout(8000)}}); return 'escaped'; }} catch(e) {{ return 'blocked'; }} }}"
                        assert "blocked" in await evaluate(function)
                        with pytest.raises(ValueError, match="Loopback browser"):
                            await navigate(external)
                        await navigate(vite)
                    secret_file = tmp_path / "browser-file-proof.txt"
                    secret_file.write_text("host-file-must-not-load")
                    with pytest.raises(ValueError, match="Loopback browser"):
                        await navigate(secret_file.as_uri())
                    for target in ["ws://169.254.169.254/", "ws://[fe80::1]/"]:
                        function = f"() => new Promise(resolve => {{ const ws = new WebSocket({json.dumps(target)}); const timer = setTimeout(() => {{ ws.close(); resolve('blocked'); }}, 8000); ws.onopen = () => {{ clearTimeout(timer); ws.close(); resolve('escaped'); }}; ws.onerror = () => {{ clearTimeout(timer); resolve('blocked'); }}; }})"
                        assert "blocked" in await evaluate(function)
                    redirected = await navigate(f"{vite}/redirect")
                    assert redirected.is_error or "ERR_" in rendered(redirected)
                finally:
                    logging.getLogger(__name__).info("Browser proof disconnecting %s", browser)
                    async with asyncio.timeout(20):
                        await manager.disconnect_all()
        assert link.split("token=", 1)[1] not in caplog.text

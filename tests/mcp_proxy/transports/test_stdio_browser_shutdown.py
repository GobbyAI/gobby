"""Browser MCP teardown cannot depend on a cooperative server or SDK exit."""

import asyncio
import json
import sys
from pathlib import Path

import psutil
import pytest

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPServerConfig


@pytest.mark.timeout(30, method="thread")
@pytest.mark.asyncio
async def test_manager_disconnect_reaps_browser_ignoring_stdin_and_sigterm(tmp_path: Path) -> None:
    script = tmp_path / "stubborn_mcp.py"
    pidfile = tmp_path / "pids.json"
    script.write_text(
        "import json, os, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])\n"
        "with open(sys.argv[1], 'w') as f: json.dump([os.getpid(), os.getppid(), child.pid], f)\n"
        "for line in sys.stdin:\n"
        " request = json.loads(line)\n"
        " if 'id' not in request: continue\n"
        " response = {'jsonrpc':'2.0', 'id':request['id']}\n"
        " if request['method'] == 'initialize':\n"
        "  response['result'] = {'protocolVersion':request['params']['protocolVersion'], 'capabilities':{'tools':{}}, 'serverInfo':{'name':'stubborn','version':'1'}}\n"
        " elif request['method'] == 'tools/list': response['result'] = {'tools':[]}\n"
        " else: response['error'] = {'code':-32601, 'message':'Method not found'}\n"
        " print(json.dumps(response), flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    config = MCPServerConfig(
        id="stubborn",
        name="stubborn",
        project_id="isolated",
        transport="stdio",
        command=sys.executable,
        args=["-u", str(script), str(pidfile)],
        runtime_hook="loopback_browser",
        template="openapi",
    )
    manager = MCPClientManager(server_configs=[config])
    shutdown: asyncio.Task[None] | None = None
    processes: list[psutil.Process] = []
    try:
        assert await manager.list_tools("stubborn") == {"stubborn": []}
        processes = [psutil.Process(pid) for pid in json.loads(pidfile.read_text())]
        shutdown = asyncio.create_task(manager.disconnect_all())
        done, _ = await asyncio.wait({shutdown}, timeout=8)
        assert shutdown in done, "manager hung disconnecting an EOF/SIGTERM-resistant browser"
        await shutdown
        _, alive = await asyncio.to_thread(psutil.wait_procs, processes, timeout=2)
        assert not alive, "browser process tree survived disconnect"
    finally:
        for process in processes:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        if shutdown is not None:
            await asyncio.wait({shutdown}, timeout=5)

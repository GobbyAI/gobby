"""Exercise the patched CONNECT server against isolated local TCP endpoints."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.srt_fixture_helpers import write_srt_proxy_fixture

_CONNECT_PROBE = r"""
import { createServer, createConnection } from 'node:net';
import { once } from 'node:events';
import { pathToFileURL } from 'node:url';
const { createHttpProxyServer } = await import(pathToFileURL(process.argv[1]));
const events = [];
globalThis.gobbySrtProxyFailure = event => events.push(event);
const origin = createServer(socket => socket.end('pong'));
origin.listen(0, '127.0.0.1');
await once(origin, 'listening');
const port = origin.address().port;
await new Promise(resolve => origin.close(resolve));
const proxy = createHttpProxyServer({
  filter: async (_port, host) => ['api.anthropic.com', 'github.com'].includes(host),
  lookupFor: () => (_host, options, callback) => options.all
    ? callback(null, [{ address: '127.0.0.1', family: 4 }])
    : callback(null, '127.0.0.1', 4),
});
proxy.listen(0, '127.0.0.1');
await once(proxy, 'listening');
async function connect(target) {
  const client = createConnection({ host: '127.0.0.1', port: proxy.address().port });
  client.setTimeout(5000, () => client.destroy(new Error('probe timed out')));
  let reply = '';
  client.on('data', chunk => reply += chunk);
  await once(client, 'connect');
  client.write(`CONNECT ${target} HTTP/1.1\r\nHost: ${target}\r\n\r\n`);
  await once(client, 'close');
  return reply;
}
try {
  const refused = await connect(`api.anthropic.com:${port}`);
  const denied = await connect(`blocked.example:${port}`);
  const invalid = await connect(`user:never-log-this-secret@blocked.example:${port}`);
  origin.listen(port, '127.0.0.1');
  await once(origin, 'listening');
  const replies = await Promise.all(Array.from({length: 32}, (_, index) =>
    connect(`${index % 2 ? 'github.com' : 'api.anthropic.com'}:${port}`)));
  console.log(JSON.stringify({ refused, denied, invalid, replies, events }));
} finally {
  await Promise.all([
    new Promise(resolve => proxy.close(resolve)),
    new Promise(resolve => origin.close(resolve)),
  ]);
}
"""


@pytest.mark.integration
def test_connect_failures_are_logged_and_allowed_tunnels_recover(tmp_path: Path) -> None:
    runtime = os.environ.get("GOBBY_SRT_TEST_ROOT")
    node = shutil.which("node")
    if runtime is None or node is None:
        pytest.skip(
            "requires Node and GOBBY_SRT_TEST_ROOT pointing to the pinned installed runtime"
        )
    shutil.copytree(
        Path(runtime) / "node_modules", tmp_path / "node_modules", copy_function=shutil.copyfile
    )
    proxy = write_srt_proxy_fixture(tmp_path)
    env = {key: value for key, value in os.environ.items() if key != "SRT_DEBUG"}
    result = subprocess.run(
        [node, "--input-type=module", "-e", _CONNECT_PROBE, str(proxy)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["refused"].startswith("HTTP/1.1 502")
    assert observed["denied"].startswith("HTTP/1.1 403")
    assert observed["invalid"].startswith("HTTP/1.1 400")
    assert len(observed["replies"]) == 32
    assert all(
        reply.startswith("HTTP/1.1 200") and reply.endswith("pong") for reply in observed["replies"]
    )
    assert any(event["error_code"] == "ECONNREFUSED" for event in observed["events"])
    assert any(event.get("status") == 403 for event in observed["events"])
    assert "ECONNREFUSED" in result.stderr and "blocked.example" in result.stderr
    assert "never-log-this-secret" not in result.stderr
    assert "never-log-this-secret" not in json.dumps(observed["events"])

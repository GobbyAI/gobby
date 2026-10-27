# Websocket lifecycle publication failure (#25150)

## Scope and evidence

Read-only investigation on 2026-10-26, approximately 11:06–11:18 CDT.
Baseline: `3d625e4ed72d2538458be58b90cc17fc573f9cda`.
Sources: local `errors.log`, `errors.log.1`, `daemon.log`, task cards #25150 and
#11940, source reads, and `gobby-agents:list_running_agents(status="running")`.
No daemon restart, cutover, runtime mutation, or agent termination occurred.
Client UUIDs and the checkout prefix below are redacted. Code-index preflight
failed with `gcode_index_timeout:113.877s`; source citations use direct reads at
the baseline revision, following the evidence fallback.

## Finding

A single recipient disconnect poisons the shared lifecycle publisher. This is a
correctness failure: subsequent lifecycle notifications and detach/workspace
requests fail, including those on new connections. Suppressing the error would
leave the publisher unavailable.

The actual logger/function is `servers.websocket.server._run_off_loop_message`;
the watchdog normalized its name to `servers.websocket.server.runoff_loop_message`.

At the initial sample, `errors.log` contained 847 matching error records, of which
385 were dated October 26. Every October 26 record ended in
`LifecyclePublicationError: lifecycle publication failed: terminal proxy relay closed: ws_close`.
Their handlers were 381 `terminal_detach` and four `workspace_attach`.

The 11:01:29–11:01:46 burst contained 36 events: one workspace attach on
`<client-B>` and 35 detaches on `<client-C>`. This matches the parent's watchdog
report of 36 events and 502 added lines. Traceback lines are not separate events;
the earlier report of 107 added lines cannot establish 107 failed messages.

The earliest retained occurrence is October 25 at 22:21:38. It already fails at
the registry's closed-admission check. The original publication that first
fenced the registry is not identifiable from retained logs. The rotated error
files overlap; their counts must not be added.

### Exact representative traceback, with identifiers redacted

```text
2026-10-26 10:48:29 - ERROR    - servers.websocket.server._run_off_loop_message - Message handling error for client <client-A>
Traceback (most recent call last):
  File "<checkout>/src/gobby/servers/websocket/server.py", line 632, in _run_off_loop_message
    await self._handle_message(websocket, message, blocks_connection=False)
  File "<checkout>/src/gobby/servers/websocket/server.py", line 579, in _handle_message
    await handler(websocket, data)
  File "<checkout>/src/gobby/servers/websocket/terminal_ws.py", line 355, in _handle_terminal_detach
    await self._leases().publish_lifecycle(payload, publish)
  File "<checkout>/src/gobby/terminals/leases.py", line 266, in publish_lifecycle
    raise self._new_lifecycle_error()
gobby.terminals.leases.LifecyclePublicationError: lifecycle publication failed: terminal proxy relay closed: ws_close
```

Other detach records pass through `ProxyHub.finalize_attachment` and
`ProxyHub.emit_lifecycle` before reaching the same closed-admission check.
The workspace-attach traceback at 11:01:29 follows `_read_shared_attach`,
`_read_attach_snapshot`, `_read_workspace`, `workspace_snapshot`,
`_publish_removal`, `_emit`, `_publish_now`, and `broadcast_workspace_event`,
then fails at `leases.py:266` with the same exception. Workspace snapshot
reconciliation therefore fails while publishing a removal, rather than while
sending an ordinary socket reply.

## Lifecycle and fleet correlation

1. A relay send observes `ConnectionClosed` and calls `shutdown("ws_close")`.
   Shutdown closes the socket, retires its proxy attachments, then settles its
   pending lifecycle completions with `ConnectionError`.
   Source: `src/gobby/servers/websocket/proxy_relay.py:141-203` at baseline.
2. `emit_lifecycle` lets the per-socket completion exception escape its publisher
   callback. Capacity refusal also raises after retiring the recipient.
   Source: `proxy_relay.py:358-369`.
3. The shared registry saves every publisher exception as a fatal error, fails
   queued entries, and refuses future submissions. The failure survives client
   reconnects. Source: `src/gobby/terminals/leases.py:248-270,309-363`.
4. The off-loop runner already handles `ConnectionClosed`, but receives the
   translated `LifecyclePublicationError`. Source: `server.py:614-644`.
5. Finalization precedes publication. Missing events do not imply rollback of
   the underlying operation. The traces prove failed notifications and missing
   successful replies; they do not establish lost PTY bytes, killed processes,
   or lost durable conversation content.

The task's initial 59-run census was 47 paused, 11 active, one awaiting handoff,
39 coordination waits, and 12 suspected stalls. A fresh census at approximately
11:13 CDT contained 59 running records: 44 paused, 15 active, 34 coordination
waits, and ten suspected stalls. Retained MCP result:
`a111c38f-11af-40ae-b1d6-1346792e6072` (seven-day tool-result retention).
“Running records” does not mean 59 simultaneously executing workers.

Websocket client IDs are minted per connection and are not run/session IDs.
The sampled logs do not reliably map clients to runs. The parent's #24374 resume
`ws_close` report is compatible with the shared failure, but these traces cannot
attribute the original trigger to that run. High load and many panes increase
delivery/disconnect opportunities; correlation does not prove load caused the bug.

The runner and registry do not retry handlers. Each new request against the
fenced registry adds a traceback and attempts an internal-error reply.
Reconnection/reattachment can produce further failures, but the logs contain no
request IDs proving replay of the same request. This is persistent failure
fanout, not an evidenced server retry loop.

## Prior fixes and resolution

#11940 (`073e8ffda923924386fa90968bb6fd5962515f1f`) guarded ordinary
`ConnectionClosed` at the per-message boundary and former tmux-list response.
That mechanism remains present. #18838 normalized ASGI send disconnects.
This incident takes a different path: socket loss becomes `ConnectionError`
in the relay completion, then a global `LifecyclePublicationError`.

The fix isolates recipient loss inside the existing publication callbacks.
Proxy completion loss and capacity shutdown settle after cleanup; a direct
detach callback catches only `ConnectionClosed`. The shared publisher retains
its ordering, backpressure, shutdown behavior, and fatal handling of real bugs.
The committed sequence advances after retiring the recipient; this does not
claim delivery to the disconnected client. Healthy clients continue normally.

Genuine handler failures retain their traceback and internal-error reply, with
structured `client_id` and message type bounded to 64 characters. Raw payloads
are not logged. Existing relay logging still exposes unexpected send exceptions.
No automatic retry or global logging suppression is added.

The already-fenced live registry remains unchanged. Recovery requires a separate
authorized deployment/runtime replacement through Triage/Orchestrator. No live
recovery is claimed here.

## Tests-first chronology

Before production edits, this command produced three expected assertion failures
in 18.04 seconds:

```sh
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/websocket/test_server.py::test_runoff_loop_message_disconnect_does_not_raise tests/servers/websocket/test_server.py::test_runoff_loop_message_unexpected_failure_has_context -q --tb=short
```

- Disconnect `[proxy]`: unexpected `Message handling error`, ending in the
  production `ws_close` signature.
- Disconnect `[direct]`: unexpected `Message handling error`, with the direct
  disconnect equivalent.
- Unexpected-failure context: `client_id` was `None`, expected `client-1`.

Original RED is retained in session gobby#18140 and local full-output reference
`rtk recall 87212d4595c6`. It was not replayed or manufactured.

The disconnect test holds a send while 36 independent healthy detach requests
queue behind it, then disconnects that recipient. It checks no message-error
logging/internal-error replies, successful ordered notifications/replies for all
36 plus a fresh subsequent detach, continuing watermarks, and bounded sends.
Both proxy and direct paths are exercised.

## Validation

```sh
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/websocket/test_server.py tests/servers/test_websocket_server_disconnects.py tests/servers/test_terminal_list_watermark.py tests/servers/test_terminal_ws_lease.py -q --tb=short
```

**88 passed in 44.79 seconds**, including the named acceptance test, real
publisher-failure fencing, shutdown/cancellation settlement, watermark ordering,
and connection cleanup.

```sh
uv run ruff check src/gobby/servers/websocket/proxy_relay.py src/gobby/servers/websocket/terminal_ws.py src/gobby/servers/websocket/server.py tests/servers/websocket/test_server.py
uv run ruff format --check src/gobby/servers/websocket/proxy_relay.py src/gobby/servers/websocket/terminal_ws.py src/gobby/servers/websocket/server.py tests/servers/websocket/test_server.py
uv run gobby test-quality audit tests/servers/websocket/test_server.py --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low
```

Run separately: Ruff clean; four files formatted; test-quality audit scanned
30 tests and reported zero issues, including low severity.

```sh
uv run mypy src/gobby/servers/websocket/proxy_relay.py src/gobby/servers/websocket/terminal_ws.py src/gobby/servers/websocket/server.py
uv run gobby test-types audit tests/servers/websocket/test_server.py --baseline .gobby/test-types-baseline.json --fail-on-new
uv run gobby test-types suppressions . --baseline .gobby/python-suppressions-baseline.json
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_proxy_relay_disconnect.py tests/servers/test_proxy_relay_encoding.py -q --tb=short
```

Run separately: mypy clean; test-types audit zero errors; suppression ratchet
4,751 files, 169 existing suppressions, zero new/stale; relay checks **four passed
in 1.69 seconds**, including preserved logging for unexpected socket failures.
No full suite was run. Total focused passing tests: **92**.

Self-review covered all three production files, the changed test file, and this
research file. OCR supplied the Python checklist; its default exclusions for
tests/Markdown were reviewed manually. No blocking findings remained. The
adjacent direct-detach poisoning path is fixed within #25150 alongside the
observed proxy path; it is not deferred as separate work.

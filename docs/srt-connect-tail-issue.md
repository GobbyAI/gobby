# Local regression evidence: CONNECT and mux response-tail truncation

The upstream report already exists: [issue 606](https://github.com/anthropics/sandbox-runtime/issues/606).
[PR 612](https://github.com/anthropics/sandbox-runtime/pull/612) addresses CONNECT,
SOCKS and mux close handling; [PR 377](https://github.com/anthropics/sandbox-runtime/pull/377)
addresses CONNECT and SOCKS. This document records Gobby's local evidence and
temporary mitigation. No duplicate report was posted.

## Versions and environment

Reproduced with `@anthropic-ai/sandbox-runtime@0.0.76`, Node.js 26.10.0, macOS arm64.
The upgraded pin is 0.0.79, which requires Node.js >=22.12.0 and retains both
close handlers. The original 0.0.76 observations below establish the diagnosis;
the same portable proof validates the upgrade. On 0.0.79, all three original
transfers were short (3,850,140 bytes), all ten CONNECT-only transfers were full,
and all thirty both-drain transfers were full with matching digests (exit 0).
The earlier CONNECT-only failure remains the reason to retain both changes.

Local preflight resolves Node to `/opt/homebrew/Cellar/node/26.10.0_2/bin/node`.
The Orchestrator's read-only process check confirmed the daemon-launched SRT
runners use the same executable, satisfying the upgraded minimum.

## Expected and observed behavior

A local HTTPS origin sends a 3,864,935-byte response with Content-Length and a normal
connection close. The client reaches it through the runtime's multiplexed proxy and
HTTPS CONNECT tunnel, reading slowly to create backpressure.

On three original-runtime runs, the client received 3,850,140 bytes: a 14,795-byte
tail was missing, and the payload SHA-256 differed. Changing only the CONNECT
handler initially produced three complete responses, but later repeated runs
still truncated: one of ten returned 3,800,988 bytes, and a separate three-run
check lost a tail once. Changing only the mux handler also did not fix it.
Both upstream-close handlers need to drain their downstream writes. A ten-run
comparison with both changes returned all 3,864,935 bytes and matching SHA-256
on every run. A subsequent run of the attached repro produced three original
truncations, one CONNECT-only truncation in ten, and thirty complete both-drain
transfers with matching digests (exit 0).

In a managed sandbox this manifested as truncated npm tarball downloads. A full
Python HTTP read raised IncompleteRead; a streaming read reached premature EOF.
Host downloads completed with the expected tarball size and SHA-256. Installer
integrity checks correctly refused the truncated bytes.

## Suspected cause and minimal correction

In `dist/sandbox/http-proxy.js`, after the established CONNECT pipes:

```diff
- upstream.on('close', () => socket.destroy());
+ upstream.on('close', () => socket.end());
```

In `dist/sandbox/mux-proxy.js`, after the mux pipes:

```diff
- upstream.once('close', () => client.destroy());
+ upstream.once('close', () => client.end());
```

`destroy()` discards downstream writes still queued when the upstream socket closes.
`end()` allows those bytes to drain before the downstream FIN at both proxy hops.
Draining the CONNECT hop alone leaves the mux hop able to discard the same tail.
Existing socket-error
handlers still destroy failed connections, and downstream close still destroys the
upstream. Error/abort and half-open cleanup remain distinct from the normal-close
case; the reproduction specifically proves the response-tail loss boundary.

## Local-only reproduction

Run `docs/repros/srt-connect-tail.mjs`. It serves synthetic bytes on loopback,
generates a disposable self-signed certificate, imports the runtime modules, and
compares original, CONNECT-only, and both-drain in-memory modules. It never modifies the package,
accesses credentials, or contacts an external service during the reproduction.

From a disposable directory, install the upstream package, then run:

```sh
npm install --ignore-scripts --no-audit --no-fund @anthropic-ai/sandbox-runtime@0.0.79
node /path/to/srt-connect-tail.mjs ./node_modules/@anthropic-ai/sandbox-runtime
```

Node.js and OpenSSL must be available. The package installation needs network access;
the 43 reproduction transfers are entirely local (3 original, 10 CONNECT-only,
30 both-drain). Output reports label, round,
expected bytes, received bytes, and SHA-256 match. Counts can depend on buffering;
a complete response must always match both size and digest. The script exits
nonzero if any both-drain transfer is short or has the wrong digest. Finite repeated
runs provide regression evidence, not a guarantee for every error or abort path.

## Temporary downstream mitigation

Gobby applies these two close-handler changes during atomic installer staging and pins
each complete patched file's SHA-256, independently of its content manifest. Unknown
upstream bytes are refused. Remove the downstream patch when an upstream release
contains and validates the fix. No sandbox policy grants are changed.

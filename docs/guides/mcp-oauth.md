# OAuth MCP servers

Gobby supports browser-based OAuth authorization for remote HTTP and legacy SSE
MCP servers. The MCP SDK discovers the authorization server, registers a public
client, and uses the authorization-code flow with PKCE. Credentials are encrypted
in Gobby's secret store and isolated by project, server instance, and endpoint URL.

For a remote endpoint (replace the example URL with the service's MCP URL):

```bash
gobby mcp-proxy add-server remote --transport http --url https://mcp.example.test/mcp --oauth
gobby mcp-proxy auth remote
```

The command verifies the authorized MCP connection and updates the daemon's
server configuration. Use `--global` on both commands for a machine-wide server.
Native MCP add does not expose an OAuth flag; use the operator login command or
the authenticated HTTP configuration interface.

For an existing HTTP/SSE server, run `gobby mcp-proxy auth NAME`; successful login
enables OAuth for that instance. Login opens a temporary `127.0.0.1` callback
listener on the machine running the command. The browser must be able to reach
that machine's loopback address. The command also prints the authorization link
if the browser does not open automatically. Consent times out after 300 seconds;
use `--timeout SECONDS` to change that deadline or Ctrl-C to cancel.

Daemon connections reuse saved credentials and refresh expired access tokens on demand,
including after a daemon restart. No periodic keepalive is required. When a first tool or
resource request needs consent, the local daemon opens the browser, waits for authorization,
and retries the connection once. If the browser cannot open, consent expires, or access is
revoked, run `gobby mcp-proxy auth NAME` again. A changed server URL or a recreated instance
requires its own authorization. Keep the callback port free when signing in again: it is reused
from the saved client registration.

Servers must support dynamic client registration and the authorization-code flow
for this login command. Provider-specific API keys remain configurable through
headers. OAuth is supported on HTTP/SSE transports; stdio and WebSocket servers do
not use this authorization flow.

If login reports that credentials were saved but updating the daemon failed,
repair the daemon connection and rerun login. Saved tokens alone do not prove
the running connection adopted authorization. Instance removal/recreation also
changes OAuth identity; rotate or reauthorize the existing row when possible.

Implementation: `src/gobby/cli/mcp_oauth.py::auth_server`,
`src/gobby/mcp_proxy/oauth.py::authorize_server`, and the client manager's
`_authorize_oauth_once` path. Verification uses isolated callback/transport
fixtures; no live account sign-in is needed to audit these procedures.

_Last verified: 2026-09-12_

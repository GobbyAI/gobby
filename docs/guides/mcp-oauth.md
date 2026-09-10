# OAuth MCP servers

Gobby supports browser-based OAuth authorization for remote HTTP and legacy SSE
MCP servers. The MCP SDK discovers the authorization server, registers a public
client, and uses the authorization-code flow with PKCE. Credentials are encrypted
in Gobby's secret store and isolated by project, server instance, and endpoint URL.

For [Fieldy](https://fieldyai.github.io/docs/#/mcp):

```bash
gobby mcp-proxy add-server fieldy --transport http --url https://api.fieldy.ai/mcp --oauth
gobby mcp-proxy auth fieldy
```

Sign in in the browser with the same email used in the Fieldy app (including your
Apple Private Relay email, when applicable). The command verifies the authorized
MCP connection and updates the daemon's server configuration. Use `--global` on
both commands for a machine-wide server.

For an existing HTTP/SSE server, run `gobby mcp-proxy auth NAME`; successful login
enables OAuth for that instance. Login opens a temporary `127.0.0.1` callback
listener on the machine running the command. The browser must be able to reach
that machine's loopback address. The command also prints the authorization link
if the browser does not open automatically. Consent times out after 300 seconds;
use `--timeout SECONDS` to change that deadline or Ctrl-C to cancel.

Daemon connections reuse saved credentials and refresh expired tokens, including
after a daemon restart. They never open a browser automatically. If consent expires
or is revoked, run `gobby mcp-proxy auth NAME` again. A changed server URL or a
recreated instance requires its own authorization. Keep the callback port free
when signing in again: it is reused from the saved client registration.

Servers must support dynamic client registration and the authorization-code flow
for this login command. Provider-specific API keys remain configurable through
headers. OAuth is supported on HTTP/SSE transports; stdio and WebSocket servers do
not use this authorization flow.

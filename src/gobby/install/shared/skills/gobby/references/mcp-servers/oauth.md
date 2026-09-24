# Browser OAuth

Load when a remote HTTP/SSE server needs browser sign-in or saved authorization
fails. Discover the exact instance and scope first. OAuth login is currently an
operator CLI procedure, not a native MCP authentication tool.

```bash
gobby mcp-proxy auth INSTANCE
gobby mcp-proxy auth INSTANCE --global
```

Successful login verifies initialization/tool discovery, saves encrypted tokens,
and updates the instance's OAuth configuration. For manual creation the operator
can use `add-server ... --oauth`; HTTP clients can set `requires_oauth`. The
native add tool does not expose that field. Do not invent an `oauth` argument.

The callback is a temporary 127.0.0.1 listener on the machine running login;
the browser must reach that machine. Default consent timeout is 300 seconds;
`--timeout SECONDS` changes it. The command prints a link if browser opening fails.
Ctrl-C cancels the login. A saved client registration reuses its callback port;
keep that port available.

Saved OAuth state is isolated by project, instance UUID, and endpoint URL.
Changing the URL or recreating the instance requires authorization for the new
identity. Daemon connections reuse/refresh saved tokens. A connection that needs
consent can open the local browser and retry once; consent failure requires
operator login, not repeated tool calls or copying another instance's tokens.

To check what is saved without exposing it, `gobby mcp-proxy oauth-shape INSTANCE`
(`--global` for a machine-wide instance) prints each stored OAuth field as its
type name or null, never its value. It reads storage directly; no daemon is needed.

The supported flow uses authorization code, PKCE, and dynamic client registration.
Stdio/WebSocket transport does not use this flow. If credentials were saved but
the daemon update failed, fix the daemon connection and rerun login; do not claim
the running connection is authorized from storage success alone.

Guide: [OAuth MCP servers](../../../../../../../../docs/guides/mcp-oauth.md).

_Last verified: 2026-09-12_

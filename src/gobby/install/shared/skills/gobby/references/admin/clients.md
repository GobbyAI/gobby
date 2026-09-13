# Client access

Load for browser access, native terminal client setup, provider MCP/hook
connections, or reconnect failures. Begin with daemon health and the configured
endpoint. Client installation and terminal/UI operation are operator procedures;
agents use their authorized domain MCP tools.

1. For the browser, open the daemon's installed UI origin or the configured dev
   UI endpoint. Sign in with the canonical account. The HTTP-only
   `gobby_session` cookie authorizes browser API calls and the `/ws` proxy.
2. The current web shell centers on Chat, its Activity panel, and the Settings
   overlay. Use current controls; retired `#dashboard`, `#projects`, and
   `#traces` navigation is not a supported diagnostic route.
3. For the terminal workspace, inspect `gclient --help`. Select the intended
   `--project`, `--daemon-url`, and `--token-file` when defaults do not identify
   the target. Startup checks configuration, health, and host protocol support.
4. Distinguish observing a terminal from taking keyboard control. Follow the
   gclient guide for control, detach, close, and orphan cleanup; a diagnostic
   request does not authorize closing terminals or removing worktrees.
5. For provider clients, use the named installer component and verify its MCP
   and hook connection with a scoped read. A connected MCP server is not an
   installed skill. Load the skills or integrations capability for those flows.

For a stdio provider transport, `gobby mcp-server` runs the MCP proxy process.
It can auto-start the daemon and forwards daemon tools; use it as the client's
configured transport command, not as a read-only health probe.

Ordinary daemon restarts preserve native terminals. gclient reconnects and
reattaches surviving terminal IDs; an unconfirmed write can leave a pane
read-only until control is reacquired. Do not blindly replay uncertain writes.
Explicit terminal drain or host replacement can remove the terminals themselves.

For 401 responses, use `authentication.md`. For protocol or binary mismatch,
verify the coherent installed versions before cutover. Browser connection
troubleshooting should preserve the specific HTTP/WebSocket error and affected
session rather than deleting local layout or session data.

See [web access](../../../../../../../../docs/guides/web-ui.md)
and [gclient](../../../../../../../../docs/guides/gclient-user-guide.md).

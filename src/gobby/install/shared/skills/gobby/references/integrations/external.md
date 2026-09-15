# External MCP integrations

Load when connecting GitHub or Linear as MCP servers. Gobby does not import,
sync, or store GitHub/Linear issue identity on projects or tasks.

1. Discover the configured server with `gobby-mcp` / `gobby mcp-proxy
   list-servers` and lease each tool schema before calling it.
2. Instantiate the bundled `github` or `linear` template when the operator
   wants those servers. Templates are not live config; inspect the installed
   row.
3. Call the server's own tools for issues, pull requests, and comments.
   Gobby has no first-party GitHub or Linear issue/PR tools.
4. Repair credentials and MCP connection state before retrying. An unavailable
   connector is not evidence that Gobby still owns the workflow.

See [integrations](../../../../../../../../docs/guides/integrations.md) and
[MCP tools](../../../../../../../../docs/guides/mcp-tools.md).

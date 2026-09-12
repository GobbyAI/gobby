# Gobby Annotate MCP

Read-only stdio MCP server and inspection CLI for portable Gobby Annotate ZIPs.
Requires Node 22.18+. No Gobby daemon, Python, browser, or checkout is required.

```sh
gobby-annotate-mcp --root /absolute/path/to/captures
gobby-annotate inspect ./capture.zip
gobby-annotate extract ./capture.zip --to ./new-directory
```

Configure your MCP client with command `gobby-annotate-mcp` and arguments
`["--root", "/absolute/path/to/captures"]`. Tools: `list_captures`,
`get_capture`, `get_annotation`, and `get_screenshot`. Images are returned only
by the screenshot tool. New exports appear without restarting the server.

The root must exist. Captures are validated with version, path, asset, and
128 MiB uncompressed limits. Diagnostics use stderr; stdout is MCP-only.
Licensed FSL-1.1-ALv2; see LICENSE.

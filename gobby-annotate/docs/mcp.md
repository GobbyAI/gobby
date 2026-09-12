# Standalone MCP and command line

Build and package from `gobby-annotate/`:

```sh
npm ci
npm run build:mcp
npm pack --workspace gobby-annotate
npm install --global ./gobby-annotate-0.1.0.tgz
```

The installed package bundles shared capture code and installs its Node
dependencies. It needs Node 22.18+ but no repository checkout, Python, or daemon.
Both `gobby-annotate-mcp` and `gobby-annotate` are executable entrypoints.

Configure any stdio MCP client with:

```json
{
  "mcpServers": {
    "gobby-annotate": {
      "command": "gobby-annotate-mcp",
      "args": ["--root", "/absolute/path/to/captures"]
    }
  }
}
```

Create that directory first. Place ZIP exports or extracted bundles directly in
it. Discovery refreshes on requests, so new exports need no server restart.
`list_captures` returns summaries and invalid-bundle diagnostics; `get_capture`
reads an export UUID; `get_annotation` addresses export and annotation UUIDs;
`get_screenshot` returns PNG image content only when explicitly requested.
Fetch the server's tool schemas for exact arguments. Ordinary text responses
contain references, never embedded image payloads. Stdout is protocol-only;
diagnostics go to stderr. No network listener or mutation tools are exposed.

For non-MCP inspection:

```sh
gobby-annotate inspect ./capture.zip
gobby-annotate extract ./capture.zip --to ./extracted-capture
```

Inspection prints validated JSON without screenshot base64. Extraction requires
a new destination and preserves validated image bytes. Unsafe paths, symlinks,
unsupported versions, invalid assets, and size-limit violations are errors.

Gobby users should follow [managed attachment](gobby-mcp.md), then
[task import](gobby-integration.md). Do not configure a parallel private server
for the skill.

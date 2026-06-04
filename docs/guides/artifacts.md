# Artifacts

Artifacts are rendered file outputs that agents can show inside Gobby chat. An
agent calls the `gobby-artifacts` MCP tool to display a repository file — source,
markdown, CSV, or image — in the web chat artifact panel.

## Mental Model

The agent calls `gobby-artifacts:show_file` with an absolute path. The daemon
validates the request, classifies the file by extension into an artifact type,
reads its contents, and broadcasts an artifact event to the web app. The web app
opens the file in the artifact panel.

## Quick Start

Discover the artifacts tool through the MCP proxy:

```text
list_mcp_servers
list_tools(server_name="gobby-artifacts")
get_tool_schema(server_name="gobby-artifacts", tool_name="show_file")
```

Show a repository file in the artifact panel with `show_file`.

## MCP

`gobby-artifacts` exposes one tool:

| Tool | Use |
|------|-----|
| `show_file` | Display a supported local file in the artifact panel |

## Artifact Panel

`show_file` requires an absolute file path. It supports:

- Text and docs: `md`, `txt`, `rst`, `adoc`.
- Code: common source extensions, including Python, TypeScript, JavaScript,
  shell, JSON, YAML, TOML, CSS, HTML, Rust, Go, Java, C, C++, and SQL.
- Data: `csv`, `tsv`.
- Images: `png`, `jpg`, `jpeg`, `gif`, `webp`, `svg`.

Text and code files must be UTF-8 and are limited to 1 MB. Images are limited to
5 MB and are delivered as base64 data URLs. The daemon broadcasts artifact
metadata to the web app, which renders the content in the artifact panel.

## CLI

There is no dedicated artifacts CLI. Create or update files through the normal
repository workflow, then call `show_file` with the absolute path. Use the daemon
commands for service state:

```bash
uv run gobby status
uv run gobby restart
```

## HTTP

Artifact events are delivered through the web app's live event channel. When
debugging a missing artifact, verify that the daemon is running, the path is
absolute, the file type is supported, and the file size is under the enforced
limit.

## File Locations

- `src/gobby/mcp_proxy/tools/artifacts.py`: `show_file` tool definition,
  extension map, size limits, and artifact broadcast logic.
- `web/src/components/chat/artifacts/`: artifact panel rendering.
- `web/src/hooks/useChat/transportArtifactEvents.ts`: client handler for
  `artifact_event` frames.

## See Also

- [web-ui.md](web-ui.md)
- [mcp-tools.md](mcp-tools.md)
- [http-endpoints.md](http-endpoints.md)
- [frontend-style-guide.md](frontend-style-guide.md)

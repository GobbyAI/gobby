# Canvas Artifacts

Canvas artifacts are the interactive and rendered outputs that agents can show
inside Gobby chat. They cover two related surfaces: A2UI canvases for structured
interactive panels, and artifact panels for files or sandboxed HTML.

## Mental Model

Agents call the `gobby-canvas` MCP tools. The daemon validates the canvas or file
request, stores per-conversation state, and broadcasts UI events to the web app.
The web app renders A2UI components in the canvas panel or opens file artifacts in
the artifact panel.

Use A2UI canvas tools when the agent needs structured interaction such as buttons,
text fields, checkboxes, cards, lists, badges, images, or a small form. Use file
artifacts when the agent needs to show a markdown file, source file, CSV, image,
or standalone HTML page.

## Quick Start

Discover the canvas tools through the MCP proxy:

```text
list_mcp_servers
list_tools(server_name="gobby-canvas")
get_tool_schema(server_name="gobby-canvas", tool_name="render_surface")
```

Render an interactive surface with `render_surface`, update it with
`update_surface`, wait for user input with `wait_for_interaction`, and close it
with `close_canvas`.

Show a repository file in the artifact panel with `show_file`. Present a
standalone HTML artifact with `canvas_present`.

## MCP

`gobby-canvas` exposes these tools:

| Tool | Use |
|------|-----|
| `render_surface` | Create or replace an A2UI canvas in a conversation |
| `update_surface` | Patch an existing canvas surface |
| `wait_for_interaction` | Block until the user interacts or a timeout expires |
| `close_canvas` | Close a canvas surface |
| `canvas_present` | Serve an absolute HTML file through the canvas sandbox |
| `show_file` | Display a supported local file in the artifact panel |

The A2UI renderer accepts component types such as `Text`, `Button`, `TextField`,
`CheckBox`, `Row`, `Column`, `Card`, `List`, `Image`, `Icon`, and `Badge`.

Important limits are enforced by the daemon:

- Maximum 50 canvases per conversation.
- Maximum 1,000 canvases globally.
- Maximum 200 components per surface.
- Maximum 64 KB data model.
- Maximum 10 render calls per minute per conversation.
- Default interaction timeout of 300 seconds.
- Maximum interaction timeout of 600 seconds.

## Artifact Panel

`show_file` requires an absolute file path. It supports:

- Text and docs: `md`, `txt`, `rst`, `adoc`.
- Code: common source extensions, including Python, TypeScript, JavaScript,
  shell, JSON, YAML, TOML, CSS, HTML, Rust, Go, Java, C, C++, and SQL.
- Data: `csv`, `tsv`.
- Images: `png`, `jpg`, `jpeg`, `gif`, `webp`, `svg`.

Text and code files must be UTF-8 and are limited to 1 MB. Images are limited to
5 MB. The daemon broadcasts artifact metadata to the web app; the browser reads
the served artifact through the daemon route.

## HTML Sandbox

`canvas_present` is for standalone HTML files. It requires an absolute path,
copies the file into Gobby's canvas storage under `~/.gobby/canvas`, and serves it
from:

```text
/__gobby__/canvas/{uuid}.html
```

Use this for generated previews that need their own DOM, CSS, or JavaScript. Keep
HTML artifacts self-contained, and do not assume access to arbitrary project
files unless the artifact explicitly embeds or fetches them through approved
routes.

## Interaction Flow

1. Agent renders a surface with stable component IDs.
2. Web UI receives the canvas event and renders it in the canvas panel.
3. User clicks, types, toggles, or submits an action.
4. The daemon records the interaction for the conversation.
5. Agent calls `wait_for_interaction` and receives the payload.
6. Agent updates or closes the surface.

Canvas interactions are conversation-scoped. They are not a replacement for
durable application state; persist important outcomes through the owning task,
plan, memory, or storage API.

## CLI

There is no dedicated canvas CLI. Operate the surface through MCP tools and use
the daemon commands for service state:

```bash
uv run gobby status
uv run gobby restart
```

For file-based artifacts, create or update files through the normal repository
workflow, then call `show_file` with the absolute path.

## HTTP

The primary HTTP surface is the static canvas mount:

```text
/__gobby__/canvas/*
```

Chat and artifact events are delivered through the web app's live event channel.
When debugging a missing artifact, verify that the daemon is running, the path is
absolute, the file type is supported, and the file size is under the enforced
limit.

## File Locations

- `src/gobby/mcp_proxy/tools/canvas.py`: MCP tool definitions, validation,
  limits, and artifact broadcast logic.
- `web/src/components/canvas/`: A2UI canvas rendering components.
- `web/src/components/chat/artifacts/`: artifact panel rendering.
- `web/src/components/chat/`: chat integration with canvas and artifacts.
- `~/.gobby/canvas`: copied HTML artifacts served by the daemon.

## See Also

- [web-ui.md](web-ui.md)
- [mcp-tools.md](mcp-tools.md)
- [http-endpoints.md](http-endpoints.md)
- [frontend-style-guide.md](frontend-style-guide.md)

_Last verified: 2026-05-08_

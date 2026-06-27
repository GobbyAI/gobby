# CLI Support Feature Matrix - Codex Findings

Status: research artifact / evaluation gate
Researched: 2026-06-27
Owner: Codex session #7708

This document evaluates popular coding CLIs that Gobby does not currently expose
as first-class providers. It is not an integration plan. Its job is to prevent
another AGY-style partial landing: hook installation exists, but web chat,
spawning, durable transcript ingestion, and live transport remain unavailable.

## Readiness Gate

A CLI is Gobby-ready only when all critical surfaces are proven from official
docs, source code, or a local probe:

| Surface | Required evidence |
| --- | --- |
| Lifecycle hooks | Session/turn start, pre-tool, post-tool, stop/turn-end events with stable payloads. |
| Context injection | A documented response channel for additional context or system messages. |
| Tool control | Allow/deny/ask and, ideally, tool-input rewrite or retry semantics. |
| Transcripts | Durable machine-readable history with messages, tool calls/results, usage where available, and a discoverable session ID/path. |
| Resume/session identity | CLI-native session IDs that can be mapped to Gobby sessions and resumed. |
| Web-chat streaming/control | Structured streaming transport suitable for Gobby web chat, preferably ACP or a stable SDK/server API. |
| Cancellation/interrupt | A documented way to cancel or interrupt a running turn. |
| Install model | Predictable global/project config paths and uninstallable hook assets. |

Status meanings:

| Status | Meaning |
| --- | --- |
| `ready-to-spike` | Enough surfaces are documented to justify a focused integration spike. |
| `probe-required` | Promising, but one or more critical surfaces need local verification. |
| `defer` | Important surfaces are missing, unstable, or product direction is shifting. |
| `reject-first-class` | Architecture does not match Gobby's hook/transcript/web-chat model. |

## Current Baseline

| CLI | Current Gobby state | Integration note |
| --- | --- | --- |
| Claude Code | Supported | Strong lifecycle hooks, transcript ingestion, web-chat backend. |
| Codex CLI | Supported | hooks.json plus app-server JSON-RPC backend. |
| Droid | Supported | Standalone adapter; no cross-CLI inheritance. |
| Grok | Supported | ACP-backed live transport plus hook adapter. |
| Qwen Code | Supported | ACP-backed live transport plus hook adapter. |
| AGY | Installed-only / unavailable | Hook install parity exists, but no proven daemon-usable context channel, transcript path, stable streaming transport, or session resume surface. Keep unavailable until upstream exposes those surfaces. |

## Candidate Matrix

| CLI | Hooks | Transcripts / session IDs | Web-chat streaming | Tool control / context | Local model / MCP story | Status | Call |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OpenCode | Plugin/server surfaces look strong; hook parity still needs proof for Gobby-grade turn/tool events. | Session APIs and storage exist; exact transcript export shape needs probe. | Strong: server/SDK style transport is promising. | Needs probe for blocking and context injection. | Strong local/open-source story; supports multiple providers. | `ready-to-spike` | Best first spike for the open-source local-LLM lane. |
| Cline | Hooks are documented for tool-use decisions. | CLI JSON output and task history exist; persistent transcript format needs probe. | CLI/SDK surfaces look usable; streaming event shape needs probe. | Promising: hooks can shape tool decisions. | Strong OSS ecosystem and model flexibility. | `ready-to-spike` | Good second first-wave spike after OpenCode. |
| GitHub Copilot CLI | Official hooks reference is strong. | Transcript/session fields need local verification for CLI-created work. | ACP/public CLI surfaces look promising; streaming behavior needs probe. | Promising: permission and hook semantics are documented. | Commercial provider, not local-first. | `ready-to-spike` | Worth a spike, but do not mark supported until transcripts and streaming are proven. |
| Goose | Hooks exist through Goose extensions/plugins. | Sessions exist; current storage format and parser cost need probe. | ACP/client docs make web-chat plausible. | Tool control appears promising; exact response semantics need probe. | Strong local-LLM candidate, especially Ollama-style workflows. | `probe-required` | Second-wave candidate; likely custom transcript parser. |
| Auggie / Augment | CLI hooks are documented. | Session/transcript export needs proof. | ACP docs exist. | Tool-execution interception is promising. | Commercial, strong repo context. | `probe-required` | Spike after OpenCode/Cline/Copilot unless user demand is high. |
| OpenHands | Lifecycle hooks are documented. | Runtime sessions/events likely exist, but durable transcript shape needs proof. | ACP/SDK direction is promising but hosting direction must be verified. | Hooks can block commands and inject checks/context. | Strong OSS platform, heavier than a simple CLI. | `probe-required` | Good research target, but likely higher operational cost. |
| Cursor CLI | CLI exists and stream-json-style output helps observability. IDE hooks are richer than CLI hooks. | Transcript path/session behavior needs proof. | CLI streaming exists; daemon control and resume semantics need probe. | Risk: current CLI hook coverage appears incomplete versus IDE hook docs. | Commercial, strong adoption. | `defer` | Do not add first-class support from IDE hook docs alone. |
| Windsurf / Cascade / Devin Desktop | Cascade hooks existed, but product surface is shifting. | Transcript/session export unclear. | No proven headless daemon transport for Gobby. | Tool control unclear in current product direction. | Commercial, moving target. | `defer` | Re-evaluate after Devin Desktop/Local publishes stable CLI contracts. |
| Aider | No Gobby-grade lifecycle/tool hook system. | Markdown chat history exists, but not a structured tool-event transcript. | Scripting/streaming exists, but no ACP-like control surface. | Weak: edit/commit loop does not expose tool mediation. | Strong local model support. | `reject-first-class` | Useful tool, wrong architecture for Gobby first-class integration. |
| Continue | CLI exists, but current public docs do not prove hooks/transcripts/control. | Unproven. | Unproven for Gobby web-chat control. | Unproven. | Strong model/config ecosystem; maintenance/product direction needs verification. | `defer` | Revisit only if current CLI docs expose lifecycle hooks and session history. |

## Recommended Order

1. OpenCode: strongest fit for the missing open-source local-LLM-focused CLI.
   Start with a contract probe for hooks, blocking decisions, session storage,
   resume, streaming, and cancellation.
2. Cline: strong OSS adoption and documented hooks. Verify persistent transcript
   path and whether CLI/SDK streaming can drive Gobby web chat without screen
   scraping.
3. GitHub Copilot CLI: likely cheap hook adapter if payloads remain close to
   Claude/Codex-style contracts. Verify local transcript availability and ACP or
   equivalent streaming before adding install flags.
4. Goose, Auggie, OpenHands: credible second wave. Each needs a focused probe,
   especially around transcript format and control direction.
5. Cursor, Windsurf, Aider, Continue: defer or reject until they expose the
   missing surfaces. Popularity is not enough.

## Probe Checklist

Run this before creating an adapter task:

1. Install the CLI in an isolated temp home.
2. Create a minimal project and run one turn with a file read, file edit, shell
   command, failed command, and cancellation.
3. Capture hook payloads for session start, user prompt/turn start, pre-tool,
   post-tool, stop/turn end, and permission prompts.
4. Confirm the hook response can block a tool, inject context, and optionally
   rewrite tool input.
5. Locate the transcript/session store. Verify message text, assistant output,
   tool calls, tool results, errors, timestamps, and token usage where available.
6. Verify resume from the captured CLI-native session ID.
7. Verify streaming transport produces structured deltas and tool events without
   terminal scraping.
8. Verify cancellation/interrupt behavior and terminal process cleanup.
9. Identify global and project config paths, precedence, and uninstall behavior.
10. Record exact CLI version, docs URL, payload samples, and confidence level in
    the matrix before implementation starts.

## Adapter Acceptance Bar

Do not expose a new CLI in `gobby install`, provider pickers, or agent spawning
until all of these are true:

- `SessionSource` and provider metadata are backed by a tested adapter.
- Capabilities declare unsupported response fields honestly.
- Transcript ingestion has fixture-backed parser tests.
- Web chat can stream, interrupt, resume, and surface tool events.
- Install/uninstall tests cover global and project scope without duplicate hooks.
- The provider is not marked generally available if any critical surface is
  probe-only.

## Sources

- Cursor CLI / hooks: <https://docs.cursor.com/en/cli/overview>,
  <https://docs.cursor.com/en/cli/commands>, <https://cursor.com/docs/hooks>,
  <https://forum.cursor.com/t/cursor-cli-doesnt-send-all-events-defined-in-hooks/148316>
- OpenCode: <https://opencode.ai/docs/cli>, <https://opencode.ai/docs/plugins/>,
  <https://opencode.ai/docs/server/>, <https://opencode.ai/docs/providers/>
- Cline: <https://docs.cline.bot/usage/cli-overview>,
  <https://cline.bot/blog/cline-v3-36-hooks>, <https://cline.bot/sdk>
- Goose: <https://goose-docs.ai/blog/2026/05/14/goose-hooks/>,
  <https://goose-docs.ai/docs/guides/goose-cli-commands/>,
  <https://goose-docs.ai/docs/getting-started/providers>
- GitHub Copilot CLI: <https://docs.github.com/en/copilot/reference/hooks-reference>,
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/overview>
- Windsurf / Cascade: <https://docs.devin.ai/desktop/cascade/hooks>
- Aider: <https://aider.chat/docs/config/options.html>,
  <https://aider.chat/docs/scripting.html>, <https://aider.chat/docs/llms.html>
- Auggie / Augment: <https://docs.augmentcode.com/cli/hooks>,
  <https://docs.augmentcode.com/cli/reference>
- OpenHands: <https://docs.openhands.dev/openhands/usage/customization/hooks>,
  <https://docs.openhands.dev/openhands/usage/cli/command-reference>
- Continue: <https://docs.continue.dev/>, <https://docs.continue.dev/guides/cli>

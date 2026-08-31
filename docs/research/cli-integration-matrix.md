# CLI Integration Feature Matrix & Readiness Gate

**Status:** research artifact / evaluation framework. Researched 2026-06-27; updated
2026-08-30 (AGY 1.1.18+ integration surfaces and readiness classification).
**Purpose:** score coding CLIs against Gobby's three integration surfaces so a CLI
is only marked "supported" once it clears all three. AGY's recovery from an obsolete
hook-only assessment demonstrates why each surface must be probed against the current
CLI version and why a stable custom subprocess transport can satisfy web chat without ACP.

This is an **evaluation framework**, not an integration plan. It records what each
CLI exposes and where it sits relative to the gate. The decision of what to build
next is separate.

**Related docs:**

- [Adapter fidelity guide](../guides/adapter-fidelity.md) — per-hook response
  fidelity after a CLI is integrated
- [AGY/Grok contract probe](../../.gobby/plans/task-15038-agy-grok-contract-probe.md) —
  template for pre-integration contract probes

## Executive summary

Gobby runs **under** coding CLIs as a control-plane daemon. A CLI is integration-ready
only when the **upstream product** exposes all three surfaces below — not when Gobby
can partially wire one of them. Do not integrate based on marketing docs; gate on
verified surfaces per invocation mode.

**The historical agy trap:** the 1.0.11 assessment stopped at hook parity because it
found an opaque protobuf store and no ACP server. AGY 1.1.18 disproved that conclusion:
it persists parseable JSONL and exposes a stable custom stream-json subprocess
transport. ACP was never required. See [The agy lesson](#the-agy-lesson-why-this-matrix-exists).

**Top candidates (research conclusion, not a build order):**

| Priority | CLI | Why |
| --- | --- | --- |
| **P1** | **GitHub Copilot CLI** | Full hooks (Claude-compat path), JSONL transcripts, ACP preview — lowest adapter effort |
| **P2** | **Goose** (AAIF, OSS) | ACP GA + Open Plugins hooks + local-LLM (Ollama); SQLite parser is the custom work — **likely the OSS local-LLM CLI** |
| **P3** | **Cursor CLI** (`agent`) | ACP GA is strong; **CLI hook parity is partial** — contract probe required before "supported" |
| **Watch** | **OpenCode** | ACP + DB transcripts; hooks pending ([issue #12472](https://github.com/anomalyco/opencode/issues/12472)) |
| **Defer** | Windsurf/Devin Desktop, Aider, ForgeCode | Mid-pivot, wrong architecture, or no hook surface |
| **Supported** | **agy / Antigravity** | Five hooks, JSONL transcripts, and custom stream-json web chat are verified on 1.1.18+ |

**Critical Cursor distinction:** Cursor **IDE** has full hooks; Cursor **CLI**
(`agent`) has GA ACP but **incomplete hook parity** in headless/`--print` modes.
Integrating today without a per-mode contract probe risks the historical AGY failure: ACP works
for web chat, but `preToolUse` and lifecycle hooks may not fire where dispatch runs.

## How to use this matrix

1. Before starting any new CLI integration, find the CLI's row and confirm all three
   surface cells are green (or have a concrete plan to turn them green).
2. If a cell is **unverified**, run the [verification protocol](#verification-protocol)
   before scoring it. Never guess.
3. A CLI with two-of-three surfaces is **limbo** — do not ship it as "supported."
   Either finish the third surface or keep it out of the install/detection layer.
4. ACP-speaking CLIs reuse the shared `ACPWebChatBackend`; non-ACP CLIs need a
   custom backend (Claude/Codex style). ACP is the cheap path and is the single
   biggest readiness lever.
5. Score hooks **per invocation mode** when a CLI has multiple entry points (IDE vs
   `agent acp` vs `agent -p` vs cloud agents). A "Ready" IDE score does not imply
   CLI readiness.

## The 3-surface readiness gate

A CLI is **"supported"** only when all three surfaces exist **in the upstream CLI**
(and are verified for the modes Gobby will use). Two-of-three = limbo. One-of-three
with the rest unavailable upstream = blocked.

| Surface | What it is | Code locations (reference) |
| --- | --- | --- |
| **Hook adapter** | Terminal lifecycle interception via `ghook` — rule enforcement, context injection, tool gating. Needs: event coverage, decision control (allow/deny/modify/ask), context-injection channel, config format + path, fail-open vs fail-closed, matchers. | `adapters/<cli>.py`, `adapters/<cli>_contract.py`, `adapters/capabilities.py`, `install/<cli>/hooks-template.json` |
| **Transcript parser** | Session history capture (messages, tool calls, token usage) from a parseable on-disk format. JSONL is the base-class path; SQLite/DB/custom formats need a dedicated parser. | `sessions/transcripts/<cli>.py`, `PARSER_REGISTRY` in `sessions/transcripts/__init__.py` |
| **Web-chat backend** | Daemon-hosted streaming for the web UI. **Cheap path = ACP** (reuses shared `ACPWebChatBackend`). **Expensive path = custom subprocess backend** (Claude/Codex style). | `servers/websocket/chat/backends/<cli>.py` |

Supporting plumbing (needed for a complete integration, not a gate surface itself):
`SessionSource` enum value, `SOURCE_ALIASES` entry, `CLI_VALIDATION_CONFIGS` entry in
`install/shared/hooks/validate_settings.py`, ghook install/detection, context-window
resolution in `sessions/context_usage.py`.

## Scoring legend

| Tier | Meaning |
| --- | --- |
| **Ready** | All three surfaces confirmed present in the CLI for the modes Gobby will use. Gobby work is straightforward. |
| **Ready\*** | All three surfaces likely present, but one needs verification (e.g. transcript persistence, SQLite parser, or per-mode hook probe). |
| **Ready?** | ACP + hooks confirmed; transcripts unverified. One quick check from full readiness. |
| **Missing-X** | Two surfaces present; one explicitly absent (e.g. OpenCode missing hooks). |
| **Defer** | Surface is unstable / mid-pivot / EOL. Re-evaluate later. |
| **Skip** | Wrong architecture; would need custom work on all three surfaces. |
| **Limbo** | Gobby integrated two-of-three; the third is missing but the CLI exposes it, so Gobby can finish it with code. |
| **Blocked** | Gobby integrated one or more surfaces, but the rest can't be built because the CLI doesn't expose them — upstream-blocked. Re-evaluate when the CLI adds them. |
| **ACP-only** | ACP confirmed; hooks and transcripts both unverified. |

## The agy lesson (why this matrix exists)

`agy` is **Antigravity CLI**, Google's replacement for Gemini CLI. The original
1.0.11 probe found opaque protobuf conversation payloads and no ACP server, so the
integration was classified as hook-only. That versioned observation became stale.

The 1.1.18 contract probe verified all three surfaces. AGY fires five hooks
(`PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, and `Stop`), persists
parseable JSONL at `brain/<id>/.system_generated/logs/transcript_full.jsonl`, and
accepts a stable `--input-format stream-json` subprocess transport used by
`AgyWebChatBackend`. AGY still does not speak ACP as a server, but ACP was never a
readiness requirement: a proven custom transport satisfies the web-chat surface.

The lesson is to **probe all three surfaces on the current minimum supported version**
and record the exact invocation modes. Do not mistake an internal adapter base class
or the absence of ACP for the provider's complete transport story.

Probe template: [task-15038 AGY/Grok contract probe](../../.gobby/plans/task-15038-agy-grok-contract-probe.md).

## Master matrix

### Baseline: already Gobby-supported

| CLI | Hook adapter | Transcript parser | Web-chat backend | Status |
| --- | --- | --- | --- | --- |
| Claude Code | Full (12+ events, command + HTTP hooks, matchers, elicitation) | JSONL | Custom (`ClaudeWebChatBackend`) | **FULL** |
| Codex CLI | Full (8 events, permission requests) | JSONL | Custom (`CodexWebChatBackend`) | **FULL** |
| Grok CLI | Full (9 events, transport capabilities) | JSONL | ACP (`GrokWebChatBackend`) | **FULL** |
| Qwen Code | Full (ACP, 11 events, enableHooks gate) | JSONL | ACP (`QwenWebChatBackend`) | **FULL** |
| Factory Droid | Full | JSONL | Custom (`DroidWebChatBackend`) | **FULL** |
| **agy / Antigravity** | Full (5 events: PreInvocation, PreToolUse, PostToolUse, PostInvocation, Stop; 1.1.18 floor) | JSONL (`brain/<id>/.system_generated/logs/transcript_full.jsonl`) | Custom stream-json (`AgyWebChatBackend`) | **FULL** |

### Named candidates (full deep-dives below)

| CLI | Hooks | Transcripts | Web-chat / ACP | Maturity | Status |
| --- | --- | --- | --- | --- | --- |
| **GitHub Copilot CLI** | Full (13 events, Claude-compat + VS Code-compat, command + HTTP + prompt hooks) | Yes (`transcriptPath`) | ACP (public preview) | GA (2026-02) | **Ready** |
| **Goose** (AAIF, OSS, local-LLM) | Yes (Open Plugins: PreToolUse/PostToolUse/UserPromptSubmit/SessionStart) | SQLite (`sessions.db`; was JSONL pre-1.10) | ACP (GA) | Stable OSS | **Ready\*** (SQLite parser needed) |
| **Cursor CLI** (`agent`) | **Partial in CLI** (see [Cursor section](#cursor-cli-vs-cursor-ide)); full in IDE | Yes (`transcript_path` + `CURSOR_TRANSCRIPT_PATH`; verify on-disk format) | ACP (GA) | GA | **Ready\*** (per-mode hook probe required) |
| **Cline** (OSS) | Yes (PreToolUse, "shape AI decisions") | `--json` NDLM + `cline history` | ACP (GA) | Stable OSS | **Ready\*** (transcript persistence verify) |
| **Aider** | None | Markdown (`.aider.chat.md`) | No ACP | Stable OSS | **Skip** (wrong architecture) |
| **Windsurf / Devin Desktop** | Cascade EOL | Unclear | No ACP | Pivoting | **Defer** |

### ACP watchlist (full deep-dives below)

| Agent | ACP | Hooks | Transcripts | Status |
| --- | --- | --- | --- | --- |
| **Kiro CLI** (AWS) | Yes | Yes (lifecycle + tool execution) | Yes (automatic session save/resume, custom storage) | **Ready** |
| **OpenClaw** | Yes | Yes (agent/tool/message/session/Gateway lifecycle) | Yes (session store + transcripts + compaction) | **Ready** |
| **Augment / Auggie** | Yes | Yes (intercept + control tool execution) | Likely (cross-repo context) | **Ready?** (verify transcripts) |
| **OpenHands** | Yes | Yes (lifecycle: block commands, quality checks, inject context) | Likely (full platform runtime) | **Ready?** (heavy runtime) |
| **Hermes Agent** (Nous) | Yes | Yes (lifecycle, webhooks) | Unverified | **Ready?** (verify transcripts) |
| **OpenCode** (sst) | Yes | **Pending** (GitHub #12472, Claude Code hooks compat in progress) | Yes (Drizzle ORM DB) | **Missing-hooks** |

### Long-tail ACP agents (ACP confirmed; hooks + transcripts unverified)

Each of these is scored **ACP-only** until the verification protocol fills the
hooks and transcripts cells. They are listed because they appear on the official
ACP agent registry (`agentclientprotocol.com/get-started/agents`) and therefore
clear the web-chat surface by construction.

| Agent | Notes |
| --- | --- |
| AgentPool | ACP client listed in registry |
| AutoDev | `github.com/phodal/auto-dev` |
| Blackbox AI | Open-source CLI, parallel agents, headless/CI, MCP support |
| Bub | via `bub-acp-server` |
| Code Assistant | `github.com/stippi/code-assistant` |
| crow-cli | `crow-ai.dev` |
| Docker cagent | `github.com/docker/cagent` |
| fast-agent | `fast-agent.ai/acp` |
| fount | `github.com/steve02081504/fount` |
| Junie (JetBrains) | Beta, LLM-agnostic, terminal + IDE + CI/CD |
| Kimi CLI | `github.com/MoonshotAI/kimi-cli` |
| Minion Code | `github.com/femto/minion-code` |
| Mistral Vibe | `github.com/mistralai/mistral-vibe` |
| Pi | via `pi-acp` adapter |
| Poolside | `github.com/poolsideai/pool` |
| Qoder CLI | `docs.qoder.com/cli/acp` |
| siGit Code | `github.com/getsigit/sigit` |
| Stakpak | `github.com/stakpak/agent` |
| stdio Bus | `github.com/stdiobus/stdiobus` |
| VT Code | `github.com/vinhnx/vtcode` |

### EOL

Gemini CLI — sunset 2026-06-18, succeeded by **agy / Antigravity CLI**, which is
fully supported in Gobby on the 1.1.18+ contract.

## OSS local-LLM CLI candidates

If you are looking for the open-source, local-model-focused CLI you couldn't name,
these are the ranked matches:

| Rank | CLI | Fit | Status |
| --- | --- | --- | --- |
| 1 | **Goose** | Ollama routing, ACP GA, Open Plugins hooks, AAIF governance | **Ready\*** |
| 2 | **OpenCode** | Active TUI, ACP, Drizzle session DB; hooks not shipped yet | **Missing-hooks** |
| 3 | **Continue** | Claude-compatible hooks; repo maintenance/read-only post-2.0 | Not scored (re-evaluate if revived) |
| 4 | **ForgeCode** | Terminal ZSH harness; no shell hook contract | **Defer** |

## Named candidates — full deep-dives

### Cursor CLI vs Cursor IDE

Cursor is two products with different integration profiles. **Do not score them as one
row.** Gobby dispatch and web chat will use the CLI (`agent`); rule enforcement
depends on hooks firing in that same mode.

#### Cursor IDE (desktop app)

- **Hooks:** Full 21-event surface documented at
  [cursor.com/docs/hooks](https://cursor.com/docs/hooks). Agent hooks:
  `sessionStart`/`sessionEnd`, `preToolUse`/`postToolUse`/`postToolUseFailure`,
  `subagentStart`/`subagentStop`, `beforeShellExecution`/`afterShellExecution`,
  `beforeMCPExecution`/`afterMCPExecution`, `beforeReadFile`/`afterFileEdit`,
  `beforeSubmitPrompt`, `preCompact`, `stop`, `afterAgentResponse`/
  `afterAgentThought`. Tab hooks: `beforeTabFileRead`/`afterTabFileEdit`. App hook:
  `workspaceOpen`. Stdio JSON; decision control via `permission` allow/deny/ask +
  `updated_input`; context via `additional_context`; matchers; `failClosed` option;
  loads Claude third-party hooks. Config: `.cursor/hooks.json` (project),
  `~/.cursor/hooks.json` (user).
- **Transcripts:** `transcript_path` on hook input + `CURSOR_TRANSCRIPT_PATH` env
  var when enabled. On-disk format fragmented across `agent-transcripts/*.jsonl`,
  per-session SQLite, and IDE state DBs — parser needs format verification.
- **Web-chat:** IDE UI only; no daemon stdio without IDE automation.

#### Cursor CLI (`agent` binary)

- **ACP (GA):** `agent acp` — stdio JSON-RPC per
  [cursor.com/docs/cli/acp](https://cursor.com/docs/cli/acp). Lifecycle:
  `initialize` → `authenticate` (`cursor_login`) → `session/new` or `session/load`
  → `session/prompt` → `session/update` streaming → `session/request_permission`
  → optional `session/cancel`. Cursor extension methods:
  `cursor/ask_question`, `cursor/create_plan` (blocking); `cursor/update_todos`,
  `cursor/task`, `cursor/generate_image` (notifications). MCP via project/user
  `.cursor/mcp.json`. **This is the cheap web-chat path for Gobby** (reuse ACP stack).
- **Headless / CI:** `agent -p` with `--output-format stream-json` emits structured
  events (thinking deltas, assistant message, tool calls, token usage, result) to
  stdout — observability without hooks. Docs:
  [cursor.com/docs/cli/headless](https://cursor.com/docs/cli/headless).
- **Hooks in CLI: PARTIAL (critical).** Forum-confirmed by Cursor staff (Jan–Jun 2026);
  [thread](https://forum.cursor.com/t/cursor-cli-doesnt-send-all-events-defined-in-hooks/148316):

| Period | CLI hook status |
| --- | --- |
| Jan 2026 | Only `beforeShellExecution`, `afterShellExecution` |
| Apr 2026 | Added `afterFileEdit`, `postToolUse`, `stop`, `sessionStart` |
| Still missing in CLI | `afterAgentResponse`, `afterAgentThought`; full `preToolUse` parity vs IDE unconfirmed |
| `--print` mode | Hooks largely absent; use `stream-json` stdout for observability |
| Cloud agents | Tool-level hooks only (`preToolUse`, `postToolUse`, shell/file hooks); no lifecycle hooks (`sessionStart`, `stop`, `afterAgentResponse`) |

#### Per-surface score (Cursor)

| Surface | IDE | CLI (`agent`) |
| --- | --- | --- |
| Hooks | **Full** | **Partial** — rule enforcement at `preToolUse` may not fire in headless/print paths |
| Transcripts | Yes (verify format) | `transcript_path` on hooks that fire; `stream-json` alternative for observe-only |
| Web-chat | N/A (IDE UI) | **ACP GA** |

**Status: Ready\*** — not Ready. ACP clears web-chat; hooks need a **contract probe**
per invocation mode before integration.

**Gobby integration path (when pursued):**

1. ACP backend first (`agent acp`) — reuse `ACPWebChatBackend` pattern from Grok/Qwen
2. Hook adapter second — map Cursor stdio JSON; document which events fire per mode
3. Transcript parser third — verify on-disk format from `transcript_path`; optional
   `stream-json` parser for headless observe-only paths

**Risk if rushed:** Ship hook install for IDE events while dispatch runs `agent -p` —
rules never fire, the same class of version-and-mode error exposed by AGY's obsolete
1.0.11 assessment.

### GitHub Copilot CLI

- **Hooks:** 13 events. `sessionStart`/`sessionEnd`, `userPromptSubmitted`,
  `preToolUse`/`postToolUse`/`postToolUseFailure`, `permissionRequest`, `preCompact`,
  `agentStop`, `subagentStart`/`subagentStop`, `notification`, `errorOccurred`.
  Two payload formats: **camelCase** (native) and **PascalCase / VS Code-compat**
  (snake_case fields). The PascalCase `PreToolUse`/`PermissionRequest` apply Claude
  matcher semantics and report Claude tool names — effectively Claude-compatible.
  Supports **command**, **HTTP**, and **prompt** hooks. `preToolUse` is
  **fail-closed**. `permissionDecision` allow/deny/ask + `modifiedArgs`.
  `postToolUse` can `modifiedResult` + `additionalContext`. Config sources:
  policy (`/etc/github-copilot/policy.d/*.json`), `.github/hooks/*.json`,
  `~/.copilot/hooks/`, inline `hooks` in `.github/copilot/settings.json` (and
  cross-tool `.claude/settings.json` is read), user `~/.copilot/settings.json`,
  plugin-contributed. `disableAllHooks` toggle.
- **Transcripts:** `transcriptPath` on `agentStop`/`preCompact`/`subagentStart`/
  `subagentStop` payloads.
- **Web-chat:** ACP in **public preview** (`github.blog/changelog/2026-01-28...`).
- **Install/detection:** `~/.copilot/` and `.github/copilot/`. Reads `.claude/`
  settings too.
- **Maturity:** GA (2026-02-25). Backed by GitHub; cloud agent + CLI surfaces.
- **Gobby effort:** ACP reuse once preview stabilizes. Hook adapter is nearly a
  Claude clone (PascalCase path) — likely the cheapest adapter to write. **P1
  candidate; lowest hook-adapter effort.**

### Goose (Agentic AI Foundation, formerly Block)

- **Hooks:** Open Plugins spec (added 2026-05-14). `PreToolUse`, `PostToolUse`,
  `UserPromptSubmit`, `SessionStart`, and more. Shell-script wiring.
- **Transcripts:** Sessions in **SQLite** at `~/.local/share/goose/sessions/
  sessions.db` since v1.10.0 (was individual `.jsonl` files before; legacy files
  remain on disk but are no longer managed). DB stores session metadata,
  conversation messages (role info), tool calls + results (IDs, args, responses,
  success/failure), token usage, extension data. Session IDs are
  `YYYYMMDD_<COUNT>`. `goose session list` enumerates them.
- **Web-chat:** ACP GA (`block.github.io/goose/docs/guides/acp-clients`).
- **Install/detection:** config dir `~/.config/goose/`. CLI + desktop app share the
  same session store.
- **Maturity:** Apache-2.0, now under the Agentic AI Foundation. Local-LLM via
  Ollama. Active.
- **Gobby effort:** ACP reuse for web-chat. Hook adapter maps Open Plugins events.
  **Transcript parser is the custom work** — SQLite, not the JSONL base class; needs
  a dedicated parser reading `sessions.db`. **P2 OSS local-LLM candidate.**

### Cline

- **Hooks:** `docs.cline.bot/features/hooks` — inject custom logic to validate
  operations, monitor tool usage, shape AI decisions. `PreToolUse` validates before
  execution (e.g. block creating `.js` files in a TS project). Resume maintains hook
  state across interruptions.
- **Transcripts:** `--json` emits newline-delimited JSON message objects
  (`{type, text, ts, say/ask, reasoning, partial}`). `cline history` shows/manages
  task history. Persistent transcript file format needs verification.
- **Web-chat:** ACP GA (listed on ACP registry).
- **Install/detection:** `npm i -g cline`. Config dir, `--data-dir` for isolated
  state. `--auto-approve` for unattended runs. Headless via `--json` / piped stdin /
  redirected stdout.
- **Maturity:** Apache-2.0, 8M+ users. CLI is newer than the VS Code extension.
- **Gobby effort:** ACP reuse. Hook adapter needs full event-coverage verification
  (PreToolUse confirmed; confirm session/stop/compact). Transcript parser needs the
  persistent format confirmed (NDLM stream vs on-disk file). **Open-source; verify
  transcript persistence before claiming full support.**

### Aider

- **Hooks:** **None.** No lifecycle hook system, no tool-interception plugin
  surface. Aider is a git-integrated pair programmer, not a tool-use agent loop.
- **Transcripts:** Chat history in `.aider.chat.md` (markdown), plus a git
  commit-based edit log. Not structured JSONL.
- **Web-chat:** **No ACP.** Would need a fully custom backend.
- **Maturity:** Active, open source. Stable.
- **Gobby effort:** Custom work required on **all three** surfaces, and the hook
  surface may not be buildable at all (Aider has no tool-call lifecycle to
  intercept). **Skip** — wrong architecture. Gobby's value proposition
  (hook enforcement, transcript capture, web-chat streaming) does not map onto
  Aider's edit-commit model.

### Windsurf / Devin Desktop

- **Hooks:** "Cascade Hooks" existed (pre/post hooks for logging, security,
  validation, governance). **Cascade is EOL** — Cognition rebranded Windsurf to
  Devin Desktop (2026-06 OTA update); Cascade was replaced by a Rust-based
  "Devin Local" with sub-agents.
- **Transcripts:** Unclear post-pivot.
- **Web-chat:** **Not on the ACP registry.** No confirmed ACP server.
- **Maturity:** Mid-pivot. Surface is unstable.
- **Gobby effort:** **Defer** until Devin Local stabilizes and publishes its hooks,
  transcript, and protocol story. Investing now risks rebuilding against a moving
  target.

## ACP watchlist — full deep-dives

### Kiro CLI (AWS)

- **Hooks:** `kiro.dev/docs/cli/hooks/` — custom commands at agent-lifecycle and
  tool-execution points. IDE hooks too (`kiro.dev/docs/hooks/`).
- **Transcripts:** `kiro.dev/docs/cli/chat/session-management/` — automatic session
  saving, resumption, and custom storage integration.
- **Web-chat:** ACP (`kiro.dev/docs/cli/acp/`).
- **Maturity:** AWS-backed, spec-driven (EARS specs), Bedrock routing. Active CLI
  changelog.
- **Gobby effort:** All three surfaces confirmed. ACP reuse. **Fully ready —
  strongest watchlist candidate.**

### OpenClaw

- **Hooks:** `docs.openclaw.ai/automation/hooks` (event-driven automation for
  commands + lifecycle) and `docs.openclaw.ai/plugins/hooks` (intercept agent,
  tool, message, session, and Gateway lifecycle events).
- **Transcripts:** `docs.openclaw.ai/reference/session-management-compaction` —
  session store + transcripts, lifecycle, and (auto)compaction internals.
- **Web-chat:** ACP (`docs.openclaw.ai/cli/acp` — ACP bridge over stdio).
- **Maturity:** Active, CLI cheatsheet maintained.
- **Gobby effort:** All three surfaces confirmed. **Fully ready.**

### Augment Code / Auggie CLI

- **Hooks:** `docs.augmentcode.com/cli/hooks` — intercept and control tool
  execution with custom scripts.
- **Transcripts:** Likely (Augment's core differentiator is cross-repo codebase
  context; session/history persistence expected but **unverified**).
- **Web-chat:** ACP (`docs.augmentcode.com/cli/acp`).
- **Maturity:** Commercial, cross-repo context, CI/CD automation focus.
- **Gobby effort:** ACP + hooks confirmed; one transcript check from full readiness.

### OpenHands

- **Hooks:** `docs.openhands.dev/openhands/usage/customization/hooks` — lifecycle
  hooks to block dangerous commands, enforce quality checks before stopping, inject
  context.
- **Transcripts:** Likely (full platform runtime with event/session streaming) but
  **unverified**.
- **Web-chat:** ACP (`docs.openhands.dev/sdk/guides/agent-acp`). Note: OpenHands can
  also act as an ACP *client* delegating to other agents — confirm it serves ACP for
  Gobby's daemon-hosted direction.
- **Maturity:** Open source, AI-driven development platform. Heavier than a CLI.
- **Gobby effort:** ACP + hooks confirmed. It is a platform runtime rather than a
  lean CLI — heavier to host. Verify transcript format + ACP-server direction.

### Hermes Agent (Nous Research)

- **Hooks:** `hermes-agent.nousresearch.com/docs/user-guide/features/hooks` — run
  custom code at lifecycle points; log activity, send alerts, post to webhooks.
- **Transcripts:** **Unverified.**
- **Web-chat:** ACP (listed on registry + features page).
- **Maturity:** Open source, model-agnostic, self-hosted.
- **Gobby effort:** ACP + hooks confirmed; one transcript check from readiness.

### OpenCode (sst)

- **Hooks:** **Pending.** GitHub issue `anomalyco/opencode#12472` tracks native
  Claude Code hooks compatibility (PreToolUse/PostToolUse). OpenCode already has
  Claude Code compat for rules (`CLAUDE.md`), skills (`~/.claude/skills/`), and
  disable env vars — but **not** lifecycle hooks yet.
- **Transcripts:** Yes — session management with Drizzle ORM database storage;
  parent/child session hierarchy; CRUD operations (`deepwiki.com/sst/opencode`).
- **Web-chat:** ACP (`opencode.ai`).
- **Maturity:** Open source, TUI-first, active.
- **Gobby effort:** Blocked on hooks. Watch issue #12472; re-score when hooks
  ship. **Missing-hooks tier today.**

## Verification protocol

Run this protocol **before** any integration task. Produce a contract probe document
(see [task-15038 probe template](../../.gobby/plans/task-15038-agy-grok-contract-probe.md))
with fixtures scrubbed of secrets.

### 1. Hooks (per invocation mode)

Test each mode Gobby will use independently:

| Mode | When to test | Minimum events |
| --- | --- | --- |
| IDE / desktop | User runs agent in GUI | `sessionStart`, `preToolUse` (allow/deny), `stop` |
| CLI interactive | `agent` TTY session | Same minimum set |
| CLI ACP | `agent acp` stdio | N/A for shell hooks — ACP handles tool approval via `session/request_permission` |
| CLI headless | `agent -p` / `--print` | Confirm whether hooks fire; if not, document `stream-json` fallback |
| Cloud agents | Remote execution | Separate matrix — often subset of local hooks |

For each mode, record:

- Config path (e.g. `.cursor/hooks.json`, `~/.copilot/hooks/`)
- Event names that actually fire (not just documented)
- Decision control: allow/deny/modify/ask; fail-open vs fail-closed
- Context injection channel (`additional_context`, `systemMessage`, etc.)
- Whether config is Claude-compatible (cheapest adapter path)

### 2. Transcripts

- Identify on-disk format: JSONL / SQLite / NDLM / markdown / binary
- Discover path: hook payload field, env var, or CLI command (`session list`)
- Confirm fields: messages, tool calls, tool results, token usage, session ID
- JSONL → base parser; anything else → dedicated parser subclass
- If the current supported version exposes only a binary or undocumented format,
  score **None** — blocked

### 3. Web-chat transport

- **ACP path (preferred):** Smoke test `initialize` → `authenticate` → `session/new`
  (absolute `cwd` required for some agents) → `session/prompt` → observe
  `session/update` → respond to `session/request_permission` → `session/cancel`
- Record extension methods the client must implement (e.g. Grok `terminal/create`,
  Cursor `cursor/ask_question`, `cursor/create_plan`)
- **Non-ACP path:** Budget custom subprocess backend (Claude/Codex class)
- Confirm MCP config install path for Gobby proxy injection

### 4. Promotion criteria

| From | To | Requirement |
| --- | --- | --- |
| **Ready?** / **Ready\*** | **Ready** | All three surfaces confirmed for target modes; probe doc merged |
| **ACP-only** | **Ready?** | Hooks + transcripts verified |
| **Missing-hooks** | **Ready\*** | Upstream ships hook surface; re-run protocol |
| **Blocked** | **Ready\*** | Upstream adds a missing surface or a new version exposes a stable custom transport |
| Any | **Supported in Gobby** | Implementation complete + [adapter fidelity](../guides/adapter-fidelity.md) declared |

A CLI may be scored **Ready** only after all three cells are confirmed for the modes
Gobby will use. **Ready\*** and **Ready?** mean one cell is likely-but-unverified and
must be checked before integration work begins. **ACP-only** means two cells are
unverified.

## Source notes

- ACP agent registry: `agentclientprotocol.com/get-started/agents` (35 agents,
  accessed 2026-06-27).
- Gemini CLI → Antigravity transition: Google blog
  `developers.googleblog.com/an-important-update-transitioning-gemini-cli...`
  (2026-05-19); sunset 2026-06-18.
- Copilot CLI hooks: `docs.github.com/en/copilot/reference/hooks-reference` and
  `docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference`.
- Cursor hooks: `cursor.com/docs/hooks`; Cursor ACP: `cursor.com/docs/cli/acp`;
  Cursor headless: `cursor.com/docs/cli/headless`; CLI hook parity forum thread:
  `forum.cursor.com/t/cursor-cli-doesnt-send-all-events-defined-in-hooks/148316`.
- Goose hooks: `goose-docs.ai/blog/2026/05/14/goose-hooks/`; Goose logs:
  `block.github.io/goose/docs/guides/logs/`.
- Cline CLI: `docs.cline.bot/usage/cli-overview`; Cline hooks:
  `docs.cline.bot/features/hooks`.
- Kiro CLI hooks: `kiro.dev/docs/cli/hooks/`; Kiro session management:
  `kiro.dev/docs/cli/chat/session-management/`.
- OpenClaw hooks: `docs.openclaw.ai/automation/hooks`,
  `docs.openclaw.ai/plugins/hooks`; session management:
  `docs.openclaw.ai/reference/session-management-compaction`.
- OpenCode hooks pending: `github.com/anomalyco/opencode/issues/12472`.
- Windsurf → Devin Desktop pivot: `devin.ai/blog/windsurf-2-0/` and
  `webdeveloper.com/news/windsurf-devin-desktop-cascade-eol/`.
- Gobby adapter fidelity: [docs/guides/adapter-fidelity.md](../guides/adapter-fidelity.md).
- Gobby probe template: [.gobby/plans/task-15038-agy-grok-contract-probe.md](../../.gobby/plans/task-15038-agy-grok-contract-probe.md).

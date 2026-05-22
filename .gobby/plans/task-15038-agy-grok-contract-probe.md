# Task #15038 AGY/Grok Contract Probe

Probe date: 2026-05-22

Local versions:

| Provider | Command | Version |
| --- | --- | --- |
| AGY | `~/.local/bin/agy` | `1.0.1` |
| Grok | `~/.grok/bin/grok` | `0.1.216 (b139744655)` |

## Final Verdict Matrix

| Area | AGY verdict | Grok verdict |
| --- | --- | --- |
| Machine transport | Not ready. `~/.gemini/antigravity-cli/bin/agentapi` exists but is only `exec "<HOME>/.local/bin/agy" agentapi "$@"`; `agentapi --help`, `agentapi send --help`, `agentapi resume --help`, and `agentapi stream --help` all returned top-level `agy` help. Pipe and PTY probes entered agent execution and timed out. | Ready. `grok agent --no-leader --always-approve stdio` speaks ACP/JSON-RPC over stdio. `initialize`, `authenticate`, `session/new`, `session/load`, and `session/prompt` were observed. |
| Send/resume/stream behavior | Fresh `agy --print "prompt"` works. `--conversation <id>` timed out. `--continue` resumed unrelated prior state and started exploratory tool planning. Do not use as live web-chat transport yet. | `session/new` requires an absolute `cwd`; `"."` returns JSON-RPC `-32602` with `Path is not absolute: .`. `session/load` accepts existing session id plus absolute `cwd`. Assistant text and thoughts stream as `session/update`. |
| Tool execution | No stable machine protocol confirmed. `agentapi send/resume/stream` did not expose a documented request/response contract in 1.0.1. | ACP emits `tool_call`, `tool_call_update`, and xAI extension notifications. In stdio ACP mode, Grok may send client-side requests such as `{"id":0,"method":"terminal/create"}`; a web-chat client must implement/respond to those extension requests or the prompt can hang. Headless `grok -p --always-approve` executes terminal tools internally. |
| Hooks | Shape is not live-proven for AGY in this probe. AGY docs/marketing expose JSON Hooks, and local Gemini-compatible hook evidence uses `hook_event_name`, `session_id`, `transcript_path`, `cwd`, `tool_name`, and `tool_input`. Keep AGY hook support behind provider-specific validation. | Live-proven. Global `~/.grok/hooks/gobby-contract-probe-15038.json` captured `session_start`, `user_prompt_submit`, `pre_tool_use`, `post_tool_use`, and `stop`. `false` exits with `PostToolUse` and `toolResult.status="completed"` rather than `PostToolUseFailure`; `SessionEnd` was not observed in headless runs. |
| Transcript paths | Binary conversation payloads are under `~/.gemini/antigravity-cli/conversations/<conversation-id>.pb`. `~/.gemini/antigravity-cli/cache/last_conversations.json` maps canonical workspace path to conversation id. Project metadata is under `~/.gemini/config/projects/<project-id>.json`. Do not commit `.pb` payloads. | Persistent session root is `~/.grok/sessions/<url-encoded-cwd>/<session-id>/`, containing `summary.json`, `updates.jsonl`, and `chat_history.jsonl`. Terminal output files are nested under `terminal/<tool-call-id>.log`. |
| Model source | Runtime logs showed selected model label `Gemini 3.5 Flash (High)` after silent keyring auth. No stable JSON model cache was found under `~/.gemini/antigravity-cli` beyond logs/settings. | `grok models` returned default `grok-build`; `~/.grok/models_cache.json` has keys `fetched_at`, `grok_version`, `auth_method`, `etag`, and object `models` with `grok-build`. ACP initialize includes `modelState.currentModelId="grok-build"` and total context metadata. |
| Chrome DevTools MCP | Shared dependency, not provider-specific. | Configured `chrome-devtools` MCP exposes navigation, snapshots, screenshots, console messages, network request inspection, and websocket-filterable network listings. Ready for downstream UI verification. |

## Probe Notes

AGY command inventory:

- `agy --help` lists `--continue`, `--conversation`, `--log-file`, `--print`, `--prompt-interactive`, `--sandbox`, and plugin/update/install subcommands.
- `agy changelog` for 1.0.1 notes OAuth persistence fixes, `proceed-in-sandbox`, consumer/free-tier onboarding, plugin discovery for skills/agents, and CLI rendering fixes.
- `~/.gemini/antigravity-cli/settings.json` keys: `colorScheme`, `enableTelemetry`, `trustedWorkspaces`.
- The `agentapi` wrapper is present and executable, but 1.0.1 does not expose usable `send`, `resume`, or `stream` help/contract through it.

AGY transcript evidence:

- Fresh print prompt created conversation `c27f2817-0ce1-4b68-b246-0afe6a8160ce` for `/private/tmp/gobby-contract-probe-15038`.
- `~/.gemini/antigravity-cli/cache/last_conversations.json` mapped `/private/tmp/gobby-contract-probe-15038` to that conversation id.
- Binary payload path observed: `~/.gemini/antigravity-cli/conversations/c27f2817-0ce1-4b68-b246-0afe6a8160ce.pb`.
- Log path observed: `~/.gemini/antigravity-cli/log/cli-20260522_171625.log`.

Grok command inventory:

- `grok version`: `grok 0.1.216 (b139744655)`.
- `grok agent --help` exposes `stdio`, `headless`, `serve`, and `leader`; `--no-leader` and `--always-approve` are supported agent options.
- `grok models` returned `Default model: grok-build` and `Available models: grok-build`.
- `grok inspect --json` reports hooks, skills, plugins, MCP servers, config sources, and model/config metadata. Raw output includes private paths and must stay scrubbed.

Grok ACP evidence:

- `initialize` returns `agentCapabilities.loadSession=true`, prompt capability metadata, MCP capability metadata, `authMethods` including `cached_token`, and `_meta.modelState`.
- `authenticate` with `{"methodId":"cached_token"}` succeeds using local OIDC state; email and subscription metadata were scrubbed from fixtures.
- `session/new` with absolute `cwd` returns `sessionId`, model list, and `_meta.currentWorkingDirectory`.
- `session/load` returns model metadata and `_meta.sessionId`.
- Prompt streams include `agent_thought_chunk`, `agent_message_chunk`, `tool_call`, `tool_call_update`, and `_x.ai/session_notification`.
- Client-side terminal execution in ACP requires handling Grok's extension request `terminal/create`; otherwise `session/prompt` can wait indefinitely.

## Chrome DevTools MCP Summary

`chrome-devtools` tool inventory contained 29 tools. Relevant downstream groups:

- Page control: `new_page`, `navigate_page`, `select_page`, `close_page`, `resize_page`, `wait_for`.
- DOM/UI interaction: `take_snapshot`, `click`, `fill`, `fill_form`, `type_text`, `press_key`, `hover`, `drag`, `upload_file`.
- Visual capture: `take_screenshot`.
- Diagnostics: `list_console_messages`, `get_console_message`, `list_network_requests`, `get_network_request`.
- Network filters include resource type `websocket`, plus `xhr`, `fetch`, `eventsource`, `document`, `script`, and related types.
- Performance and quality: `performance_start_trace`, `performance_stop_trace`, `performance_analyze_insight`, `lighthouse_audit`, `take_memory_snapshot`.

## Fixture Map

- `tests/fixtures/acp_contract/grok-0.1.216-session-new-prompt.stdout.jsonl`
- `tests/fixtures/acp_contract/grok-0.1.216-session-load-tool-prompt.stdout.jsonl`
- `tests/fixtures/provider_contracts/agy/`
- `tests/fixtures/provider_contracts/grok/`
- `tests/adapters/test_provider_contract_fixtures.py`

## Source Checks

- Local CLI/help/log/cache sources listed above are the source of truth for implementation.
- xAI Headless and Scripting docs confirm `grok -p`, JSON/streaming output, ACP over `grok agent stdio`, local auth or `XAI_API_KEY`, `initialize`, `authenticate`, `session/new`, and `session/prompt`: `https://docs.x.ai/build/cli/headless-scripting`.
- Antigravity public docs are SPA-rendered at `https://www.antigravity.google/docs/hooks`, `https://antigravity.google/docs/cli-using`, and `https://antigravity.google/docs/cli-features`; local CLI behavior above takes precedence for 1.0.1.

## Downstream Guidance

- Implement Grok first. Its ACP contract is concrete enough for #15033/#15037.
- Treat Grok ACP extension requests as bidirectional JSON-RPC. A browser-backed chat client must either implement the xAI terminal/filesystem extension methods or constrain prompts/tools so those requests are not emitted.
- Do not wire AGY live web-chat against `agentapi` in 1.0.1. Use AGY `--print` only for disposable one-shot checks until a stable machine contract appears.
- Do not parse or commit AGY `.pb` conversation payloads. Record IDs and manifests only.

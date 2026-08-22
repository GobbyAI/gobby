# AGY Provider Contract Captures (Gate 0, plan `agy-full-integration` §1.1)

Captured 2026-08-22 by task #19563 in **both** print mode (`agy -p … --output-format
stream-json`) and interactive terminal mode (raw tmux, plan §1.1 mechanics).

**Version.** The run started on the installed AGY **1.1.16** (the two `/hooks`
captures at 03:17–03:18 local) and AGY's auto-updater replaced `~/.local/bin/agy`
with **1.1.18** at 03:18:34, before the first live turn. Every live record below
was therefore observed on **1.1.18**; `agy --version` and every `cli-*.log`
(`Language server version: 1.1.18`) prove it. There is no pin or downgrade path, so
the probed floor is 1.1.18 (plan rule: the floor is the version the contracts were
probed against; pre-approval condition 5 requires the then-current release).

**Probe environment.** The agent session ran under an SRT sandbox whose proxy
forbids Google domains, so every AGY invocation was executed in an outside tmux
pane (`tmux new-session … ; tmux send-keys …`), which is also the plan's
terminal-mode mechanism. After the Gemini weekly bucket hit 0 % mid-run
(`Individual quota reached`), remaining turns used `--model gpt-oss-120b-medium`
(separate, untouched bucket); records note the model where it matters.

## Files

| File | Records | Content |
| --- | --- | --- |
| `hook-payloads.jsonl` | 1.1.3, 1.1.5, 1.1.17, 1.1.24 | 27 live camelCase payloads with `mode` (`print` / `interactive`), hook cwd, env, the capture hook's answer and exit code |
| `transcript-manifest.json` | 1.1.2, 1.1.10, 1.1.22 | transcript layout, literal `transcriptPath`, record census, zero/nonzero-exit shell records, truncation evidence |
| `stream-json-samples.jsonl` | 1.1.1, 1.1.6, 1.1.8, 1.1.13, 1.1.18, 1.1.20 | scrubbed NDJSON records (init, resumed turn, text_delta, tool ACTIVE/DONE/ERROR, failure results, stream-input errors, synthetic malformed line) |
| `command-captures.json` | 1.1.7, 1.1.13, 1.1.15, 1.1.19, 1.1.20, 1.1.21 | `/hooks` before/with/after the capture hook, `/usage` `/quota` `/credits` `/model`, `models`/`agents` JSON, `mcp list`, flag-syntax errors, auth probes |

Deleted: `agy_models_v1.0.10.txt`, `model-cache-summary.json` (superseded by
`command-captures.json`).

## Capture procedure

1. `agy -p "/hooks" --output-format json` (before). Install `gate0-capture` beside
   `gobby` in `~/.gemini/config/hooks.json`: five events, `timeout` 45, command
   `gate0-capture.sh <Event>` writing stdin verbatim plus `PWD` and
   `ANTIGRAVITY_CONVERSATION_ID` to `<scratch>/hook-captures/NNNN-<mode>-<event>.json`
   and answering `{"decision":"allow"}` (PreToolUse) / `{}` (others), with per-event
   response/exit/stderr overrides for 1.1.24. `/hooks` again (both hooks listed).
2. Print-mode turns in a throwaway workspace: built-in (`list the files in this
   directory`), shell (`run: ls -la`), MCP (`call the gobby list_mcp_servers tool …`),
   plus the targeted probes per record below. 566 hook invocations captured.
3. Interactive: `tmux new-session -d -s agy-gate0 -x 200 -y 50 -c <ws> "agy
   --sandbox=false --dangerously-skip-permissions --model gpt-oss-120b-medium
   --add-dir <ws>"`; prompt glyph `>` on its own line between two horizontal rules,
   status line `? for shortcuts … <model label>`; the same three prompts, `shift+tab`,
   `/plan`, `ctrl+r`, `C-c`; a second session without the two flags for the native
   permission prompt. `tmux kill-session` and orphan check at the end.
4. Remove `gate0-capture`; `/hooks` after shows only `gobby` (5 actions);
   `hooks.json` is byte-equivalent to the pre-probe backup.
5. Scrub: `$HOME`→`~`, conversation ids→`<CONVERSATION_ID>`, workspace→`<WORKSPACE>`,
   probe scratch→`<PROBE_SCRATCH>`, app-data paths other than `brain/<id>/…`→
   `<AGY_APP_DATA>/…`, the local user name→`<USER>`, emails/tokens→`<REDACTED>`,
   tool output >4 KiB truncated with `<TRUNCATED n bytes>`.

## Contract-outcome table (1.1.11)

Outcomes: **confirmed** (open record answered as the plan assumed), **re-confirmed
unchanged** (1.1.10 record identical on 1.1.18), **disproven** (observed behaviour
contradicts plan text; the affected sections were revised), **negative** (the
capability is absent; recorded as a negative contract).

| Record | Outcome | Command / observed output |
| --- | --- | --- |
| 1.1.1 resume | **re-confirmed unchanged** (+1 delta) | `agy -p '…' --output-format stream-json --conversation <id>`: same `conversation_id`, `num_turns` 1→2, prior turn recalled; a `system_message` step precedes the reply. Resume after SIGINT/SIGTERM also works. Delta: `result.duration_seconds` on a resumed turn is measured from conversation creation (213 s for a 5 s wall-clock turn), so it is not per-turn latency. |
| 1.1.2 transcriptPath | **re-confirmed, layout disproven** | Literal value in both modes: `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl` — the `_full` file, not `transcript.jsonl`. Arrives on the first `PreInvocation` (`invocationNum` 0). No workspace-local file is ever created. |
| 1.1.3 cwd remedy | **confirmed, remedy named** | From an unregistered cwd, `init.cwd` is the cwd but `workspacePaths` is `[]`, the conversation binds to `default-cli-project` (`<AGY_APP_DATA>/scratch`), `run_command` carries no `Cwd`, and the model lists `~/.gemini/antigravity-cli`. Remedy: pass **`--add-dir <cwd>`** on every launch (stateless; `workspacePaths=[<cwd>]`, `Cwd` injected). `--new-project` also works but writes a new `~/.gemini/config/projects/<uuid>.json` per launch and does not auto-bind later launches; `--project <id>` needs that id. |
| 1.1.4 image input | **negative with a model-driven path** | `--help` has no image flag; `@img.png …` is plain text (model ran `find_by_name`); stream-input `{"type":"image"}` content blocks are rejected (`only "text"`). The model's own `view_file` on a PNG delivers the image (transcript record "entire, complete content", model named the colour without decoding). So: no input attachment from Gobby; vision only via a prompt that names a file path. `VISION_EXTRACT` stays unavailable as a binding. |
| 1.1.5 payloads | **confirmed (hook→daemon); session row disproven for now** | All five events captured live in camelCase in both modes for `list_dir`, `run_command`, `call_mcp_tool` (`hook-payloads.jsonl`). Keys: common `conversationId, workspacePaths, transcriptPath, artifactDirectoryPath, modelName`; Pre/PostInvocation `invocationNum, initialNumSteps`; Pre/PostToolUse `stepIdx, toolCall{name,args}` (+ `error` on PostToolUse); Stop `executionNum, terminationReason, error, fullyIdle`. Hook cwd is `~/.gemini/config`; env carries `ANTIGRAVITY_CONVERSATION_ID`. The same turns reached the daemon through the installed `gobby` hook (`hooks.log` 03:21:47 `Failed to broadcast event HookEventType.BEFORE_TOOL … tool_name Field required`), but **no `source=agy` line and no AGY session row can exist before §4.1 lands**: `source=agy` is only logged by `_log_session_start_lifecycle`, and today's adapter reads `session_id`/`cwd`/`tool_name`, none of which AGY sends. `list_sessions(source="agy")` → 0. |
| 1.1.6 stream-json | **re-confirmed unchanged** (+2 deltas) | Nested `{"event":…, "<event>":{…}}` shape unchanged; `init.tools` has 57 entries (was 56). `step_type` vocabulary observed: `user_input, checkpoint, agent_response, tool, system_message, error_message, unknown`. Deltas: a >64 KiB tool output cannot be captured — AGY caps tool output at ~8 KiB with `<truncated N bytes>`; `result.status` adds `CANCELED` (headless auto-deny). |
| 1.1.7 sandbox flags | **re-confirmed unchanged** | `--sandbox` boolean (`--sandbox=bogus` → exit 2 `strconv.ParseBool`); `--sandbox=false` accepted in both modes; `--dangerously-skip-permissions` → `init.permission_mode: always-proceed`, otherwise `request-review`. Without it, headless tools are auto-denied (`status CANCELED`, exit 0, stderr `jetski: no output produced …`) and a hook `decision: allow` does not override that. Interactive without it shows the native 4-option prompt (1.1.14). |
| 1.1.8 cancellation | **confirmed** | Print: SIGINT and SIGTERM both exit 1 immediately with a final `result{status:ERROR,error:"timeout waiting for response"}` — byte-identical to timeout expiry — leaving the tool step `ACTIVE`; the shell child (`zsh` → `sleep 40`) keeps running to completion; the MCP child dies with agy; the conversation resumes afterwards. SIGINT sent before the turn's first model call is ignored and the turn completes. Terminal: `C-c` interrupts the turn ("Interrupted · What should Antigravity CLI do instead?"), fires no `Stop`/`PostToolUse`, the backgrounded command keeps running; a second `C-c` at idle arms exit ("press ctrl+c again to exit"), the third exits with no `Stop` hook. `esc` mid-turn interrupts the same way. |
| 1.1.9 network/roots | **confirmed** | Domains: `daily-cloudcode-pa.googleapis.com` (model API), `oauth2.googleapis.com` + `accounts.google.com` (auth), `play.googleapis.com` (telemetry), `playwright{,-akamai,-verizon}.azureedge.net` (browser-driver download attempt), a `googleusercontent.com` host. Reads/writes under `~/.gemini/antigravity-cli/`: `brain/<id>/…`, `conversations/<id>.db(-wal,-shm)`, `conversation_summaries.db`, `cache/last_conversations.json`, `cache/onboarding.json`, `log/cli-*.log`, `crashes/crash_*.log`, `presence/<id>.lock`, `knowledge/knowledge.lock`, `mcp/<server>/*.json`, `bin/agentapi` (rewritten on every launch), `installation_id`, `jetski_state.pbtxt`; plus `~/.gemini/config/projects/*.json` (read), `~/Library/Caches/ms-playwright-go/`, and the login Keychain. |
| 1.1.10 RUN_COMMAND | **disproven** | Zero-exit `run: ls -la` and nonzero-exit `sh -c 'echo boom >&2; exit 7'` both produce `source: MODEL, type: GENERIC` with free-text `content` (`The command exited with code 7.\nOutput:\nboom`). No `RUN_COMMAND` record and no structured `exit_code` exist on 1.1.18 (last seen 2026-08-03 on 1.1.10). The stream shows the exit-7 step as `DONE` with `output: "boom\n"`, and `PostToolUse.error` is `""`. |
| 1.1.11 outcome table | **confirmed** | This table. |
| 1.1.12 controlled-tool bridge | **confirmed (supported)** | Transport: `PreToolUse` hook `decision: "deny"` + `reason`. Denied tool → stream `tool ERROR {type: TOOL_ERROR, message: "tool call denied by pre-tool hook: <reason>"}`, no `PostToolUse`, model sees the reason, `result.status ERROR` carrying the message, exit 0. MCP tools surface as the built-in `call_mcp_tool{ServerName,ToolName,Arguments}`, so the set is bounded by matching `toolCall.name` + `args.ServerName/ToolName`. `agy mcp list` shows the `gobby` stdio server. |
| 1.1.13 `--print-timeout` | **re-confirmed unchanged** (+1 delta) | Go duration syntax (`banana` → exit 2), default `5m0s`, no disable sentinel (`0` expires immediately); `2562047h` (the effectively-unbounded form) accepted, turn completes normally. Expiry: exit 1; text mode writes `Error: timeout waiting for response` to stderr; **delta:** under `--output-format json|stream-json` the payload is a stdout `result{status:ERROR,error:"timeout waiting for response"}`. Mid-stream expiry leaves the tool `ACTIVE` and its shell child running. Under `--input-format stream-json` the clock is per turn. |
| 1.1.14 terminal plan menu | **confirmed (menu exists, keystrokes recorded)** | `shift+tab` cycles default → `accept-edits` → `plan` (status-line label). `/plan …` or plan mode writes the plan as an artifact (`brain/<id>/<name>.md`), no inline approve/reject; `ctrl+r` or `/artifact` opens "Action required" with `↑/↓`, `y`/`n` approve/reject, `shift+a` approve all, `p` preview, `ctrl+g` editor, `esc` done; approving submits `[Approved] <artifact>` as a user turn while staying in plan mode. Native permission prompt (no skip flag): `1` Yes, `2` always-allow this conversation, `3` persist to settings.json, `4` No, `esc` cancel, `tab` amend; `Enter` selects the highlighted option. |
| 1.1.15 auth footprint | **confirmed** | Credential = macOS login Keychain item (`svce=gemini`, `acct=antigravity`) gated on state under the real `~/.gemini/antigravity-cli/`; `env -i HOME=$HOME PATH=…` authenticates (`ChainedAuth: authenticated via keyring`); ambient `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS` are ignored; a foreign `HOME` fails silent auth and prompts OAuth (exit 1 after 60 s). No credential env var is accepted by the CLI auth chain; no auth-CLI inference is required. |
| 1.1.16 compaction | **negative** | No compaction/context-pressure event exists. The only adjacent signal is `step_type: "checkpoint"` (transcript `SYSTEM/CHECKPOINT` whose content is a conversation summary), emitted at step 1 of every conversation, even single-turn ones — unusable as a pressure trigger. |
| 1.1.17 interactive dispatch | **confirmed, no negative** | All five events fire in interactive mode; per-event key sets are identical to print mode (field-by-field diff in `hook-payloads.jsonl`). Negative events apply to both modes equally: no `PostToolUse` for a `TOOL_ERROR` step, no `Stop` on interrupt or process exit. |
| 1.1.18 `--input-format stream-json` | **confirmed** | Launch without `-p` (`-p` needs an argument and a CLI prompt is rejected). Message: `{"event":"user","message":{"content":"…" or [{"type":"text","text":"…"}]}}`; one `init` per process, one `result` per turn (turn delimiter), same `conversation_id`, `num_turns` increments, `--conversation <id>` accepted (continues `num_turns`); stdin EOF → exit 0 after the current turn, no extra record; `--print-timeout` is per turn (process survives idle); SIGINT mid-turn → `result{status:ERROR,error:"context canceled"}` and exit 1 — **no in-flight cancel keeps the process alive**; malformed line / missing `event` / non-text content block → `result ERROR` + exit 1; unknown event → stderr warning, ignored. |
| 1.1.19 usage/quota | **confirmed, `/credits` negative** | `/usage` → `command.data.{description, groups[].{name,description,buckets[].{id,name,description,window,remaining_fraction,reset_time}}}`, exit 0, `num_turns 0`; `/quota` is an alias (`command.name: usage`); `/credits` → exit 1 `"/credits failed: retrieving credits: no credits info found"`. Exhausted state: `remaining_fraction: 0` + description "You have hit your weekly limit…"; a turn then returns `result ERROR "Individual quota reached … Resets in 120h…"` exit 1. |
| 1.1.20 models | **disproven (placement), shape confirmed** | `agy models --output-format json` → exit 1 `flags provided but not defined`; the flag must precede the subcommand: `agy --output-format json models` → `command.data.models[].{id,label}`; `stream-json` → `command_result` + `result`. No default marker and no effort field on the list: effort is the id suffix (`-high/-medium/-low`, `--effort low|medium|high`), the default lives in `-p "/model"` → `{id,label,effort,is_default}`. Unauthenticated exit not reproducible (Keychain). |
| 1.1.21 `/hooks` | **confirmed** | `command.data.hooks[].{name,enabled,source,actions[].{event,matcher?,type,command,timeout_seconds}}`, `num_turns 0`, `response` is the TSV form. `"enabled": false` shows `enabled:false` with its actions; a malformed hook (`{"Stop":[{"type":"command"}],"NotAnEvent":[]}`) shows `enabled:true` with an action lacking `command` and no warning; unknown event keys vanish. Unauthenticated: OAuth prompt, exit 1 `authentication failed or timed out`. |
| 1.1.22 transcript layout | **confirmed** | Parser input = `transcript_full.jsonl` (complete, native-typed args, the file `transcriptPath` names). `transcript.jsonl` = token-efficient twin (content ≤ ~4 KiB + `truncated_fields`, JSON-string-encoded args). `chunks/{transcript,transcript_full}/00000000.jsonl` were byte-identical copies in every conversation; rollover never observed. |
| 1.1.23 `--mode` | **confirmed** | Headless `--mode plan`: `init.expanded_commands:[{name:plan,type:system}]`, plan written to `brain/<id>/<name>.md`, no workspace write, no approval record, `result SUCCESS`. Headless `--mode accept-edits`: file writes proceed without prompting; `init.permission_mode` stays `request-review`. `--mode bogus` → stderr warning, default mode. Terminal: see 1.1.14. |
| 1.1.24 response fields | **confirmed / partly negative** | `deny` honored; `deny_unless_prior_grant` honored (grant = `--dangerously-skip-permissions`; otherwise `Permission denied … <reason>`); `overwrite` honored (rewritten command executes; stream/transcript still show the original args); `permissionOverrides` **not honored headless** (auto-deny wins, exit 1 `user denied permission`); `terminationBehavior: terminate` honored (`Stop.terminationReason: TERMINAL_CUSTOM_HOOK`), `force_continue` honored (46 invocations until timeout); `injectSteps.userMessage`/`ephemeralMessage` honored, **`toolCall` rejected** (`unknown injected step type: <nil>`, `Stop.terminationReason: ERROR`, exit 1); `Stop decision:"continue"` honored **10** times, the 11th is ignored (forced end; enum `stop|continue|block`); PreToolUse hook exit 1 or 2 with legal stdout → tool blocked `JSON hook … failed: command failed: exit status N, stderr: …` (fail-closed, stdout ignored); Stop hook exit 2 → ignored. |

## Negative contracts consumers must honor

- `PostToolUse` never fires for a `TOOL_ERROR` step (hook-denied, permission-denied,
  protected path, runtime failure) and its `error` was `""` in every capture,
  including a shell exit 7. Tool failure is visible only in the stream (`state:
  ERROR`, `tool_info.error`) and as the turn-level `result.error`.
- `Stop` does not fire on interrupt (`C-c`, `esc`, SIGINT/SIGTERM) or on interactive
  exit; `PreInvocation` fires once per model call, not per user turn.
- Cancellation is indistinguishable from timeout in the stream, leaves shell
  children running, and `duration_seconds` is cumulative per conversation.
- `--dangerously-skip-permissions` is mandatory for any headless tool use; hook
  `allow`/`permissionOverrides` cannot substitute for it.
